# Event Generator

Synthetic data producer for testing Kafka infrastructure and data pipelines.

## Overview

The event generator produces simulated event streams and publishes them to Kafka topics. It's designed for:

- Testing Kafka deployments
- Demonstrating stream processing
- Developing and debugging data pipelines
- Load testing

## Features

- Multiple event types (configurable)
- Adjustable event rate (global or per team)
- Multi-team or single-cluster support
- Optional deterministic generation via `RANDOM_SEED`
- Health check endpoints

## Building the Image

```bash
cd event-generator

# Build with Docker/Podman
podman build -t event-generator:latest .

# Tag for your registry
podman tag event-generator:latest your-registry.io/event-generator:latest

# Push
podman push your-registry.io/event-generator:latest
```

Alternatively, use OpenShift BuildConfig:

```bash
oc new-build --binary --name=event-generator -n ${INFRA_NAMESPACE}
oc start-build event-generator --from-dir=. --follow -n ${INFRA_NAMESPACE}
```

## Configuration

### ConfigMap

Edit [event-generator/configmap.yaml](ds551-langdon-dev/mnt/data-eng-at-scale-infra/event-generator/configmap.yaml) to configure:

```yaml
data:
  EVENT_RATE_PER_SEC: "10"                                  # Events/second total
  RATE_PER_TEAM: "false"                                    # true = rate applies per team
  TOPIC_PREFIX: "events.team"                               # Topic prefix
  TOPIC_SUFFIX: ".raw"                                      # Topic suffix
  # TOPIC: "events.raw"                                     # Optional explicit topic (single-cluster)
  EVENT_STREAMS: "symptom_report,clinic_visit,environmental_conditions" # Streams
  REGIONS: "Boston,NYC,Chicago"                              # Geographic regions

### Kafka Bootstrap

- Multi-team: set `TEAM_BOOTSTRAP_SERVERS` mapping (`team01=host:9092,team02=host:9092`).
- Single-cluster: set `KAFKA_BOOTSTRAP_SERVERS` and optionally `TOPIC`.
## Verifying Events in Kafka

## Deployment

```bash
# Edit ConfigMap with your settings
vi configmap.yaml

# Apply
kubectl apply -f configmap.yaml -n ${INFRA_NAMESPACE}
kubectl apply -f deployment.yaml -n ${INFRA_NAMESPACE}

# Verify
kubectl get pods -n ${INFRA_NAMESPACE} -l app=event-generator
kubectl logs -f deployment/event-generator -n ${INFRA_NAMESPACE}
```

## Generated Event Types

### Symptom Report Example

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

### Clinic Visit Example

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

### Environmental Conditions Example

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

## Topic Naming
The generator emits mixed event types to a raw input topic. Downstream pipelines typically split by `event_type` into subtopics.

- Multi-team: `${TOPIC_PREFIX}${TEAM_ID}${TOPIC_SUFFIX}` (e.g., `events.team01.raw`)
- Single-cluster: `${TOPIC_PREFIX}${TOPIC_SUFFIX}` (e.g., `events.raw`) or explicit `TOPIC`

## Monitoring

### Health Endpoints

```bash
# Port forward to access health endpoints
kubectl port-forward deployment/event-generator 8000:8000 -n ${INFRA_NAMESPACE}

# Check health
curl http://localhost:8000/health

# Check readiness (shows team count)
curl http://localhost:8000/ready
```

Expected response:

```json
{
  "status": "ready",
  "teams": 10,
  "rate": 10.0
}
```

### Logs

```bash
# Follow logs
kubectl logs -f deployment/event-generator -n ${INFRA_NAMESPACE}

# Look for:
# - "Loaded N team Kafka mappings"
# - "Connected to Kafka for teamXX"
# - "Produced N events to M teams"
```

## Verifying Events in Kafka

```bash
# Exec into Kafka pod
kubectl exec -it kafka-team01-0 -n team-01 -- bash

# List topics
kafka-topics --bootstrap-server localhost:9092 --list

# Consume events
kafka-console-consumer --bootstrap-server localhost:9092 \
  --topic events.team01.raw \
  --from-beginning \
  --max-messages 10
```

## Customization

### Adding Event Types

Edit `event_generator.py` to add a new generator and route:

```python
def generate_custom_event():
  return {
    "event_type": "custom",
    "timestamp": datetime.utcnow().isoformat(),
    "data": "example"
  }

# In generate_event()
elif stream_type == 'custom':
  return generate_custom_event()
```

Then include `custom` in the ConfigMap:

```yaml
EVENT_STREAMS: "symptom_report,clinic_visit,environmental_conditions,custom"
```

### Changing Event Rate

Adjust in ConfigMap:

```yaml
EVENT_RATE_PER_SEC: "100"  # 100 events/second total
```

Or scale the deployment for higher throughput:

```bash
kubectl scale deployment event-generator --replicas=3 -n ${INFRA_NAMESPACE}
```

### Deterministic Generation

Set `RANDOM_SEED` in ConfigMap to produce reproducible sequences.

## Troubleshooting

### No events produced

```bash
# Check logs for errors
kubectl logs deployment/event-generator -n ${INFRA_NAMESPACE}

# Common issues:
# - Kafka not reachable (check service names)
# - ConfigMap not loaded (check deployment)
# - Bootstrap servers incorrect
```

### Connection errors

```bash
# Verify Kafka service exists
kubectl get svc -n team-01

# Test connectivity
kubectl exec -it deployment/event-generator -n ${INFRA_NAMESPACE} -- \
  nc -zv kafka-team01.team-01.svc.cluster.local 9092
```

### Events only to some teams

Check ConfigMap for correct bootstrap servers:

```bash
kubectl get configmap event-generator-config -n ${INFRA_NAMESPACE} -o yaml
```

Restart deployment after ConfigMap changes:

```bash
kubectl rollout restart deployment event-generator -n ${INFRA_NAMESPACE}
```

## Performance Tuning

For high event rates:

1. **Scale replicas** - Multiple generator pods
2. **Batch production** - Modify producer config for batching
3. **Async sends** - Use async Kafka producer mode
4. **Increase resources** - More CPU/memory

Example resource adjustment:

```yaml
resources:
  requests:
    memory: "256Mi"
    cpu: "250m"
  limits:
    memory: "512Mi"
    cpu: "500m"
```

## Cleanup

```bash
kubectl delete deployment event-generator -n ${INFRA_NAMESPACE}
kubectl delete configmap event-generator-config -n ${INFRA_NAMESPACE}
```

## Use Cases

- **Development** - Test Kafka consumers without real data sources
- **Demos** - Show real-time streaming pipelines
- **Load Testing** - Stress test Kafka and downstream systems
- **Training** - Provide consistent data for learning exercises

## Further Reading

- [kafka-python Documentation](https://kafka-python.readthedocs.io/)
- [Kafka Producer Config](https://kafka.apache.org/documentation/#producerconfigs)
