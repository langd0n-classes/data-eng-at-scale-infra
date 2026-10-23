# Portable Fall 2026 Platform — Orchestration

The single index for the whole portable platform's orchestration
commands. Each one just calls into the real, already-documented
per-component scripts — nothing here duplicates those; see each
component's own `README.md` for what each piece actually does and how to
test it standalone.

Covered elsewhere: `onboarding/portable/README.md`, `kafka/portable/README.md`,
`event-generator/portable/README.md`, `ingress/portable/README.md`,
`spark-queue/portable/README.md`, `pipeline/portable/README.md`,
`providers/README.md`.

## Before you start

**1. Have a cluster ready** — `install-prerequisites.sh` doesn't create
one for you.

- **Local Kind cluster**: create it with a NetworkPolicy-enforcing CNI
  (Calico), not Kind's own default — see `onboarding/portable/README.md`'s
  walkthrough step 1 for the exact commands.
- **A real cluster you already have** (managed or self-hosted): confirm
  Kubernetes 1.30+ and a dynamic StorageClass. NetworkPolicy enforcement
  doesn't need manual checking — `apply-onboarding.sh` proves it live,
  either way.

**2. Pick your provider** — see `providers/README.md` for what this is
and how to use it. Defaults to `kind` — nothing to do for local
validation.

**3. Copy and edit the two env files**:

```bash
cp config.env.example config.env
cp onboarding/cluster.env.example onboarding/cluster.env
# edit both — see each component's own README for which variables matter
```

**4. Create the GHCR pull secret**

Event-generator and the Spark queue controller are private GHCR images
(`ghcr.io/langd0n-classes/...`), built by GitHub Actions — nothing in
this repo builds them in-cluster anymore. `install-prerequisites.sh`
below deploys the queue controller from this image, so the pull
credential has to exist first:

```bash
kubectl create namespace infra --dry-run=client -o yaml | kubectl apply -f -

# Generate a classic GitHub PAT first: Settings -> Developer settings ->
# Personal access tokens -> Tokens (classic) -> scope read:packages only,
# with an expiration (don't leave it permanent).
#
# Read it into a variable rather than typing it as a literal argument —
# a literal --docker-password=YOUR_TOKEN leaks into shell history and
# briefly into `ps aux`. The -p/prompt flag on `read` differs between
# bash and zsh, so prompt with a plain echo instead — portable either way:
echo -n "GHCR PAT (read:packages): "
read -rs GHCR_PAT
echo
# Replace YOUR_GITHUB_USERNAME below with your actual GitHub username —
# no brackets, this is a real argument, not a placeholder marker (a
# literal <...> is shell redirection syntax, not something to paste as-is).
kubectl create secret docker-registry ghcr-pull-secret \
  --docker-server=ghcr.io \
  --docker-username=YOUR_GITHUB_USERNAME \
  --docker-password="${GHCR_PAT}" \
  -n infra
unset GHCR_PAT
```

Never redirect `kubectl create secret ... -o yaml` to a file inside the
repo, even temporarily. GitHub Actions pushes using its own built-in
`GITHUB_TOKEN`, not a PAT.

**5. Create the Tekton Dashboard auth + TLS secrets**

The Dashboard Ingress (`pipeline/portable/manifests/tekton-dashboard-ingress-template.yaml`)
requires two Secrets to already exist in `tekton-pipelines` before it's
applied — pick a username and password for Dashboard access first:

```bash
# tekton-pipelines doesn't exist yet this early (Tekton itself is
# installed later, in install-prerequisites.sh) — create it now so the
# secrets below have somewhere to go; Tekton's own install merges into
# this same namespace cleanly.
kubectl create namespace tekton-pipelines --dry-run=client -o yaml | kubectl apply -f -

echo -n "Tekton Dashboard username: "
read -r DASHBOARD_USER
echo -n "Tekton Dashboard password: "
read -rs DASHBOARD_PASS
echo

# bcrypt-hash it into htpasswd format (requires htpasswd from
# apache2-utils/httpd-tools; falls back to a one-off container if you
# don't have it installed locally)
if command -v htpasswd >/dev/null 2>&1; then
  HTPASSWD_LINE="$(htpasswd -nbB "${DASHBOARD_USER}" "${DASHBOARD_PASS}")"
else
  HTPASSWD_LINE="$(docker run --rm httpd:2.4-alpine htpasswd -nbB "${DASHBOARD_USER}" "${DASHBOARD_PASS}")"
fi
kubectl create secret generic tekton-dashboard-auth \
  --from-literal=auth="${HTPASSWD_LINE}" \
  -n tekton-pipelines
unset DASHBOARD_PASS HTPASSWD_LINE

# Self-signed TLS cert for DASHBOARD_HOST — RSA 2048 + SHA-256, with a
# subjectAltName (modern clients require SAN, not just CN)
source config.env
openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
  -keyout /tmp/tekton-dashboard.key -out /tmp/tekton-dashboard.crt \
  -subj "/CN=${DASHBOARD_HOST}" \
  -addext "subjectAltName=DNS:${DASHBOARD_HOST}"
kubectl create secret tls tekton-dashboard-tls \
  --cert=/tmp/tekton-dashboard.crt --key=/tmp/tekton-dashboard.key \
  -n tekton-pipelines
rm -f /tmp/tekton-dashboard.key /tmp/tekton-dashboard.crt
```

Never redirect `kubectl create secret ... -o yaml` to a file inside the
repo, even temporarily. Nothing else to create manually.

## The commands

Split by **what actually changes together**: prerequisites (install once,
rarely touched again) vs. the pipeline definition (changes only if you
modify the deploy pipeline itself) vs. triggering a run (the thing you
actually do often).

**1. `install-prerequisites.sh`** — everything needed before the pipeline
can run, and none of it changes when the pipeline's own definition does:
cluster-wide installs (Strimzi, Tekton Pipelines, Tekton Dashboard,
ingress-nginx), onboarding (team namespaces, quota, ServiceAccount, RBAC,
NetworkPolicy), and shared infra services (the Spark queue controller +
admission policy, the Dashboard's Ingress). Idempotent — safe to re-run.

**Only if you're on Kind**: this step detects it (via your `kubectl`
context starting with `kind-`) and tells you, mid-run, that
`ingress-nginx` has no external IP yet and to start this separately, in
its own terminal — Kind has no LoadBalancer implementation of its own,
unlike a real cluster, which needs nothing here:

```bash
sudo bash ingress/portable/prerequisites/install-cloud-provider-kind.sh
```

Needs `sudo` and must keep running in the background for as long as
you want `LoadBalancer` Services to keep working — that's why it's a
separate command, not folded into the script itself (a script can't
prompt for a sudo password mid-run, and can't leave a daemon running
past its own exit). See `ingress/portable/README.md` for more.

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
three, and isn't subject to the queue at all — only Jobs matching the
Spark image or carrying the `queue: spark` label are gated.

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
source "providers/${PROVIDER:-kind}.env"   # sets STORAGE_CLASS for the run below
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
resubmit a run without redoing onboarding. Asks for confirmation first;
`FORCE=true` skips the prompt.

```bash
bash portable/scripts/teardown-pipeline.sh              # interactive
FORCE=true bash portable/scripts/teardown-pipeline.sh   # no prompt
```

**6. `teardown-prerequisites.sh`** — undoes step 1, in full symmetry with
onboarding's own namespace creation: every team namespace *and* the infra
namespace itself (cascading everything inside each — the Spark queue
controller included), plus the Spark queue's cluster-scoped
objects (admission policy, RBAC — a namespace delete can't reach those)
and the Dashboard Ingress. Asks for confirmation first.

The cluster-wide prerequisites themselves (ingress-nginx, Tekton,
Strimzi) are **not** touched by default — they're shared, cluster-wide
singletons another tenant on the same cluster might depend on. Set
`CLUSTER_WIDE_TEARDOWN=true` to also remove them (asks a second,
separate confirmation); `FORCE=true` skips any prompt.

```bash
bash portable/scripts/teardown-prerequisites.sh                                 # this platform's own stuff only
FORCE=true bash portable/scripts/teardown-prerequisites.sh                       # same, no prompt
CLUSTER_WIDE_TEARDOWN=true bash portable/scripts/teardown-prerequisites.sh       # also removes ingress-nginx/Tekton/Strimzi
FORCE=true CLUSTER_WIDE_TEARDOWN=true bash portable/scripts/teardown-prerequisites.sh   # full wipe, no prompts
```

**On Kind**: skip all of this entirely — just delete the whole cluster
instead:

```bash
kind delete cluster --name portable-fall-platform
```

**On a real cluster**: deleting the whole cluster usually isn't an
option, so the commands above are how you tear the platform down
without touching the cluster itself.

## Validated end to end

This record is from the Kind path specifically — the mechanics are
identical on a real cluster, per "Before you start" above.

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
3. `install-prerequisites.sh` — from that fully torn-down state, all 5
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

── GHCR pull credential ──
ghcr-pull-secret   kubernetes.io/dockerconfigjson   1   2m13s

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

## OVH validation

The OVH adapter (`providers/ovh.env`) has been validated on an OVHcloud
MKS cluster prior to merge, covering each item requested in review:

- **StorageClass** — confirmed as `csi-cinder-high-speed-gen2` (the
  cluster's own default), matching `providers/ovh.env`.
- **LoadBalancer behavior** — `ingress-nginx-controller` received an
  external IP from Octavia automatically. The Tekton Dashboard was
  reachable through it with valid TLS and basic auth, verified from an
  external machine via `curl`, not `kubectl exec`.
- **NetworkPolicy enforcement check** — confirmed that a deny-all-ingress
  policy is genuinely enforced, matching the Kind result. The CNI
  differs (OVH's Free plan uses Canal; Kind uses pure Calico), but
  enforcement behavior is equivalent.
- **Isolation tests** — all 9 reachability probes (5 same/cross-namespace,
  3 Kafka, 2 Tekton Dashboard) matched the expected reached/blocked
  outcome exactly; the shared-ingress host-takeover and `ExternalName`
  backend bypass were each confirmed denied; and direct `LoadBalancer`/
  `NodePort` Service creation in a team namespace was confirmed rejected
  by quota.
- **Queue tests** — the full 8-step Spark queue walkthrough (compliant
  job, evasion rejected, non-Spark job unaffected, 4-slot cluster-wide
  cap, cancellation freeing a slot, stuck-job force-termination, retry
  correctly not misclassified as stuck, one-job-per-team queueing) ran
  against real namespaces, with every result matching the documented
  expectation.

Full per-item evidence is recorded alongside the Kind results in each
component's own README: `providers/README.md`,
`onboarding/portable/README.md`, `ingress/portable/README.md`, and
`spark-queue/portable/README.md`.

Separately, the complete orchestration cycle described above has also
been run end to end on a 15-team deployment on the same cluster type:
`install-prerequisites.sh` → staged `deploy-pipeline.sh` reruns
(10 → 13 → 15 teams) → the full workload, retention, isolation,
snapshot, and node-recovery test suite → full teardown, with
`status-platform.sh` reporting correctly at every team count throughout.
Two issues were identified and fixed at the source in the process: a
Kafka `PodDisruptionBudget` selector that matched no pods, and
`event-generator` permanently dropping a team after a connection race.
