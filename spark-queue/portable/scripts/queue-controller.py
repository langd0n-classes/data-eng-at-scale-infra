#!/usr/bin/env python3
"""Cluster-wide Spark job queue controller.

Watches Jobs labeled queue=spark and admits up to MAX_ACTIVE_JOBS at a
time (oldest first, suspend -> false), at most one running per namespace.

Classification is label-only, not image-based — the admission policy
makes the label unspoofable, so checking the image too would only risk
wrongly excluding a legitimate Job on a different registry/digest.

A stuck Job (no pod reaches Running within STUCK_GRACE_SECONDS) is
force-terminated via activeDeadlineSeconds rather than just excluded from
the count — otherwise a pod that starts moments later could run for real
while already uncounted, breaking the cluster-wide cap.

Polls every few seconds instead of watching, so a missed event can't
wedge the queue until a restart.
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
    """True if STUCK_GRACE_SECONDS has passed with no pod ever reaching
    Running/terminated (bad image tag, or quota-blocked pod).

    Checks run history, not just current phase: a retrying Job's failed
    pod shows phase="Failed", so a current-phase check would wrongly
    flag a legitimately-retrying job as stuck. Once any pod has run, the
    job is exempt from this check for good — a hang after that is bounded
    only by activeDeadlineSeconds, same as before this function existed."""
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
    """Patches activeDeadlineSeconds to 1, so Kubernetes' own Job
    controller fails it within moments — safer than just excluding it
    from the count, which would let a late-starting pod run uncounted."""
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
