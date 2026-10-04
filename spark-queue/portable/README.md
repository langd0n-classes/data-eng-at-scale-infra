# Portable Spark Job Queue (Kind / k3s)

A `kubectl`-only job queue for the portable Fall 2026 platform: at most
four Spark jobs active across the whole cluster, at most one active per
team, with an abandoned or finished job releasing its slot automatically.

This is a brand-new top-level component — Spark has no existing footprint
anywhere in this repo (only a label in the root README's architecture
diagram, referring to something entirely downstream of this infra).

This is infra-maintainer-built, the same way every other component in this
repo is: students only ever fill in and submit
`manifests/team-spark-job-template.yaml` — they never touch the
ResourceQuota, the admission policy, or the queue controller.

## Compatibility

Plain `kubectl` + one `ValidatingAdmissionPolicy` (GA in Kubernetes since
1.30, no webhook server to run or manage). Runs on any conformant
Kubernetes cluster: Kind, k3s, EKS, GKE, AKS, a bare-metal cluster, etc.

## How the two limits are actually enforced

**"One active job per team"** — a plain namespace-scoped `ResourceQuota`
(`count/jobs.batch: "1"`), applied once per team namespace at onboarding
time. This is a hard admission check the API server itself enforces,
counting every Job in that namespace regardless of label or image — a
student cannot create a second Job while their first one still exists,
whether it's running or just sitting there. `ttlSecondsAfterFinished` on
the student's own Job (set by the template) is what frees this slot
automatically once the Job finishes.

**"Four active across the cluster"** — the one thing a namespace-scoped
`ResourceQuota` can't express (it's namespace-scoped only). A small Python
controller (`scripts/queue-controller.py`, ~70 lines, reusing the
`kubernetes` client pattern already used in `chatops/src/commands.py`)
polls every 5 seconds, counts Jobs labeled `queue: spark` across every
namespace that are running and not yet finished, and admits queued ones
(flips `spec.suspend` from `true` to `false`) oldest-submission-first, up
to 4 at a time.

**Enforcement, not just convention** — a student could skip the template
and submit a raw Job using the Spark image directly, with no
`queue: spark` label and no `suspend: true`, which would run immediately
and bypass the cluster-wide cap (the per-team cap stays safe either way,
since `ResourceQuota` counts everything regardless). A
`ValidatingAdmissionPolicy` closes this: matched only to Jobs using the
pinned Spark image (item 12's own wording says "Spark jobs" specifically,
never "jobs" generally — a plain Job using any other image is never
touched by this at all), it **rejects** a Job outright if it doesn't
already have `suspend: true` and the `queue: spark` label.
(`MutatingAdmissionPolicy` — which could silently fix a non-compliant
submission instead of rejecting it — isn't available on this cluster:
confirmed directly via `kubectl api-resources`, not assumed. Rejecting is
arguably the better fit anyway: it forces the actual template to be used
rather than silently rewriting what a student submitted.)

## Before you start: update config.env

```bash
# one-time: create your own config.env from the template
cp config.env.example config.env
```
Then edit `config.env` and set:

| Variable | Example value | Notes |
|---|---|---|
| `INFRA_NAMESPACE` | `infra` | Where the queue controller runs |
| `SPARK_IMAGE` | `apache/spark:3.5.3` | Must match exactly what `team-spark-job-template.yaml` is filled in with, since the admission policy matches on this image reference |
| `TEAM_NAMESPACE_PREFIX` | `team` | Must match `onboarding/cluster.env`'s value |

`TEAM_ID` below is **not** a `config.env` variable — it's set per-command,
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

# the controller script, as a ConfigMap the Deployment below mounts
kubectl create configmap spark-queue-controller-script \
  --from-file=queue-controller.py=spark-queue/portable/scripts/queue-controller.py \
  -n infra --dry-run=client -o yaml | kubectl apply -f -
# the controller itself (RBAC + Deployment)
envsubst '${INFRA_NAMESPACE}' < spark-queue/portable/manifests/queue-controller-deployment.yaml | kubectl apply -f -

# the enforcement policy
envsubst '${SPARK_IMAGE}' < spark-queue/portable/manifests/spark-job-admission-policy.yaml | kubectl apply -f -
```
```
deployment.apps/spark-queue-controller condition met
2026-09-29 05:26:17,410 INFO Spark queue controller started: max_active_jobs=4 poll_interval=5s
validatingadmissionpolicy.admissionregistration.k8s.io/spark-job-queue-policy created
validatingadmissionpolicybinding.admissionregistration.k8s.io/spark-job-queue-policy-binding created
```

**2. Evasion test — a raw Spark-image Job, bypassing the template**

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

**3. A non-Spark Job in the same namespace is completely unaffected**

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

**4. The cluster-wide 4-slot cap, genuinely exercised (5 namespaces)**

Two team namespaces (each capped at 1) can never reach 4 concurrently on
their own, so this uses 5 lightweight test namespaces to actually force
queueing — a compliant Job (via the template) submitted to each:

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
Jobs 1–4 admitted immediately; job-5 genuinely held (`suspend: true`),
waiting — proving the cap actually binds, not just that it doesn't get in
the way.

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

**6. The issue's own literal wording — 5 job attempts across 2 team namespaces**

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
Exactly 2 Jobs end up created (1 per namespace) — well within "never
exceed 4 active or 1 active per namespace," and the rejections themselves
are the per-team cap actually enforcing, not merely coincidental.

## Variables

| Variable | Default | Description |
|---|---|---|
| `TEAM_NAMESPACE_PREFIX`, `TEAM_ID` | *(required)* | Which team namespace |
| `INFRA_NAMESPACE` | *(required)* | Where the queue controller runs |
| `SPARK_IMAGE` | *(required)* | Must match exactly what the admission policy and the Job template both use |
| `SPARK_JOB_NAME`, `SPARK_COMMAND` | *(required)* | Set per submission by the student |
| `MAX_ACTIVE_JOBS` (controller env) | `4` | Cluster-wide cap |
| `POLL_INTERVAL_SECONDS` (controller env) | `5` | Reconcile loop interval |

## Cleanup

```bash
kubectl delete -f spark-queue/portable/manifests/spark-job-admission-policy.yaml --ignore-not-found
kubectl delete -f spark-queue/portable/manifests/queue-controller-deployment.yaml --ignore-not-found
kubectl delete configmap spark-queue-controller-script -n infra --ignore-not-found
for id in 01 02; do kubectl delete resourcequota team-spark-jobs-quota -n team-$id --ignore-not-found; done
```
