# Portable Event Generator (Kind / k3s)

A `kubectl`-only variant of the event generator deployment, for a no-cost
local Kubernetes cluster — no OpenShift ImageStream/BuildConfig required.

This does **not** replace the OpenShift path. `event-generator/k8s/` is
unchanged and remains the primary deployment method for the classroom.

**Out of scope here:** the event generator's own source code
(`event-generator/src/*.py`) — nothing there needed to change at all. Both
`event_generator.py` and `check_events.py` are already fully portable
(env-var driven Kafka connectivity, a plain Flask/waitress health server,
no OpenShift API calls anywhere in the application logic).

## Compatibility

Plain `kubectl`, a pre-built image pulled from an in-cluster registry
(`registry/portable/`) instead of an `ImageStream`/`BuildConfig` pair. Runs
on any conformant Kubernetes cluster: Kind, k3s, EKS, GKE, AKS, a
bare-metal cluster, etc.

## Validation walkthrough

Validated on: Kubernetes (Kind) v1.34.11, using the image built and pushed
in `pipeline/portable/` (`localhost:30500/event-generator:test2`) and Kafka
for `team01` deployed via `kafka/portable/`.

**1. Deploy the ConfigMap and Deployment**

```bash
export INFRA_NAMESPACE=infra
export EVENT_GENERATOR_NAME=event-generator
export EVENT_RATE_PER_SEC=5
export TOPIC_PREFIX="events." TOPIC_SUFFIX=".raw" TOPIC=""
export REGIONS="Boston,Worcester"
export TEAM_BOOTSTRAP_SERVERS="team01=kafka-team01-kafka-bootstrap.team-01.svc.cluster.local:9092"
export KAFKA_BOOTSTRAP_SERVERS=""
export EVENT_GENERATOR_IMAGE="localhost:30500/event-generator:test2"

envsubst < event-generator/portable/k8s/configmap.yaml | kubectl apply -f -
envsubst < event-generator/portable/k8s/deployment.yaml | kubectl apply -f -
```
```
configmap/event-generator-config created
deployment.apps/event-generator created
service/event-generator created
```

**2. Confirm it connects and starts emitting**

```bash
kubectl logs -n infra -l app=event-generator --tail=5
```
```
Connected to Kafka for team01: kafka-team01-kafka-bootstrap.team-01.svc.cluster.local:9092
Connected to 1/1 team Kafka instances
Successfully connected teams: team01
Starting emission to 1 teams
Serving on http://0.0.0.0:8000
```

**3. Confirm the team's Kafka actually receives real events**

```bash
kubectl run kafka-consumer-test -n team-01 --image=confluentinc/cp-kafka:7.5.0 --restart=Never --command -- sleep 300
kubectl exec kafka-consumer-test -n team-01 -- kafka-console-consumer \
  --bootstrap-server kafka-team01-kafka-bootstrap.team-01.svc.cluster.local:9092 \
  --topic events.team01.raw --from-beginning --max-messages 3 --timeout-ms 20000
```
```
{"event_type": "hospital_admission", ..., "region": "Boston", ..., "source": "event-generator", "schema_version": "1.0"}
{"event_type": "symptom_report", ..., "region": "Worcester", ..., "source": "event-generator", "schema_version": "1.0"}
{"event_type": "hospital_admission", ..., "region": "Boston", ..., "source": "event-generator", "schema_version": "1.0"}
Processed a total of 3 messages
```

```bash
kubectl delete pod kafka-consumer-test -n team-01 --ignore-not-found
```

## Cleanup

```bash
kubectl delete -f event-generator/portable/k8s/deployment.yaml --ignore-not-found
kubectl delete -f event-generator/portable/k8s/configmap.yaml --ignore-not-found
```
