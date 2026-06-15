# ChatOps

Slack slash command handler for classroom infrastructure operations. Deployed as
an always-on OpenShift Deployment in the `infra` namespace.

## One-Time Setup

1. Create a Slack App → add a slash command `/infra`
2. Create the credentials Secret before running the pipeline:
   ```bash
   oc create secret generic slack-credentials \
     --from-literal=signing-secret=<Slack App → Basic Information → Signing Secret> \
     -n ${INFRA_NAMESPACE}
   ```
3. Set in `config.env`: `CHATOPS_ENABLED=true`, `ADMIN_CHANNEL_ID`, `CHATOPS_NAME`
4. Run `bash tekton/setup.sh` — deploys everything including ChatOps
5. Copy the Route URL from task output and paste it into your Slack App slash command settings

For local testing when the cluster isn't internet-accessible:
```bash
ROUTE=$(oc get route slack-chatops -n infra -o jsonpath='{.spec.host}')
ngrok http https://${ROUTE}
# use the ngrok URL in Slack slash command settings
```

## Slash Commands

| Command | Args | What it does |
|---------|------|-------------|
| `status` | `<name> <ns>` | Pods, services, PVCs, route for a team |
| `add-kafka` | `<name> <ns>` | Deploy Kafka for a team |
| `add-nifi` | `<name> <ns> <pwd>` | Deploy NiFi for a team (waits until Ready) |
| `remove-team` | `<name> <ns>` | Remove Kafka + NiFi |
| `remove-kafka` | `<name> <ns>` | Remove only Kafka |
| `remove-nifi` | `<name> <ns>` | Remove only NiFi |
| `wipe-kafka-data` | `<name> <ns>` | Delete Kafka PVC, restart fresh |
| `restart-kafka` | `<name> <ns>` | Restart Kafka pod |
| `restart-nifi` | `<name> <ns>` | Restart NiFi pod |
| `pause-events` | — | Scale event generator to 0 |
| `resume-events` | — | Scale event generator to 1 |
| `remove-events` | — | Delete entire event generator |
| `run-pipeline` | — | Trigger deploy-all-teams pipeline |
| `run-reset` | — | Trigger reset-and-deploy pipeline |
| `pipeline-status` | — | Show last 5 PipelineRuns |
| `cleanup-runs` | — | Delete old PipelineRuns (keep newest 3) |

Admin channel gets all commands. Any other channel only gets `status`.

## Troubleshoot

- **No response from Slack** — check pod logs: `oc logs -f deployment/slack-chatops -n infra`
- **403 Forbidden** — signing-secret mismatch; re-create Secret with correct value
- **`add-nifi` hangs** — NiFi image pull takes 10-15 min on first run; wait or check pod events
- **`NIFI_IMAGE not set`** — add `NIFI_IMAGE` env var to `chatops/k8s/03-deployment.yaml` and redeploy
