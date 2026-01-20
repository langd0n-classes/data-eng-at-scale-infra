# Kafka Deployment Options

This directory contains Kubernetes manifests for deploying Apache Kafka in two modes:

1. **Per-Team Isolation** (`per-team/`) - Separate Kafka instance per namespace
2. **Shared Cluster** (`shared-deployment/`) - Single Kafka cluster with topic-based multitenancy

## Per-Team Kafka Deployment

Deploy isolated Kafka instances for complete tenant separation.

### Features

- KRaft mode (no ZooKeeper dependency)
- Single-node brokers (suitable for dev/test)
- PLAINTEXT listeners (in-cluster only)
- Auto-topic creation enabled
- Aggressive log retention (24 hours, 100MB)
- Low resource footprint (512MB RAM, 2GB storage)

### Deployment

```bash
cd per-team

# Set variables
export TEAM_NAME=team01
export TEAM_NAMESPACE=team-01
export STORAGE_CLASS=standard

# Deploy
./deploy-team.sh ${TEAM_NAME} ${TEAM_NAMESPACE}

# Or manually
envsubst < kafka-per-team-template.yaml | kubectl apply -f -
```

### Access

**From within the same namespace:**

```bash
kafka-${TEAM_NAME}:9092
```

**From other namespaces:**

```bash
kafka-${TEAM_NAME}.${TEAM_NAMESPACE}.svc.cluster.local:9092
```

### Testing

```bash
# Check pod status
kubectl get pods -n ${TEAM_NAMESPACE} -l component=kafka

# View logs
kubectl logs -f kafka-${TEAM_NAME}-0 -n ${TEAM_NAMESPACE}

# Test connectivity
kubectl run kafka-test --rm -it --image=confluentinc/cp-kafka:7.5.0 \
  -n ${TEAM_NAMESPACE} -- bash

# Inside the pod:
kafka-topics --bootstrap-server kafka-${TEAM_NAME}:9092 --list
```

### Cleanup

```bash
./delete-team.sh ${TEAM_NAME} ${TEAM_NAMESPACE}
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

### External Access

For SSL-enabled external access, generate certificates:

```bash
./generate-certs.sh
kubectl create secret generic kafka-ssl-certs \
  --from-file=server.keystore.jks \
  --from-file=server.truststore.jks \
  -n ${INFRA_NAMESPACE}
```

Then deploy with SSL:

```bash
envsubst < kafka-statefulset.yaml | kubectl apply -f -
envsubst < kafka-service-ssl.yaml | kubectl apply -f -
```

## Configuration

### Resource Tuning

Adjust in the StatefulSet template:

```yaml
resources:
  requests:
    memory: "512Mi"   # Minimum memory
    cpu: "250m"       # Minimum CPU
  limits:
    memory: "1Gi"     # Maximum memory
    cpu: "500m"       # Maximum CPU
```

### Storage

Modify storage size:

```yaml
volumeClaimTemplates:
  - spec:
      resources:
        requests:
          storage: 2Gi  # Adjust as needed
```

### Retention Policy

Change log retention:

```yaml
env:
  - name: KAFKA_LOG_RETENTION_HOURS
    value: "168"  # 1 week
  - name: KAFKA_LOG_RETENTION_BYTES
    value: "1073741824"  # 1GB
```

## Troubleshooting

### Pod won't start

```bash
# Check events
kubectl describe pod kafka-${TEAM_NAME}-0 -n ${TEAM_NAMESPACE}

# Check PVC
kubectl get pvc -n ${TEAM_NAMESPACE}

# Common issues:
# - Storage class not available
# - Insufficient cluster resources
# - Permission issues (check SCC on OpenShift)
```

### Can't connect to Kafka

```bash
# Verify service exists
kubectl get svc -n ${TEAM_NAMESPACE}

# Test DNS resolution
kubectl run dns-test --rm -it --image=busybox -n ${TEAM_NAMESPACE} \
  -- nslookup kafka-${TEAM_NAME}

# Check pod is ready
kubectl get pods -n ${TEAM_NAMESPACE} -l app=kafka-${TEAM_NAME}
```

### Topics not created

```bash
# Exec into Kafka pod
kubectl exec -it kafka-${TEAM_NAME}-0 -n ${TEAM_NAMESPACE} -- bash

# List topics
kafka-topics --bootstrap-server localhost:9092 --list

# Create topic manually
kafka-topics --bootstrap-server localhost:9092 \
  --create --topic test --partitions 1 --replication-factor 1
```

## Performance Tuning

For production workloads, consider:

1. **Multiple brokers** - Set replicas > 1 for HA
2. **Increased storage** - Size based on retention needs
3. **Resource allocation** - More memory and CPU
4. **Network policies** - Restrict access
5. **Monitoring** - Add Prometheus JMX exporter

## Further Reading

- [Apache Kafka Documentation](https://kafka.apache.org/documentation/)
- [KRaft Mode](https://kafka.apache.org/documentation/#kraft)
- [Kubernetes StatefulSets](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/)
