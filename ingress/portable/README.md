# Portable Ingress (Kind / k3s)

A `kubectl`-only ingress controller setup for the portable Fall 2026
platform, for exposing HTTP services (currently: the read-only Tekton
Dashboard, see `pipeline/portable/`) on a no-cost local Kubernetes cluster —
no OpenShift Routes, no cloud account, no paid service.

This is a brand-new top-level component — no existing directory in this
repo owned ingress before. It has no OpenShift equivalent to stay separate
from (OpenShift Routes, replaced here, live in `nifi/team-route-template.yaml`,
`chatops/k8s/04-route.yaml`, and `storage/minio.yaml`, none of which are in
this platform's scope).

**Out of scope here:** any component not part of this platform's required
work (NiFi, ChatOps, storage, Kafka Console all keep their existing Routes
on the OpenShift path, untouched).

## Compatibility

Plain `kubectl`, ingress-nginx's own upstream manifests, no OpenShift-only
resources. Runs on any conformant Kubernetes cluster with an ingress
controller install path — Kind, k3s, EKS, GKE, AKS, a bare-metal cluster,
etc.

**On Kind specifically**, a `type: LoadBalancer` Service never gets an
external IP on its own — Kind has no cloud provider. This package installs
`cloud-provider-kind` to simulate one locally. That piece is Kind-only /
local-only and is **not** part of the eventual OVH-cloud path — a real cloud
already provides its own LoadBalancer implementation, so
`install-cloud-provider-kind.sh` would simply not be run there.

## Validation walkthrough

Validated on: Kubernetes (Kind) v1.34.11, ingress-nginx `controller-v1.15.1`,
cloud-provider-kind `v0.11.1`.

**1. Install the ingress-nginx controller**

Uses the generic "cloud" provider manifest (not Kind's NodePort-specific
one), so the controller's Service is a real `type: LoadBalancer` — matching
item 9's requirement and how this will actually behave on a real cloud
later.

```bash
bash ingress/portable/prerequisites/install-ingress-nginx.sh
```
```
deployment.apps/ingress-nginx-controller condition met

NAME                       READY   UP-TO-DATE   AVAILABLE   AGE
ingress-nginx-controller   0/1     1            0           0s
NAME                       TYPE           CLUSTER-IP    EXTERNAL-IP   PORT(S)
ingress-nginx-controller   LoadBalancer   10.96.72.33   <pending>     80:31177/TCP,443:32471/TCP

EXTERNAL-IP will stay <pending> on Kind until install-cloud-provider-kind.sh
is installed AND running.
```

**2. Install and start cloud-provider-kind (Kind-only, requires root)**

cloud-provider-kind manipulates host networking (iptables / network
namespaces) to route traffic to its LoadBalancer proxy containers, so it
must run with `sudo`, and it must keep running in the background for
LoadBalancer Services to keep working:

```bash
sudo bash ingress/portable/prerequisites/install-cloud-provider-kind.sh
```
```
==========================================
cloud-provider-kind running (pid 52533)
==========================================
Stop it with: kill $(cat ingress/portable/prerequisites/.bin/cloud-provider-kind.pid)
```

**3. Confirm the Service gets a real external IP**

```bash
kubectl get service ingress-nginx-controller -n ingress-nginx
```
```
NAME                       TYPE           CLUSTER-IP    EXTERNAL-IP   PORT(S)
ingress-nginx-controller   LoadBalancer   10.96.72.33   172.18.0.3    80:31177/TCP,443:32471/TCP
```

**4. Confirm it's actually reachable end to end**

```bash
curl -sv --max-time 5 http://172.18.0.3/
```
```
< HTTP/1.1 404 Not Found
< Content-Type: text/html
<html>
<head><title>404 Not Found</title></head>
<body><center><h1>404 Not Found</h1></center><hr><center>nginx</center></body>
</html>
```
The `404` here is expected and correct — it's ingress-nginx's own default
backend response, confirming traffic reached the controller through the
real LoadBalancer IP. It will route to a real service once an `Ingress`
resource exists (see `pipeline/portable/manifests/tekton-dashboard-ingress-template.yaml`).

## Cleanup

```bash
# Stop cloud-provider-kind (Kind-only)
sudo kill $(cat ingress/portable/prerequisites/.bin/cloud-provider-kind.pid)

# Remove ingress-nginx
kubectl delete -f "https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.15.1/deploy/static/provider/cloud/deploy.yaml"
```
