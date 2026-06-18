# ChatOps

Slack slash command handler for classroom infrastructure operations. Deployed as
an always-on OpenShift Deployment in the `infra` namespace.

Instructors type `/infra <command>` in Slack instead of running `oc` or `bash ops.sh`
from a terminal. The admin channel gets all commands. Any other channel only gets `status`.

---

## One-Time Setup

### 1. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Under **Slash Commands** → **Create New Command**:
   - Command: `/infra`
   - Request URL: *(fill in after deploy — see step 5 below)*
   - Short Description: `Classroom infrastructure operations`
3. Under **Basic Information** → copy the **Signing Secret** (you'll need it next)

### 2. Create the Slack Credentials Secret

Run this **before** `pipeline/setup.sh`:

```bash
source config.env
oc create secret generic slack-credentials \
  --from-literal=signing-secret=<Signing Secret from Slack App Basic Info> \
  -n ${INFRA_NAMESPACE}
```

### 3. Configure `config.env`

```bash
export CHATOPS_ENABLED=true
export CHATOPS_NAME=slack-chatops
# Right-click your admin channel in Slack → Copy Channel ID
export ADMIN_CHANNEL_ID="C0XXXXXXXXX"
```

### 4. Deploy via Pipeline

```bash
bash pipeline/setup.sh
```

The `deploy-chatops` Tekton task runs automatically when `CHATOPS_ENABLED=true`.
Watch for the Route URL in task output:

```bash
tkn taskrun logs --last -f -n ${INFRA_NAMESPACE}
# Look for: Route: https://slack-chatops-infra.apps.your-cluster.example.com
```

Or retrieve it manually:

```bash
oc get route slack-chatops -n ${INFRA_NAMESPACE} -o jsonpath='{.spec.host}'
```

### 5. Wire Up Slack

Paste `https://<route-host>/slack/command` as the **Request URL** in your Slack App slash command settings.

Test it:
```
/infra help
```

---

## Rebuild After Code Changes

Use `ops.sh` to trigger a rebuild — it handles both online and offline clusters:

```bash
# Cluster has internet access (NERC) — pulls from GIT_REPO_URL / GIT_BRANCH
source config.env && bash pipeline/ops.sh rebuild-chatops

# Cluster is offline (CRC) — uploads local repo root as binary source
source config.env && bash pipeline/ops.sh rebuild-chatops --local
```

When the cluster can reach GitHub, a `git push` to the tracked branch also triggers a build automatically via the BuildConfig's ConfigChange trigger — no manual rebuild needed.

---

## Slash Command Reference

All commands are invoked as `/infra <command> [args]`.

Admin channel gets all commands. Any other channel only gets `status`.

### Status

| Command | Args | What it does |
|---------|------|-------------|
| `status` | `<name> <ns>` | Pods, services, PVCs, route for one team |
| `status-all` | — | Full cluster overview: all pods + PVCs in infra, pipeline runs, then every team namespace |

### Kafka / NiFi — Single Team

| Command | Args | What it does |
|---------|------|-------------|
| `add-kafka` | `<name> <ns>` | Deploy Kafka for a team |
| `add-nifi` | `<name> <ns> <pwd>` | Deploy NiFi — skips if StatefulSet already has a ready replica |
| `force-update-nifi` | `<name> <ns> <pwd>` | Force redeploy NiFi regardless of current state |
| `add-team` | `<name> <ns> <pwd>` | Deploy Kafka + NiFi together in sequence |
| `reset-team` | `<name> <ns> <pwd>` | Remove then redeploy Kafka + NiFi (fresh start for one team) |
| `remove-team` | `<name> <ns>` | Remove Kafka + NiFi |
| `remove-kafka` | `<name> <ns>` | Remove only Kafka |
| `remove-nifi` | `<name> <ns>` | Remove only NiFi |
| `wipe-kafka-data` | `<name> <ns>` | Scale to 0, delete PVC, scale back to 1 (fresh disk) |
| `restart-kafka` | `<name> <ns>` | Delete Kafka pod — StatefulSet restarts it |
| `restart-nifi` | `<name> <ns>` | Delete NiFi pod — StatefulSet restarts it |
| `reset-password` | `<name> <ns> <pwd>` | Patch StatefulSet env var + restart pod (min 12 chars) |

> **`add-nifi` vs `force-update-nifi`:** `add-nifi` checks if NiFi is already running and skips if healthy — use it when onboarding a new team. `force-update-nifi` always applies resources and redeploys — use it to push a config change to a running instance.

### Bulk Operations

| Command | Args | What it does |
|---------|------|-------------|
| `remove-all-teams` | — | Remove all StatefulSets, Services, PVCs, Routes, NetworkPolicies from every non-system namespace |
| `teardown-all` | — | Remove event generator + remove all teams |
| `teardown-all clean` | — | Cancel in-flight Tekton runs → remove events → remove all teams |
| `teardown-all wipe` | — | Cancel runs → delete all PipelineRun/TaskRun history + workspace PVCs → remove events → remove all teams *(ChatOps stays up)* |
| `reset-all` | — | `teardown-all clean` + trigger reset-and-deploy pipeline |

> **Choosing a teardown variant:**
> - End of class, all teams done → `teardown-all`
> - Pipeline stuck mid-run → `teardown-all clean` (cancels first)
> - Starting completely fresh, want clean Tekton history → `teardown-all wipe`
> - Between semesters, full reset → `reset-all`

### Event Generator

| Command | Args | What it does |
|---------|------|-------------|
| `pause-events` | — | Scale event generator to 0 replicas |
| `resume-events` | — | Scale event generator back to 1 replica |
| `remove-events` | — | Delete entire event generator (Deployment, Service, ConfigMap, BuildConfig, ImageStream) |

### Pipeline

| Command | Args | What it does |
|---------|------|-------------|
| `run-pipeline` | — | Trigger `deploy-all-teams` pipeline (inherits params from last successful run) |
| `run-reset` | — | Trigger `reset-and-deploy` pipeline |
| `pipeline-status` | — | Show last 5 PipelineRuns with status |
| `cleanup-runs` | — | Delete old PipelineRuns, keep newest 3 |

> **First run:** `run-pipeline` and `run-reset` read params from the last successful PipelineRun. They require at least one prior run via `bash pipeline/setup.sh`. They are day-2 operations.

---

## Common Workflows

### Before Class — Verify Everything Is Up

```
/infra status-all
```

Shows all pods in infra (event generator, chatops, Tekton), workspace PVCs, last pipeline runs, then every team namespace with pods/PVCs/routes.

### Add a New Team Mid-Semester

```
/infra add-team team04 team-04 SecurePass2026!!
```

Deploys Kafka, then NiFi (waits up to 25 min for NiFi image pull + startup). You'll get a reply when both are ready with the NiFi URL.

### Student Forgot Their NiFi Password

```
/infra reset-password team02 team-02 NewPass2026!!
```

Patches the StatefulSet env var and restarts the pod. NiFi is back in ~2 min.

### Kafka Is Stuck / CrashLoop

```
/infra restart-kafka team03 team-03
```

### NiFi Route Is Broken / Misconfigured

```
/infra force-update-nifi team01 team-01 SecurePass2026!!
```

Reapplies all NiFi resources (StatefulSet, Service, Route, NetworkPolicy) regardless of current state.

### End of Class — Stop Events

```
/infra pause-events
```

Resume next class:

```
/infra resume-events
```

### End of Semester — Full Teardown

```
/infra teardown-all wipe
```

Cancels any running pipelines, deletes all PipelineRun/TaskRun history and workspace PVCs, removes the event generator, removes Kafka + NiFi from all team namespaces. ChatOps stays up so you can run more commands.

---

## Troubleshoot

| Symptom | Cause | Fix |
|---------|-------|-----|
| No response from Slack | Pod crash or image pull | `oc logs -f deployment/slack-chatops -n infra` |
| `403 Forbidden` on every command | Signing secret mismatch | Recreate Secret with correct value from Slack App Basic Info |
| `add-nifi` says "already deployed" | NiFi is healthy, skip is intentional | Use `force-update-nifi` to override |
| `add-nifi` hangs | Image pull (3.5 GB) on first run | Wait 15-20 min or check pod events: `oc get events -n <ns>` |
| `NIFI_IMAGE not set` | Missing env var in Deployment | Add `NIFI_IMAGE` to `chatops/k8s/03-deployment.yaml` and redeploy |
| `teardown-all wipe` — ChatOps went down | ChatOps Deployment was included in wipe scope | Should not happen; check PVC protection logic in `_wipe_tekton_history` |
| Build fails "context not found" | Binary build uploaded wrong directory | Upload from repo root: `oc start-build slack-chatops --from-dir=. -n infra` |

```bash
# Check pod status
oc get pod -l app=slack-chatops -n infra

# Application logs
oc logs -f deployment/slack-chatops -n infra

# Verify Secret exists
oc get secret slack-credentials -n infra

# Check Route URL
oc get route slack-chatops -n infra -o jsonpath='{.spec.host}'
```
