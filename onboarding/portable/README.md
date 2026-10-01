# Portable Onboarding (Kind / k3s)

A `kubectl`-only variant of cluster onboarding, for creating team namespaces,
quotas, RBAC, and network isolation on a no-cost local Kubernetes cluster —
no OpenShift, no cloud account, no paid service.

This does **not** replace the OpenShift path. `onboarding/apply-onboarding.sh`
and its manifests (`onboarding/0*-*.yaml`) are unchanged and remain the
primary onboarding method for the classroom.

**Out of scope here:** Kafka Console, Tekton, ChatOps, storage (MinIO/
Postgres), NiFi, and the event generator's own deployment — none of it is
needed to onboard namespaces on their own, and none of it is touched by
this package. See `kafka/portable/`, `pipeline/portable/`,
`event-generator/portable/`, `ingress/portable/`, and `spark-queue/portable/`
for those pieces.

**Note on config.env:** this package uses `onboarding/cluster.env` only
(see below) — not the repo-root `config.env` that every other `portable/`
component reads from. The two are deliberately kept separate (same split
the OpenShift path already uses): `cluster.env` holds one-time cluster
setup values, `config.env` holds everything the Tekton pipeline needs at
runtime.

## Compatibility

Everything here is plain `kubectl` — no OpenShift-only resources (Routes,
Templates, SCCs, `oc adm groups`). It runs on **any** conformant Kubernetes
cluster: Kind, k3s, EKS, GKE, AKS, a bare-metal cluster, etc.

**Requires a CNI that enforces `NetworkPolicy`.** Kind's default CNI
(kindnet) does **not** enforce NetworkPolicy at all — the policy in
`manifests/10-team-networkpolicy.yaml` would apply successfully but silently
do nothing. This was caught during validation (see below) and is why the
validation walkthrough installs Calico first. Most managed clusters (EKS,
GKE, AKS) already ship a NetworkPolicy-enforcing CNI; only a from-scratch
Kind/k3s cluster needs this extra step.

## Validation walkthrough

Validated on: Kubernetes (Kind) v1.34.11, Calico v3.32.2 (for NetworkPolicy
enforcement — see Compatibility above).

**1. Create a cluster with a NetworkPolicy-enforcing CNI**

Kind's default CNI must be disabled and replaced, since kindnet doesn't
enforce NetworkPolicy. Calico's default IP pool (`192.168.0.0/16`) must
match the cluster's pod subnet, so it's set explicitly:

```bash
kind create cluster --name portable-fall-platform --image kindest/node:v1.34.11 --config - <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
networking:
  disableDefaultCNI: true
  podSubnet: 192.168.0.0/16
nodes:
  - role: control-plane
EOF

kubectl create -f https://raw.githubusercontent.com/projectcalico/calico/v3.32.2/manifests/tigera-operator.yaml
kubectl wait --for=condition=Established crd/installations.operator.tigera.io --timeout=60s
curl -sL https://raw.githubusercontent.com/projectcalico/calico/v3.32.2/manifests/custom-resources.yaml | kubectl create -f -
kubectl wait --for=condition=Ready node --all --timeout=180s
kubectl wait --for=condition=Ready pods --all -n calico-system --timeout=180s
```
```
NAME        AVAILABLE   PROGRESSING   DEGRADED
apiserver   True        False         False
calico      True        False         False
goldmane    True        False         False
ippools     True        False         False
tiers       True        False         False
whisker     True        False         False
```

**2. Onboard two team namespaces + infra**

```bash
cp onboarding/cluster.env.example onboarding/cluster.env
# edit onboarding/cluster.env: STORAGE_CLASS=standard, NUM_TEAMS=2
bash onboarding/portable/apply-onboarding.sh --dry-run   # verify first
bash onboarding/portable/apply-onboarding.sh
```
```
Namespaces created:
   infra
   team-01
   team-02
```

Verify:

```bash
kubectl get resourcequota,limitrange,serviceaccount,rolebinding,networkpolicy -n team-01
```
```
resourcequota/team-quota      requests.cpu: 0/6, requests.memory: 0/8Gi, requests.storage: 0/30Gi
limitrange/team-limits
serviceaccount/team-workload
rolebinding.rbac.authorization.k8s.io/infra-admins-edit    ClusterRole/edit
rolebinding.rbac.authorization.k8s.io/team-devs-edit       ClusterRole/edit
rolebinding.rbac.authorization.k8s.io/team-workload-edit   ClusterRole/edit
networkpolicy.networking.k8s.io/team-isolation
```

**3. Test cross-team isolation, for real**

```bash
kubectl run web-01 --image=nginx:alpine -n team-01 --labels=app=web --port=80 --restart=Never
kubectl expose pod web-01 -n team-01 --port=80 --name=web-01
kubectl run web-02 --image=nginx:alpine -n team-02 --labels=app=web --port=80 --restart=Never
kubectl expose pod web-02 -n team-02 --port=80 --name=web-02
kubectl run test-01 --image=busybox:stable -n team-01 --restart=Never --command -- sleep 3600
kubectl run test-02 --image=busybox:stable -n team-02 --restart=Never --command -- sleep 3600
kubectl run test-infra --image=busybox:stable -n infra --restart=Never --command -- sleep 3600

# same namespace — expect success
kubectl exec test-01 -n team-01 -- wget -qO- --timeout=4 http://web-01.team-01.svc.cluster.local

# cross-team, both directions — expect blocked
kubectl exec test-01 -n team-01 -- wget -qO- --timeout=4 http://web-02.team-02.svc.cluster.local
kubectl exec test-02 -n team-02 -- wget -qO- --timeout=4 http://web-01.team-01.svc.cluster.local

# infra can reach both teams — expect success
kubectl exec test-infra -n infra -- wget -qO- --timeout=4 http://web-01.team-01.svc.cluster.local
kubectl exec test-infra -n infra -- wget -qO- --timeout=4 http://web-02.team-02.svc.cluster.local
```
```
test-01 -> web-01 (own namespace):   <!DOCTYPE html> ... Welcome to nginx!   [REACHED]
test-01 -> web-02 (team-02):         wget: download timed out                [BLOCKED]
test-02 -> web-01 (team-01):         wget: download timed out                [BLOCKED]
test-infra -> web-01 (team-01):      <!DOCTYPE html> ... Welcome to nginx!   [REACHED]
test-infra -> web-02 (team-02):      <!DOCTYPE html> ... Welcome to nginx!   [REACHED]
```

All five results matched expectations exactly: same-namespace traffic and
`infra`-sourced traffic are allowed, cross-team traffic is blocked in both
directions.

```bash
# cleanup test pods (keep the onboarded namespaces)
kubectl delete pod web-01 test-01 -n team-01 --ignore-not-found
kubectl delete svc web-01 -n team-01 --ignore-not-found
kubectl delete pod web-02 test-02 -n team-02 --ignore-not-found
kubectl delete svc web-02 -n team-02 --ignore-not-found
kubectl delete pod test-infra -n infra --ignore-not-found
```

## Variables

Same `onboarding/cluster.env` as the OpenShift path (see
`onboarding/cluster.env.example`) — no separate portable env file. Only two
things typically change for local Kind validation:

| Variable | OpenShift (CRC/NERC) | Kind |
|---|---|---|
| `STORAGE_CLASS` | `crc-csi-hostpath-provisioner` or `standard` (NERC) | `standard` (Kind's default) |
| `NUM_TEAMS` | however many teams the class needs | as few as needed to validate (2 is enough to test cross-team isolation) |

## Group membership without `oc adm groups`

The RBAC manifests here are unchanged from the OpenShift path — `kind: Group`
is a standard Kubernetes RBAC subject type, not an OpenShift-only concept.
What's actually OpenShift-only is `oc adm groups new`/`add-users`, the tool
used to manage *which usernames belong to which group*. On a vanilla
cluster, group membership instead comes from whatever the cluster's
authentication mechanism asserts:

- **X.509 client certificates** — Kubernetes natively maps a cert's
  `Organization` (`O=`) field to RBAC group membership. Example (Kind ships
  a cluster CA you can sign against):
  ```bash
  openssl req -new -newkey rsa:2048 -nodes \
    -keyout student.key -out student.csr \
    -subj "/CN=student1/O=team-01-devs"
  # then a CertificateSigningRequest approved by a cluster-admin, or a
  # directly-signed cert if you hold the cluster CA — see the Kubernetes
  # docs on "Certificate Signing Requests" for the full flow.
  ```
- **An OIDC provider's `groups` claim** — the standard approach for a real
  multi-user cluster (Keycloak, Dex, a cloud IdP, etc.).

Neither was exercised end-to-end in this local validation (Kind has no
identity provider wired up by default, and every command above ran as the
cluster-admin kubeconfig user) — this is a known limitation of the local
proof, not a gap in the RBAC manifests themselves, which are identical in
shape to the already-working OpenShift ones.

## Cleanup

```bash
kind delete cluster --name portable-fall-platform
```
