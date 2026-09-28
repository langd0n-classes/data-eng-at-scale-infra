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

## What's still to come in `pipeline/portable/`

- `manifests/registry.yaml` + `tasks/build-push-image-task.yaml` — the
  in-cluster image-build/registry path for team `PipelineRun`s (item 7).
- `tasks/deploy-kafka-task.yaml`, `tasks/deploy-event-generator-task.yaml`,
  `pipelines/deploy-all-teams-pipeline.yaml` — the per-team deploy pipeline
  itself, chaining build → deploy Kafka → deploy event generator with no
  NiFi task anywhere in the chain (item 10).
- `scripts/{deploy,status,teardown}-platform.sh` — the orchestrator tying
  every prerequisite and per-team step together as one repeatable command
  (item 11).

## Cleanup (prerequisites so far)

```bash
kubectl delete -f pipeline/portable/manifests/tekton-dashboard-ingress-template.yaml --ignore-not-found
kubectl delete -f "https://github.com/tektoncd/dashboard/releases/download/v0.72.0/release.yaml" --ignore-not-found
kubectl delete -f "https://github.com/tektoncd/pipeline/releases/download/v1.15.3/release.yaml" --ignore-not-found
```
