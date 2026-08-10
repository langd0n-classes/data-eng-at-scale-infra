# Kafka Console

Instructor-facing live visibility into all team Kafka clusters. Students are not given
access — they debug using `oc` CLI from their team namespaces.

## What It Shows

- **Topics** — partition count, message count, retention settings
- **Messages** — browse messages live within any topic
- **Consumer Groups** — which groups are active and their current offsets
- **Consumer Lag** — per-partition lag derived from Kafka's own offset data (no Prometheus needed)

All data comes directly from Kafka's internal offset state via the Strimzi operator —
no metrics pipeline or Prometheus required.

## Operator Installation

The Kafka Console is provided by the **Streams for Apache Kafka Console** operator.
Installation depends on the cluster environment:

### Self-managed cluster (kubeadmin available)

Apply the OLM Subscription, or install via OperatorHub UI:

```bash
oc apply -f monitoring/console/01-kafka-console-subscription.yaml
```

### Managed cluster (no kubeadmin)

Ask the cluster admin to install the **Streams for Apache Kafka Console** operator
from OperatorHub with install mode **All namespaces**. Once installed, all the steps
below work with developer access only.

## Deployment

### Via Tekton (runs automatically with `bash pipeline/setup.sh`)

The console deploys after all NiFi instances are up:

```
deploy-nifi-team1..15 → deploy-console
```

### Via ops.sh (no Tekton needed)

```bash
source config.env
bash pipeline/ops.sh deploy-console
```

### Check status

```bash
bash pipeline/ops.sh console-status
```

## Accessing the Console

```bash
# Get the URL
oc get route -n infra -l app.kubernetes.io/name=console -o jsonpath='{.items[0].spec.host}'
```

Open `https://<host>` in a browser. All active team Kafka clusters appear in the sidebar.

## Self-Healing

Strimzi automatically restarts crashed Kafka broker pods. The operator monitors the
KafkaNodePool and recreates pods that fail liveness or readiness probes.

### Probe settings (kafka-cr-template.yaml)

| Probe | Initial delay | Period | Timeout | Failure threshold |
|-------|--------------|--------|---------|-------------------|
| Liveness | 30s | 10s | 5s | 3 |
| Readiness | 20s | 10s | 5s | 3 |

A broker is restarted after 3 consecutive probe failures (~30s of unresponsiveness).

### PodDisruptionBudget

Each team has a PDB (`maxUnavailable: 1`) that ensures at most one broker is
unavailable at a time during voluntary disruptions (node drain, rolling restarts).

### Verify self-healing

```bash
# Delete the broker pod — Strimzi recreates it automatically
bash kafka/per-team/verify-self-healing.sh team01 team-01
# Expected output: "Recovery time: N seconds" where N is typically < 60
```

## Slack Alerting

The ChatOps server polls Kafka broker restart counts every 60 seconds. When a broker
restarts 4 or more times within a 10-minute window, an alert is posted to the admin
Slack channel.

### Prerequisites

Add a Slack bot token (`xoxb-...`) to the existing `slack-credentials` Secret:

```bash
oc create secret generic slack-credentials \
  --from-literal=signing-secret=<existing-value> \
  --from-literal=bot-token=xoxb-... \
  -n infra --dry-run=client -o yaml | oc apply -f -
```

If the bot token is missing or empty, the alerting loop starts but stays silent — no
alerts are sent and the rest of ChatOps is unaffected.

### Alert format

```
:rotating_light: *Kafka broker crash-loop detected*
• *Namespace*: `team-01`
• *Pod*: `kafka-team01-dual-role-0`
• *Restarts*: +5 in last 10 minutes (total: 12)
• *Action*: Run `/infra status team01 team-01` to investigate
```

### Tuning

| Env var | Default | Effect |
|---------|---------|--------|
| `ALERT_RESTART_THRESHOLD` | `4` | Restart delta that triggers an alert |
| `ALERT_WINDOW_MINUTES` | `10` | Rolling window for counting restarts |

Alerts for the same pod are suppressed for 300 seconds after firing to avoid spam
during a sustained crash-loop.

## Troubleshooting

### Console shows no clusters

```bash
# Check Console CR status
oc get console kafka-console -n infra -o yaml | grep -A 10 status

# Check console pod logs
oc logs -n infra -l app.kubernetes.io/name=console
```

### Console pod not starting

```bash
# Check operator is installed
oc get csv -n openshift-operators | grep -i console

# Check CRD exists
oc get crd consoles.console.streamshub.github.com
```

## Files

```
monitoring/
  console/
    01-kafka-console-subscription.yaml   OLM Subscription (cluster-admin required)
  README.md                              This file
```
