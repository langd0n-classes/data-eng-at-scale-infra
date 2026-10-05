# Portable Fall 2026 Platform — Orchestration

The single index for the whole portable platform's orchestration
commands. Each one just calls into the real, already-documented
per-component scripts — nothing here duplicates those; see each
component's own `README.md` for what each piece actually does and how to
test it standalone.

Covered elsewhere: `onboarding/portable/README.md`, `kafka/portable/README.md`,
`registry/portable/README.md`, `event-generator/portable/README.md`,
`ingress/portable/README.md`, `spark-queue/portable/README.md`,
`pipeline/portable/README.md`.

## Before you start

A cluster must already exist and be the current `kubectl` context —
`install-prerequisites.sh` doesn't create one.

### On Kind

The cluster must be created with a NetworkPolicy-enforcing CNI (Calico)
already installed, not just Kind's own default — see
`onboarding/portable/README.md`'s walkthrough step 1 for the exact
commands. Skipping that doesn't make anything fail outright, it just
means onboarding's own team isolation silently isn't enforced.

### Then, on any cluster

```bash
cp config.env.example config.env
cp onboarding/cluster.env.example onboarding/cluster.env
# edit both — see each component's own README for which variables matter
```

## The commands

Split by **what actually changes together**: prerequisites (install once,
rarely touched again) vs. the pipeline definition (changes only if you
modify the deploy pipeline itself) vs. triggering a run (the thing you
actually do often).

**1. `install-prerequisites.sh`** — everything needed before the pipeline
can run, and none of it changes when the pipeline's own definition does:
cluster-wide installs (Strimzi, Tekton Pipelines, Tekton Dashboard,
ingress-nginx), onboarding (team namespaces, quota, ServiceAccount, RBAC,
NetworkPolicy, per-team Spark quota), and shared infra services (the
in-cluster registry, the Spark queue controller + admission policy, the
Dashboard's Ingress). Idempotent — safe to re-run.

> Students submit Spark jobs using
> `spark-queue/portable/manifests/team-spark-job-template.yaml` — it
> already sets the three things required together for a Spark job to be
> accepted and actually run: an image matching `SPARK_IMAGE` from
> `config.env`, `spec.suspend: true`, and the `queue: spark` label. A
> student writing their own Job YAML must include all three, or the
> job either gets rejected outright (missing `suspend`/the label) or
> never runs (image mismatch, silently excluded from the queue). See
> `spark-queue/portable/README.md` for the full enforcement model.

A student submitting any other (non-Spark) Job doesn't need any of the
three — but the per-team `ResourceQuota` still caps them at one Job object
at a time regardless, so they can't submit it while a Spark job is still
sitting in their namespace, queued or not.

```bash
bash portable/scripts/install-prerequisites.sh
```

**2. `deploy-pipeline.sh`** — the part that *does* change if you modify
the deploy pipeline itself: pipeline RBAC (including the `pipeline`
ServiceAccount), the Tekton Tasks, the Pipeline definition — then submits
the first `PipelineRun`. Assumes step 1 already ran.

```bash
bash portable/scripts/deploy-pipeline.sh
```

**3. Trigger another run** — after the first time, skip re-applying
RBAC/Tasks/Pipeline for nothing; just submit a new run directly. Same
reasoning Tekton itself uses everywhere: apply Tasks/Pipeline once, submit
`PipelineRun`s repeatedly. Use this whenever you push new code, add a
team, or retry after a partial failure.

```bash
source config.env
export RUN_TIMESTAMP=$(date +%Y%m%d-%H%M%S)
envsubst < pipeline/portable/runs/run-all-teams-pipeline-run.yaml | kubectl create -f -
```

**4. `status-platform.sh`** — read-only report across every piece, both
prerequisites and pipeline. Never changes anything.

```bash
bash portable/scripts/status-platform.sh
```

**5. `teardown-pipeline.sh`** — undoes step 2: every `PipelineRun`, the
Pipeline, the Tasks, pipeline RBAC (both the dedicated-cluster
ClusterRole/ClusterRoleBinding path and the shared-cluster
Role/RoleBinding-per-namespace path `apply-rbac.sh` can use), the shared
event generator, and — since this is what a `PipelineRun` actually
produces, not onboarding — each team's Kafka (CR, KafkaNodePool, PDB,
PVCs). Team namespaces and onboarding are left alone, so you can wipe and
resubmit a run without redoing onboarding.

```bash
bash portable/scripts/teardown-pipeline.sh
```

**6. `teardown-prerequisites.sh`** — undoes step 1, in full symmetry with
onboarding's own namespace creation: every team namespace *and* the infra
namespace itself (cascading everything inside each — the registry, the
Spark queue controller included), plus the Spark queue's cluster-scoped
objects (admission policy, RBAC — a namespace delete can't reach those),
the Dashboard Ingress, and the cluster-wide prerequisites themselves.
Rarely needed — mainly for a real shared cloud cluster, where deleting the
whole cluster isn't an option the way it is on Kind.

```bash
bash portable/scripts/teardown-prerequisites.sh
```

## Validated end to end

Full cycle, in this order, on the same cluster:

1. `teardown-pipeline.sh` — removed every team's Kafka (CR, KafkaNodePool,
   PDB, PVC) via `kafka/portable/scripts/remove.sh`, the event generator,
   Tekton Tasks/Pipeline/runs, and pipeline RBAC. Confirmed team
   namespaces survived untouched (`kubectl get ns team-01 team-02
   team-03` still `Active`) and Kafka was actually gone
   (`kubectl get kafka -A` → nothing).
2. `teardown-prerequisites.sh` — confirmed Strimzi, Tekton Pipelines,
   Tekton Dashboard, and ingress-nginx all actually gone (their CRDs and
   namespaces deleted).
3. `install-prerequisites.sh` — from that fully torn-down state, all 6
   steps succeed. Found and fixed a real race along the way: applying the
   Dashboard Ingress can hit "connection refused" against ingress-nginx's
   admission webhook — confirmed this isn't just an ordering issue (moving
   the step later, and even explicitly waiting for the webhook's
   `Endpoints` object to look ready, both still hit it once); the real
   cause is kube-proxy's own iptables/ipvs rules lagging behind a freshly
   created pod's `Endpoints` entry. Fixed with a retry loop around the
   apply itself (confirmed live: attempt 1 failed with the exact race,
   attempt 2 succeeded) rather than trusting any indirect readiness
   signal.
4. `deploy-pipeline.sh` — all 3 steps succeed; a real `PipelineRun`
   completes (`SUCCEEDED: True, Completed`).
5. The one-liner re-trigger — submitted a second run directly, without
   re-applying RBAC/Tasks/Pipeline; also completed successfully.
6. `status-platform.sh` — reports every piece correctly:

```
── Cluster-wide prerequisites ──
strimzi-cluster-operator   1/1   1   1   5m19s
tekton-pipelines-controller   1/1   1   1   4m45s
tekton-pipelines-webhook      1/1   1   1   4m45s
tekton-dashboard   1/1   1   1   4m31s
ingress-nginx-controller   1/1   1   1   4m29s
ingress-nginx external IP: 172.18.0.3

── Registry ──
registry   1/1   1   1   2m13s
registry   NodePort   10.96.42.67   <none>   5000:30500/TCP   2m13s

── Onboarding (namespaces) ──
infra     Active   3d14h
team-01   Active   2m14s
team-02   Active   2m14s
team-03   Active   2m14s

── Spark queue ──
spark-queue-controller   1/1   1   1   2m2s
spark-job-queue-policy   2     <unset>   2m2s

── Per-team Kafka ──
kafka-team01   True   4.2.0   4.2-IV0
kafka-team02   True   4.2.0   4.2-IV0
kafka-team03   True   4.2.0   4.2-IV0

── Event generator ──
event-generator   1/1   1   1   43s

── Latest PipelineRun ──
deploy-all-teams-run-20261004-180506   True   Completed   20s   1s
```
