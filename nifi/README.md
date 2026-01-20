# NiFi Deployment

Deploy isolated Apache NiFi instances for data workflow orchestration and ETL.

## Overview

Each deployment creates:

- NiFi StatefulSet (single pod)
- ClusterIP Service (HTTPS on 8443)
- Persistent Volume Claim (stores flows, state, repositories)
- Route or Ingress (for external UI access)

## Prerequisites

### OpenShift

NiFi requires `anyuid` Security Context Constraint:

```bash
oc adm policy add-scc-to-user anyuid -z default -n ${NAMESPACE}
```

### Kubernetes

Configure Pod Security admission to allow NiFi's UID:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: team-01
  labels:
    pod-security.kubernetes.io/enforce: baseline
```

## Deployment

### Using the Script

```bash
# Set variables
export TEAM_NAME=team01
export INFRA_NAMESPACE=team-01
export TEAM_PASSWORD="SecurePassword123"
export NIFI_IMAGE=apache/nifi:latest
export STORAGE_CLASS=standard

# Deploy
./deploy-team.sh ${TEAM_NAME} ${INFRA_NAMESPACE} ${TEAM_PASSWORD}
```

### Manual Deployment

```bash
# Source configuration
source ../config.env
export TEAM_NAME=team01
export TEAM_PASSWORD="SecurePassword123"

# Apply manifests
envsubst < team-pvc-template.yaml | kubectl apply -f -
envsubst < team-statefulset-template.yaml | kubectl apply -f -
envsubst < team-route-template.yaml | kubectl apply -f -
```

## Access

### Get the URL

**OpenShift (Route):**

```bash
kubectl get route nifi-${TEAM_NAME} -n ${INFRA_NAMESPACE} \
  -o jsonpath='{.spec.host}'
```

**Kubernetes (Ingress):**

```bash
kubectl get ingress nifi-${TEAM_NAME} -n ${INFRA_NAMESPACE}
```

**Port Forward (for testing):**

```bash
kubectl port-forward -n ${INFRA_NAMESPACE} nifi-${TEAM_NAME}-0 8443:8443
# Access at https://localhost:8443/nifi
```

### Login Credentials

- **Username:** Value of `${TEAM_NAME}` variable
- **Password:** Value of `${TEAM_PASSWORD}` variable

## Configuration

### Storage

NiFi uses a single PVC with subPaths:

```yaml
volumeMounts:
  - name: data
    mountPath: /opt/nifi/nifi-current/conf
    subPath: conf
  - name: data
    mountPath: /opt/nifi/nifi-current/state
    subPath: state
  - name: data
    mountPath: /opt/nifi/nifi-current/flowfile_repository
    subPath: flowfile_repository
  # ... more repositories
```

Default PVC size: 5Gi (adjust in template)

### Resources

Default allocation:

```yaml
resources:
  requests:
    memory: "1Gi"
    cpu: "500m"
  limits:
    memory: "2560Mi"
    cpu: "1500m"
```

Adjust based on workflow complexity.

### JVM Heap

Modify in StatefulSet template:

```yaml
env:
  - name: NIFI_JVM_HEAP_INIT
    value: "512M"
  - name: NIFI_JVM_HEAP_MAX
    value: "1G"
```

### Custom NiFi Image

To use a custom image (e.g., with additional NAR files):

```dockerfile
FROM apache/nifi:latest
COPY custom-nar-1.0.nar /opt/nifi/nifi-current/lib/
```

Build and push, then set `NIFI_IMAGE`:

```bash
export NIFI_IMAGE=your-registry/custom-nifi:latest
```

## Persistence

Data persisted in PVC:

- **Flows** - Process group configurations
- **State** - Component state management
- **Repositories** - FlowFile, Content, Provenance

**Backup:** Export flows regularly via NiFi UI (Download Flow Definition).

**Disaster Recovery:** To recover, redeploy and import flows.

## Security

### HTTPS

NiFi generates a self-signed certificate at startup. For production:

1. Generate proper certificates
2. Mount as secrets
3. Configure keystore/truststore paths

### Single User Authentication

The deployment uses single-user mode (username/password). For production:

- Enable LDAP/AD integration
- Configure client certificates
- Implement OIDC/SAML

### Secrets Management

**Never hardcode passwords.** Use:

```bash
# Create secret
kubectl create secret generic nifi-${TEAM_NAME}-creds \
  --from-literal=password="${TEAM_PASSWORD}" \
  -n ${INFRA_NAMESPACE}

# Reference in StatefulSet
env:
  - name: SINGLE_USER_CREDENTIALS_PASSWORD
    valueFrom:
      secretKeyRef:
        name: nifi-${TEAM_NAME}-creds
        key: password
```

## Kafka Integration

NiFi processors for Kafka:

- **PublishKafka_2_6** - Produce messages
- **ConsumeKafka_2_6** - Consume messages

Example bootstrap servers:

```
kafka-team01.team-01.svc.cluster.local:9092
```

For SSL, configure:

- Security Protocol: SSL
- SSL Context Service: Upload certificates

## Troubleshooting

### Pod won't start

```bash
# Check pod status
kubectl describe pod nifi-${TEAM_NAME}-0 -n ${INFRA_NAMESPACE}

# Common issues:
# - SCC not granted (OpenShift)
# - PVC not bound
# - Insufficient resources
```

### Can't access UI

```bash
# Check route/ingress
kubectl get route,ingress -n ${INFRA_NAMESPACE}

# Check service
kubectl get svc nifi-${TEAM_NAME} -n ${INFRA_NAMESPACE}

# Check pod logs
kubectl logs -f nifi-${TEAM_NAME}-0 -n ${INFRA_NAMESPACE}
```

### Login fails

- Verify username matches `${TEAM_NAME}`
- Verify password matches `${TEAM_PASSWORD}`
- Check pod logs for authentication errors
- Clear browser cache/try incognito

### Slow startup

NiFi can take 2-3 minutes to start. Monitor logs:

```bash
kubectl logs -f nifi-${TEAM_NAME}-0 -n ${INFRA_NAMESPACE}
```

Look for: `NiFi has started. The UI is available`

## Cleanup

```bash
./delete-team.sh ${TEAM_NAME} ${INFRA_NAMESPACE}

# To also delete PVC (⚠️ loses all data):
kubectl delete pvc nifi-${TEAM_NAME}-data -n ${INFRA_NAMESPACE}
```

## Advanced Topics

### Clustering

For clustered NiFi (3+ nodes):

- Modify StatefulSet replicas
- Configure embedded ZooKeeper or external coordinator
- Adjust load balancer settings
- See [NiFi Clustering Guide](https://nifi.apache.org/docs/nifi-docs/html/administration-guide.html#clustering)

### Custom Processors

Add custom NARs:

1. Build your processor
2. Create custom Docker image
3. Deploy with `NIFI_IMAGE` pointing to custom image

### Monitoring

Add Prometheus metrics:

- Enable NiFi metrics reporting
- Deploy Prometheus JMX exporter sidecar
- Configure ServiceMonitor for Prometheus Operator

## Further Reading

- [Apache NiFi Documentation](https://nifi.apache.org/docs.html)
- [NiFi Expression Language](https://nifi.apache.org/docs/nifi-docs/html/expression-language-guide.html)
- [NiFi System Administrator's Guide](https://nifi.apache.org/docs/nifi-docs/html/administration-guide.html)
