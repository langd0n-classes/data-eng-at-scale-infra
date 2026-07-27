# Kafka Observability Stack

Instructor-facing monitoring for all team Kafka clusters. Students are not given
access to these dashboards — they debug using `oc` CLI from their team namespaces.

## Two Tools

| Tool | What it shows | When to use |
|------|---------------|-------------|
| **Grafana** | Historical time-series: throughput trends, GC pressure, lag over time | After class, long-running analysis |
| **Kafka Console** | Live state: topics right now, messages right now, consumer group lag right now | During class, live debugging |

Both are deployed in the `infra` namespace. Routes are instructor-only.

## Architecture

```
Kafka pod (JMX Exporter sidecar, port 9404) [team-01, team-02, ...]
  → PodMonitor (infra namespace, any: true — auto-picks up new teams)
  → OpenShift User Workload Prometheus (openshift-user-workload-monitoring)
  → thanos-querier (openshift-monitoring:9091)
  → Grafana (infra namespace) — all teams on one screen

Kafka CRs [team-01, team-02, ...] (referenced by name, not bootstrap URL)
  → Kafka Console (infra namespace) — live state via operator
```

## Prerequisites

OpenShift User Workload Monitoring must be enabled. `pipeline/setup.sh` does
this automatically by applying `monitoring/01-cluster-monitoring-config.yaml`.

No `kubeadmin` is required. Developer user with `cluster-admin` handles everything —
same as existing RBAC in `pipeline/rbac/` applied by `setup.sh`.

## Deployment

### Primary: Tekton (auto, runs with `bash pipeline/setup.sh`)

The full observability stack deploys automatically during the normal pipeline run:

```
deploy-kafka-teamN → deploy-monitoring-teamN → deploy-nifi-teamN
                     ↓ (after all teams)
                 deploy-grafana → deploy-console
```

### Fallback: ops.sh (no Tekton needed)

```bash
source config.env

# Enable UWM (one-time, already done by setup.sh)
oc apply -f monitoring/01-cluster-monitoring-config.yaml

# Per team: apply JMX ConfigMap + Prometheus RBAC + patch Kafka CR
bash pipeline/ops.sh deploy-monitoring team01 team-01
bash pipeline/ops.sh deploy-monitoring team02 team-02

# One-time: deploy Grafana to infra
bash pipeline/ops.sh deploy-grafana

# One-time: deploy Kafka Console to infra
bash pipeline/ops.sh deploy-console
```

### All teams at once (ops.sh)

```bash
bash pipeline/ops.sh deploy-monitoring-all
```

### ChatOps (Slack)

```
/infra deploy-monitoring team01 team-01
/infra deploy-grafana
/infra monitoring-status team01 team-01
```

## Accessing Grafana

```bash
# Get the URL
oc get route grafana -n infra -o jsonpath='{.spec.host}'

# Login: admin / value of GRAFANA_ADMIN_PASSWORD from config.env
```

Dashboards loaded automatically:
- **Strimzi Kafka** — broker throughput, log size, KRaft controller state
- **Strimzi Kafka Exporter** — consumer group lag (requires kafkaExporter on Kafka CR)
- **Strimzi Operators** — Strimzi cluster operator reconciliation metrics

Note: ZooKeeper panels show "No data" — this is expected (KRaft mode, no ZooKeeper).

## Accessing Kafka Console

```bash
# Get the URL
oc get route -n infra | grep console
```

Shows all team Kafka clusters. Click a team → Topics → see messages and consumer groups live.

## Key PromQL Queries (Grafana Explore)

```promql
# Messages per second per team
rate(kafka_server_brokertopicmetrics_messagesinpersec_rate{namespace="team-01"}[5m])

# Consumer group lag
kafka_consumergroup_lag{namespace="team-01"}

# JVM heap usage
jvm_memory_heap_used_bytes{namespace="team-01"} / jvm_memory_heap_max_bytes{namespace="team-01"}

# Active controller (should always be 1)
kafka_controller_kafkacontroller_activecontrollercount{namespace="team-01"}
```

## Troubleshooting

### Grafana shows "No data"

```bash
# 1. Check UWM is running
oc get pods -n openshift-user-workload-monitoring

# 2. Check JMX exporter is running on Kafka pod
oc exec kafka-team01-dual-role-0 -n team-01 -- curl -s http://localhost:9404/metrics | head -5

# 3. Check PodMonitor exists
oc get podmonitor kafka-all-teams-metrics -n infra

# 4. Check Prometheus RBAC in team namespace
oc get rolebinding prometheus-scrape -n team-01

# 5. Port-forward to Prometheus and check targets
oc port-forward -n openshift-user-workload-monitoring pod/prometheus-user-workload-0 9090:9090
# Open http://localhost:9090/targets — look for infra/kafka-all-teams-metrics
```

### Kafka Console shows no clusters

```bash
# Check Console CR status
oc get console kafka-console -n infra -o yaml | grep -A 10 status

# Check console pod logs
oc logs -n infra deployment/kafka-console 2>/dev/null || \
  oc logs -n infra $(oc get pod -n infra -l app.kubernetes.io/name=console -o name | head -1)
```

### New team not appearing in Grafana

New teams are picked up automatically by the PodMonitor (`namespaceSelector: any: true`).
After running `ops.sh deploy-monitoring <name> <ns>`, allow 2-3 minutes for the first
Prometheus scrape to complete. Then the team's metrics appear in Grafana.

## Files

```
monitoring/
  01-cluster-monitoring-config.yaml    Enable OpenShift User Workload Monitoring
  kafka/
    kafka-jmx-configmap-template.yaml  JMX rules ConfigMap (applied per team namespace)
    kafka-prometheus-rbac-template.yaml RoleBinding for cross-namespace scraping
    kafka-podmonitor.yaml              PodMonitor covering all team namespaces
  grafana/
    01-grafana-deployment.yaml         Grafana Deployment
    02-grafana-service.yaml            Grafana Service
    03-grafana-route.yaml              Grafana Route (instructor only)
    04-grafana-datasource-configmap.yaml thanos-querier datasource (token injected at deploy)
    06-grafana-provisioning-configmap.yaml Dashboard auto-discovery config
    07-grafana-sa.yaml                 grafana-sa ServiceAccount + token Secret
    08-grafana-clusterrolebinding.yaml cluster-monitoring-view only (least privilege)
  console/
    01-kafka-console-subscription.yaml OLM Subscription for Kafka Console operator
```
