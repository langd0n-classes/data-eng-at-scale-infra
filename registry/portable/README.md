# Portable Image Registry (Kind / k3s)

A `kubectl`-only in-cluster image registry for the portable Fall 2026
platform, so team `PipelineRun`s can build and push images with no external
registry, no cloud account, and no credentials of any kind.

This is a brand-new top-level component — no existing directory in this
repo owned an image registry before (the OpenShift path builds images via
`BuildConfig`/`ImageStream`, which push into OpenShift's own internal
registry automatically; there's nothing to extract or mirror here, this is
built from scratch for the portable path).

## Compatibility

Plain `kubectl`, the standard `registry:2` (CNCF Distribution) image — the
same open-source project Docker Hub, Quay, and GHCR are all built on top
of. Runs on any conformant Kubernetes cluster: Kind, k3s, EKS, GKE, AKS, a
bare-metal cluster, etc.

## A non-obvious fact this design depends on

Image **pushes** happen from inside a pod (a Tekton Task's build step),
which resolves the registry fine via its normal in-cluster Service DNS
name. Image **pulls**, however, are done by `kubelet`/`containerd` running
in the *node's* network namespace — which cannot resolve a
`*.svc.cluster.local` name at all (confirmed directly: pulling
`registry.infra.svc.cluster.local:5000/...` from a Deployment fails with
`dial tcp: lookup registry.infra.svc.cluster.local ...: server misbehaving`).
This is true on every Kubernetes cluster, not a Kind-specific quirk.

The fix: expose the registry as a `NodePort`, not a plain `ClusterIP`. A
NodePort is reachable via `localhost:<nodePort>` from *any* node in the
cluster regardless of which node the registry pod actually landed on
(standard Kubernetes NodePort routing). Every image reference in this
platform — the build Task's `--destination` and every Deployment's `image:`
field — uses the exact same `localhost:${REGISTRY_NODE_PORT}/<repo>:<tag>`
string. The build Task's own pod needs `hostNetwork: true` (set on its
PipelineRun, see `pipeline/portable/`) so that *it* can also reach
`localhost:<nodePort>` for the push, the same way kubelet does for the
pull — pods don't share the node's loopback by default.

## Before you start: update config.env

```bash
cp config.env.example config.env
```
Then edit `config.env` and set: `INFRA_NAMESPACE`, `STORAGE_CLASS` (`standard`
on Kind), `REGISTRY_VOLUME_SIZE` (default `5Gi`), `REGISTRY_NODE_PORT`
(default `30500` — this exact value ends up in every image reference
everywhere else in the platform, see the section above for why).

## Validation walkthrough

Validated on: Kubernetes (Kind) v1.34.11, `registry:2` (Docker
Distribution).

**1. Deploy the registry**

```bash
source config.env
envsubst '${INFRA_NAMESPACE} ${STORAGE_CLASS} ${REGISTRY_VOLUME_SIZE} ${REGISTRY_NODE_PORT}' \
  < registry/portable/manifests/registry.yaml | kubectl apply -f -
kubectl rollout status deployment/registry -n infra --timeout=120s
```
```
deployment "registry" successfully rolled out
NAME       TYPE       CLUSTER-IP      EXTERNAL-IP   PORT(S)          AGE
registry   NodePort   10.96.218.107   <none>        5000:30500/TCP   29m
```

**2. A real `PipelineRun` builds, pushes, and starts a pod from the image**

(Full pipeline definition lives in `pipeline/portable/`; this is the
underlying registry-specific behavior it depends on.) A build pushing
`localhost:30500/event-generator:test2` via kaniko, immediately followed by
starting a real pod from that same reference:

```
INFO Pushing image to localhost:30500/event-generator:test2
INFO Pushed localhost:30500/event-generator@sha256:755e0c484f3664e0d115ee0d649be607ecfbc276b46a8df02bb478a3b955423c
```
```bash
kubectl run event-gen-from-registry -n infra --image=localhost:30500/event-generator:test2 --restart=Never --command -- sleep 60
kubectl get pod event-gen-from-registry -n infra
kubectl get pod event-gen-from-registry -n infra -o jsonpath='{.status.containerStatuses[0].imageID}'
```
```
NAME                      READY   STATUS    RESTARTS   AGE
event-gen-from-registry   1/1     Running   0          2s
localhost:30500/event-generator@sha256:755e0c484f3664e0d115ee0d649be607ecfbc276b46a8df02bb478a3b955423c
```

The running pod's `imageID` digest matches exactly what was pushed —
confirms the whole build→push→pull path works, digest included.

## Variables

| Variable | Default | Description |
|---|---|---|
| `INFRA_NAMESPACE` | *(required)* | Shared namespace the registry lives in |
| `STORAGE_CLASS` | `standard` | Falls back to `config.env` |
| `REGISTRY_VOLUME_SIZE` | `5Gi` | Falls back to `config.env` |
| `REGISTRY_NODE_PORT` | `30500` | Falls back to `config.env` — used in every image reference |

## Cleanup

```bash
kubectl delete -f registry/portable/manifests/registry.yaml -n infra --ignore-not-found
```
