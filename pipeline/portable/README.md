# Portable Pipeline (Kind / k3s)

A `kubectl`-only variant of Tekton CI/CD for the portable Fall 2026
platform: Tekton Pipelines, a read-only Tekton Dashboard, an in-cluster
image registry, and a per-team deploy pipeline (Kafka + event generator,
deliberately no NiFi) — no OpenShift Pipelines operator, no OpenShift
Routes/BuildConfigs/ImageStreams, no cloud account, no paid service.

This does **not** replace the OpenShift path. `pipeline/setup.sh`,
`pipeline/ops.sh`, and everything under `pipeline/tasks/`,
`pipeline/pipelines/`, and `pipeline/rbac/` are unchanged and remain the
primary CI/CD method for the classroom.

**Out of scope here:** ChatOps, Kafka Console, NiFi's own deployment (NiFi
itself stays fully available and untouched via the OpenShift path — only
the *pipeline's active dependency* on deploying it is removed here, per
issue #57 item 10).

## Compatibility

Plain `kubectl`, Tekton's own upstream release manifests, no OpenShift-only
resources (no SCC dependency — Tekton Pipelines' own ServiceAccounts run
under standard Pod Security, unlike the OpenShift path's `pipeline`
ServiceAccount, which specifically exists to receive the `pipelines-scc`
auto-binding OpenShift Pipelines grants it). Runs on any conformant
Kubernetes cluster: Kind, k3s, EKS, GKE, AKS, a bare-metal cluster, etc.

## Validation walkthrough (prerequisites so far)

Validated on: Kubernetes (Kind) v1.34.11, Tekton Pipelines `v1.15.3` (LTS),
Tekton Dashboard `v0.72.0` (read-only), ingress-nginx `controller-v1.15.1` +
cloud-provider-kind `v0.11.1` (from `ingress/portable/`).

**1. Install Tekton Pipelines**

```bash
bash pipeline/portable/prerequisites/install-tekton.sh
```
```
deployment.apps/tekton-pipelines-controller condition met
deployment.apps/tekton-pipelines-webhook condition met

NAME                          READY   UP-TO-DATE   AVAILABLE   AGE
tekton-events-controller      1/1     1            1           40s
tekton-pipelines-controller   1/1     1            1           40s
tekton-pipelines-webhook      1/1     1            1           40s
```

**2. Install the read-only Tekton Dashboard**

Confirmed directly in the downloaded manifest (not assumed): the plain
`release.yaml` build passes `--read-only=true` to the dashboard container —
`release-full.yaml` is the read/write variant and is deliberately not used.

```bash
bash pipeline/portable/prerequisites/install-tekton-dashboard.sh
```
```
deployment.apps/tekton-dashboard condition met

NAME               READY   UP-TO-DATE   AVAILABLE   AGE
tekton-dashboard   1/1     1            1           4s
NAME               TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)    AGE
tekton-dashboard   ClusterIP   10.96.139.146   <none>        9097/TCP   4s
```

**3. Expose it through the shared ingress-nginx LoadBalancer**

Requires `ingress/portable/` already installed (ingress-nginx +
cloud-provider-kind).

```bash
DASHBOARD_HOST=tekton.local envsubst '${DASHBOARD_HOST}' \
  < pipeline/portable/manifests/tekton-dashboard-ingress-template.yaml | kubectl apply -f -
```
```
ingress.networking.k8s.io/tekton-dashboard created
```

**4. Confirm it's reachable, and confirm read-only is actually enforced**

```bash
# Reads succeed
curl -s --resolve tekton.local:80:172.18.0.3 http://tekton.local/api/v1/namespaces -o - -w "\nHTTP %{http_code}\n"
```
```
{"kind":"NamespaceList", ... }
HTTP 200
```

```bash
# Writes are rejected
curl -s -X POST --resolve tekton.local:80:172.18.0.3 \
  http://tekton.local/api/v1/namespaces/default/pipelineruns -d '{}' -w "\nHTTP %{http_code}\n"
```
```
Forbidden - CSRF header not found in request
HTTP 403
```

`172.18.0.3` above is the ingress-nginx LoadBalancer's external IP from
`ingress/portable/` — substitute your own (`kubectl get svc
ingress-nginx-controller -n ingress-nginx`). On a real DNS-backed cluster,
`--resolve` isn't needed — point `DASHBOARD_HOST` at a real hostname
instead.

## Image build and registry path

The in-cluster registry itself lives in `registry/portable/` (its own
top-level component — see that directory's README for the full
validation walkthrough and a non-obvious fact its design depends on: image
pulls are done by kubelet on the node, not from inside a pod, so the
registry is exposed as a NodePort, not a ClusterIP).

`tasks/git-clone-task.yaml` and `tasks/build-push-image-task.yaml`, both
defined once in the `infra` namespace, are the two Tasks a team's
`PipelineRun` chains together to build and push its own image. Validated
end to end with a real `PipelineRun`: cloned this repo, built
`event-generator`'s image with kaniko, pushed it, and started a real pod
from the exact same reference — the running pod's `imageID` digest matched
what was pushed:

```
INFO Pushing image to localhost:30500/event-generator:test2
INFO Pushed localhost:30500/event-generator@sha256:755e0c484f3664e0d115ee0d649be607ecfbc276b46a8df02bb478a3b955423c
```
```bash
kubectl get pod event-gen-from-registry -n infra -o jsonpath='{.status.containerStatuses[0].imageID}'
```
```
localhost:30500/event-generator@sha256:755e0c484f3664e0d115ee0d649be607ecfbc276b46a8df02bb478a3b955423c
```

Both Tasks need `hostNetwork: true` set on the PipelineRun's pod template
(`spec.taskRunTemplate.podTemplate.hostNetwork: true`), so the build step
can reach `localhost:${REGISTRY_NODE_PORT}` the same way kubelet does.

## Pipeline RBAC

`rbac/` auto-detects "dedicated" vs. "shared" cluster the same way
`onboarding/apply-onboarding.sh` already does (`kubectl auth can-i create
clusterrolebindings`):

```bash
bash pipeline/portable/rbac/apply-rbac.sh
```
```
serviceaccount/pipeline created
Cluster type: dedicated (cluster-admin available) — using a ClusterRoleBinding
clusterrole.rbac.authorization.k8s.io/pipeline-runner-role-portable created
clusterrolebinding.rbac.authorization.k8s.io/pipeline-runner-binding-portable created
```

OVHcloud's default kubeconfig also gives full cluster-admin access, so this
same dedicated-cluster path applies there too — confirmed via OVHcloud's
own documentation, not assumed. The shared-cluster fallback
(`role-rolebinding-namespace.yaml` + `clusterrole-namespaces-read.yaml`,
mirroring `pipeline/rbac/04-role-rolebinding-namespace.yaml`'s NERC
scenario) exists for if that ever turns out not to hold.

## The full per-team deploy pipeline

`tasks/deploy-kafka-task.yaml` and `tasks/deploy-event-generator-task.yaml`
kubectl-wrap the already-portable `kafka/portable/scripts/deploy.sh` and
`event-generator/portable/k8s/` respectively, from a git-cloned copy of
this repo. `pipelines/deploy-all-teams-pipeline.yaml` chains them all
together: clone → build the event generator's image → deploy every team's
Kafka in parallel → deploy the event generator once, after every team's
Kafka is up — the same shape as
`pipeline/pipelines/01-pipeline-deploy-all-teams.yaml`, minus any NiFi
task anywhere in the chain. It hardcodes team1 through team15 the same way
the existing pipeline already does (Tekton's `matrix` feature can't derive
one param from another, e.g. building "team-01" out of "team" + "01").

Validated end to end with a real `PipelineRun` for two teams — clone, build
+ push the event-generator image, deploy Kafka for both teams in parallel,
then deploy the event generator once both are ready:

```
NAME                   SUCCEEDED   REASON      STARTTIME   COMPLETIONTIME
deploy-all-teams-run   True        Succeeded   40s         0s
```
```bash
kubectl get kafka -n team-01   # Ready
kubectl get kafka -n team-02   # Ready
kubectl logs -n infra -l app=event-generator --tail=3
```
```
[EMIT] Produced 100 events → teams: [team01, team02]
```

Both teams' Kafka independently confirmed to receive real events (same
consume-from-topic check as `event-generator/portable/README.md`, repeated
for `team-02`).

**Idempotency** (re-running the exact same `PipelineRun` a second time):

```bash
kubectl get kafka kafka-team01 -n team-01 -o jsonpath='{.metadata.resourceVersion}'
# 42057
# ... re-run ...
kubectl get kafka kafka-team01 -n team-01 -o jsonpath='{.metadata.resourceVersion}'
# 42057 — unchanged
kubectl get pod -n team-01 -l strimzi.io/cluster=kafka-team01 -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}'
# 0 — broker never restarted
```

The full 15-team pipeline definition (18 Tasks, 46 params) was confirmed
structurally valid by Tekton's own admission webhook; live validation used
2 teams — each team block is mechanically identical to the two already
proven, and running all 15 Kafka brokers simultaneously wasn't necessary
to prove that.

Every `TEAMn_NAME`/`TEAMn_NAMESPACE` defaults to `""`, and each
`deploy-kafka-teamN` task carries a `when` guard skipping it entirely when
its own `TEAMn_NAME` is blank or `"skip"` (matching `config.env`'s own
existing convention for marking a team slot unused) — a class with fewer
than 15 teams just leaves the unused slots that way rather than needing a
trimmed-down copy of this file. Confirmed for real: with `config.env`
configured for 2 real teams and 13 slots left as `"skip"`, only 2
`deploy-kafka-teamN` pods were created; the other 13 were cleanly skipped,
not attempted and failed.

## The orchestrator: one repeatable deployment path

`scripts/deploy-platform.sh` is the single documented command — installs
every cluster-wide prerequisite (idempotent throughout), onboards teams,
deploys the registry and Spark queue, applies pipeline RBAC and every
Tekton resource, and submits the `deploy-all-teams` `PipelineRun`.
`scripts/status-platform.sh` is a read-only report across every piece.
`scripts/teardown-platform.sh` removes platform and team resources —
every team namespace (cascading to its Kafka CRs, PVCs, quota, RBAC,
NetworkPolicy), the event generator, the Spark queue, and every Tekton
resource — deliberately leaving the cluster-wide prerequisites (Strimzi,
Tekton itself, ingress-nginx) installed, the same scoping
`kafka/portable/README.md` already documents for its own operator cleanup.

Validated end to end, in this order, on the same cluster:

1. `deploy-platform.sh` from an already-partially-deployed state — full
   idempotent re-apply, then a real `PipelineRun` succeeds
   (`SUCCEEDED: True, Completed`).
2. `status-platform.sh` — reports every piece correctly (operator,
   Tekton, Dashboard, ingress-nginx + its external IP, registry,
   namespaces, Spark queue, per-team Kafka, event generator, latest
   PipelineRun).
3. `teardown-platform.sh` — removes both team namespaces; confirmed their
   Kafka CRs and PVCs are actually gone
   (`kubectl get pvc -A | grep team` → nothing); re-running teardown a
   second time is a clean no-op.
4. `deploy-platform.sh` again, from that fully torn-down state — a
   complete fresh deploy succeeds, both teams' Kafka comes up Ready, the
   event generator deploys and starts producing within seconds.

## Cleanup (prerequisites so far)

```bash
kubectl delete -f pipeline/portable/manifests/tekton-dashboard-ingress-template.yaml --ignore-not-found
kubectl delete -f "https://github.com/tektoncd/dashboard/releases/download/v0.72.0/release.yaml" --ignore-not-found
kubectl delete -f "https://github.com/tektoncd/pipeline/releases/download/v1.15.3/release.yaml" --ignore-not-found
```
