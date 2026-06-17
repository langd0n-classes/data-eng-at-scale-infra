# Onboarding

First step to get the entire project running on a fresh cluster. Sets up the cluster structure — namespaces, resource limits, and access control — that everything else builds on. Run this once as `kubeadmin` on any OpenShift cluster (CRC, NERC, or any OpenShift 4.x).

**What it creates:**
- Infra namespace (`infra`) — shared namespace for infra team workloads, with ResourceQuota + LimitRange
- Team namespaces (`team-01`, `team-02`, ...) with ResourceQuota + LimitRange
- Team ConfigMap with Kafka/NiFi connection details
- OpenShift Groups (`infra-admins`, `team-01-devs`, ...) for RBAC
- RoleBindings — teachers edit infra + all team namespaces; students edit their ONE team namespace only

---

## Prerequisites

These steps require `kubeadmin` and must be done before running `apply-onboarding.sh`.

### 1. Install OpenShift Pipelines operator

The `apply-onboarding.sh` script checks for this and exits with an error if missing.

**Web console:** OperatorHub → search "Red Hat OpenShift Pipelines" → Install (default settings)

**CLI:**
```bash
oc apply -f - <<EOF
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: openshift-pipelines-operator
  namespace: openshift-operators
spec:
  channel: latest
  name: openshift-pipelines-operator-rh
  source: redhat-operators
  sourceNamespace: openshift-marketplace
EOF

# Verify — wait until pods are Running
oc get pods -n openshift-pipelines
```

### 2. Copy and fill in the env files

```bash
# Runtime config (used by Tekton pipelines at deploy time):
cp config.env.example config.env
# Edit config.env — set GIT_REPO_URL, EXTERNAL_DOMAIN, STORAGE_CLASS, etc.

# Onboarding config (one-time cluster setup only):
cp onboarding/cluster.env.example onboarding/cluster.env
# Edit onboarding/cluster.env — set STORAGE_CLASS, NUM_TEAMS, quotas, INFRA_ADMIN_GROUP
```

Key values to update in `onboarding/cluster.env`:

**Cluster identity:**

| Variable | CRC default | NERC / cloud |
|---|---|---|
| `STORAGE_CLASS` | `crc-csi-hostpath-provisioner` | `standard` |
| `NUM_TEAMS` | `3` | as needed |

**Infra namespace (shared workloads — sized larger than team namespaces):**

| Variable | CRC default | NERC / cloud |
|---|---|---|
| `INFRA_RESOURCE_QUOTA_CPU` | `4` | `16` |
| `INFRA_RESOURCE_QUOTA_MEMORY` | `6Gi` | `32Gi` |
| `INFRA_RESOURCE_QUOTA_STORAGE` | `30Gi` | `100Gi` |
| `INFRA_LIMIT_POD_CPU_MAX` | `4` | `8` |
| `INFRA_LIMIT_POD_MEM_MAX` | `6Gi` | `16Gi` |
| `INFRA_LIMIT_CONTAINER_CPU_MAX` | `2` | `4` |
| `INFRA_LIMIT_CONTAINER_MEM_MAX` | `3Gi` | `8Gi` |
| `INFRA_LIMIT_CONTAINER_CPU_DEFAULT` | `500m` | `500m` |
| `INFRA_LIMIT_CONTAINER_MEM_DEFAULT` | `512Mi` | `512Mi` |
| `INFRA_LIMIT_CONTAINER_CPU_REQUEST` | `100m` | `100m` |
| `INFRA_LIMIT_CONTAINER_MEM_REQUEST` | `256Mi` | `256Mi` |

**Team namespaces (per team — Kafka + NiFi + student workloads):**

| Variable | CRC default | NERC / cloud |
|---|---|---|
| `RESOURCE_QUOTA_CPU` | `2` | `8` |
| `RESOURCE_QUOTA_MEMORY` | `3Gi` | `16Gi` |
| `RESOURCE_QUOTA_STORAGE` | `20Gi` | `100Gi` |
| `LIMIT_POD_CPU_MAX` | `2` | `4` |
| `LIMIT_POD_MEM_MAX` | `4Gi` | `8Gi` |
| `LIMIT_CONTAINER_CPU_MAX` | `1` | `2` |
| `LIMIT_CONTAINER_MEM_MAX` | `2Gi` | `4Gi` |
| `LIMIT_CONTAINER_CPU_DEFAULT` | `200m` | `500m` |
| `LIMIT_CONTAINER_MEM_DEFAULT` | `256Mi` | `512Mi` |
| `LIMIT_CONTAINER_CPU_REQUEST` | `50m` | `100m` |
| `LIMIT_CONTAINER_MEM_REQUEST` | `128Mi` | `256Mi` |

---

## Run onboarding

```bash
# Log in as kubeadmin
oc login -u kubeadmin -p <password> https://api.crc.testing:6443

# Dry run first — prints all commands without executing
bash onboarding/apply-onboarding.sh --dry-run

# Real run
bash onboarding/apply-onboarding.sh
```

---

## Add users to groups

After onboarding, add real users to their groups (kubeadmin only):

```bash
# Teachers/TAs — edit access in infra namespace + all team namespaces:
oc adm groups add-users infra-admins <teacher-username>

# Students — edit access in their ONE team namespace only:
oc adm groups add-users team-01-devs <student-username>
oc adm groups add-users team-02-devs <student-username>

# Remove a user from a group (revokes all access granted by that group):
oc adm groups remove-users infra-admins <teacher-username>
oc adm groups remove-users team-01-devs <student-username>
```

### Fine-grained access (no built-in group for these)

The group model only covers teacher (all namespaces) and student (one namespace). For anything
in between, use direct RoleBindings per namespace — you can mix both approaches for the same user:

```bash
# Infra only — no team access:
oc adm policy add-role-to-user edit <username> -n infra

# Infra + one specific team (add each separately):
oc adm policy add-role-to-user edit <username> -n infra
oc adm groups add-users team-01-devs <username>

# Add more teams later as needed:
oc adm groups add-users team-02-devs <username>

# Remove direct infra binding:
oc adm policy remove-role-from-user edit <username> -n infra
```

---

## Grant namespace-scoped admin (CRC / replicating NERC)

**Why this is needed:** The groups above give `edit` access, which is enough for students to deploy workloads. However, `pipeline/setup.sh` creates Roles and RoleBindings inside namespaces — this requires `admin` role, not just `edit`. On NERC, the instructor grants namespace-scoped `admin` to the deploying user. On CRC, you must do the same for the `developer` user.

Run as `kubeadmin` after onboarding:

```bash
oc adm policy add-role-to-user admin developer -n infra
oc adm policy add-role-to-user admin developer -n team-01
oc adm policy add-role-to-user admin developer -n team-02
oc adm policy add-role-to-user admin developer -n team-03
```

Add one line per team namespace you created. If you added more teams, repeat for each.

**What this unlocks for `developer`:**
- Can create Roles and RoleBindings within these namespaces (required by `pipeline/setup.sh`)
- Can manage ServiceAccounts, Deployments, StatefulSets, PVCs — everything needed for Kafka + NiFi
- Still cannot create ClusterRoleBindings or access other namespaces — this is correct (Option B in `pipeline/setup.sh`)

Without this step, `pipeline/setup.sh` fails immediately with `Forbidden` when it tries to create pipeline RBAC resources.

> **On NERC:** The instructor ran the equivalent command for each student's namespace. On CRC you do it yourself as `kubeadmin`.

---

## RBAC model

| Role | Group | Access |
|---|---|---|
| kubeadmin | (cluster-admin) | Full cluster — installs operators, creates namespaces, manages groups |
| Teacher/TA | `infra-admins` | `edit` in `infra` namespace + `edit` in every team namespace |
| Student | `team-XX-devs` | Full `edit` in their ONE team namespace — can deploy anything including deleting Kafka/NiFi (teacher reruns Tekton to restore) |

Students have **no binding** in the infra namespace — they cannot see or access it.

---

## Flag reference

| Flag | Effect |
|---|---|
| `--dry-run` | Print all `oc` commands without executing them |
| `--skip-rbac` | Skip Group creation and RoleBinding application — useful when re-running to update quotas or ConfigMaps |
| `--teams-only` | Skip infra namespace and infra RBAC; only create/update team namespaces |
| `--infra-only` | Only create infra namespace and infra RBAC — useful for initial admin bootstrap before teams are ready |
| `--from-team=N` | Start team loop from team N — use when adding new teams to an existing cluster |

---

## Verify

```bash
# Namespaces
oc get namespaces | grep -E "^infra|^team-"

# Quotas and limits for a team namespace
oc get resourcequota,limitrange -n team-01

# ConfigMap and Secret
oc get configmap,secret -n team-01

# RoleBindings
oc get rolebindings -n infra
oc get rolebindings -n team-01

# Groups
oc get groups
```
