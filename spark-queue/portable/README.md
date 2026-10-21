# Portable Spark Job Queue (Kind / k3s)

A `kubectl`-only job queue: at most four Spark jobs active across the
cluster, at most one active per team at a time — but a team can have any
number of jobs waiting. A finished or abandoned job releases its slot
automatically.

Infra-maintainer-built. Students only ever fill in and submit
`manifests/team-spark-job-template.yaml` — they never touch the
admission policy or the queue controller.

> A Job must have `spec.suspend: true`, the `queue: spark` label,
> `spec.activeDeadlineSeconds` set between 1 and 7200, and
> `spec.parallelism` at most 1 to be accepted as a Spark job — the
> template already sets all of these. Only the queue controller may
> change `spec.suspend` or the `queue` label once a Job exists. A student
> submitting any other (non-Spark) Job doesn't need any of this and isn't
> subject to the queue at all.

## Compatibility

Plain `kubectl` + `ValidatingAdmissionPolicy` (GA since Kubernetes 1.30,
no webhook server to run). Runs on any conformant Kubernetes cluster:
Kind, k3s, EKS, GKE, AKS, bare-metal, etc.

## Prerequisites

- **Team namespaces onboarded** — `onboarding/portable/README.md` (the
  queue controller deploys into the `infra` namespace onboarding creates)
- **`config.env` created and filled in** — see below

## How it's enforced

**One active job per team, any number waiting** — enforced entirely by
`scripts/queue-controller.py`, not by a quota: a team can submit as many
Jobs as it wants (a suspended Job creates zero pods and costs nothing),
but the controller only ever admits one per namespace at a time,
regardless of how many cluster-wide slots are free. `ttlSecondsAfterFinished`
on the Job template frees up etcd once a finished Job's grace period
passes.

**Four active across the cluster** — the same controller polls every 5
seconds, counts Jobs labeled `queue: spark`, and admits queued ones
(flips `spec.suspend` to `false`) oldest-first, up to 4 at a time,
skipping any team that already has one running. A Job whose pod never
reaches `Running` within `STUCK_GRACE_SECONDS` is force-terminated — its
`activeDeadlineSeconds` is patched down to 1, handing it to Kubernetes'
own Job controller to mark `Failed`/`DeadlineExceeded` within moments —
rather than merely excluded from the count, which would leave a window
for a pod that starts just afterward to run for real while already
uncounted. A Job that already had a pod reach `Running` at least once
(e.g. mid-retry after a crash) is never treated as stuck, even if its
current pod is still `Pending`. The Deployment uses `strategy: Recreate`
so this cap can't be broken by a restart/redeploy itself — the default
`RollingUpdate` would briefly run an old and new controller pod
together, each independently admitting jobs with no coordination between
them.

**Submission is validated, not just conventional** — a
`ValidatingAdmissionPolicy` rejects a Job using the configured Spark
image, or carrying the `queue: spark` label, unless it already has
`suspend: true`, the label, `activeDeadlineSeconds` between 1 and 7200,
`parallelism` at most 1, and `ttlSecondsAfterFinished` at most 86400 (one
day — keeps finished Jobs from piling up now that there's no per-team
count quota). A second policy rejects *any* ownerless Pod in a team
namespace, regardless of image — not just ones matching the Spark image
by name, since a custom-built image (`FROM apache/spark` under a
different name) would otherwise dodge the queue entirely. This also
covers `spark-submit --master k8s://`, since it creates its driver Pod
the same ownerless way.

## Before you start: update config.env

```bash
# one-time: create your own config.env from the template
cp config.env.example config.env
```
Then edit `config.env` and set:

| Variable | Example value | Notes |
|---|---|---|
| `INFRA_NAMESPACE` | `infra` | Where the queue controller runs |
| `SPARK_IMAGE` | `apache/spark@sha256:d0f5d6...` | Digest-pinned; must match exactly what `team-spark-job-template.yaml` is filled in with |
| `TEAM_NAMESPACE_PREFIX` | `team` | Must match `onboarding/cluster.env`'s value |

`TEAM_ID` below is **not** a `config.env` variable — it's set per command,
once for each team namespace a job is submitted to.

## Validation walkthrough

Validated on: Kubernetes (Kind) v1.34.11.

**1. Deploy the pieces**

```bash
source config.env

# the controller itself (RBAC + Deployment)
# repo@digest form: strip at '@', not ':' (the digest itself has a colon)
export SPARK_IMAGE_REPO="${SPARK_IMAGE%%@*}"
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

**7. A stuck job is force-terminated, not just uncounted**

Step 6 covered a person cancelling a job. This step covers the other
way a slot frees up: a job that gets *stuck* — its pod never even
starts — instead of a person noticing and deleting it.

To simulate that, submit a Job with a bogus image tag, so its pod sits
in `ImagePullBackOff` forever instead of running. The queue has no way
to know in advance that this will happen, so it's admitted normally
like any other Job first:

```bash
source config.env
SPARK_JOB_NAME=stuck-job TEAM_NAMESPACE_PREFIX=team TEAM_ID=01 \
SPARK_IMAGE="apache/spark:nonexistent-tag-for-stuck-test" SPARK_COMMAND="echo unreachable" \
envsubst '${TEAM_NAMESPACE_PREFIX} ${TEAM_ID} ${SPARK_JOB_NAME} ${SPARK_IMAGE} ${SPARK_COMMAND}' \
< spark-queue/portable/manifests/team-spark-job-template.yaml | kubectl apply -f -

kubectl get pods -n team-01 -l job-name=stuck-job
# stuck-job-fpjdd   0/1   ImagePullBackOff
```
`STUCK_GRACE_SECONDS` is how long the controller waits for a Job's pod
to reach `Running` before giving up on it. It defaults to 300s (5 min)
in production — lowered here to 20s purely so this test doesn't take 5
minutes to show a result, not a change to the shipped default:
```bash
kubectl set env deployment/spark-queue-controller -n infra STUCK_GRACE_SECONDS=20
```
Once the grace window passes, the controller force-terminates it by
patching `activeDeadlineSeconds` down to 1 — handing termination to
Kubernetes' own Job controller, rather than just excluding it from the
count and leaving it alive. Confirmed by its own log, not just inferred:
```
2026-10-21 02:28:38,282 WARNING team-01/stuck-job stuck (no pod ever reached Running after 20s) — forcing termination
2026-10-21 02:28:38,293 INFO active=0 waiting=0 free_slots=4
```
The Job reaches a real `Failed`/`DeadlineExceeded` condition within
seconds — not merely excluded from counting while still technically
alive:
```bash
kubectl get job stuck-job -n team-01 -o jsonpath='{.status.conditions}'
```
```
[..., {"type":"FailureTarget","status":"True","reason":"DeadlineExceeded","lastProbeTime":"2026-10-21T02:28:38Z",...},
 {"type":"Failed","status":"True","reason":"DeadlineExceeded","lastProbeTime":"2026-10-21T02:28:40Z",
  "message":"Job was active longer than specified deadline"}]
```
Revert the test-only override afterward:
```bash
kubectl set env deployment/spark-queue-controller -n infra STUCK_GRACE_SECONDS-
```

**7b. A legitimate retry isn't misclassified as stuck**

A Job with `backoffLimit > 0` retries after a pod failure by creating a
*new* pod, not restarting the old one — so a naive "is any current pod
Running" check would misread a job that already ran and is correctly
retrying as "never started," and force-terminate a job that's actually
making progress. Submit a Job whose command always fails, forcing
`team-spark-job-template.yaml`'s `backoffLimit: 2` to exhaust itself
across multiple real, started-and-failed pod attempts:

```bash
source config.env
SPARK_JOB_NAME=retry-test TEAM_NAMESPACE_PREFIX=team TEAM_ID=01 SPARK_COMMAND="exit 1" \
envsubst '${TEAM_NAMESPACE_PREFIX} ${TEAM_ID} ${SPARK_JOB_NAME} ${SPARK_IMAGE} ${SPARK_COMMAND}' \
< spark-queue/portable/manifests/team-spark-job-template.yaml | kubectl apply -f -
```
Each retry pod genuinely starts and fails (`Error`, not stuck in
`Pending`), and the Job is never force-terminated by the stuck-job path
— it reaches its own natural terminal state instead:
```bash
kubectl get pods -n team-01 -l job-name=retry-test
```
```
NAME               READY   STATUS   RESTARTS   AGE
retry-test-dqblh   0/1     Error    0          85s
retry-test-jjk7t   0/1     Error    0          96s
retry-test-thbrs   0/1     Error    0          65s
```
```bash
kubectl get job retry-test -n team-01 -o jsonpath='{.status.conditions}'
kubectl get job retry-test -n team-01 -o jsonpath='{.spec.activeDeadlineSeconds}'
```
```
[..., {"type":"Failed","status":"True","reason":"BackoffLimitExceeded",
  "message":"Job has reached the specified backoff limit"}]
7200
```
`activeDeadlineSeconds` stayed at its original `7200` the whole time —
never patched down to `1` — and the failure reason is `BackoffLimitExceeded`,
not `DeadlineExceeded`: proof the stuck-job path never triggered for this
job, confirmed by grepping the controller's log across the job's entire
lifetime and finding no `stuck` warning for it at all.

**8. One running job per team, any number waiting — queued, not rejected**

```bash
source config.env
SPARK_JOB_NAME=demo-job-1 TEAM_NAMESPACE_PREFIX=team TEAM_ID=01 SPARK_COMMAND="sleep 40" \
envsubst '${TEAM_NAMESPACE_PREFIX} ${TEAM_ID} ${SPARK_JOB_NAME} ${SPARK_IMAGE} ${SPARK_COMMAND}' \
< spark-queue/portable/manifests/team-spark-job-template.yaml | kubectl apply -f -
SPARK_JOB_NAME=demo-job-2 TEAM_NAMESPACE_PREFIX=team TEAM_ID=01 SPARK_COMMAND="echo hi" \
envsubst '${TEAM_NAMESPACE_PREFIX} ${TEAM_ID} ${SPARK_JOB_NAME} ${SPARK_IMAGE} ${SPARK_COMMAND}' \
< spark-queue/portable/manifests/team-spark-job-template.yaml | kubectl apply -f -
SPARK_JOB_NAME=demo-job-3 TEAM_NAMESPACE_PREFIX=team TEAM_ID=01 SPARK_COMMAND="echo hi" \
envsubst '${TEAM_NAMESPACE_PREFIX} ${TEAM_ID} ${SPARK_JOB_NAME} ${SPARK_IMAGE} ${SPARK_COMMAND}' \
< spark-queue/portable/manifests/team-spark-job-template.yaml | kubectl apply -f -
```
```
job.batch/demo-job-1 created
job.batch/demo-job-2 created
job.batch/demo-job-3 created
```
All three are created — no rejection. While `demo-job-1` is still
running (`sleep 40`), both others stay queued:
```bash
kubectl get job demo-job-1 demo-job-2 demo-job-3 -n team-01 -o custom-columns=NAME:.metadata.name,SUSPEND:.spec.suspend
```
```
NAME         SUSPEND
demo-job-1   false
demo-job-2   true
demo-job-3   true
```
Once `demo-job-1` finishes, `demo-job-2` (the next-oldest submission) is
admitted automatically — `demo-job-3` stays queued, even though
cluster-wide slots are free, since team-01 already has one running:
```
2026-10-21 02:41:38,752 INFO active=0 waiting=2 free_slots=4
2026-10-21 02:41:38,752 INFO admitting team-01/demo-job-2 (suspend -> false)
```
```bash
kubectl get job demo-job-2 demo-job-3 -n team-01 -o custom-columns=NAME:.metadata.name,SUSPEND:.spec.suspend
```
```
NAME         SUSPEND
demo-job-2   false
demo-job-3   true
```

## Variables

| Variable | Default | Description |
|---|---|---|
| `TEAM_NAMESPACE_PREFIX`, `TEAM_ID` | *(required)* | Which team namespace |
| `INFRA_NAMESPACE` | *(required)* | Where the queue controller runs |
| `SPARK_IMAGE` | *(required)* | Must match exactly what the admission policy and the Job template both use |
| `SPARK_JOB_NAME`, `SPARK_COMMAND` | *(required)* | Set per submission by the student |
| `MAX_ACTIVE_JOBS` (controller env) | `4` | Cluster-wide cap |
| `POLL_INTERVAL_SECONDS` (controller env) | `5` | Reconcile loop interval |
| `STUCK_GRACE_SECONDS` (controller env) | `300` | How long an admitted Job gets before a pod that never starts gets force-terminated |
| `SPARK_IMAGE_REPO` | *(required)* | `SPARK_IMAGE` with its `@digest` stripped — used by the admission policies |
| `SPARK_QUEUE_CONTROLLER_IMAGE` | *(required)* | The controller's own pre-built image |

## Cleanup

```bash
kubectl delete -f spark-queue/portable/manifests/spark-job-admission-policy.yaml --ignore-not-found
kubectl delete -f spark-queue/portable/manifests/queue-controller-deployment.yaml --ignore-not-found
```
