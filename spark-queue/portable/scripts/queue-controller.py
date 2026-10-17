#!/usr/bin/env python3
"""Cluster-wide Spark job queue controller.

Watches Jobs labeled queue=spark across every namespace and admits at most
MAX_ACTIVE_JOBS at a time by flipping spec.suspend from true to false,
oldest submission first. Never re-suspends a job once admitted.

Classification is by the queue=spark label alone, matching
spark-job-admission-policy.yaml's own design: once a Job wears that
label, it's fully subject to suspend-gating and the 4-slot cap regardless
of what image it runs — the admission policy already makes the label
unspoofable (only the controller's own ServiceAccount may add/remove it
after creation), so there's no need to additionally verify the image
here too. An earlier version of this controller also filtered by image
(SPARK_IMAGE_REPO) — removed, since keeping it would now incorrectly
exclude a legitimately-labeled Job from counting just because it uses a
different registry prefix or a digest reference, contradicting the
label-only design the policy itself enforces.

Also excludes a Job from the active count if it's been admitted
(suspend=false) for longer than STUCK_GRACE_SECONDS with none of its pods
ever reaching Running/Succeeded (a bad image tag stuck in
ImagePullBackOff, or a pod stuck Pending because the team's ResourceQuota
is full) — frees the slot for a waiting Job instead of letting a stuck
submission hold it for its entire activeDeadlineSeconds window. Doesn't
touch spec.suspend itself (an immediate resuspend would make it look
"waiting" again and could get it right back in, right away, since it's
still the oldest submission) — activeDeadlineSeconds (now required by
the admission policy) is what actually terminates it, independently of
this accounting fix.

Uses a short poll loop rather than a live watch, deliberately: a missed
watch event would otherwise wedge the queue until the controller restarts,
and a 5-second poll is cheap enough for this scale (a handful of team
namespaces) that the simplicity is worth it.
"""
import logging
import os
import time
from datetime import datetime, timezone

from kubernetes import client, config

MAX_ACTIVE_JOBS = int(os.environ.get("MAX_ACTIVE_JOBS", "4"))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
STUCK_GRACE_SECONDS = int(os.environ.get("STUCK_GRACE_SECONDS", "300"))
QUEUE_LABEL_SELECTOR = "queue=spark"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("spark-queue-controller")


def is_stuck(job, core_api: "client.CoreV1Api") -> bool:
    """True once a job has had STUCK_GRACE_SECONDS to start a pod and
    still hasn't — no pod has reached Running or Succeeded. Covers both
    a bad image tag (ImagePullBackOff) and a pod stuck Pending because
    the team's ResourceQuota is full, without needing to distinguish
    which — "never actually started" is the only thing that matters
    for freeing its slot."""
    start = job.status.start_time
    if start is None:
        return False
    age = (datetime.now(timezone.utc) - start).total_seconds()
    if age < STUCK_GRACE_SECONDS:
        return False
    pods = core_api.list_namespaced_pod(
        job.metadata.namespace,
        label_selector=f"job-name={job.metadata.name}",
    ).items
    return not any(p.status.phase in ("Running", "Succeeded") for p in pods)


def is_active(job) -> bool:
    """A job counts against the cluster-wide cap once admitted (suspend=false)
    and until Kubernetes marks it finished (Complete or Failed)."""
    if job.spec.suspend:
        return False
    for cond in job.status.conditions or []:
        if cond.type in ("Complete", "Failed") and cond.status == "True":
            return False
    return True


def reconcile(batch_api: "client.BatchV1Api", core_api: "client.CoreV1Api") -> None:
    jobs = batch_api.list_job_for_all_namespaces(label_selector=QUEUE_LABEL_SELECTOR).items

    active = []
    for job in jobs:
        if not is_active(job):
            continue
        if is_stuck(job, core_api):
            log.warning(
                "%s/%s stuck (admitted %ds+ ago, no pod ever reached Running) — "
                "freeing its slot; activeDeadlineSeconds will actually terminate it",
                job.metadata.namespace, job.metadata.name, STUCK_GRACE_SECONDS,
            )
            continue
        active.append(job)

    waiting = [j for j in jobs if j.spec.suspend]
    waiting.sort(key=lambda j: j.metadata.creation_timestamp)  # oldest submission first

    free_slots = max(MAX_ACTIVE_JOBS - len(active), 0)
    log.info("active=%d waiting=%d free_slots=%d", len(active), len(waiting), free_slots)

    for job in waiting[:free_slots]:
        log.info("admitting %s/%s (suspend -> false)", job.metadata.namespace, job.metadata.name)
        batch_api.patch_namespaced_job(
            name=job.metadata.name,
            namespace=job.metadata.namespace,
            body={"spec": {"suspend": False}},
        )


def main() -> None:
    config.load_incluster_config()
    batch_api = client.BatchV1Api()
    core_api = client.CoreV1Api()
    log.info(
        "Spark queue controller started: max_active_jobs=%d poll_interval=%ds stuck_grace=%ds",
        MAX_ACTIVE_JOBS,
        POLL_INTERVAL_SECONDS,
        STUCK_GRACE_SECONDS,
    )
    while True:
        try:
            reconcile(batch_api, core_api)
        except Exception:
            log.exception("reconcile failed, will retry next poll")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
