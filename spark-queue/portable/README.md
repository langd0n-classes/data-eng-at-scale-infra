# Portable Spark Job Queue (Kind / k3s)

A `kubectl`-only job queue: at most four Spark jobs active across the
cluster, at most one active per team. A finished or abandoned job
releases its slot automatically.

Infra-maintainer-built. Students only ever fill in and submit
`manifests/team-spark-job-template.yaml` — they never touch the
ResourceQuota, the admission policy, or the queue controller.

> A Job must have `spec.suspend: true`, the `queue: spark` label, and
> `spec.activeDeadlineSeconds` set to be accepted as a Spark job — the
> template already sets all three. Only the queue controller may change
> `spec.suspend` or the `queue` label once a Job exists. A student
> submitting any other (non-Spark) Job doesn't need any of this, but the
> per-team `ResourceQuota` below still caps them at one Job object at a
> time regardless.

## Compatibility

Plain `kubectl` + `ValidatingAdmissionPolicy` (GA since Kubernetes 1.30,
no webhook server to run). Runs on any conformant Kubernetes cluster:
Kind, k3s, EKS, GKE, AKS, bare-metal, etc.

## Prerequisites

- **Team namespaces onboarded** — `onboarding/portable/README.md` (needed
  for the per-team `ResourceQuota` below, and because the queue controller
  deploys into the `infra` namespace onboarding creates)
- **`config.env` created and filled in** — see below

## How it's enforced

**One active job per team** — a namespace-scoped `ResourceQuota`
(`count/jobs.batch: "1"`), applied per team namespace at onboarding time.
`ttlSecondsAfterFinished` on the Job template frees the slot once it
finishes.

**Four active across the cluster** — `scripts/queue-controller.py` polls
every 5 seconds, counts Jobs labeled `queue: spark`, and admits queued
ones (flips `spec.suspend` to `false`) oldest-first, up to 4 at a time. A
Job whose pod never reaches `Running` within `STUCK_GRACE_SECONDS` is
excluded from the count so its slot frees up for a waiting Job. The
Deployment uses `strategy: Recreate` so this cap can't be broken by a
restart/redeploy itself — the default `RollingUpdate` would briefly run
an old and new controller pod together, each independently admitting
jobs with no coordination between them.

**Submission is validated, not just conventional** — a
`ValidatingAdmissionPolicy` rejects a Job using the configured Spark
image, or carrying the `queue: spark` label, unless it already has
`suspend: true`, the label, and `activeDeadlineSeconds` set. A second
policy rejects bare Pods using the Spark image with no owner (also
covers `spark-submit --master k8s://`, since it creates its driver Pod
the same ownerless way).

## Before you start: update config.env

```bash
# one-time: create your own config.env from the template
cp config.env.example config.env
```
Then edit `config.env` and set:

| Variable | Example value | Notes |
|---|---|---|
| `INFRA_NAMESPACE` | `infra` | Where the queue controller runs |
| `SPARK_IMAGE` | `apache/spark:3.5.3` | Must match exactly what `team-spark-job-template.yaml` is filled in with |
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

# the controller itself (RBAC + Deployment)
export SPARK_IMAGE_REPO="${SPARK_IMAGE%:*}"
envsubst '${INFRA_NAMESPACE} ${SPARK_QUEUE_CONTROLLER_IMAGE}' < spark-queue/portable/manifests/queue-controller-deployment.yaml | kubectl apply -f -

# the enforcement policies
envsubst '${SPARK_IMAGE_REPO} ${INFRA_NAMESPACE}' < spark-queue/portable/manifests/spark-job-admission-policy.yaml | kubectl apply -f -
```
```
deployment.apps/spark-queue-controller condition met
validatingadmissionpolicy.admissionregistration.k8s.io/spark-job-queue-policy created
validatingadmissionpolicybinding.admissionregistration.k8s.io/spark-job-queue-policy-binding created
validatingadmissionpolicy.admissionregistration.k8s.io/spark-bare-pod-policy created
validatingadmissionpolicybinding.admissionregistration.k8s.io/spark-bare-pod-policy-binding created
```

**2. A compliant Job runs**

```bash
source config.env
SPARK_JOB_NAME=demo-job TEAM_NAMESPACE_PREFIX=team TEAM_ID=01 SPARK_COMMAND="echo hi" \
envsubst '${TEAM_NAMESPACE_PREFIX} ${TEAM_ID} ${SPARK_JOB_NAME} ${SPARK_IMAGE} ${SPARK_COMMAND}' \
< spark-queue/portable/manifests/team-spark-job-template.yaml | kubectl apply -f -
```
```
job.batch/demo-job created
```
Within a few seconds the controller admits it (logs `admitting
team-01/demo-job (suspend -> false)`) and it runs to completion.

**3. A raw Job bypassing the template is rejected**

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

**4. A non-Spark Job in the same namespace is unaffected**

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

**5. The cluster-wide 4-slot cap (5 test namespaces)**

Two team namespaces (each capped at 1) can't reach 4 concurrently on
their own, so this uses 5 lightweight test namespaces instead — a
compliant Job submitted to each via the template. Each Job runs `sleep
600` (10 minutes) on purpose: steps 6 and 7 below need jobs that are
still genuinely running when they test against them, not ones that
already finished on their own:

```bash
source config.env
for i in 1 2 3 4 5; do
  SPARK_JOB_NAME=job-$i TEAM_NAMESPACE_PREFIX=spark-test TEAM_ID=$i SPARK_COMMAND="sleep 600" \
  envsubst '${TEAM_NAMESPACE_PREFIX} ${TEAM_ID} ${SPARK_JOB_NAME} ${SPARK_IMAGE} ${SPARK_COMMAND}' \
  < spark-queue/portable/manifests/team-spark-job-template.yaml | kubectl apply -f -
done
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
Jobs 1–4 are admitted immediately; job-5 stays suspended, queued.

**6. Cancellation (not completion) releases a slot and starts queued work**

The point of this step is to prove a *person* cancelling a running job
frees its slot — as opposed to the job simply finishing on its own,
which would free the slot anyway and prove nothing about cancellation
specifically. So before deleting job-1, check that it's still actually
running: its only condition is `Suspended: False` (meaning "admitted,
not currently suspended"), with no `Complete` or `Failed` condition, and
its pod is `Running`, not finished:

```bash
kubectl get job job-1 -n spark-test-1 -o jsonpath='{.status.conditions}'
# [{"type":"Suspended","status":"False",...}]   — no Complete/Failed condition
kubectl get pods -n spark-test-1 -l job-name=job-1 -o custom-columns=NAME:.metadata.name,PHASE:.status.phase
# job-1-88lr4   Running

kubectl delete job job-1 -n spark-test-1
kubectl get job job-5 -n spark-test-5 -o jsonpath='{.spec.suspend}'
# false
```
The controller's own log ties cause to effect directly:
```
06:31:54 INFO active=4 waiting=1 free_slots=0
06:31:59 INFO active=4 waiting=1 free_slots=0
06:32:04 INFO active=3 waiting=1 free_slots=1
06:32:04 INFO admitting spark-test-5/job-5 (suspend -> false)
06:32:09 INFO active=4 waiting=0 free_slots=0
```

**7. Timeout releases a slot, and `activeDeadlineSeconds` actually terminates it**

Step 6 covered a person cancelling a job. This step covers the other
way a slot frees up on its own: a job that gets *stuck* — its pod never
even starts — instead of a person noticing and deleting it.

To simulate that, submit a Job with a bogus image tag, so its pod sits
in `ImagePullBackOff` forever instead of running. The queue has no way
to know in advance that this will happen, so it's admitted normally
like any other Job first:

```bash
source config.env
SPARK_JOB_NAME=stuck-job TEAM_NAMESPACE_PREFIX=spark-test TEAM_ID=1 \
SPARK_IMAGE="apache/spark:nonexistent-tag-for-stuck-test" SPARK_COMMAND="echo unreachable" \
envsubst '${TEAM_NAMESPACE_PREFIX} ${TEAM_ID} ${SPARK_JOB_NAME} ${SPARK_IMAGE} ${SPARK_COMMAND}' \
< spark-queue/portable/manifests/team-spark-job-template.yaml | kubectl apply -f -

kubectl get pods -n spark-test-1 -l job-name=stuck-job
# stuck-job-4lzzv   0/1   ImagePullBackOff
```
`STUCK_GRACE_SECONDS` is how long the controller waits for a Job's pod
to reach `Running` before giving up on it and freeing its slot. It
defaults to 300s (5 min) in production — lowered here to 30s purely so
this test doesn't take 5 minutes to show a result, not a change to the
shipped default:
```bash
kubectl set env deployment/spark-queue-controller -n infra STUCK_GRACE_SECONDS=30
```
Once the grace window passes, the controller recognizes it as stuck and
stops counting it — confirmed by its own log, not just inferred:
```
06:34:18 WARNING spark-test-1/stuck-job stuck (admitted 30s+ ago, no pod ever reached Running) — freeing its slot; activeDeadlineSeconds will actually terminate it
06:34:18 INFO active=0 waiting=0 free_slots=4
```
A freshly submitted Job is admitted immediately, proving the slot is
genuinely usable, not just excluded from a count:
```bash
SPARK_JOB_NAME=proof-job TEAM_NAMESPACE_PREFIX=spark-test TEAM_ID=2 SPARK_COMMAND="echo admitted-after-stuck-freed" \
envsubst '${TEAM_NAMESPACE_PREFIX} ${TEAM_ID} ${SPARK_JOB_NAME} ${SPARK_IMAGE} ${SPARK_COMMAND}' \
< spark-queue/portable/manifests/team-spark-job-template.yaml | kubectl apply -f -
kubectl get job proof-job -n spark-test-2 -o jsonpath='{.spec.suspend}'
# false
```
Being excluded from the count doesn't delete the stuck Job itself —
`activeDeadlineSeconds` (set by the template, mutable after creation) is
what actually terminates it:
```bash
kubectl patch job stuck-job -n spark-test-1 --type=merge -p '{"spec":{"activeDeadlineSeconds":20}}'
kubectl get job stuck-job -n spark-test-1 -o jsonpath='{.status.conditions}'
```
```
[..., {"type":"Failed","status":"True","reason":"DeadlineExceeded",
  "message":"Job was active longer than specified deadline"}]
```
Revert the test-only override afterward:
```bash
kubectl set env deployment/spark-queue-controller -n infra STUCK_GRACE_SECONDS-
```

**8. The per-team 1-job cap**

```bash
source config.env
for n in a b c; do
  SPARK_JOB_NAME=team01-job-$n TEAM_NAMESPACE_PREFIX=team TEAM_ID=01 SPARK_COMMAND="sleep 30" \
  envsubst '${TEAM_NAMESPACE_PREFIX} ${TEAM_ID} ${SPARK_JOB_NAME} ${SPARK_IMAGE} ${SPARK_COMMAND}' \
  < spark-queue/portable/manifests/team-spark-job-template.yaml | kubectl apply -f -
done
```
```
job.batch/team01-job-a created
Error: jobs.batch "team01-job-b" is forbidden: exceeded quota: team-spark-jobs-quota ...
Error: jobs.batch "team01-job-c" is forbidden: exceeded quota: team-spark-jobs-quota ...
```
Only the first Job is created — the per-team cap rejects every other attempt.

## Variables

| Variable | Default | Description |
|---|---|---|
| `TEAM_NAMESPACE_PREFIX`, `TEAM_ID` | *(required)* | Which team namespace |
| `INFRA_NAMESPACE` | *(required)* | Where the queue controller runs |
| `SPARK_IMAGE` | *(required)* | Must match exactly what the admission policy and the Job template both use |
| `SPARK_JOB_NAME`, `SPARK_COMMAND` | *(required)* | Set per submission by the student |
| `MAX_ACTIVE_JOBS` (controller env) | `4` | Cluster-wide cap |
| `POLL_INTERVAL_SECONDS` (controller env) | `5` | Reconcile loop interval |
| `STUCK_GRACE_SECONDS` (controller env) | `300` | How long an admitted Job gets before a pod that never starts frees its slot |
| `SPARK_IMAGE_REPO` | *(required)* | `SPARK_IMAGE` with its tag stripped — used by the admission policies |
| `SPARK_QUEUE_CONTROLLER_IMAGE` | *(required)* | The controller's own pre-built image |

## Cleanup

```bash
kubectl delete -f spark-queue/portable/manifests/spark-job-admission-policy.yaml --ignore-not-found
kubectl delete -f spark-queue/portable/manifests/queue-controller-deployment.yaml --ignore-not-found
for id in 01 02; do kubectl delete resourcequota team-spark-jobs-quota -n team-$id --ignore-not-found; done
```
