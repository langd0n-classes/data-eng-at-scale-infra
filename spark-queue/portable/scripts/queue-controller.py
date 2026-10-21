#!/usr/bin/env python3
"""Cluster-wide Spark job queue controller.

Watches Jobs labeled queue=spark across every namespace and admits at most
MAX_ACTIVE_JOBS at a time by flipping spec.suspend from true to false,
oldest submission first, with at most one admitted per namespace (team) at
a time regardless of how many cluster-wide slots are free. Never
re-suspends a job once admitted.

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

A Job that's been admitted (suspend=false) for longer than
STUCK_GRACE_SECONDS with no pod ever reaching Running/Succeeded (a bad
image tag stuck in ImagePullBackOff, a retry pod still Pending, an init
container taking a while) gets force-terminated: its activeDeadlineSeconds
is patched down to 1, handing termination to Kubernetes' own Job
controller, which marks it Failed/DeadlineExceeded within moments. This
closes a real race in an earlier version of this logic, which only
excluded a stuck Job from the active count without touching it — if its
pod later did start, it could run for real while already excluded,
letting a 5th job slip past the cluster-wide cap. Patching the deadline
instead of simply ignoring the Job means even a pod that starts in that
exact window gets killed anyway, since the deadline is already in the
past relative to status.startTime.

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
    still hasn't — no pod has ever had a container reach Running or
    Succeeded/terminated. Covers both a bad image tag (ImagePullBackOff)
    and a pod stuck Pending because the team's ResourceQuota is full,
    without needing to distinguish which — "never actually started" is
    the only thing that matters for freeing its slot.

    Checks container-level run history, not just each pod's current
    phase: a Job with backoffLimit > 0 legitimately retries after a pod
    failure, and a failed pod's phase is "Failed", not "Running" — so a
    current-phase-only check would misread a job that already ran once
    and is correctly retrying as "never started", and this function now
    feeds a force-termination action (see terminate_stuck_job), not just
    an accounting exclusion. Once a job has had any pod ever reach
    Running/terminated, it's permanently exempt from this fast path —
    a job that runs fine and then hangs forever, or crashes once and
    whose retry then gets stuck in ImagePullBackOff, is bounded only by
    the (separately enforced) activeDeadlineSeconds ceiling from then on,
    same as before this function existed at all."""
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
    for pod in pods:
        if pod.status.phase == "Succeeded":
            return False
        for cs in (pod.status.container_statuses or []):
            if cs.state.running or cs.state.terminated or (cs.last_state and cs.last_state.terminated):
                return False
    return True


def terminate_stuck_job(batch_api: "client.BatchV1Api", job) -> None:
    """Forces a stuck Job to a real terminal state instead of merely
    excluding it from the active count. Patching activeDeadlineSeconds
    down to 1 hands termination to Kubernetes' own Job controller, which
    marks it Failed/DeadlineExceeded within moments — even a pod that
    starts Running in that exact window gets killed anyway, since the
    deadline is already in the past relative to status.start_time."""
    log.warning(
        "%s/%s stuck (no pod ever reached Running after %ds) — forcing termination",
        job.metadata.namespace, job.metadata.name, STUCK_GRACE_SECONDS,
    )
    batch_api.patch_namespaced_job(
        name=job.metadata.name,
        namespace=job.metadata.namespace,
        body={"spec": {"activeDeadlineSeconds": 1}},
    )


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
            terminate_stuck_job(batch_api, job)
            continue
        active.append(job)

    active_namespaces = {j.metadata.namespace for j in active}

    waiting = [j for j in jobs if j.spec.suspend]
    waiting.sort(key=lambda j: j.metadata.creation_timestamp)  # oldest submission first

    free_slots = max(MAX_ACTIVE_JOBS - len(active), 0)
    log.info("active=%d waiting=%d free_slots=%d", len(active), len(waiting), free_slots)

    admitted_namespaces = set()
    admitted = 0
    for job in waiting:
        if admitted >= free_slots:
            break
        ns = job.metadata.namespace
        if ns in active_namespaces or ns in admitted_namespaces:
            continue  # this team already has one running — stays queued
        log.info("admitting %s/%s (suspend -> false)", ns, job.metadata.name)
        batch_api.patch_namespaced_job(
            name=job.metadata.name,
            namespace=ns,
            body={"spec": {"suspend": False}},
        )
        admitted_namespaces.add(ns)
        admitted += 1


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
