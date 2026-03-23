# Event Generator

Synthetic data producer that generates realistic event streams and publishes them to Kafka topics for testing, demos, and learning.

## What It Does

Produces three types of events continuously:
- **Symptom Reports** - Patient-reported health symptoms
- **Clinic Visits** - Clinical encounter records
- **Environmental Conditions** - Environmental monitoring data

Events are published to Kafka topics for downstream processing and analysis.

---

## CLI Convention

All commands in this README use `kubectl`, which works on both plain Kubernetes and OpenShift (since `oc` is a superset of `kubectl`).

Commands that use OpenShift-specific resources — `ImageStream`, `BuildConfig`, `oc new-project`, `oc start-build` — are marked with **`# OpenShift only`** and must use `oc`.

## Prerequisites

1. **Kubernetes/OpenShift cluster** with access to create namespaces (or pre-provisioned by your platform/cloud provider) and deployments
2. **kubectl** (or `oc` on OpenShift) installed and configured
3. **Kafka cluster(s)** deployed and accessible — two supported modes:
   - **Per-Team Isolation** (`kafka/per-team/`) - Separate Kafka instance per namespace
   - **Shared Cluster** (`kafka/shared-deployment/`) - Single Kafka cluster with topic-based multitenancy
4. **infra namespace** created (if not already provisioned):
   ```bash
   # Kubernetes
   kubectl create namespace ${INFRA_NAMESPACE}

   # OpenShift only
   oc new-project ${INFRA_NAMESPACE}
   ```

5. **Configuration file** set up:
   ```bash
   # From project root
   cp config.env.example config.env
   vi config.env  # Edit with your Kafka bootstrap servers and settings
   ```

---

## Kafka Bootstrap Server Reference

When configuring `TEAM_BOOTSTRAP_SERVERS`, use the appropriate address based on your deployment mode:

**Per-team Kafka** (Kafka runs in each team's namespace; event generator connects cross-namespace from `infra`):
```
kafka-${TEAM_NAME}.${TEAM_NAMESPACE}.svc.cluster.local:9092
```

**Shared Kafka cluster** (Kafka runs in the same `${INFRA_NAMESPACE}` namespace as the event generator):
```
kafka.${INFRA_NAMESPACE}.svc.cluster.local:9092
```

---

## Initial Deployment (First Time)

Run these commands **once** to build and deploy the event generator.

```bash
# 1. Load configuration
source config.env

# 2. Create ImageStream (tracks container images) — OpenShift only
envsubst < event-generator/k8s/01-imagestream.yaml | oc apply -f -

# 3. Create BuildConfig (defines build process) — OpenShift only
envsubst < event-generator/k8s/02-buildconfig.yaml | oc apply -f -

# 3a. Verify BuildConfig was created successfully — OpenShift only
oc get bc ${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}
# Expected output: NAME   TYPE   FROM   LATEST
# If not found, check for errors: oc describe bc ${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}

# 4. Build the container image — OpenShift only
# BuildConfig auto-triggers a build on creation (ConfigChange trigger) — no manual start needed
# Uncomment below only if you need to trigger a rebuild manually later:
# oc start-build ${EVENT_GENERATOR_NAME} --follow -n ${INFRA_NAMESPACE}

# 5. Deploy ConfigMap (Kafka settings, event rate, etc.)
envsubst < event-generator/k8s/03-configmap.yaml | kubectl apply -f -

# 6. Deploy the application
envsubst < event-generator/k8s/04-deployment.yaml | kubectl apply -f -
```

**Note for plain Kubernetes users**: Steps 2–4 use OpenShift's built-in image build system. On plain Kubernetes, build and push the image manually with Docker/Podman first. See [Building with Docker/Podman](#building-with-dockerpodman).

---

## View Build Progress (Without Rebuilding)

To check the status of an existing build **without starting a new one** — OpenShift only:

```bash
# View build logs from the latest build
oc logs -f build/${EVENT_GENERATOR_NAME}-1 -n ${INFRA_NAMESPACE}

# List all builds
oc get builds -n ${INFRA_NAMESPACE}

# View specific build logs (replace '2' with your build number)
oc logs -f build/${EVENT_GENERATOR_NAME}-2 -n ${INFRA_NAMESPACE}
```

---

## Verify Deployment

```bash
# Check pod status
kubectl get pods -n ${INFRA_NAMESPACE} -l app=${EVENT_GENERATOR_NAME}

# View application logs (should show "Produced N events to M teams")
kubectl logs -f deployment/${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}
```

**Expected log output:**
```
INFO - Loaded 2 team Kafka mappings
INFO - Connected to Kafka for team01
INFO - Connected to Kafka for team02
INFO - Produced 20 events to 2 teams (10.0 events/sec each)
```

---

## Redeployment (After Configuration Changes)

When you update settings in `config.env` or need to redeploy:

```bash
# 1. Load updated configuration
source config.env

# 2. Update ConfigMap
envsubst < event-generator/k8s/03-configmap.yaml | kubectl apply -f -

# 3. Update Deployment (only if you changed 04-deployment.yaml)
# envsubst < event-generator/k8s/04-deployment.yaml | kubectl apply -f -

# 4. Restart to pick up ConfigMap changes
# (always needed if you changed config.env — skip if you only changed deployment.yaml)
kubectl rollout restart deployment/${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}
```

**Quick one-liner (config.env changes only):**
```bash
source config.env && \
envsubst < event-generator/k8s/03-configmap.yaml | kubectl apply -f - && \
kubectl rollout restart deployment/${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}
```

---

## Verify Events in Kafka

### Check events are being produced:

```bash
# Connect to Kafka pod and consume events
# Topic name pattern: ${TOPIC_PREFIX}${TEAM_NAME}${TOPIC_SUFFIX} (e.g. events.team01.raw)
kubectl exec -it kafka-${TEAM_NAME}-0 -n ${TEAM_NAMESPACE} -- kafka-console-consumer \
  --bootstrap-server localhost:9092 \
  --topic ${TOPIC_PREFIX}${TEAM_NAME}${TOPIC_SUFFIX} \
  --from-beginning \
  --max-messages 10
```

You should see JSON events like:
```json
{"event_type":"symptom_report","timestamp":"2024-01-15T10:30:00Z","patient_id":"P12345",...}
{"event_type":"clinic_visit","timestamp":"2024-01-15T10:30:15Z","visit_id":"V98765",...}
{"event_type":"environmental_conditions","timestamp":"2024-01-15T10:30:30Z","region":"Boston",...}
```

---

## Troubleshooting

### No events produced

```bash
# Check pod logs for errors
kubectl logs deployment/${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}

# Common issues:
# - Kafka bootstrap servers incorrect in config.env
# - Kafka pods not running
# - Network connectivity issues
```

### Pod not starting

```bash
# Check pod status and events
kubectl describe pod -l app=${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}

# Check if ConfigMap loaded correctly
kubectl get configmap ${EVENT_GENERATOR_NAME}-config -n ${INFRA_NAMESPACE} -o yaml
```

### Connection errors to Kafka

```bash
# Verify Kafka service exists
kubectl get svc -n ${TEAM_NAMESPACE}

# Test DNS resolution
kubectl run dns-test --rm -it --image=busybox -n ${INFRA_NAMESPACE} \
  -- nslookup kafka-${TEAM_NAME}.${TEAM_NAMESPACE}.svc.cluster.local

# Test TCP connectivity from event-generator pod
kubectl exec deployment/${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE} -- \
  nc -zv kafka-${TEAM_NAME}.${TEAM_NAMESPACE}.svc.cluster.local 9092
```

### Topics not created automatically

```bash
# Exec into Kafka pod and inspect
kubectl exec -it kafka-${TEAM_NAME}-0 -n ${TEAM_NAMESPACE} -- bash

# List existing topics
kafka-topics --bootstrap-server localhost:9092 --list

# Create topic manually if needed
kafka-topics --bootstrap-server localhost:9092 \
  --create --topic ${TOPIC_PREFIX}${TEAM_NAME}${TOPIC_SUFFIX} --partitions 1 --replication-factor 1
```

### Events to some teams but not others

```bash
# Check TEAM_BOOTSTRAP_SERVERS in ConfigMap
kubectl get configmap ${EVENT_GENERATOR_NAME}-config -n ${INFRA_NAMESPACE} -o yaml | grep TEAM_BOOTSTRAP_SERVERS

# Verify all team Kafka pods are running
kubectl get pods -A | grep kafka-team
```

---

## Generated Event Types

### Symptom Report
```json
{
  "event_type": "symptom_report",
  "timestamp": "2024-01-15T10:30:00.000Z",
  "patient_id": "P12345",
  "age": 35,
  "region": "Boston",
  "symptoms": ["fever", "cough", "fatigue"],
  "severity": "moderate",
  "duration_days": 3,
  "reported_via": "mobile_app"
}
```

### Clinic Visit
```json
{
  "event_type": "clinic_visit",
  "timestamp": "2024-01-15T10:30:00.000Z",
  "visit_id": "V123456",
  "patient_id": "P12345",
  "clinic_id": "C15",
  "region": "Boston",
  "visit_type": "emergency",
  "primary_complaint": "shortness_of_breath",
  "temperature_f": 101.2,
  "diagnosis_code": "ICD456",
  "prescribed_medication": true
}
```

### Environmental Conditions
```json
{
  "event_type": "environmental_conditions",
  "timestamp": "2024-01-15T10:30:00.000Z",
  "region": "Boston",
  "station_id": "S5",
  "temperature_f": 68.5,
  "humidity_percent": 65,
  "air_quality_index": 45,
  "pollen_count": 120,
  "uv_index": 6
}
```

---

## Configuration Options

All configuration is done via `config.env`. Key settings:

| Variable | Description | Example |
|----------|-------------|---------|
| `INFRA_NAMESPACE` | Namespace for event generator | `infra` |
| `EVENT_RATE_PER_SEC` | Events per second (total or per team) | `10` |
| `RATE_PER_TEAM` | If `true`, rate applies per team; if `false`, total rate | `false` |
| `TEAM_BOOTSTRAP_SERVERS` | Multi-team Kafka mappings | `team01=kafka-team01.team-01.svc:9092,team02=...` |
| `TOPIC_PREFIX` | Topic name prefix | `events.` |
| `TOPIC_SUFFIX` | Topic name suffix | `.raw` |
| `EVENT_STREAMS` | Event types to generate | `symptom_report,clinic_visit,environmental_conditions` |
| `REGIONS` | Geographic regions for events | `Boston,NYC,Chicago` |

**Topic naming pattern:**
- Multi-team: `${TOPIC_PREFIX}${TEAM_ID}${TOPIC_SUFFIX}` → `events.team01.raw`
- Single cluster: `${TOPIC_PREFIX}${TOPIC_SUFFIX}` → `events.raw`

---

## Advanced Usage

### Health Check Endpoints

```bash
# Port forward to access health endpoints
kubectl port-forward deployment/${EVENT_GENERATOR_NAME} 8000:8000 -n ${INFRA_NAMESPACE}

# Check health status
curl http://localhost:8000/health

# Check readiness (shows team count and rate)
curl http://localhost:8000/ready
```

Expected response:
```json
{"status":"ready","teams":2,"rate":10.0}
```

### Scale for Higher Throughput

```bash
# Run multiple replicas
kubectl scale deployment ${EVENT_GENERATOR_NAME} --replicas=3 -n ${INFRA_NAMESPACE}
```

### Change Event Rate

Edit `config.env`:
```bash
EVENT_RATE_PER_SEC="100"  # 100 events/second
```

Then redeploy:
```bash
source config.env && \
envsubst < event-generator/k8s/03-configmap.yaml | kubectl apply -f - && \
kubectl rollout restart deployment/${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}
```

### Connecting to SSL-Enabled Kafka (Shared Cluster)

If using the shared Kafka cluster with SSL/TLS enabled, update `config.env` to point to the SSL listener and ensure the appropriate certificates are available in the cluster:

```bash
# SSL bootstrap server (shared cluster)
KAFKA_BOOTSTRAP_SERVERS="kafka.${INFRA_NAMESPACE}.svc.cluster.local:9093"
```

Refer to `kafka/shared-deployment/` for certificate generation and SSL setup.

---

## Customization

### Adding New Event Types

You can extend the event generator to produce custom event types.

**Step 1: Edit the Python source**

Add your event generator function in `src/event_generator.py`:

```python
def generate_custom_event():
    return {
        "event_type": "custom",
        "timestamp": datetime.utcnow().isoformat(),
        "custom_field": "value",
        "data": random.choice(["option1", "option2", "option3"])
    }

# In the generate_event() function, add your new type:
elif stream_type == 'custom':
    return generate_custom_event()
```

**Step 2: Update ConfigMap**

Edit `config.env` to include your new event type:

```bash
EVENT_STREAMS="symptom_report,clinic_visit,environmental_conditions,custom"
```

**Step 3: Rebuild and redeploy**

```bash
# Rebuild the image — OpenShift only
oc start-build ${EVENT_GENERATOR_NAME} --follow -n ${INFRA_NAMESPACE}

# Redeploy with updated config
source config.env && \
envsubst < event-generator/k8s/03-configmap.yaml | kubectl apply -f - && \
kubectl rollout restart deployment/event-generator -n ${INFRA_NAMESPACE}
```

### Deterministic Event Generation

For testing or demos requiring reproducible event sequences:

Edit `config.env` and add:
```bash
RANDOM_SEED="12345"  # Any integer value
```

Then redeploy. Events will be generated in the same sequence every time.

---

## Performance Tuning

### For High Event Rates

**1. Scale Replicas**

Run multiple generator pods in parallel:

```bash
kubectl scale deployment ${EVENT_GENERATOR_NAME} --replicas=3 -n ${INFRA_NAMESPACE}
```

**2. Increase Event Rate**

Edit `config.env`:
```bash
EVENT_RATE_PER_SEC="100"  # 100 events/second total or per team
```

**3. Adjust Resources**

Edit `k8s/04-deployment.yaml` to allocate more CPU/memory:

```yaml
resources:
  requests:
    memory: "256Mi"
    cpu: "250m"
  limits:
    memory: "512Mi"
    cpu: "500m"
```

**4. Batch Production**

For very high throughput, modify Kafka producer settings in `src/event_generator.py`:

```python
producer = KafkaProducer(
    bootstrap_servers=bootstrap_servers,
    value_serializer=lambda v: json.dumps(v).encode('utf-8'),
    linger_ms=10,           # Wait up to 10ms to batch messages
    batch_size=16384,       # Batch size in bytes
    compression_type='lz4'  # Enable compression
)
```

Then rebuild and redeploy.

**5. Monitoring**

For observing producer throughput and Kafka broker health, consider adding a Prometheus JMX exporter sidecar to the Kafka StatefulSet (see `kafka/shared-deployment/` for configuration options).

### Resource Recommendations

| Event Rate | Replicas | CPU Request | Memory Request |
|------------|----------|-------------|----------------|
| < 50/sec   | 1        | 100m        | 128Mi          |
| 50-200/sec | 1-2      | 250m        | 256Mi          |
| 200-500/sec| 2-3      | 500m        | 512Mi          |
| > 500/sec  | 3-5      | 1000m       | 1Gi            |

---

## Building with Docker/Podman

If using plain Kubernetes (not OpenShift), build and push the image manually:

```bash
cd event-generator

# Build
podman build -t your-registry.io/event-generator:latest .

# Push to registry
podman push your-registry.io/event-generator:latest

# Update k8s/04-deployment.yaml with your image
# Then deploy ConfigMap and Deployment
kubectl apply -f k8s/03-configmap.yaml -n ${INFRA_NAMESPACE}
kubectl apply -f k8s/04-deployment.yaml -n ${INFRA_NAMESPACE}
```

---

## Use Cases

- **Development** - Test Kafka consumers without real data sources
- **Demos** - Showcase real-time streaming pipelines
- **Load Testing** - Stress test Kafka clusters and downstream systems
- **Training** - Provide consistent data for learning exercises
- **CI/CD** - Generate test data for automated pipeline testing

---

## Cleanup

```bash
# Delete event generator (OpenShift — single command)
oc delete deployment/${EVENT_GENERATOR_NAME} configmap/${EVENT_GENERATOR_NAME}-config imagestream/${EVENT_GENERATOR_NAME} buildconfig/${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}

# OR with kubectl (if using Docker/Podman build — single command)
kubectl delete deployment/${EVENT_GENERATOR_NAME} configmap/${EVENT_GENERATOR_NAME}-config -n ${INFRA_NAMESPACE}
```

---

## Directory Structure

```
event-generator/
├── src/                      # Python source code
│   └── event_generator.py    # Main application
├── k8s/                      # Kubernetes manifests
│   ├── 01-imagestream.yaml   # Image tracking (OpenShift)
│   ├── 02-buildconfig.yaml   # Build configuration (OpenShift)
│   ├── 03-configmap.yaml     # Application configuration
│   └── 04-deployment.yaml    # Deployment spec
├── Dockerfile                # Container image build
└── README.md                 # This file
```

---

## Further Reading

### Apache Kafka
- [Kafka Documentation](https://kafka.apache.org/documentation/)
- [Kafka Producer Configuration](https://kafka.apache.org/documentation/#producerconfigs)
- [Kafka Performance Tuning](https://kafka.apache.org/documentation/#maximizingefficiency)
- [KRaft Mode (no ZooKeeper)](https://kafka.apache.org/documentation/#kraft)

### Kafka Python Client
- [kafka-python Documentation](https://kafka-python.readthedocs.io/)
- [kafka-python Producer API](https://kafka-python.readthedocs.io/en/master/apidoc/KafkaProducer.html)

### Kubernetes/OpenShift
- [Kubernetes Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)
- [ConfigMaps](https://kubernetes.io/docs/concepts/configuration/configmap/)
- [OpenShift Builds and BuildConfigs](https://docs.openshift.com/container-platform/latest/cicd/builds/understanding-buildconfigs.html)
- [Kubernetes StatefulSets](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/)

### Project Resources
- [Project Root README](../README.md) - Main project documentation
- [Kafka Deployment Guide](../kafka/README.md) - Deploy Kafka clusters (per-team and shared)
- [Configuration Reference](../config.env.example) - All available settings
