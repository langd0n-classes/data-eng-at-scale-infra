# Kafka Deployment Options

This directory contains Kubernetes manifests for deploying Apache Kafka in two modes:

1. **Per-Team Isolation** (`per-team/`) - Separate Kafka instance per namespace, managed by the Kafka operator
2. **Shared Cluster** (`shared-deployment/`) - Single Kafka cluster with topic-based multitenancy

## Pre-Requisite: Kafka Operator

Per-team Kafka is managed by the Strimzi operator. Install it once per cluster before deploying any team:

1. Go to OperatorHub in the OpenShift console
2. Search for **Strimzi**
3. Install with mode: **All namespaces**
4. OLM installs the CRDs and Cluster Operator automatically — no repo changes needed

## Per-Team Kafka Deployment

Deploy isolated Kafka instances for complete tenant separation. The operator manages the full
lifecycle — pod, services, PVC — from a single `Kafka` CR declaration.

### Features

- KRaft mode (no ZooKeeper dependency)
- Single-node broker/controller (dual-role KafkaNodePool)
- PLAINTEXT listener on port 9092 (in-cluster only)
- Auto-topic creation enabled
- Aggressive log retention (24 hours, 100MB)
- Low resource footprint (512MB RAM, 2GB storage)
- Entity Operator included (TopicOperator + UserOperator)

### Deployment

```bash
# Via script (recommended)
./per-team/deploy-team.sh team01 team-01

# Or manually with envsubst
export TEAM_NAME=team01
export TEAM_NAMESPACE=team-01
export STORAGE_CLASS=standard
envsubst < operator/kafka-cr-template.yaml | kubectl apply -f -
```

### Access

**From within the same namespace:**

```bash
kafka-${TEAM_NAME}-kafka-bootstrap:9092
```

**From other namespaces:**

```bash
kafka-${TEAM_NAME}-kafka-bootstrap.${TEAM_NAMESPACE}.svc.cluster.local:9092
```

### Verify Deployment

```bash
# Check Kafka CR status
kubectl get kafka -n ${TEAM_NAMESPACE}

# Check pod and services (operator creates these automatically)
kubectl get pods,svc -n ${TEAM_NAMESPACE} -l strimzi.io/cluster=kafka-${TEAM_NAME}

# View logs
kubectl logs -f kafka-${TEAM_NAME}-dual-role-0 -n ${TEAM_NAMESPACE}
```

### Testing

```bash
# Exec into Kafka pod
kubectl exec -it kafka-${TEAM_NAME}-dual-role-0 -n ${TEAM_NAMESPACE} -- bash

# Inside the pod — list topics
kafka-topics --bootstrap-server localhost:9092 --list

# Test connectivity from another pod
kubectl run kafka-test --rm -it --image=confluentinc/cp-kafka:7.5.0 \
  -n ${TEAM_NAMESPACE} -- \
  kafka-topics --bootstrap-server kafka-${TEAM_NAME}-kafka-bootstrap:9092 --list
```

### Cleanup

```bash
./per-team/delete-team.sh ${TEAM_NAME} ${TEAM_NAMESPACE}
```

## Shared Kafka Cluster

Deploy a single Kafka cluster for all teams with topic-based isolation.

### Features

- Multi-broker support (scale replicas)
- SSL/TLS support with external access
- NodePort, LoadBalancer, or Route options
- Higher resource allocation
- Topic naming conventions for isolation

### Deployment

```bash
cd shared-deployment

# Configure environment
source ../config.env

# Deploy StatefulSet
envsubst < kafka-statefulset.yaml | kubectl apply -f -

# Choose external access method:
# Option 1: NodePort
envsubst < kafka-nodeport.yaml | kubectl apply -f -

# Option 2: LoadBalancer
envsubst < kafka-loadbalancer.yaml | kubectl apply -f -

# Option 3: OpenShift Route
envsubst < kafka-route.yaml | kubectl apply -f -
```

### Topic Naming Convention

Use prefixes to isolate teams:

```
${TOPIC_PREFIX}.team01.raw
${TOPIC_PREFIX}.team01.processed
${TOPIC_PREFIX}.team02.raw
${TOPIC_PREFIX}.team02.processed
```

Example with `TOPIC_PREFIX=events`:

```
events.team01.raw
events.team01.processed
```

## Troubleshooting

### Kafka CR not becoming Ready

```bash
# Check operator events on the CR
kubectl describe kafka kafka-${TEAM_NAME} -n ${TEAM_NAMESPACE}

# Check Strimzi Cluster Operator logs
kubectl logs deployment/strimzi-cluster-operator -n infra

# Common issues:
# - Strimzi operator not installed (check OperatorHub)
# - Storage class not available: kubectl get sc
# - Insufficient cluster resources
```

### Pod won't start

```bash
# Find the actual pod name (operator naming: kafka-{name}-dual-role-0)
kubectl get pods -n ${TEAM_NAMESPACE} -l strimzi.io/cluster=kafka-${TEAM_NAME}

# Check events
kubectl describe pod kafka-${TEAM_NAME}-dual-role-0 -n ${TEAM_NAMESPACE}

# Check PVC
kubectl get pvc -n ${TEAM_NAMESPACE}
```

### Can't connect to Kafka

```bash
# Verify bootstrap service exists (operator creates automatically)
kubectl get svc kafka-${TEAM_NAME}-kafka-bootstrap -n ${TEAM_NAMESPACE}

# Test DNS resolution
kubectl run dns-test --rm -it --image=busybox -n ${TEAM_NAMESPACE} \
  -- nslookup kafka-${TEAM_NAME}-kafka-bootstrap

# Check Kafka CR is Ready
kubectl get kafka kafka-${TEAM_NAME} -n ${TEAM_NAMESPACE}
```

### Topics not created

```bash
# Exec into Kafka pod
kubectl exec -it kafka-${TEAM_NAME}-dual-role-0 -n ${TEAM_NAMESPACE} -- bash

# List topics
kafka-topics --bootstrap-server localhost:9092 --list

# Create topic manually
kafka-topics --bootstrap-server localhost:9092 \
  --create --topic test --partitions 1 --replication-factor 1
```

## Further Reading

- [Strimzi Documentation](https://strimzi.io/documentation/)
- [Apache Kafka Documentation](https://kafka.apache.org/documentation/)
- [KRaft Mode](https://kafka.apache.org/documentation/#kraft)
