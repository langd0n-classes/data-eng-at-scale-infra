# Tekton — Kafka + Event Generator Deployment

Automates Kafka and event generator deployments on OpenShift. All config comes from `config.env`.

**What you'll do — in order:**

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
│   ├── 01-serviceaccount.yaml          service account the pipeline runs as
│   ├── 02-clusterrolebinding.yaml      cross-namespace permissions (dedicated cluster)
│   └── 03-rolebinding-per-team.yaml    per-namespace permissions (shared cluster)
│
├── tasks/
│   ├── 01-task-deploy-kafka.yaml       deploy/redeploy Kafka for one team
│   ├── 02-task-deploy-event-generator.yaml  deploy/update event generator
│   ├── 03-task-verify-health.yaml      check pod status and logs
│   └── 04-task-teardown-all.yaml       delete Kafka + event generator (used by reset pipeline)
│
├── pipelines/
│   ├── 01-pipeline-deploy-all-teams.yaml   N teams in parallel + event generator
│   └── 02-pipeline-reset-and-deploy.yaml   teardown everything, then full redeploy
│
└── runs/
    ├── run-all-teams.yaml              PipelineRun: initial classroom deploy
    ├── run-reset-all-teams.yaml        PipelineRun: teardown + full redeploy
    ├── taskrun-deploy-kafka.yaml       TaskRun: one team's Kafka
    ├── taskrun-deploy-event-gen.yaml   TaskRun: event generator
    └── taskrun-verify-health.yaml      TaskRun: health check (no workspace)
```

---

## Step 1 — Verify Prerequisites

Run these checks before touching anything else:

```bash
# Logged into OpenShift
oc whoami

# OpenShift Pipelines operator is installed
oc get pods -n openshift-pipelines | grep tekton
# If nothing shows: oc get csv -n openshift-operators | grep pipelines

# git-clone ClusterTask is available
oc get clustertask git-clone
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

**Always apply the service account first:**

```bash
source config.env && envsubst '${INFRA_NAMESPACE} ${TEKTON_SA_NAME}' \
  < tekton/rbac/01-serviceaccount.yaml | oc apply -f -
```

Then choose **one** based on your cluster type:

**Dedicated cluster** — you have cluster-admin:

```bash
oc auth can-i create clusterrolebindings
# "yes" → use this option
```

```bash
source config.env && envsubst '${INFRA_NAMESPACE} ${TEKTON_SA_NAME}' \
  < tekton/rbac/02-clusterrolebinding.yaml | oc apply -f -
```

**Shared cluster (e.g. NERC)** — ClusterRoleBinding is not allowed. Apply a RoleBinding per team namespace instead:

```bash
for ns in team-01 team-02 team-03; do
  source config.env && envsubst '${INFRA_NAMESPACE} ${TEKTON_SA_NAME}' \
    < tekton/rbac/03-rolebinding-per-team.yaml | oc apply -f - -n $ns
done
```

Add each new team namespace to this loop whenever a team is provisioned.

**Verify permissions are in place before continuing:**

```bash
# ServiceAccount exists
oc get serviceaccount ${TEKTON_SA_NAME} -n ${INFRA_NAMESPACE}

# pipeline-runner can deploy into a team namespace
oc auth can-i create statefulsets -n ${TEAM_NAMESPACE} \
  --as=system:serviceaccount:${INFRA_NAMESPACE}:${TEKTON_SA_NAME}
# Expected: yes
```

---

## Step 3 — Apply Tasks and Pipelines

```bash
source config.env

envsubst '${INFRA_NAMESPACE} ${OC_CLI_IMAGE}' < tekton/tasks/01-task-deploy-kafka.yaml          | oc apply -f -
envsubst '${INFRA_NAMESPACE} ${OC_CLI_IMAGE}' < tekton/tasks/02-task-deploy-event-generator.yaml | oc apply -f -
envsubst '${INFRA_NAMESPACE} ${OC_CLI_IMAGE}' < tekton/tasks/03-task-verify-health.yaml          | oc apply -f -
envsubst '${INFRA_NAMESPACE} ${OC_CLI_IMAGE}' < tekton/tasks/04-task-teardown-all.yaml           | oc apply -f -

envsubst '${INFRA_NAMESPACE}' < tekton/pipelines/01-pipeline-deploy-all-teams.yaml | oc apply -f -
envsubst '${INFRA_NAMESPACE}' < tekton/pipelines/02-pipeline-reset-and-deploy.yaml | oc apply -f -

# Verify all objects are registered
oc get tasks,pipeline -n ${INFRA_NAMESPACE}
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
source config.env && envsubst < tekton/runs/run-all-teams.yaml | oc create -f -
tkn pipelinerun logs --last -f -n ${INFRA_NAMESPACE}
```

Pipeline flow: `clone-repo` → Kafka per team (parallel) → `deploy-event-generator` → `verify-health` per team (parallel)

---

## Step 6 — Verify Deployment

After the pipeline completes, confirm everything is running:

```bash
# Kafka pod, service, and PVC for each team
oc get pods,svc,pvc -n team-01 -l component=kafka

# Event generator pod
oc get pods -n infra -l app=event-generator

# Check events are flowing (consume 5 messages)
oc run kafka-consumer --rm -it \
  --image=confluentinc/cp-kafka:7.5.0 \
  -n team-01 -- \
  kafka-console-consumer \
  --bootstrap-server kafka-team01:9092 \
  --topic events.team01.raw \
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

Use after config changes, a broken deployment, or a full classroom reset. Tears down all Kafka and the event generator, then redeploys everything fresh.

The teardown derives which namespaces to wipe from `TEAM1_NAMESPACE` through `TEAM15_NAMESPACE` — any set to `skip` are bypassed automatically. No separate list needed.

```bash
source config.env && envsubst < tekton/runs/run-reset-all-teams.yaml | oc create -f -
tkn pipelinerun logs --last -f -n ${INFRA_NAMESPACE}
```

Pipeline flow: `teardown-all` → `clone-repo` → Kafka per team (parallel) → `deploy-event-generator` → `verify-health` per team (parallel)

---

### Deploy / Redeploy One Team's Kafka

Use when adding a new team or recovering a crashed Kafka pod. Tasks are idempotent — safe to rerun on a healthy deployment.

```bash
source config.env && \
envsubst '${TEAM_NAME} ${TEAM_NAMESPACE} ${INFRA_NAMESPACE} ${STORAGE_CLASS} ${TEKTON_WORKSPACE_PVC}' \
  < tekton/runs/taskrun-deploy-kafka.yaml | oc create -f -

tkn taskrun logs --last -f -n ${INFRA_NAMESPACE}
```

If adding a new team, run the event generator TaskRun next to add it to the bootstrap servers list.

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

### Remove One Team

```bash
# Remove Kafka resources
oc delete statefulset,svc,pvc -l component=kafka -n team-01

# Update event generator to remove the team from bootstrap servers
source config.env
oc set env deployment/${EVENT_GENERATOR_NAME} \
  TEAM_BOOTSTRAP_SERVERS="${TEAM_BOOTSTRAP_SERVERS}" \
  -n ${INFRA_NAMESPACE}
oc rollout restart deployment/${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}
```

### Remove All Teams

```bash
for ns in team-01 team-02 team-03; do
  oc delete statefulset,svc,pvc -l component=kafka -n $ns
done

# Stop the event generator
oc scale deployment/${EVENT_GENERATOR_NAME} --replicas=0 -n ${INFRA_NAMESPACE}
```

### Full Teardown — Remove Everything

```bash
# Delete Tekton run history
tkn pipelinerun delete --all -n ${INFRA_NAMESPACE}
tkn taskrun delete --all -n ${INFRA_NAMESPACE}

# Delete Tekton objects
oc delete pipeline,task --all -n ${INFRA_NAMESPACE}

# Delete RBAC
source config.env
oc delete serviceaccount ${TEKTON_SA_NAME} -n ${INFRA_NAMESPACE}
oc delete clusterrolebinding tekton-pipeline-runner

# Delete event generator
oc delete deployment,svc,configmap,buildconfig,imagestream \
  -l app=${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}

# Delete workspace PVCs
oc delete pvc -l tekton.dev/pipeline=deploy-all-teams -n ${INFRA_NAMESPACE}

# Delete team Kafka resources
for ns in team-01 team-02 team-03; do
  oc delete statefulset,svc,pvc -l component=kafka -n $ns
done
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

### "git-clone ClusterTask not found"

```bash
oc get clustertask git-clone
# If missing, check if it's scoped to openshift-pipelines namespace:
oc get tasks -n openshift-pipelines | grep git-clone
```

### "Kafka pod not found" in wait step

The `oc wait` command needs the pod to exist before it can watch it. If Kafka takes too long to schedule, increase `--timeout=300s` to `--timeout=600s` in `tekton/tasks/01-task-deploy-kafka.yaml`.

### RBAC errors: "cannot create statefulsets"

```bash
oc auth can-i create statefulsets -n team-01 \
  --as=system:serviceaccount:${INFRA_NAMESPACE}:${TEKTON_SA_NAME}
```

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
