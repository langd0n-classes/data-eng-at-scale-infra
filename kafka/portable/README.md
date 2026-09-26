# Portable Kafka Package (Kind / k3s)

A `kubectl`-only variant of the per-team Kafka deployment, for validating the
Strimzi setup on a no-cost local Kubernetes cluster — no OpenShift, no cloud
account, no paid service, no public ingress.

This does **not** replace the OpenShift path. `kafka/operator/` and
`kafka/per-team/` are unchanged and remain the primary deployment method for
the classroom. This package exists to prove the same Kafka/Strimzi setup is
portable to a plain Kubernetes cluster.

## What's excluded, and why

Only the Kafka piece is covered here. Everything else in the classroom stack
is out of scope for this package:

- **Kafka Console** — not included; excluded by design (instructor-only
  tooling, not part of the portability proof).
- **Topic Operator / User Operator** — off by default (no `entityOperator`
  block in `manifests/kafka-template.yaml`), unlike the OpenShift template.
- **Tekton, ChatOps, onboarding namespaces/RBAC/quotas** — none of this is
  needed for a single-team validation and none of it is touched here.
- **NiFi, event generator, team registry** — untouched, unrelated to this
  package.

## 1. Local cluster creation

Kind is the preferred no-cost test environment (an existing local k3s
installation is also acceptable — the scripts below are plain `kubectl` and
work the same way against either).

```bash
kind create cluster --name strimzi-portable --image kindest/node:v1.34.11
kubectl get storageclass
```

Kind ships a default dynamic StorageClass named `standard`
(`rancher.io/local-path`) — no extra provisioning needed. The exact node image
above matches this project's production Kubernetes version (`1.34.x`); any
recent Kind-published `v1.34.x` image works equally well.

## 2. Pre-Requisite: Strimzi Operator

Cluster-admin, one-time action — done before any team's Kafka is deployed.
Installs the Strimzi 0.51.0 Cluster Operator cluster-wide (watches all
namespaces), the same way the OpenShift path installs it via OperatorHub
"All namespaces" mode.

**Requires cluster-admin** (or an explicitly-granted equivalent): this
creates CustomResourceDefinitions and ClusterRole/ClusterRoleBinding objects,
which are cluster-scoped and are not covered by a namespace-scoped `edit`
role.

```bash
bash prerequisites/install-strimzi.sh
```

Verify:

```bash
kubectl get deployment strimzi-cluster-operator -n strimzi-system
kubectl get crd | grep strimzi.io
```

## Variables

| Variable | Default | Description |
|---|---|---|
| `TEAM_NAME` | *(required)* | Team identifier, e.g. `team01` |
| `TEAM_NAMESPACE` | *(required)* | Kubernetes namespace, e.g. `team-portable-test` |
| `STORAGE_CLASS` | `standard` | Falls back to `config.env`'s value if set, then this default |
| `VOLUME_SIZE` | `2Gi` | Falls back to `config.env`'s value if set, then this default |

`scripts/deploy.sh` and `scripts/reset-data.sh` source the repo-root
`config.env` for `STORAGE_CLASS`/`VOLUME_SIZE` defaults if it exists (same
convention as `kafka/per-team/deploy-team.sh`), but `config.env` is entirely
optional here — pass `storage_class`/`volume_size` as trailing CLI args to
override it at runtime, e.g. for a one-off Kind run without editing
`config.env`.

## Usage

```bash
kubectl create namespace team-portable-test

# Deploy — applies KafkaNodePool + Kafka CR + PodDisruptionBudget, waits for
# Ready, prints the bootstrap endpoint
bash scripts/deploy.sh team01 team-portable-test
bash scripts/deploy.sh team01 team-portable-test standard 2Gi   # explicit override

# Restart the broker pod (Strimzi recreates it)
bash scripts/restart.sh team01 team-portable-test

# Wipe persistent data — deletes the KafkaNodePool + PVCs, recreates with
# fresh storage, waits for Ready again
bash scripts/reset-data.sh team01 team-portable-test

# Remove everything (Kafka CR, KafkaNodePool, PodDisruptionBudget, PVCs)
bash scripts/remove.sh team01 team-portable-test
```

All four scripts are safe to re-run (`deploy.sh` is a no-op on unchanged
input; `restart.sh`/`reset-data.sh`/`remove.sh` use `--ignore-not-found` /
existence checks throughout).

## Verify a deployment

```bash
kubectl get kafka,pod,pvc,pdb -n team-portable-test
```

## Test produce/consume (in-cluster client pod + ClusterIP bootstrap)

No external exposure — everything happens inside the cluster, against the
Kafka bootstrap `Service`'s ClusterIP.

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

kubectl delete pod kafka-test -n team-portable-test
```

## Cleanup

```bash
bash scripts/remove.sh team01 team-portable-test
kubectl delete namespace team-portable-test
```

To remove the Strimzi operator itself (rarely needed — it's a one-time,
cluster-wide install):

```bash
kubectl delete namespace strimzi-system
kubectl delete crd -l app=strimzi
kubectl delete clusterrole,clusterrolebinding -l app=strimzi
```
