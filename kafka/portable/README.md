# Portable Kafka Package (Kind / k3s)

A `kubectl`-only variant of the per-team Kafka deployment, for running the
Strimzi setup on a no-cost local Kubernetes cluster — no OpenShift, no cloud
account, no paid service, no public ingress.

This does **not** replace the OpenShift path. `kafka/operator/` and
`kafka/per-team/` are unchanged and remain the primary deployment method for
the classroom.

**Out of scope here:** Kafka Console, Topic/User Operator (off by default),
Tekton, ChatOps, onboarding namespaces/RBAC/quotas, NiFi, the event
generator, and the team registry — none of it is needed to run or test Kafka
on its own, and none of it is touched by this package.

## Compatibility

Everything here is plain `kubectl` — no OpenShift-only resources (Routes,
BuildConfigs, SCCs, Templates). It runs on **any** conformant Kubernetes
cluster with a dynamic StorageClass: Kind, k3s, EKS, GKE, AKS, a bare-metal
cluster, etc.

Requirements: Kubernetes 1.30+ (Strimzi 0.51.0's minimum), `kubectl`,
`envsubst`, and a dynamic StorageClass.

## Validation walkthrough

The steps below are exactly how this package was validated — on a local
Kind cluster, since that's the fastest no-cost way to test it. Only step 1
(creating the cluster) is Kind-specific; everything from step 2 onward is
identical on k3s, EKS, or any other cluster — just point `kubectl` at it.

Validated on: Kubernetes (Kind) 1.34.11, Strimzi 0.51.0, Kafka 4.2.0 / metadata 4.2-IV0.

All commands below are run from this directory (`kafka/portable/`):

```bash
cd kafka/portable
```

**1. Create a cluster** *(Kind shown; substitute your own cluster/context for k3s, EKS, etc. — requires the `kind` CLI and Docker or Podman installed)*

```bash
kind create cluster --name strimzi-portable --image kindest/node:v1.34.11
kubectl get storageclass
```
```
standard (default)   rancher.io/local-path
```
Confirms a dynamic StorageClass exists — Kind ships one by default; on a
managed cluster (EKS/GKE/AKS) one is already there too.

**2. Install the Strimzi operator** *(cluster-admin, one-time — see [Variables](#variables) for why)*

```bash
bash prerequisites/install-strimzi.sh
```
```
strimzi-cluster-operator   1/1   1   1   Available
```

**3. Create a team namespace and deploy Kafka**

```bash
kubectl create namespace team-portable-test
bash scripts/deploy.sh team01 team-portable-test
```
```
kafka-team01   Ready=True   Kafka 4.2.0   Metadata 4.2-IV0
kafka-team01-dual-role-0   1/1   Running
data-kafka-team01-dual-role-0   Bound   standard
```
Re-running the same command is a no-op — `kubectl apply` reports
`unchanged`, and nothing about the running broker changes.

**4. Test produce/consume, in-cluster (no external exposure)**

```bash
kubectl run kafka-test -n team-portable-test \
  --image=confluentinc/cp-kafka:7.5.0 --restart=Never --command -- sleep 3600
kubectl wait pod kafka-test -n team-portable-test --for=condition=Ready --timeout=120s

BOOTSTRAP="kafka-team01-kafka-bootstrap.team-portable-test.svc.cluster.local:9092"

echo "test-message-1" | kubectl exec -i kafka-test -n team-portable-test -- \
  kafka-console-producer --bootstrap-server "$BOOTSTRAP" --topic portable-test-topic

kubectl exec kafka-test -n team-portable-test -- \
  kafka-console-consumer --bootstrap-server "$BOOTSTRAP" --topic portable-test-topic \
  --from-beginning --max-messages 1 --timeout-ms 15000
```
```
test-message-1
```

**5. Restart the broker — confirms data survives**

```bash
bash scripts/restart.sh team01 team-portable-test
```
```
Recovery time: 32 seconds
```
Re-consuming from `portable-test-topic` afterward still returns `test-message-1`.

**6. Reset persistent data — confirms fresh storage**

```bash
bash scripts/reset-data.sh team01 team-portable-test
```
```
Kafka data wiped and broker Ready for team01 in team-portable-test.
```
The PVC gets a new UID and `portable-test-topic` no longer exists afterward.

**7. Remove everything**

```bash
bash scripts/remove.sh team01 team-portable-test
kubectl delete namespace team-portable-test
kubectl delete pod kafka-test -n team-portable-test --ignore-not-found
```
```
kubectl get kafka,kafkanodepool,pdb,pvc -n team-portable-test
# (empty)
```

All four `scripts/` commands are safe to re-run — `deploy.sh` is idempotent
by construction (`kubectl apply`), the others use `--ignore-not-found` /
existence checks throughout.

## Variables

| Variable | Default | Description |
|---|---|---|
| `TEAM_NAME` | *(required)* | Team identifier, e.g. `team01` |
| `TEAM_NAMESPACE` | *(required)* | Kubernetes namespace, e.g. `team-portable-test` |
| `STORAGE_CLASS` | `standard` | Falls back to `config.env`'s value if set, then this default |
| `VOLUME_SIZE` | `2Gi` | Falls back to `config.env`'s value if set, then this default |

`scripts/deploy.sh` and `scripts/reset-data.sh` source the repo-root
`config.env` for these two if it exists (same convention as
`kafka/per-team/deploy-team.sh`), but `config.env` is entirely optional here
— pass `storage_class`/`volume_size` as trailing CLI args to override it at
runtime instead, e.g. `bash scripts/deploy.sh team01 team-portable-test standard 2Gi`.

`prerequisites/install-strimzi.sh` requires cluster-admin (or an
explicitly-granted equivalent): it creates CustomResourceDefinitions and
ClusterRole/ClusterRoleBinding objects, which are cluster-scoped and not
covered by a namespace-scoped `edit` role.

## Cleanup: removing the Strimzi operator itself

Rarely needed — it's a one-time, cluster-wide install, unrelated to any one
team's lifecycle.

```bash
kubectl delete namespace strimzi-system
kubectl delete crd -l app=strimzi
kubectl delete clusterrole,clusterrolebinding -l app=strimzi
```
