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

**Note on config.env:** this package uses `onboarding/cluster.env` only —
not the repo-root `config.env` that every other `portable/` component reads
from. The two are deliberately kept separate (same split the OpenShift path
already uses): `cluster.env` holds one-time cluster setup values, `config.env`
holds everything the Tekton pipeline needs at runtime.

```bash
# one-time: create your own cluster.env from the template
cp onboarding/cluster.env.example onboarding/cluster.env
```
Only two values in `onboarding/cluster.env` typically need changing for
local Kind validation — everything else already has a working default:

| Variable | OpenShift (CRC/NERC) | Kind |
|---|---|---|
| `STORAGE_CLASS` | `crc-csi-hostpath-provisioner` or `standard` (NERC) | `standard` (Kind's default) |
| `NUM_TEAMS` | however many teams the class needs | as few as needed to validate (2 is enough to test cross-team isolation) |

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

`apply-onboarding.sh` proves this live before onboarding any namespace
(`providers/lib/check-networkpolicy.sh` — a real deny-all-ingress test),
and aborts if it fails. See `portable/README.md`'s "Then, pick your
provider" for `PROVIDER`.

## Validation walkthrough

Validated on: Kubernetes (Kind) v1.34.11, Calico v3.32.2 (for NetworkPolicy
enforcement — see Compatibility above).

Only step 1 (creating the cluster) is Kind-specific — if you already
have a real cluster meeting the two requirements above (Kubernetes
1.30+, NetworkPolicy enforcement), skip straight to step 2.

**1. Create a cluster with a NetworkPolicy-enforcing CNI**

Kind's default CNI must be disabled and replaced, since kindnet doesn't
enforce NetworkPolicy. Calico's default IP pool (`192.168.0.0/16`) must
match the cluster's pod subnet, so it's set explicitly:

```bash
# version pins (KIND_NODE_IMAGE, CALICO_VERSION) come from portable/versions.env
source portable/versions.env

# create the Kind cluster with its default CNI disabled
kind create cluster --name portable-fall-platform --image "${KIND_NODE_IMAGE}" --config - <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
networking:
  disableDefaultCNI: true
  podSubnet: 192.168.0.0/16
nodes:
  - role: control-plane
EOF

# install the Calico operator
kubectl create -f "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/tigera-operator.yaml"
# wait for its CRDs to exist before using them
kubectl wait --for=condition=Established crd/installations.operator.tigera.io --timeout=60s
# apply Calico's actual configuration (IP pool, etc.)
curl -sL "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/custom-resources.yaml" | kubectl create -f -
# wait for the node and all Calico pods to come up
kubectl wait --for=condition=Ready node --all --timeout=180s
kubectl wait --for=condition=Ready pods --all -n calico-system --timeout=180s
# confirm every Calico component is actually healthy, not just "a pod exists"
kubectl get tigerastatus
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

**2. Onboard your team namespaces + infra**

(Assumes `onboarding/cluster.env` is already set up — see "Note on
config.env" near the top of this file. Team namespaces run from
`FIRST_TEAM` to `NUM_TEAMS`, inclusive. `FIRST_TEAM` defaults to `0`:
`team-00` is the staff reference team, where staff test the student
experience and keep the reference solution. The example below uses
`NUM_TEAMS=2`, which creates `team-00`, `team-01`, and `team-02`.)

```bash
bash onboarding/portable/apply-onboarding.sh --dry-run   # preview commands, no changes
bash onboarding/portable/apply-onboarding.sh             # actually create everything
```
```
Namespaces created:
   infra
   team-00
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

Team namespaces also enforce Pod Security Admission at `baseline`
(blocks privileged/hostPath/hostNetwork pods — `edit` RBAC alone doesn't)
with `restricted` set as `warn`/`audit` only, since `restricted` rejects
Kafka's own broker pod:

```bash
kubectl get namespace team-01 -o jsonpath='{.metadata.labels}'
```
```
{"pod-security.kubernetes.io/audit":"restricted", ...
 "pod-security.kubernetes.io/enforce":"baseline", ...
 "pod-security.kubernetes.io/warn":"restricted", ...}
```

```bash
kubectl run test --image=nginx:alpine -n team-01 --overrides='{"spec":{"hostNetwork":true}}'
```
```
Error from server (Forbidden): pods "test" is forbidden: violates PodSecurity "baseline:v1.34": host namespaces (hostNetwork=true)
```

**3. Test cross-team isolation, for real**

Team namespaces deny any ownerless Pod (`spark-bare-pod-policy`, closes a
Spark-queue bypass — see `spark-queue/portable/`), so the probe pods here
need an owner: a Deployment for the web targets, a Job for the clients.

```bash
# a small web service in each team namespace, to test reachability against
kubectl create deployment web-01 --image=nginx:alpine -n team-01
kubectl expose deployment web-01 -n team-01 --port=80
kubectl create deployment web-02 --image=nginx:alpine -n team-02
kubectl expose deployment web-02 -n team-02 --port=80

# one client pod per namespace, to run the actual reachability tests from
kubectl create job test-01 --image=busybox:stable -n team-01 -- sleep 3600
kubectl create job test-02 --image=busybox:stable -n team-02 -- sleep 3600
kubectl create job test-infra --image=busybox:stable -n infra -- sleep 3600

# resolve each Job's actual pod name once, reused through steps 3-3c
TEST01=$(kubectl get pod -n team-01 -l job-name=test-01 -o jsonpath='{.items[0].metadata.name}')
TEST02=$(kubectl get pod -n team-02 -l job-name=test-02 -o jsonpath='{.items[0].metadata.name}')
TESTINFRA=$(kubectl get pod -n infra -l job-name=test-infra -o jsonpath='{.items[0].metadata.name}')
kubectl wait pod "$TEST01" -n team-01 --for=condition=Ready --timeout=60s
kubectl wait pod "$TEST02" -n team-02 --for=condition=Ready --timeout=60s
kubectl wait pod "$TESTINFRA" -n infra --for=condition=Ready --timeout=60s

# same namespace — expect success
kubectl exec "$TEST01" -n team-01 -- wget -qO- --timeout=4 http://web-01.team-01.svc.cluster.local

# cross-team, both directions — expect blocked
kubectl exec "$TEST01" -n team-01 -- wget -qO- --timeout=4 http://web-02.team-02.svc.cluster.local
kubectl exec "$TEST02" -n team-02 -- wget -qO- --timeout=4 http://web-01.team-01.svc.cluster.local

# infra can reach both teams — expect success
kubectl exec "$TESTINFRA" -n infra -- wget -qO- --timeout=4 http://web-01.team-01.svc.cluster.local
kubectl exec "$TESTINFRA" -n infra -- wget -qO- --timeout=4 http://web-02.team-02.svc.cluster.local
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

**3b. Same test, against Kafka itself**

nginx proves `team-isolation` works for a plain Service — it can't catch
a Strimzi listener-specific leak (fixed via `networkPolicyPeers` in
`kafka/portable/manifests/kafka-template.yaml`). This re-runs the same
probe (same `$TEST01`/`$TEST02`/`$TESTINFRA` pods from step 3) against
the real thing.

```bash
bash kafka/portable/prerequisites/install-strimzi.sh   # one-time, skip if already installed
INFRA_NAMESPACE=infra bash kafka/portable/scripts/deploy.sh team01 team-01
kubectl wait kafka kafka-team01 -n team-01 --for=condition=Ready --timeout=180s

BOOTSTRAP=kafka-team01-kafka-bootstrap.team-01.svc.cluster.local
kubectl exec "$TEST01" -n team-01 -- nc -zvw4 "$BOOTSTRAP" 9092      # same ns — expect open
kubectl exec "$TEST02" -n team-02 -- nc -zvw4 "$BOOTSTRAP" 9092      # cross-team — expect blocked
kubectl exec "$TESTINFRA" -n infra -- nc -zvw4 "$BOOTSTRAP" 9092     # infra — expect open
```
```
test-01 -> kafka-team01:    open                    [REACHED]
test-02 -> kafka-team01:    Connection timed out     [BLOCKED]
test-infra -> kafka-team01: open                     [REACHED]
```

**3c. Same test, against the Tekton Dashboard**

The dashboard's Service had no NetworkPolicy of its own — reachable
cluster-wide regardless of the Ingress's basic auth/TLS. Fixed by
`pipeline/portable/manifests/tekton-dashboard-networkpolicy.yaml`,
admitting only `ingress-nginx`. Same `$TEST01`/`$TEST02` pods:

```bash
kubectl exec "$TEST01" -n team-01 -- wget -qO- --timeout=4 http://tekton-dashboard.tekton-pipelines:9097/
kubectl exec "$TEST02" -n team-02 -- wget -qO- --timeout=4 http://tekton-dashboard.tekton-pipelines:9097/
```
```
test-01 -> tekton-dashboard: wget: download timed out   [BLOCKED]
test-02 -> tekton-dashboard: wget: download timed out   [BLOCKED]
```

**OVHcloud MKS (managed cloud, 2026-10-22):** steps 3/3b/3c re-run against
real `team-01`/`team-02`/`infra` — all results identical to Kind above.
CNI differs though: Kind uses pure Calico, OVH Free-plan MKS uses **Canal**
(Flannel for the data-plane overlay, Calico only for policy enforcement —
confirmed via `canal-*` pods each running both a `calico-node` and a
`kube-flannel` container). Enforcement *behavior* verified equivalent;
the CNI *stack* is not the same.

**3d. `LoadBalancer`/`NodePort` Service quota, confirmed on OVHcloud MKS**

`06-team-resourcequota.yaml` caps both at zero per team namespace — a
team can't expose a Service directly, bypassing the shared ingress:

```bash
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Service
metadata: { name: lb-quota-test, namespace: team-01 }
spec: { type: LoadBalancer, selector: { app: web-01 }, ports: [{ port: 80 }] }
EOF
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Service
metadata: { name: nodeport-quota-test, namespace: team-01 }
spec: { type: NodePort, selector: { app: web-01 }, ports: [{ port: 80 }] }
EOF
```
```
Error from server (Forbidden): services "lb-quota-test" is forbidden: exceeded quota: team-quota, requested: services.loadbalancers=1, used: services.loadbalancers=0, limited: services.loadbalancers=0
Error from server (Forbidden): services "nodeport-quota-test" is forbidden: exceeded quota: team-quota, requested: services.nodeports=1, used: services.nodeports=0, limited: services.nodeports=0
```
Both rejected, as designed.

```bash
# cleanup (keep the onboarded namespaces)
bash kafka/portable/scripts/remove.sh --yes team01 team-01
kubectl delete deployment web-01 -n team-01 --ignore-not-found
kubectl delete svc web-01 -n team-01 --ignore-not-found
kubectl delete job test-01 -n team-01 --ignore-not-found
kubectl delete deployment web-02 -n team-02 --ignore-not-found
kubectl delete svc web-02 -n team-02 --ignore-not-found
kubectl delete job test-02 -n team-02 --ignore-not-found
kubectl delete job test-infra -n infra --ignore-not-found
```

## Variables

Same `onboarding/cluster.env` as the OpenShift path (see
`onboarding/cluster.env.example`) — no separate portable env file. See the
"Note on config.env" near the top of this file for which two values
typically change for local Kind validation.

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

## Team and Staff Kubeconfigs

On a cluster whose authentication cannot assert group membership, give each
team a kubeconfig for its namespace's `team-workload` ServiceAccount, and
give staff one kubeconfig for every team namespace:

```bash
bash onboarding/portable/make-kubeconfigs.sh OUTPUT_DIR
```

The script writes `team-NN.kubeconfig` for each team and
`staff.kubeconfig`, with mode `0600`.
It refuses an `OUTPUT_DIR` that Git would track.
Each file holds a long-lived token: share each team's file with that team
only.

- A team kubeconfig has `edit` and the `team-kafka` Role in the team's
  namespace, and nothing outside it.
- The staff kubeconfig has `edit` and the `team-kafka` Role in every team
  namespace, and `edit` in the infra namespace.
- To revoke a token, delete its Secret, for example
  `kubectl delete secret team-workload-token -n team-03`, then run the
  script again to issue a new one.

## Cleanup

```bash
kind delete cluster --name portable-fall-platform
```
