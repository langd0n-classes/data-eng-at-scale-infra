#!/usr/bin/env python3
"""Cluster-wide Spark job queue controller.

Watches Jobs labeled queue=spark across every namespace and admits at most
MAX_ACTIVE_JOBS at a time by flipping spec.suspend from true to false,
oldest submission first. Never re-suspends a job once admitted.

Also checks each Job's own container image against SPARK_IMAGE_REPO,
the same repository (tag-stripped) the admission policy itself matches
on — the label alone isn't enough to prove a Job is actually a Spark job,
since nothing stops an unrelated Job from carrying it. Without this, any
Job could occupy one of the cluster-wide slots this queue exists
specifically to cap for Spark jobs (confirmed directly: a job labeled
queue=spark but using a completely different image was still admitted
and counted before this check existed).

Uses a short poll loop rather than a live watch, deliberately: a missed
watch event would otherwise wedge the queue until the controller restarts,
and a 5-second poll is cheap enough for this scale (a handful of team
namespaces) that the simplicity is worth it.
"""
import logging
import os
import time

from kubernetes import client, config

MAX_ACTIVE_JOBS = int(os.environ.get("MAX_ACTIVE_JOBS", "4"))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
SPARK_IMAGE_REPO = os.environ.get("SPARK_IMAGE_REPO", "")
QUEUE_LABEL_SELECTOR = "queue=spark"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("spark-queue-controller")


def uses_spark_image(job) -> bool:
    """Same scope the admission policy itself enforces: at least one
    container using SPARK_IMAGE_REPO (any tag)."""
    containers = job.spec.template.spec.containers or []
    return any(
        c.image and c.image.startswith(f"{SPARK_IMAGE_REPO}:")
        for c in containers
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


def reconcile(batch_api: "client.BatchV1Api") -> None:
    labeled = batch_api.list_job_for_all_namespaces(label_selector=QUEUE_LABEL_SELECTOR).items

    jobs = [j for j in labeled if uses_spark_image(j)]
    skipped = len(labeled) - len(jobs)
    if skipped:
        log.warning(
            "ignoring %d job(s) labeled queue=spark but not using %s:* — "
            "not actually a Spark job, not counted toward the cap",
            skipped, SPARK_IMAGE_REPO,
        )

    active = [j for j in jobs if is_active(j)]
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
    log.info(
        "Spark queue controller started: max_active_jobs=%d poll_interval=%ds",
        MAX_ACTIVE_JOBS,
        POLL_INTERVAL_SECONDS,
    )
    while True:
        try:
            reconcile(batch_api)
        except Exception:
            log.exception("reconcile failed, will retry next poll")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
