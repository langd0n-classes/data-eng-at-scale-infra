# Pipeline — Classroom Provisioning and Day-to-Day Operations

Two tools for operating the classroom infrastructure:

| Tool | When to use | How it works | Speed |
|------|-------------|--------------|-------|
| `setup.sh` | Before class — provision all teams | Submits a Tekton PipelineRun that runs inside the cluster. Fire-and-forget, parallel per-team. | ~20 min (NiFi image pull) |
| `ops.sh` | During class — fix one team right now | Runs raw `oc` commands directly. No Tekton dependency. Works even if Tekton is broken. | 2-10 sec |

**ChatOps** (Slack `/infra` command) exposes `ops.sh`-equivalent operations without needing a terminal — see [chatops/README.md](../chatops/README.md).

---

## Prerequisites

Run `onboarding/apply-onboarding.sh` first — namespaces, quotas, LimitRanges, and RBAC groups must exist before Tekton or ops.sh can deploy into them.

```bash
# Verify OpenShift Pipelines operator is installed
oc get pods -n openshift-pipelines | grep tekton

# Verify git-clone Task is available
oc get tasks -n openshift-pipelines | grep git-clone

# Source config before any command
source config.env
```

---

## setup.sh — Full Classroom Provisioning

Deploys Kafka + NiFi for every configured team in parallel, then the shared event generator, then ChatOps. Submit it and close your laptop — it runs entirely in-cluster.

```bash
source config.env
bash pipeline/setup.sh
```

### Flags

| Flag | Purpose |
|------|---------|
| *(none)* | Full setup: RBAC + tasks + pipelines + PipelineRun |
| `--skip-rbac` | Skip RBAC (already applied) |
| `--skip-tasks` | Skip tasks/pipelines (already applied) |
| `--run-only` | Only submit the PipelineRun (RBAC and tasks already in place) |
| `--reset` | Submit reset-and-deploy pipeline (teardown → full redeploy) |
| `--dry-run` | Print all commands without executing |

### Pipeline Flow

```
clone-repo
  ├── deploy-kafka (team01)  ─┐
  ├── deploy-kafka (team02)   │  parallel per team
  └── deploy-kafka (teamN)  ──┤
       ↓                      │
  deploy-nifi (per team, sequential within each team)
       ↓
  deploy-event-generator  (once, all bootstrap servers)
       ↓
  deploy-chatops          (parallel, when CHATOPS_ENABLED=true)
       ↓
  verify-health (per team, parallel)
```

### Watch Progress

```bash
tkn pipelinerun logs --last -f -n ${INFRA_NAMESPACE}
# or without tkn:
oc get pipelinerun -n ${INFRA_NAMESPACE} -w
```

### After First Run — Save Workspace PVC

```bash
oc get pvc -n ${INFRA_NAMESPACE} | grep shared-data
# e.g.: shared-data-deploy-all-teams-run-001

# Add to config.env:
export TEKTON_WORKSPACE_PVC="shared-data-deploy-all-teams-run-001"
```

Required for standalone TaskRuns (see Day-2 below).

---

## ops.sh — Day-to-Day Classroom Operations

Direct `oc` commands — no Tekton involved. Immediate results. Use this during class when you need to fix something right now.

```bash
source config.env
bash pipeline/ops.sh help      # list all commands
```

Supports `--dry-run` (print commands without running) and `FORCE=true` (skip confirmation prompts for destructive commands).

### Team Lifecycle

```bash
# Deploy Kafka + NiFi for a new team
bash pipeline/ops.sh add-team team04 team-04 "SecurePass2026!!"

# Deploy only Kafka
bash pipeline/ops.sh add-kafka team04 team-04

# Deploy only NiFi
bash pipeline/ops.sh add-nifi team04 team-04 "SecurePass2026!!"

# Remove Kafka + NiFi (namespace kept)
bash pipeline/ops.sh remove-team team04 team-04

# Remove + redeploy (fresh start for one team)
bash pipeline/ops.sh reset-team team04 team-04 "SecurePass2026!!"
```

### Day-2 Per-Team Operations

```bash
# Show pods, services, PVCs, routes, and recent events
bash pipeline/ops.sh status team01 team-01

# Reset NiFi password
bash pipeline/ops.sh reset-password team01 team-01 "NewPass2026!!"

# Restart Kafka or NiFi pod
bash pipeline/ops.sh restart-kafka team01 team-01
bash pipeline/ops.sh restart-nifi team01 team-01

# Wipe Kafka data (scale to 0, delete PVC, scale back to 1)
bash pipeline/ops.sh wipe-kafka-data team01 team-01

# Force redeploy NiFi (clear SHA annotation + reapply resources)
bash pipeline/ops.sh force-update-nifi team01 team-01 "SecurePass2026!!"

# Remove only Kafka or only NiFi
bash pipeline/ops.sh remove-kafka team01 team-01
bash pipeline/ops.sh remove-nifi team01 team-01
```

### Event Generator

```bash
bash pipeline/ops.sh pause-events      # scale to 0 (stop sending events)
bash pipeline/ops.sh resume-events     # scale back to 1
bash pipeline/ops.sh remove-events     # delete entire event generator
```

### Bulk / Teardown Operations

```bash
# Overview of every namespace (infra + all teams)
bash pipeline/ops.sh status-all

# Remove Kafka + NiFi from all configured teams
bash pipeline/ops.sh remove-all-teams

# Cancel runs → remove events → remove all teams
bash pipeline/ops.sh teardown-all

# teardown-all + delete PipelineRun/TaskRun history and workspace PVCs
WIPE_PVCS=true bash pipeline/ops.sh teardown-all
# or with --clean flag:
bash pipeline/ops.sh teardown-all --clean

# teardown-all (with run cancellation) + re-run setup pipeline
bash pipeline/ops.sh reset-all

# Keep 3 PipelineRuns + 5 TaskRuns, delete older ones
bash pipeline/ops.sh cleanup-runs
```

---

## Day-2 via Tekton TaskRuns

For operations that benefit from in-cluster execution (slow network, large image push):

### Redeploy One Team's Kafka

```bash
source config.env
envsubst '${TEAM_NAME} ${TEAM_NAMESPACE} ${INFRA_NAMESPACE} ${STORAGE_CLASS} ${TEKTON_WORKSPACE_PVC}' \
  < pipeline/runs/taskrun-deploy-kafka.yaml | oc create -f -
tkn taskrun logs --last -f -n ${INFRA_NAMESPACE}
```

### Redeploy One Team's NiFi

Set `TEAM_PASSWORD` in `config.env` first:

```bash
source config.env
envsubst '${TEAM_NAME} ${TEAM_NAMESPACE} ${INFRA_NAMESPACE} ${NIFI_IMAGE} ${TEAM_PASSWORD} ${STORAGE_CLASS} ${EXTERNAL_DOMAIN} ${TEKTON_WORKSPACE_PVC}' \
  < pipeline/runs/taskrun-deploy-nifi.yaml | oc create -f -
tkn taskrun logs --last -f -n ${INFRA_NAMESPACE}
```

### Redeploy Event Generator

```bash
source config.env
envsubst '${INFRA_NAMESPACE} ${EVENT_GENERATOR_NAME} ${GIT_REPO_URL} ${GIT_BRANCH} ${TEAM_BOOTSTRAP_SERVERS} ${EVENT_RATE_PER_SEC} ${TOPIC_PREFIX} ${TOPIC_SUFFIX} ${REGIONS} ${TEKTON_WORKSPACE_PVC}' \
  < pipeline/runs/taskrun-deploy-event-gen.yaml | oc create -f -
tkn taskrun logs --last -f -n ${INFRA_NAMESPACE}
```

### Full Reset Pipeline

```bash
source config.env
envsubst < pipeline/runs/run-reset-all-teams.yaml | oc create -f -
tkn pipelinerun logs --last -f -n ${INFRA_NAMESPACE}
```

---

## Directory Structure

```
pipeline/
├── setup.sh              Fire-and-forget provisioning: RBAC + tasks + PipelineRun
├── ops.sh                Day-to-day surgical fixes via raw oc (no Tekton)
├── lib/
│   └── common.sh         Shared helpers sourced by both setup.sh and ops.sh
├── rbac/
│   ├── 01-serviceaccount.yaml
│   ├── 02-clusterrolebinding.yaml       dedicated clusters (cluster-admin)
│   ├── 03-rolebinding-per-team.yaml     dedicated clusters (namespace-admin)
│   └── 04-role-rolebinding-namespace.yaml  shared clusters (NERC)
├── tasks/
│   ├── 01-task-deploy-kafka.yaml
│   ├── 02-task-deploy-event-generator.yaml
│   ├── 03-task-verify-health.yaml
│   ├── 04-task-teardown-all.yaml
│   ├── 05-task-deploy-nifi.yaml
│   └── 06-task-deploy-chatops.yaml
├── pipelines/
│   ├── 01-pipeline-deploy-all-teams.yaml
│   └── 02-pipeline-reset-and-deploy.yaml
└── runs/
    ├── run-all-teams.yaml
    ├── run-reset-all-teams.yaml
    ├── taskrun-deploy-kafka.yaml
    ├── taskrun-deploy-nifi.yaml
    ├── taskrun-deploy-event-gen.yaml
    └── taskrun-verify-health.yaml
```

---

## Troubleshooting

### Pipeline run stuck or failed

```bash
# See which task failed
tkn pipelinerun describe --last -n ${INFRA_NAMESPACE}

# Follow logs of the failed task
tkn taskrun logs --last -f -n ${INFRA_NAMESPACE}

# Raw pod logs
oc get pods -n ${INFRA_NAMESPACE} -l tekton.dev/pipelineRun=<run-name>
oc logs <pod-name> -n ${INFRA_NAMESPACE}
```

### ops.sh: "cannot create statefulsets" / permission denied

```bash
oc auth can-i create statefulsets -n team-01 \
  --as=system:serviceaccount:${INFRA_NAMESPACE}:pipeline
# If denied: re-apply RBAC for that namespace
source config.env && envsubst '${INFRA_NAMESPACE}' \
  < pipeline/rbac/04-role-rolebinding-namespace.yaml | oc apply -f - -n team-01
```

### "git-clone Task not found" / resolver error

```bash
oc get tasks -n openshift-pipelines | grep git-clone
```

Newer OpenShift Pipelines (1.14+) removed ClusterTasks. The pipelines use the `cluster` resolver pointing to `openshift-pipelines`. If the task is missing, check operator version and reinstall.

### ExceededResourceQuota — deploy-event-generator stuck

On shared clusters (NERC), delete old pipeline runs to free quota:

```bash
oc describe resourcequota -n ${INFRA_NAMESPACE}
tkn pipelinerun delete --keep 2 -n ${INFRA_NAMESPACE}
```

### Workspace PVC not found

```bash
oc get pvc -n ${INFRA_NAMESPACE} | grep shared-data
# Update TEKTON_WORKSPACE_PVC in config.env with the correct name
```

### NiFi pod stuck in Init state

The init container generates a keystore and copies NiFi config — it needs ~30 seconds. If stuck longer:

```bash
oc logs <nifi-pod> -c init-config -n <team-ns>
```

Common cause: PVC not yet bound (storage class slow to provision).

---

## tkn Quick Reference

```bash
tkn pipeline list -n ${INFRA_NAMESPACE}
tkn pipelinerun list -n ${INFRA_NAMESPACE}
tkn pipelinerun logs --last -f -n ${INFRA_NAMESPACE}
tkn pipelinerun describe --last -n ${INFRA_NAMESPACE}
tkn taskrun logs --last -f -n ${INFRA_NAMESPACE}
tkn pipelinerun delete --keep 2 -n ${INFRA_NAMESPACE}
tkn taskrun delete --keep 5 -n ${INFRA_NAMESPACE}
```
