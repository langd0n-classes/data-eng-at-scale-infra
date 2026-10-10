# Portable Spark Job Queue (Kind / k3s)

A `kubectl`-only job queue: at most four Spark jobs active across the
cluster, at most one active per team. A finished or abandoned job
releases its slot automatically.

Infra-maintainer-built. Students only ever fill in and submit
`manifests/team-spark-job-template.yaml` — they never touch the
ResourceQuota, the admission policy, or the queue controller.

> **A Job using the `SPARK_IMAGE` configured in `config.env` is only
> accepted if it also has `spec.suspend: true` and the label
> `queue: spark`.** All three are required together — the template
> already sets them. A student writing their own Job YAML from scratch
> must include all three too, or it gets rejected (missing `suspend`/the
> label) or never runs (image mismatch, silently excluded from the
> queue). Tell students this explicitly if they ever ask why a custom Job
> of theirs was denied or never started.

A student submitting any other (non-Spark) Job doesn't need any of the
three — but the per-team `ResourceQuota` below still caps them at one Job
object at a time regardless, so they can't submit it while a Spark job is
still sitting in their namespace, queued or not.

## Compatibility

Plain `kubectl` + one `ValidatingAdmissionPolicy` (GA since Kubernetes
1.30, no webhook server to run). Runs on any conformant Kubernetes
cluster: Kind, k3s, EKS, GKE, AKS, bare-metal, etc.

## Prerequisites

- **Team namespaces onboarded** — `onboarding/portable/README.md` (needed
  for the per-team `ResourceQuota` below, and because the queue controller
  deploys into the `infra` namespace onboarding creates)
- **`config.env` created and filled in** — see below

## How the two limits are enforced

**One active job per team** — a namespace-scoped `ResourceQuota`
(`count/jobs.batch: "1"`), applied per team namespace at onboarding time.
The API server enforces this directly: a student can't create a second
Job while their first still exists, running or not.
`ttlSecondsAfterFinished` on the Job template frees the slot once it
finishes.

**Four active across the cluster** — a namespace-scoped `ResourceQuota`
can't express a cluster-wide limit. A small Python controller
(`scripts/queue-controller.py`) polls every 5 seconds, counts Jobs
labeled `queue: spark` that also use the configured `SPARK_IMAGE`
repository, and admits queued ones (flips `spec.suspend` to `false`)
oldest-first, up to 4 at a time. The image check matters on its own: the
label alone doesn't prove a Job is actually a Spark job, so without it any
Job could carry `queue: spark` and consume one of the 4 slots this queue
exists specifically to cap for Spark jobs. A Job with the label but a
different image is logged and simply ignored — never counted, never
admitted by this controller.

**Enforcement, not convention** — a student could skip the template and
submit a raw Job with no `queue: spark` label and no `suspend: true`,
bypassing the cluster-wide cap (the per-team `ResourceQuota` still
catches it either way). A `ValidatingAdmissionPolicy`, matched to any Job
using `SPARK_IMAGE`'s repository (any tag, not just the exact one
configured — a version bump shouldn't silently exempt a Job from the
queue), rejects any such Job outright unless it already has
`suspend: true` and the `queue: spark` label.

## Before you start: update config.env

```bash
# one-time: create your own config.env from the template
cp config.env.example config.env
```
Then edit `config.env` and set:

| Variable | Example value | Notes |
|---|---|---|
| `INFRA_NAMESPACE` | `infra` | Where the queue controller runs |
| `SPARK_IMAGE` | `apache/spark:3.5.3` | Must match exactly what `team-spark-job-template.yaml` is filled in with — the admission policy matches on this image reference |
| `TEAM_NAMESPACE_PREFIX` | `team` | Must match `onboarding/cluster.env`'s value |

`TEAM_ID` below is **not** a `config.env` variable — it's set per command,
once for each team namespace you're applying the quota to.

## Validation walkthrough

Validated on: Kubernetes (Kind) v1.34.11.

**1. Deploy the pieces**

```bash
source config.env

# per-team ResourceQuota — one per onboarded team namespace
for id in 01 02; do
  TEAM_ID=$id envsubst '${TEAM_NAMESPACE_PREFIX} ${TEAM_ID}' \
    < spark-queue/portable/manifests/team-jobs-resourcequota.yaml | kubectl apply -f -
done

# the controller itself (RBAC + Deployment, running a pre-built GHCR
# image, no ConfigMap/pip-install step) — also reads SPARK_IMAGE_REPO
# so it only counts Jobs that actually use the configured Spark image, not
# anything that merely carries the queue=spark label
export SPARK_IMAGE_REPO="${SPARK_IMAGE%:*}"
envsubst '${INFRA_NAMESPACE} ${SPARK_IMAGE_REPO} ${SPARK_QUEUE_CONTROLLER_IMAGE}' < spark-queue/portable/manifests/queue-controller-deployment.yaml | kubectl apply -f -

# the enforcement policy — matches on SPARK_IMAGE's repository (tag
# stripped), so any tag of the configured image is caught, not just the
# exact one
envsubst '${SPARK_IMAGE_REPO}' < spark-queue/portable/manifests/spark-job-admission-policy.yaml | kubectl apply -f -
```
```
deployment.apps/spark-queue-controller condition met
2026-09-29 05:26:17,410 INFO Spark queue controller started: max_active_jobs=4 poll_interval=5s
validatingadmissionpolicy.admissionregistration.k8s.io/spark-job-queue-policy created
validatingadmissionpolicybinding.admissionregistration.k8s.io/spark-job-queue-policy-binding created
```

**2. A raw Spark-image Job, bypassing the template, is rejected**

```bash
kubectl apply -f - <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: evasion-test
  namespace: team-01
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: spark
          image: apache/spark:3.5.3
          command: ["/bin/sh", "-c", "echo hi"]
EOF
```
```
The jobs "evasion-test" is invalid: : ValidatingAdmissionPolicy 'spark-job-queue-policy'
with binding 'spark-job-queue-policy-binding' denied request: Spark Jobs must be
submitted via spark-queue/portable/manifests/team-spark-job-template.yaml, which sets
spec.suspend: true so the queue controller can admit it when a cluster-wide slot is free.
```

**3. A non-Spark Job in the same namespace is unaffected**

```bash
kubectl apply -f - <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: unrelated-job
  namespace: team-01
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: work
          image: busybox
          command: ["/bin/sh", "-c", "echo unrelated job runs fine"]
EOF
```
```
job.batch/unrelated-job created
NAME            STATUS     COMPLETIONS   DURATION   AGE
unrelated-job   Complete   1/1           4s         5s
unrelated job runs fine
```

**4. The cluster-wide 4-slot cap (5 test namespaces)**

Two team namespaces (each capped at 1) can't reach 4 concurrently on
their own, so this uses 5 lightweight test namespaces instead — a
compliant Job (via the template) submitted to each:

```bash
kubectl get job -A -l queue=spark -o custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name,SUSPEND:.spec.suspend
```
```
NAMESPACE      NAME    SUSPEND
spark-test-1   job-1   false
spark-test-2   job-2   false
spark-test-3   job-3   false
spark-test-4   job-4   false
spark-test-5   job-5   true
```
Controller log, same moment:
```
INFO active=0 waiting=5 free_slots=4
INFO admitting spark-test-1/job-1 (suspend -> false)
INFO admitting spark-test-2/job-2 (suspend -> false)
INFO admitting spark-test-3/job-3 (suspend -> false)
INFO admitting spark-test-4/job-4 (suspend -> false)
INFO active=4 waiting=1 free_slots=0
```
Jobs 1–4 are admitted immediately; job-5 stays suspended, queued.

**5. Cancellation releases a slot and starts queued work**

```bash
kubectl delete job job-1 -n spark-test-1
```
```
INFO active=4 waiting=1 free_slots=0
INFO active=0 waiting=1 free_slots=4
INFO admitting spark-test-5/job-5 (suspend -> false)
```
```bash
kubectl get job job-5 -n spark-test-5 -o jsonpath='{.spec.suspend}'
# false
```

**6. 5 submission attempts across 2 team namespaces**

```bash
source config.env

# 3 submission attempts to team-01 (only the first should succeed)
for n in a b c; do
  SPARK_JOB_NAME=team01-job-$n TEAM_NAMESPACE_PREFIX=team TEAM_ID=01 SPARK_COMMAND="sleep 30" \
  envsubst '${TEAM_NAMESPACE_PREFIX} ${TEAM_ID} ${SPARK_JOB_NAME} ${SPARK_IMAGE} ${SPARK_COMMAND}' \
  < spark-queue/portable/manifests/team-spark-job-template.yaml | kubectl apply -f -
done

# 2 submission attempts to team-02 (only the first should succeed)
for n in a b; do
  SPARK_JOB_NAME=team02-job-$n TEAM_NAMESPACE_PREFIX=team TEAM_ID=02 SPARK_COMMAND="sleep 30" \
  envsubst '${TEAM_NAMESPACE_PREFIX} ${TEAM_ID} ${SPARK_JOB_NAME} ${SPARK_IMAGE} ${SPARK_COMMAND}' \
  < spark-queue/portable/manifests/team-spark-job-template.yaml | kubectl apply -f -
done
```
```
job.batch/team01-job-a created
Error: jobs.batch "team01-job-b" is forbidden: exceeded quota: team-spark-jobs-quota ...
Error: jobs.batch "team01-job-c" is forbidden: exceeded quota: team-spark-jobs-quota ...
job.batch/team02-job-a created
Error: jobs.batch "team02-job-b" is forbidden: exceeded quota: team-spark-jobs-quota ...
```
Exactly 2 Jobs are created (1 per namespace) — the per-team cap rejects
every other attempt.

## Variables

| Variable | Default | Description |
|---|---|---|
| `TEAM_NAMESPACE_PREFIX`, `TEAM_ID` | *(required)* | Which team namespace |
| `INFRA_NAMESPACE` | *(required)* | Where the queue controller runs |
| `SPARK_IMAGE` | *(required)* | Must match exactly what the admission policy and the Job template both use |
| `SPARK_JOB_NAME`, `SPARK_COMMAND` | *(required)* | Set per submission by the student |
| `MAX_ACTIVE_JOBS` (controller env) | `4` | Cluster-wide cap |
| `POLL_INTERVAL_SECONDS` (controller env) | `5` | Reconcile loop interval |
| `SPARK_IMAGE_REPO` (controller env) | *(required)* | `SPARK_IMAGE` with its tag stripped — same value the admission policy matches on; a labeled Job using any other image is ignored |
| `SPARK_QUEUE_CONTROLLER_IMAGE` | *(required)* | The controller's own pre-built GHCR image (not to be confused with `SPARK_IMAGE`, which is what student Spark jobs run) |

## Cleanup

```bash
kubectl delete -f spark-queue/portable/manifests/spark-job-admission-policy.yaml --ignore-not-found
kubectl delete -f spark-queue/portable/manifests/queue-controller-deployment.yaml --ignore-not-found
for id in 01 02; do kubectl delete resourcequota team-spark-jobs-quota -n team-$id --ignore-not-found; done
```
