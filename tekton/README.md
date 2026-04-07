# Tekton — Kafka + Event Generator Deployment

Automates Kafka and event generator deployments on OpenShift. All config comes from `config.env`.

## Quick Start

```bash
# 1. Copy and fill in config.env (if you haven't already)
cp config.env.example config.env
# Edit config.env — set INFRA_NAMESPACE, GIT_REPO_URL, EXTERNAL_DOMAIN, STORAGE_CLASS

# 2. Run the setup script (full setup: RBAC + tasks + pipelines + PipelineRun)
bash tekton/setup.sh

# 3. Watch the pipeline
tkn pipelinerun logs --last -f -n <your-infra-namespace>
```

**Flags:**

| Flag | Purpose |
|------|---------|
| *(none)* | Full setup: RBAC + tasks + pipelines + run |
| `--skip-rbac` | Skip RBAC step (already applied) |
| `--skip-tasks` | Skip tasks/pipelines step (already applied) |
| `--run-only` | Only submit the PipelineRun |
| `--reset` | Use `run-reset-all-teams.yaml` instead of `run-all-teams.yaml` |
| `--dry-run` | Print all commands without executing |

The script auto-detects your cluster type (dedicated vs shared/NERC) and chooses the correct RBAC path. It also auto-increments the PipelineRun name (`run-001` → `run-002` → ...) so you never hit name collisions.

**Teardown:**

```bash
bash tekton/cleanup.sh
# With namespace deletion (self-provisioned clusters only):
DELETE_NAMESPACES=true bash tekton/cleanup.sh
```

---

**Manual setup — step by step:**

1. Verify prerequisites
2. Apply RBAC (service account + permissions)
3. Apply tasks and pipelines
4. Configure teams in `config.env`
5. Run the pipeline
6. Verify deployment

Day-2 operations (add a team, update event generator, health checks, cleanup) follow after the initial setup.

---

## Directory Structure

```
tekton/
├── rbac/
│   ├── 01-serviceaccount.yaml          optional: custom SA (not needed on most clusters)
│   ├── 02-clusterrolebinding.yaml      cross-namespace permissions (dedicated cluster, cluster-admin only)
│   ├── 03-rolebinding-per-team.yaml    per-namespace permissions (dedicated cluster, namespace-admin)
│   └── 04-role-rolebinding-namespace.yaml  Role + RoleBinding for shared clusters (e.g. NERC)
│
├── tasks/
│   ├── 01-task-deploy-kafka.yaml       deploy/redeploy Kafka for one team
│   ├── 02-task-deploy-event-generator.yaml  deploy/update event generator
│   ├── 03-task-verify-health.yaml      check Kafka + NiFi + event generator status
│   ├── 04-task-teardown-all.yaml       delete Kafka + NiFi + event generator (used by reset pipeline)
│   └── 05-task-deploy-nifi.yaml        deploy/redeploy NiFi for one team
│
├── pipelines/
│   ├── 01-pipeline-deploy-all-teams.yaml   N teams in parallel + event generator
│   └── 02-pipeline-reset-and-deploy.yaml   teardown everything, then full redeploy
│
└── runs/
    ├── run-all-teams.yaml              PipelineRun: initial classroom deploy
    ├── run-reset-all-teams.yaml        PipelineRun: teardown + full redeploy
    ├── taskrun-deploy-kafka.yaml       TaskRun: one team's Kafka
    ├── taskrun-deploy-nifi.yaml        TaskRun: one team's NiFi
    ├── taskrun-deploy-event-gen.yaml   TaskRun: event generator
    └── taskrun-verify-health.yaml      TaskRun: health check (no workspace)
```

Pipeline flow: `clone-repo` → Kafka per team (parallel) → NiFi per team (parallel, per-team chain) → `deploy-event-generator` → `verify-health` per team (parallel)

---

## Step 1 — Verify Prerequisites

Run these checks before touching anything else:

```bash
# Logged into OpenShift
oc whoami

# OpenShift Pipelines operator is installed
oc get pods -n openshift-pipelines | grep tekton
# If nothing shows: oc get csv -n openshift-operators | grep pipelines

# git-clone Task is available (newer clusters use Tasks, not ClusterTasks)
oc get tasks -n openshift-pipelines | grep git-clone
# Older clusters: oc get clustertask git-clone
```

Create namespaces — Tekton will not create them:

```bash
oc new-project infra
oc new-project team-01
oc new-project team-02
# repeat for each team
```

---

## Step 2 — Apply RBAC

OpenShift Pipelines automatically creates a `pipeline` ServiceAccount in every namespace.
The pipeline runs as this SA — no custom SA setup needed.

Check your cluster type first:

```bash
oc auth can-i create clusterrolebindings
# "yes" → dedicated cluster   (use option A)
# "no"  → shared cluster      (use option B)
```

**Option A — Dedicated cluster (cluster-admin):**

```bash
source config.env && envsubst '${INFRA_NAMESPACE} ${TEKTON_SA_NAME}' \
  < tekton/rbac/02-clusterrolebinding.yaml | oc apply -f -
```

**Option B — Shared cluster (e.g. NERC) — no ClusterRoleBinding allowed:**

Apply a namespace-scoped Role + RoleBinding to every namespace the pipeline touches
(infra namespace + all team namespaces):

```bash
source config.env
for ns in ${INFRA_NAMESPACE} \
  ${TEAM1_NAMESPACE}  ${TEAM2_NAMESPACE}  ${TEAM3_NAMESPACE} \
  ${TEAM4_NAMESPACE}  ${TEAM5_NAMESPACE}  ${TEAM6_NAMESPACE} \
  ${TEAM7_NAMESPACE}  ${TEAM8_NAMESPACE}  ${TEAM9_NAMESPACE} \
  ${TEAM10_NAMESPACE} ${TEAM11_NAMESPACE} ${TEAM12_NAMESPACE} \
  ${TEAM13_NAMESPACE} ${TEAM14_NAMESPACE} ${TEAM15_NAMESPACE}; do
  [[ "$ns" == "skip" ]] && continue
  envsubst '${INFRA_NAMESPACE}' \
    < tekton/rbac/04-role-rolebinding-namespace.yaml | oc apply -f - -n $ns
done
```

Add each new team namespace to this loop whenever a team is provisioned.

**Verify permissions are in place before continuing:**

```bash
# pipeline SA can deploy into a team namespace
oc auth can-i create statefulsets -n ${TEAM_NAMESPACE} \
  --as=system:serviceaccount:${INFRA_NAMESPACE}:pipeline
# Expected: yes
```

---

## Step 3 — Apply Tasks and Pipelines

```bash
# IMPORTANT: source config.env && must be on the same line as envsubst
# Running source separately then envsubst in the next command can cause
# variables to not be substituted (shell scope issue).

source config.env && envsubst '${INFRA_NAMESPACE} ${OC_CLI_IMAGE}' < tekton/tasks/01-task-deploy-kafka.yaml          | oc apply -f -
source config.env && envsubst '${INFRA_NAMESPACE} ${OC_CLI_IMAGE}' < tekton/tasks/02-task-deploy-event-generator.yaml | oc apply -f -
source config.env && envsubst '${INFRA_NAMESPACE} ${OC_CLI_IMAGE}' < tekton/tasks/03-task-verify-health.yaml          | oc apply -f -
source config.env && envsubst '${INFRA_NAMESPACE} ${OC_CLI_IMAGE}' < tekton/tasks/04-task-teardown-all.yaml           | oc apply -f -
source config.env && envsubst '${INFRA_NAMESPACE} ${OC_CLI_IMAGE}' < tekton/tasks/05-task-deploy-nifi.yaml            | oc apply -f -

source config.env && envsubst '${INFRA_NAMESPACE}' < tekton/pipelines/01-pipeline-deploy-all-teams.yaml | oc apply -f -
source config.env && envsubst '${INFRA_NAMESPACE}' < tekton/pipelines/02-pipeline-reset-and-deploy.yaml | oc apply -f -

# Verify all objects are registered
# Note: use pipelines.tekton.dev to avoid collision with Kubeflow's "pipeline" resource
oc get tasks -n ${INFRA_NAMESPACE}
oc get pipelines.tekton.dev -n ${INFRA_NAMESPACE}
```

---

## Step 4 — Configure Teams in config.env

Set active teams and mark unused slots as `"skip"`:

```bash
export TEAM1_NAME=team01
export TEAM1_NAMESPACE=team-01
export TEAM2_NAME=team02
export TEAM2_NAMESPACE=team-02
export TEAM3_NAME=skip          # not deploying
export TEAM3_NAMESPACE=skip

export TEAM_BOOTSTRAP_SERVERS="\
team01=kafka-team01.team-01.svc.cluster.local:9092,\
team02=kafka-team02.team-02.svc.cluster.local:9092"
```

---

## Step 5 — Run the Pipeline

### Initial Deploy — All Teams

Deploys Kafka for every active team in parallel, then the event generator once with all bootstrap servers.

```bash
# First run
source config.env && envsubst < tekton/runs/run-all-teams.yaml | sed 's/run-001/run-001/' | oc create -f -

# Each subsequent run — increment the suffix to avoid name collision
source config.env && envsubst < tekton/runs/run-all-teams.yaml | sed 's/run-001/run-002/' | oc create -f -
```

Watch progress:
```bash
tkn pipelinerun logs --last -f -n ${INFRA_NAMESPACE}
# or without tkn:
oc get pipelinerun -n ${INFRA_NAMESPACE}
```

Pipeline flow: `clone-repo` → Kafka per team (parallel) → NiFi per team (per-team chain) → `deploy-event-generator` → `verify-health` per team (parallel)

---

## Step 6 — Verify Deployment

After the pipeline completes, confirm everything is running:

```bash
# Kafka pod, service, and PVC for each team
oc get pods,svc,pvc -n ${TEAM_NAMESPACE} -l component=kafka

# Event generator pod
oc get pods -n ${INFRA_NAMESPACE} -l app=${EVENT_GENERATOR_NAME}

# Check events are flowing (consume 5 messages)
oc run kafka-consumer --rm -it \
  --image=confluentinc/cp-kafka:7.5.0 \
  -n ${TEAM1_NAMESPACE} -- \
  kafka-console-consumer \
  --bootstrap-server kafka-${TEAM1_NAME}.${TEAM1_NAMESPACE}.svc.cluster.local:9092 \
  --topic ${TOPIC_PREFIX}${TEAM1_NAME}${TOPIC_SUFFIX} \
  --max-messages 5
```

---

## Step 7 — Save the Workspace PVC (for TaskRuns)

After the first PipelineRun completes, find the workspace PVC it created and save it to `config.env`:

```bash
oc get pvc -n ${INFRA_NAMESPACE} | grep shared-data
# e.g.: shared-data-deploy-all-teams-run-001

# Add to config.env:
export TEKTON_WORKSPACE_PVC="shared-data-deploy-all-teams-run-001"
```

All TaskRun files read `TEKTON_WORKSPACE_PVC` — set it once, reuse for every standalone TaskRun.

---

## Day-2 Operations

### Reset and Redeploy — All Teams

For most config changes, re-running the initial deploy pipeline is enough — all tasks are idempotent and safe to rerun on a healthy deployment.

Use the reset pipeline only when you need a true clean slate:

- **Broken StatefulSets** — if a Kafka pod is stuck (`CrashLoopBackOff`, `Pending`), `oc apply` patches the spec but won't recreate the broken resource. Teardown forces a clean delete + recreate.
- **Stale PVC data** — `oc apply` never deletes PVCs. If you want Kafka to start with a fresh disk (e.g. resetting a classroom between semesters), teardown explicitly deletes the PVCs.
- **Immutable StatefulSet fields** — fields like `volumeClaimTemplates` and storage size cannot be patched in-place. `oc apply` will error; delete + recreate is the only fix.

Tears down all Kafka and the event generator, then redeploys everything fresh.

The teardown derives which namespaces to wipe from `TEAM1_NAMESPACE` through `TEAM15_NAMESPACE` — any set to `skip` are bypassed automatically. No separate list needed.

```bash
source config.env && envsubst < tekton/runs/run-reset-all-teams.yaml | oc create -f -
tkn pipelinerun logs --last -f -n ${INFRA_NAMESPACE}
```

Pipeline flow: `teardown-all` → `clone-repo` → Kafka per team (parallel) → NiFi per team (per-team chain) → `deploy-event-generator` → `verify-health` per team (parallel)

---

### Deploy / Redeploy One Team's Kafka

Use when adding a new team or recovering a crashed Kafka pod. Tasks are idempotent — safe to rerun on a healthy deployment.

```bash
source config.env && \
envsubst '${TEAM_NAME} ${TEAM_NAMESPACE} ${INFRA_NAMESPACE} ${STORAGE_CLASS} ${TEKTON_WORKSPACE_PVC}' \
  < tekton/runs/taskrun-deploy-kafka.yaml | oc create -f -

tkn taskrun logs --last -f -n ${INFRA_NAMESPACE}
```

If adding a new team, run the NiFi TaskRun and then the event generator TaskRun next.

---

### Deploy / Redeploy One Team's NiFi

Use when adding a new team's NiFi or recovering a crashed NiFi pod. Set `TEAM_PASSWORD` in `config.env` to this team's password before running.

> **Note:** NiFi startup takes 3-5 minutes (keytool init + NiFi startup). `wait-for-nifi` uses a 600s timeout.

```bash
source config.env && \
envsubst '${TEAM_NAME} ${TEAM_NAMESPACE} ${INFRA_NAMESPACE} ${NIFI_IMAGE} ${TEAM_PASSWORD} ${STORAGE_CLASS} ${EXTERNAL_DOMAIN} ${TEKTON_WORKSPACE_PVC}' \
  < tekton/runs/taskrun-deploy-nifi.yaml | oc create -f -

tkn taskrun logs --last -f -n ${INFRA_NAMESPACE}
```

Access NiFi UI after the TaskRun completes:
```bash
# URL printed in task output (hostname auto-generated by OpenShift):
# https://nifi-${TEAM_NAME}-${TEAM_NAMESPACE}.${EXTERNAL_DOMAIN}/nifi
# Username: ${TEAM_NAME}  Password: ${TEAM_PASSWORD}
oc get route nifi-${TEAM_NAME} -n ${TEAM_NAMESPACE}
```

---

### Deploy / Update Event Generator

Use after adding a new team, changing event rate, topic names, regions, or any config. Update `TEAM_BOOTSTRAP_SERVERS` in `config.env` first.

The task applies the ConfigMap with your new values and runs `oc rollout restart` automatically — no teardown needed.

```bash
source config.env && \
envsubst '${INFRA_NAMESPACE} ${EVENT_GENERATOR_NAME} ${GIT_REPO_URL} ${GIT_BRANCH} ${TEAM_BOOTSTRAP_SERVERS} ${EVENT_RATE_PER_SEC} ${RATE_PER_TEAM} ${TOPIC_PREFIX} ${TOPIC_SUFFIX} ${EVENT_STREAMS} ${REGIONS} ${TEKTON_WORKSPACE_PVC}' \
  < tekton/runs/taskrun-deploy-event-gen.yaml | oc create -f -

tkn taskrun logs --last -f -n ${INFRA_NAMESPACE}
```

---

### Verify Health — Standalone

Both pipelines run `verify-health` automatically as the final step. For a manual check (e.g. after a standalone Kafka redeploy), run it directly:

```bash
source config.env && \
envsubst '${TEAM_NAME} ${TEAM_NAMESPACE} ${INFRA_NAMESPACE} ${EVENT_GENERATOR_NAME}' \
  < tekton/runs/taskrun-verify-health.yaml | oc create -f -

tkn taskrun logs --last -f -n ${INFRA_NAMESPACE}
```

---

### Managing Run History

Tekton stores every run as a Kubernetes object. Clean up periodically to avoid accumulation:

```bash
tkn pipelinerun delete --keep 2 -n ${INFRA_NAMESPACE}
tkn taskrun delete --keep 5 -n ${INFRA_NAMESPACE}
```

**Naming notes:**
- TaskRun files use `generateName` — no name collisions, no incrementing needed.
- PipelineRun files use a fixed name with a suffix (`run-001`). Increment the suffix before each new execution, or delete the old run first.

---

## Cleanup

### Prerequisites

Before running any cleanup commands, verify you are on the right cluster:

```bash
# Confirm tkn CLI is installed
tkn version
# If not installed:
#   Mac:         brew install tektoncd/tools/tektoncd-cli
#   RHEL/Fedora: sudo dnf install tektoncd-cli
#   Other:       https://tekton.dev/docs/cli/#installation

# Confirm you are logged into the correct cluster
oc whoami
oc project   # sanity-check before deleting anything

# Source config.env — all commands below require it
# Run from repo root (same directory as config.env)
source config.env
```

---

### Remove One Team

Update `TEAM_BOOTSTRAP_SERVERS` in `config.env` first to remove the team, then:

```bash
source config.env

# Remove Kafka resources (scoped to this team's exact deployment name)
oc delete statefulset,svc,pvc \
  -l "app=kafka-${TEAM_NAME}" \
  -n ${TEAM_NAMESPACE} --ignore-not-found

# Remove NiFi resources
oc delete statefulset,svc,pvc \
  -l "app=nifi-${TEAM_NAME}" \
  -n ${TEAM_NAMESPACE} --ignore-not-found
oc delete route \
  -l "app=nifi-${TEAM_NAME}" \
  -n ${TEAM_NAMESPACE} --ignore-not-found
oc delete networkpolicy allow-from-openshift-ingress \
  -n ${TEAM_NAMESPACE} --ignore-not-found

# Update event generator bootstrap servers and restart
oc set env deployment/${EVENT_GENERATOR_NAME} \
  TEAM_BOOTSTRAP_SERVERS="${TEAM_BOOTSTRAP_SERVERS}" \
  -n ${INFRA_NAMESPACE}
oc rollout restart deployment/${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}
```

---

### Remove All Teams

```bash
source config.env

# Remove Kafka from every active team namespace
for i in $(seq 1 15); do
  ns_var="TEAM${i}_NAMESPACE"; name_var="TEAM${i}_NAME"
  ns="${!ns_var:-skip}";       name="${!name_var:-skip}"
  [[ "$ns" == "skip" ]] && continue
  oc delete statefulset,svc,pvc \
    -l "app=kafka-${name}" \
    -n "${ns}" --ignore-not-found
done

# Remove NiFi from every active team namespace
for i in $(seq 1 15); do
  ns_var="TEAM${i}_NAMESPACE"; name_var="TEAM${i}_NAME"
  ns="${!ns_var:-skip}";       name="${!name_var:-skip}"
  [[ "$ns" == "skip" ]] && continue
  oc delete statefulset,svc,pvc \
    -l "app=nifi-${name}" \
    -n "${ns}" --ignore-not-found
  oc delete route \
    -l "app=nifi-${name}" \
    -n "${ns}" --ignore-not-found
  oc delete networkpolicy allow-from-openshift-ingress \
    -n "${ns}" --ignore-not-found
done

# Stop the event generator
oc scale deployment/${EVENT_GENERATOR_NAME} --replicas=0 -n ${INFRA_NAMESPACE}
```

---

### Full Teardown — Remove Everything

**Quickest option — single command:**

```bash
bash tekton/cleanup.sh
# With namespace deletion (self-provisioned clusters only):
# DELETE_NAMESPACES=true bash tekton/cleanup.sh
```

**Or run each step manually in order** — order matters, do not rearrange:

```bash
source config.env

# Step 1 — Cancel any in-flight pipeline and task runs first
tkn pipelinerun delete --all --force -n ${INFRA_NAMESPACE}
tkn taskrun delete --all --force -n ${INFRA_NAMESPACE}

# Step 2 — Delete event generator
# Deployment pinned by name; other resource types use label (no fixed name pattern)
oc delete deployment/${EVENT_GENERATOR_NAME} \
  -n ${INFRA_NAMESPACE} --ignore-not-found
oc delete svc,configmap,buildconfig,imagestream \
  -l "app=${EVENT_GENERATOR_NAME}" \
  -n ${INFRA_NAMESPACE} --ignore-not-found

# Step 3 — Delete Kafka from every active team namespace
for i in $(seq 1 15); do
  ns_var="TEAM${i}_NAMESPACE"; name_var="TEAM${i}_NAME"
  ns="${!ns_var:-skip}";       name="${!name_var:-skip}"
  [[ "$ns" == "skip" ]] && continue
  oc delete statefulset,svc,pvc \
    -l "app=kafka-${name}" \
    -n "${ns}" --ignore-not-found
done

# Step 3b — Delete NiFi from every active team namespace
for i in $(seq 1 15); do
  ns_var="TEAM${i}_NAMESPACE"; name_var="TEAM${i}_NAME"
  ns="${!ns_var:-skip}";       name="${!name_var:-skip}"
  [[ "$ns" == "skip" ]] && continue
  oc delete statefulset,svc,pvc \
    -l "app=nifi-${name}" \
    -n "${ns}" --ignore-not-found
  oc delete route \
    -l "app=nifi-${name}" \
    -n "${ns}" --ignore-not-found
  oc delete networkpolicy allow-from-openshift-ingress \
    -n "${ns}" --ignore-not-found
done

# Step 4 — Delete Tekton tasks and pipelines (by name, not --all)
oc delete task \
  deploy-kafka deploy-event-generator verify-health teardown-all deploy-nifi \
  -n ${INFRA_NAMESPACE} --ignore-not-found
oc delete pipeline.tekton.dev \
  deploy-all-teams reset-and-deploy \
  -n ${INFRA_NAMESPACE} --ignore-not-found

# Step 5 — Delete workspace PVCs
oc delete pvc \
  -l "tekton.dev/pipeline=deploy-all-teams" \
  -n ${INFRA_NAMESPACE} --ignore-not-found

# Step 6 — Delete RBAC from every team namespace
#           (covers both 03-rolebinding-per-team.yaml and 04-role-rolebinding-namespace.yaml)
for i in $(seq 1 15); do
  ns_var="TEAM${i}_NAMESPACE"
  ns="${!ns_var:-skip}"
  [[ "$ns" == "skip" ]] && continue
  oc delete role,rolebinding \
    -l "app=tekton-pipeline,component=rbac" \
    -n "${ns}" --ignore-not-found
done

# Step 7 — Delete RBAC from infra namespace (shared cluster only)
oc delete role,rolebinding \
  -l "app=tekton-pipeline,component=rbac" \
  -n ${INFRA_NAMESPACE} --ignore-not-found

# Step 8 — Delete cluster-scoped RBAC (dedicated cluster only)
#           Skip this block on shared clusters (NERC etc.) — you won't have permission
if oc auth can-i create clusterrolebindings; then
  oc delete clusterrolebinding pipeline-runner-binding --ignore-not-found
  oc delete clusterrole pipeline-runner-role --ignore-not-found
fi

# Step 9 — Delete namespaces (self-provisioned clusters only)
#           Skip on NERC or any cluster where namespaces were provisioned for you
# oc delete namespace ${INFRA_NAMESPACE} ${TEAM1_NAMESPACE} ${TEAM2_NAMESPACE}
```

**What is NOT deleted:**
- Namespaces — comment out Step 9 block above if you own them, or use `DELETE_NAMESPACES=true` with the script
- The default `pipeline` ServiceAccount — created and managed by the OpenShift Pipelines operator

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

### "git-clone ClusterTask not found" / cluster resolver error

Newer OpenShift Pipelines (1.14+) removed ClusterTasks. The pipelines in this repo
already use the `cluster` resolver pointing to `openshift-pipelines` namespace.

If you see a resolver error, verify the task exists:
```bash
oc get tasks -n openshift-pipelines | grep git-clone
```

The pipelines reference `git-clone` via:
```yaml
taskRef:
  resolver: cluster
  params:
    - name: kind
      value: task
    - name: name
      value: git-clone
    - name: namespace
      value: openshift-pipelines
```

### "Kafka pod not found" in wait step

The `oc wait` command needs the pod to exist before it can watch it. If Kafka takes too long to schedule, increase `--timeout=300s` to `--timeout=600s` in `tekton/tasks/01-task-deploy-kafka.yaml`.

### RBAC errors: "cannot create statefulsets"

```bash
oc auth can-i create statefulsets -n team-01 \
  --as=system:serviceaccount:${INFRA_NAMESPACE}:pipeline
```

If denied, re-apply the RBAC file for that namespace (see Step 2).

### ExceededResourceQuota — deploy-event-generator stuck

On shared clusters (e.g. NERC), the default LimitRange applies **4Gi memory per container**.
The event-generator task has 3 steps = 12Gi requested. If your namespace quota is tight,
delete old pipeline runs to free memory:

```bash
# Check quota usage
oc describe resourcequota -n ${INFRA_NAMESPACE}

# Delete old runs (keeps history clean too)
oc delete pipelinerun <run-name-001> <run-name-002> -n ${INFRA_NAMESPACE}
```

All task steps in this repo explicitly set `computeResources: limits: memory: 256Mi`
to stay well within quota.

If denied, re-apply the appropriate RBAC file (see Step 2).

### Workspace PVC not found (TaskRun fails immediately)

`TEKTON_WORKSPACE_PVC` in `config.env` is either empty or points to a deleted PVC. Find a valid one:

```bash
oc get pvc -n ${INFRA_NAMESPACE} | grep shared-data
```

Update `config.env` with the correct PVC name.

---

## tkn Quick Reference

```bash
# List pipelines and tasks
tkn pipeline list -n ${INFRA_NAMESPACE}
tkn task list -n ${INFRA_NAMESPACE}

# List and follow runs
tkn pipelinerun list -n ${INFRA_NAMESPACE}
tkn pipelinerun logs --last -f -n ${INFRA_NAMESPACE}
tkn taskrun logs --last -f -n ${INFRA_NAMESPACE}

# Describe a run (shows per-task status)
tkn pipelinerun describe --last -n ${INFRA_NAMESPACE}

# Delete old runs
tkn pipelinerun delete --keep 2 -n ${INFRA_NAMESPACE}
tkn taskrun delete --keep 5 -n ${INFRA_NAMESPACE}

# Start a task manually for testing (no run file needed)
tkn task start deploy-kafka -n ${INFRA_NAMESPACE} \
  --param team-name=team01 \
  --param team-namespace=team-01 \
  --workspace name=source,emptyDir="" \
  --showlog
```
