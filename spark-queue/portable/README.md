# Portable Spark Job Queue (Kind / k3s)

A `kubectl`-only job queue: at most four Spark jobs active across the
cluster, at most one active per team. A finished or abandoned job
releases its slot automatically.

Infra-maintainer-built. Students only ever fill in and submit
`manifests/team-spark-job-template.yaml` — they never touch the
ResourceQuota, the admission policy, or the queue controller.

> **A Job using the configured Spark image (any registry prefix, tag, or
> digest), or carrying the `queue: spark` label, is only accepted if it
> also has `spec.suspend: true` and `spec.activeDeadlineSeconds` set.**
> All three are required together — the template already sets them. A
> student writing their own Job YAML from scratch must include all three
> too, or it's rejected outright — including a plain, unlabeled Job using
> the official Spark image directly. Only the queue controller's own
> ServiceAccount may change `spec.suspend` or the `queue` label once a Job
> exists — any other attempt to flip `suspend` directly or strip the
> label is rejected the same way. Tell students this explicitly if they
> ever ask why a custom Job of theirs was denied.

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
labeled `queue: spark`, and admits queued ones (flips `spec.suspend` to
`false`) oldest-first, up to 4 at a time. A Job that's been admitted for
more than `STUCK_GRACE_SECONDS` (default 300s) with no pod ever reaching
`Running`/`Succeeded` — a bad image tag stuck in `ImagePullBackOff`, or a
pod stuck `Pending` because the team's `ResourceQuota` is full — is
excluded from the count so its slot frees up for a waiting Job;
`spec.activeDeadlineSeconds` is what actually terminates it.

**Enforcement, not convention** — a `ValidatingAdmissionPolicy` matches
any Job using the configured Spark image (checked against the `repo:tag`
and `repo@digest` forms, under any registry prefix — not just an exact
string match, so a version bump or a different registry doesn't
silently exempt a Job) or carrying the `queue: spark` label, and rejects
it outright unless it already has `suspend: true`, the `queue: spark`
label, and `spec.activeDeadlineSeconds` set. A team's own image built
from Spark under a different name is covered by the label instead, since
that can't be detected from the image string — the per-team
`ResourceQuota` is the backstop for the one case neither check can catch:
a custom-named derivative with no label at all.

**Bare Pods and `spark-submit --master k8s://` are blocked outright** —
neither one ever creates a Job, so neither the policy above nor the
controller (which only watches Jobs) would otherwise see them. A second
`ValidatingAdmissionPolicy` denies any Pod using the Spark image with no
`ownerReferences` at all — a student's own hand-written Pod has none,
and so does `--deploy-mode cluster`'s externally-spawned driver Pod
(Spark's own client creates it directly; nothing sets an owner back to
anything), so cluster-mode submission isn't supported. A Job's own child
Pod, and the executor Pods a client-mode driver spawns, both have a real
owner and are unaffected.

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
# image, no ConfigMap/pip-install step)
export SPARK_IMAGE_REPO="${SPARK_IMAGE%:*}"
envsubst '${INFRA_NAMESPACE} ${SPARK_QUEUE_CONTROLLER_IMAGE}' < spark-queue/portable/manifests/queue-controller-deployment.yaml | kubectl apply -f -

# the enforcement policies — matches on SPARK_IMAGE's repository (any
# tag or digest, not just the exact one configured), or the queue=spark
# label for a differently-named image
envsubst '${SPARK_IMAGE_REPO} ${INFRA_NAMESPACE}' < spark-queue/portable/manifests/spark-job-admission-policy.yaml | kubectl apply -f -
```
```
deployment.apps/spark-queue-controller condition met
2026-10-10 22:59:17,039 INFO Spark queue controller started: max_active_jobs=4 poll_interval=5s stuck_grace=300s
validatingadmissionpolicy.admissionregistration.k8s.io/spark-job-queue-policy created
validatingadmissionpolicybinding.admissionregistration.k8s.io/spark-job-queue-policy-binding created
validatingadmissionpolicy.admissionregistration.k8s.io/spark-bare-pod-policy created
validatingadmissionpolicybinding.admissionregistration.k8s.io/spark-bare-pod-policy-binding created
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

**7. Only the controller may change `suspend` or the `queue` label**

```bash
# a compliant Job, then two different bypass attempts on it
kubectl patch job team01-job-a -n team-01 -p '{"spec":{"suspend":false}}' --type=merge
kubectl patch job team01-job-a -n team-01 --type=json \
  -p='[{"op":"remove","path":"/metadata/labels/queue"}]'
```
```
The jobs "team01-job-a" is invalid: : ValidatingAdmissionPolicy 'spark-job-queue-policy'
with binding 'spark-job-queue-policy-binding' denied request: Only the queue controller
may change spec.suspend or the queue label after a Spark Job is created.
```
Both attempts are denied identically. The controller's own admission (the
same `suspend -> false` patch, just from its own ServiceAccount) is
unaffected — already proven by every other step here succeeding.

**8. A bare Pod using the Spark image is rejected; a Job's own Pod isn't**

```bash
kubectl run bare-spark-pod -n team-01 --image=apache/spark:3.5.3 --restart=Never --command -- sleep 60
```
```
The pods "bare-spark-pod" is invalid: : ValidatingAdmissionPolicy 'spark-bare-pod-policy'
with binding 'spark-bare-pod-policy-binding' denied request: Bare Pods using the Spark
image aren't allowed in team namespaces — submit via
spark-queue/portable/manifests/team-spark-job-template.yaml.
```
A Job submitted via the template still works normally — its own child
Pod has a real `ownerReferences` entry (`kind: Job`), so this policy
never rejects it.

**9. A Job missing `activeDeadlineSeconds` is rejected**

```bash
kubectl apply -f - <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: no-deadline-test
  namespace: team-01
  labels:
    queue: spark
spec:
  suspend: true
  template:
    metadata:
      labels:
        queue: spark
    spec:
      restartPolicy: Never
      containers:
        - name: spark
          image: apache/spark:3.5.3
          command: ["/bin/sh", "-c", "echo hi"]
EOF
```
```
The jobs "no-deadline-test" is invalid: : ValidatingAdmissionPolicy 'spark-job-queue-policy'
with binding 'spark-job-queue-policy-binding' denied request: Spark Jobs must set
spec.activeDeadlineSeconds (team-spark-job-template.yaml already does) — otherwise an
abandoned job can hold its cluster-wide slot forever.
```

**10. A stuck job (pod never starts) is excluded from the active count**

A Job admitted with a bad image tag never reaches `Running` — after
`STUCK_GRACE_SECONDS`, the controller stops counting it, freeing its
slot for a waiting Job, without touching its own `suspend`:

```
INFO admitting team-01/stuck-job-test (suspend -> false)
WARNING team-01/stuck-job-test stuck (admitted 20s+ ago, no pod ever reached Running) —
freeing its slot; activeDeadlineSeconds will actually terminate it
INFO active=1 waiting=0 free_slots=3
```
`spec.activeDeadlineSeconds` (required by step 9 above) is what actually
terminates the stuck Job later — this just stops it from wasting a slot
in the meantime.

## Variables

| Variable | Default | Description |
|---|---|---|
| `TEAM_NAMESPACE_PREFIX`, `TEAM_ID` | *(required)* | Which team namespace |
| `INFRA_NAMESPACE` | *(required)* | Where the queue controller runs |
| `SPARK_IMAGE` | *(required)* | Must match exactly what the admission policy and the Job template both use |
| `SPARK_JOB_NAME`, `SPARK_COMMAND` | *(required)* | Set per submission by the student |
| `MAX_ACTIVE_JOBS` (controller env) | `4` | Cluster-wide cap |
| `POLL_INTERVAL_SECONDS` (controller env) | `5` | Reconcile loop interval |
| `STUCK_GRACE_SECONDS` (controller env) | `300` | How long an admitted Job gets before a pod that never reaches Running frees its slot |
| `SPARK_IMAGE_REPO` | *(required)* | `SPARK_IMAGE` with its tag stripped — used by the admission policy only (both to match Jobs by image and to match bare Pods); the controller itself counts by the `queue: spark` label alone |
| `SPARK_QUEUE_CONTROLLER_IMAGE` | *(required)* | The controller's own pre-built GHCR image (not to be confused with `SPARK_IMAGE`, which is what student Spark jobs run) |

## Cleanup

```bash
kubectl delete -f spark-queue/portable/manifests/spark-job-admission-policy.yaml --ignore-not-found
kubectl delete -f spark-queue/portable/manifests/queue-controller-deployment.yaml --ignore-not-found
for id in 01 02; do kubectl delete resourcequota team-spark-jobs-quota -n team-$id --ignore-not-found; done
```
