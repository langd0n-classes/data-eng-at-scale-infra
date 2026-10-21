# Portable Spark Job Queue (Kind / k3s)

A `kubectl`-only job queue: at most 4 Spark jobs active cluster-wide, at
most 1 per team — any number can wait. A finished or abandoned job
releases its slot automatically.

Students only fill in and submit `manifests/team-spark-job-template.yaml`
— the admission policy and controller handle the rest. A non-Spark Job
isn't subject to the queue at all.

## Compatibility

Plain `kubectl` + `ValidatingAdmissionPolicy` (GA since Kubernetes 1.30,
no webhook server to run). Runs on any conformant Kubernetes cluster:
Kind, k3s, EKS, GKE, AKS, bare-metal, etc.

## Prerequisites

- **Team namespaces onboarded** — `onboarding/portable/README.md` (the
  queue controller deploys into the `infra` namespace onboarding creates)
- **`config.env` created and filled in** — see below

## How it's enforced

**One active job per team, any number waiting** — enforced by
`scripts/queue-controller.py`, not a quota: a suspended Job costs
nothing, so a team can submit as many as it wants, but only one per
namespace is ever admitted at a time, regardless of free cluster-wide
slots.

**Four active across the cluster** — the controller polls every 5s,
counts Jobs labeled `queue: spark`, and admits queued ones oldest-first,
skipping any team that already has one running. A Job whose pod never
reaches `Running` within `STUCK_GRACE_SECONDS` is force-terminated (its
`activeDeadlineSeconds` is patched to 1, so Kubernetes' own Job
controller fails it) rather than just excluded from the count, which
would let a late-starting pod run uncounted past the cap. A Job that's
had any pod reach `Running` at least once (e.g. mid-retry after a crash)
is never treated as stuck. `strategy: Recreate` on the Deployment stops a
redeploy from briefly running two uncoordinated controllers each able to
admit their own 4.

**Submission is validated, not just conventional** — one policy rejects a
Job using the Spark image or the `queue: spark` label unless it already
has `suspend: true`, the label, `activeDeadlineSeconds` in 1-7200,
`parallelism <= 1`, and `ttlSecondsAfterFinished <= 86400`. A second
policy rejects any ownerless Pod in a team namespace regardless of image
— covers a custom-built Spark image and `spark-submit --master k8s://`'s
ownerless driver Pod, both of which would otherwise dodge the queue.

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
with binding 'spark-job-queue-policy-binding' denied request: Use
spark-queue/portable/manifests/team-spark-job-template.yaml — it sets
spec.suspend: true so the queue controller can admit it.
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

Two team namespaces (capped at 1 each) can't reach 4 on their own, so
this uses 5 lightweight test namespaces instead. Each Job runs `sleep
600` so it's still genuinely running when steps 6-7 test against it:

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

To prove cancellation specifically (not the job just finishing on its
own), confirm job-1 is still genuinely running before deleting it:

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

The other way a slot frees up: a job whose pod never starts, instead of
a person deleting it. Submit a Job with a bogus image tag so its pod
sits in `ImagePullBackOff` — it's admitted normally first, since the
queue has no way to know in advance:

```bash
source config.env
SPARK_JOB_NAME=stuck-job TEAM_NAMESPACE_PREFIX=team TEAM_ID=01 \
SPARK_IMAGE="apache/spark:nonexistent-tag-for-stuck-test" SPARK_COMMAND="echo unreachable" \
envsubst '${TEAM_NAMESPACE_PREFIX} ${TEAM_ID} ${SPARK_JOB_NAME} ${SPARK_IMAGE} ${SPARK_COMMAND}' \
< spark-queue/portable/manifests/team-spark-job-template.yaml | kubectl apply -f -

kubectl get pods -n team-01 -l job-name=stuck-job
# stuck-job-fpjdd   0/1   ImagePullBackOff
```
`STUCK_GRACE_SECONDS` defaults to 300s (5 min) — lowered here to 20s so
this test doesn't take 5 minutes, not a change to the shipped default:
```bash
kubectl set env deployment/spark-queue-controller -n infra STUCK_GRACE_SECONDS=20
```
Once the grace window passes, the controller force-terminates it —
confirmed by its own log:
```
2026-10-21 02:28:38,282 WARNING team-01/stuck-job stuck (no pod ever reached Running after 20s) — forcing termination
2026-10-21 02:28:38,293 INFO active=0 waiting=0 free_slots=4
```
The Job reaches a real `Failed`/`DeadlineExceeded` condition within
seconds:
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

A Job retries after a pod failure by creating a *new* pod, not
restarting the old one — a naive "is any current pod Running" check
would misread a retrying job as "never started" and kill it. Submit a
Job that always fails, exhausting the template's `backoffLimit: 2`
across multiple real, started-and-failed attempts:

```bash
source config.env
SPARK_JOB_NAME=retry-test TEAM_NAMESPACE_PREFIX=team TEAM_ID=01 SPARK_COMMAND="exit 1" \
envsubst '${TEAM_NAMESPACE_PREFIX} ${TEAM_ID} ${SPARK_JOB_NAME} ${SPARK_IMAGE} ${SPARK_COMMAND}' \
< spark-queue/portable/manifests/team-spark-job-template.yaml | kubectl apply -f -
```
Each retry pod genuinely starts and fails (`Error`, not `Pending`), and
the Job reaches its own natural terminal state, never force-terminated:
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
`activeDeadlineSeconds` stayed `7200` (never patched to `1`), and the
reason is `BackoffLimitExceeded`, not `DeadlineExceeded` — proof the
stuck-job path never triggered.

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
