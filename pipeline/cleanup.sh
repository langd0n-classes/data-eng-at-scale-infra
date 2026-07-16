#!/usr/bin/env bash
# pipeline/cleanup.sh — Full teardown of everything deployed by the Tekton setup.
#
# Only deletes resources created by this repo's config.env values.
# Does NOT touch namespaces, the default pipeline SA, or anything else.
#
# Usage:
#   bash pipeline/cleanup.sh                                      # interactive prompts, skips namespaces
#   DELETE_NAMESPACES=true bash pipeline/cleanup.sh               # also delete namespaces (self-provisioned clusters)
#   FORCE=true bash pipeline/cleanup.sh                           # skip all confirmation prompts (CI/automation)
#   FORCE=true DELETE_NAMESPACES=true bash pipeline/cleanup.sh    # skip prompts + delete namespaces
#
# Prerequisites:
#   - oc CLI logged into the target cluster
#   - tkn CLI installed (https://tekton.dev/docs/cli/#installation)
#   - config.env present at repo root
#
# Run from repo root or from pipeline/ — the script finds config.env automatically.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../config.env"

info() { echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }

confirm() {
  local prompt="$1"
  if [[ "${FORCE:-false}" == "true" ]]; then
    echo "  (FORCE=true — skipping prompt: ${prompt})"
    return 0
  fi
  read -r -p "${prompt} (y/N): " answer
  [[ "$answer" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
}

echo "============================================================"
echo " Tekton Full Cleanup"
echo " Cluster : $(oc whoami --show-server 2>/dev/null || echo 'unknown')"
echo " User    : $(oc whoami 2>/dev/null || echo 'unknown')"
echo " Infra NS: ${INFRA_NAMESPACE}"
echo "============================================================"
echo ""
confirm "This will permanently delete all Tekton resources in ${INFRA_NAMESPACE}. Continue?"
echo ""

# ------------------------------------------------------------
# 1. PipelineRuns — cancel any in-flight work first
# ------------------------------------------------------------
info "Step 1/10 — Deleting PipelineRuns..."
tkn pipelinerun delete --all --force -n "${INFRA_NAMESPACE}" 2>/dev/null || true
ok "PipelineRuns done"

# ------------------------------------------------------------
# 2. TaskRuns
# ------------------------------------------------------------
info "Step 2/10 — Deleting TaskRuns..."
tkn taskrun delete --all --force -n "${INFRA_NAMESPACE}" 2>/dev/null || true
ok "TaskRuns done"

# ------------------------------------------------------------
# 3. Event generator (label: app=${EVENT_GENERATOR_NAME})
# ------------------------------------------------------------
info "Step 3/10 — Deleting event generator (${EVENT_GENERATOR_NAME})..."
# Pin the Deployment by name — avoids hitting another deployment that happens
# to carry the same app= label. Other resource types (svc, configmap, etc.)
# don't have a fixed predictable name so label-select is fine for those.
oc delete deployment/"${EVENT_GENERATOR_NAME}" \
  -n "${INFRA_NAMESPACE}" --ignore-not-found
oc delete svc,configmap,buildconfig,imagestream \
  -l "app=${EVENT_GENERATOR_NAME}" \
  -n "${INFRA_NAMESPACE}" --ignore-not-found
ok "Event generator done"

# ------------------------------------------------------------
# 4. Kafka per team (label: app=kafka-${TEAM_NAME} — scoped to our exact deployment)
# ------------------------------------------------------------
info "Step 4/10 — Deleting Kafka from team namespaces..."
for i in $(seq 1 15); do
  ns_var="TEAM${i}_NAMESPACE"
  name_var="TEAM${i}_NAME"
  ns="${!ns_var:-skip}"
  name="${!name_var:-skip}"
  [[ "$ns" == "skip" ]] && continue
  echo "         kafka-${name} in ${ns}"
  oc delete kafka "kafka-${name}" -n "${ns}" --ignore-not-found
  oc delete kafkanodepool dual-role -n "${ns}" --ignore-not-found
done
ok "Kafka done"

# ------------------------------------------------------------
# 4b. NiFi per team (label: app=nifi-${TEAM_NAME} — scoped to our exact deployment)
# ------------------------------------------------------------
info "Step 4b/10 — Deleting NiFi from team namespaces..."
for i in $(seq 1 15); do
  ns_var="TEAM${i}_NAMESPACE"
  name_var="TEAM${i}_NAME"
  ns="${!ns_var:-skip}"
  name="${!name_var:-skip}"
  [[ "$ns" == "skip" ]] && continue
  echo "         nifi-${name} in ${ns}"
  oc delete statefulset,svc,pvc \
    -l "app=nifi-${name}" \
    -n "${ns}" --ignore-not-found
  oc delete route \
    -l "app=nifi-${name}" \
    -n "${ns}" --ignore-not-found
  oc delete networkpolicy allow-from-openshift-ingress \
    -n "${ns}" --ignore-not-found
done
ok "NiFi done"

# ------------------------------------------------------------
# 5. Tekton tasks + pipelines (by name — not --all, avoids hitting unrelated objects)
# ------------------------------------------------------------
info "Step 5/10 — Deleting Tekton tasks..."
oc delete task \
  deploy-kafka deploy-event-generator verify-health teardown-all deploy-nifi \
  -n "${INFRA_NAMESPACE}" --ignore-not-found

info "          Deleting Tekton pipelines..."
oc delete pipeline.tekton.dev \
  deploy-all-teams reset-and-deploy \
  -n "${INFRA_NAMESPACE}" --ignore-not-found
ok "Tasks and pipelines done"

# ------------------------------------------------------------
# 6. Workspace PVCs (label: tekton.dev/pipeline=deploy-all-teams)
# ------------------------------------------------------------
info "Step 6/10 — Deleting workspace PVCs..."
oc delete pvc \
  -l "tekton.dev/pipeline=deploy-all-teams" \
  -n "${INFRA_NAMESPACE}" --ignore-not-found
ok "Workspace PVCs done"

# ------------------------------------------------------------
# 7. RBAC per team namespace
#    label: app=tekton-pipeline,component=rbac
#    covers both 03-rolebinding-per-team.yaml (dedicated) and
#    04-role-rolebinding-namespace.yaml (shared) — both use these labels
# ------------------------------------------------------------
info "Step 7/10 — Deleting RBAC from team namespaces..."
for i in $(seq 1 15); do
  ns_var="TEAM${i}_NAMESPACE"
  ns="${!ns_var:-skip}"
  [[ "$ns" == "skip" ]] && continue
  oc delete role,rolebinding \
    -l "app=tekton-pipeline,component=rbac" \
    -n "${ns}" --ignore-not-found 2>/dev/null || true
done
ok "Team namespace RBAC done"

# ------------------------------------------------------------
# 8. RBAC in infra namespace (shared cluster: Role + RoleBinding applied per namespace)
# ------------------------------------------------------------
info "Step 8/10 — Deleting RBAC from infra namespace..."
oc delete role,rolebinding \
  -l "app=tekton-pipeline,component=rbac" \
  -n "${INFRA_NAMESPACE}" --ignore-not-found 2>/dev/null || true
ok "Infra namespace RBAC done"

# ------------------------------------------------------------
# 9. Cluster-scoped RBAC (dedicated cluster only)
#    ClusterRole + ClusterRoleBinding created by 02-clusterrolebinding.yaml
# ------------------------------------------------------------
info "Step 9/10 — Cluster-scoped RBAC..."
if oc auth can-i create clusterrolebindings &>/dev/null; then
  echo "         Dedicated cluster detected — deleting ClusterRole + ClusterRoleBinding"
  oc delete clusterrolebinding pipeline-runner-binding --ignore-not-found
  oc delete clusterrole pipeline-runner-role --ignore-not-found
  ok "Cluster RBAC done"
else
  echo "         Shared cluster — no ClusterRoleBinding to delete"
  ok "Cluster RBAC skipped"
fi

# ------------------------------------------------------------
# 10. Namespaces (opt-in only — set DELETE_NAMESPACES=true to enable)
# ------------------------------------------------------------
info "Step 10/10 — Namespaces..."
if [[ "${DELETE_NAMESPACES:-false}" == "true" ]]; then
  echo ""
  confirm "WARNING: This will permanently delete namespaces and all their contents. Are you sure?"
  echo "         Deleting team namespaces..."
  ns_failed=()
  for i in $(seq 1 15); do
    ns_var="TEAM${i}_NAMESPACE"
    ns="${!ns_var:-skip}"
    [[ "$ns" == "skip" ]] && continue
    echo "         deleting namespace ${ns}"
    if ! oc delete namespace "${ns}" --ignore-not-found 2>/tmp/oc_ns_err; then
      ns_failed+=("${ns}")
      echo "  ✗ Could not delete ${ns}: $(cat /tmp/oc_ns_err)"
      echo "    You may not have permission — contact your cluster admin to remove it."
    fi
  done
  echo "         deleting infra namespace ${INFRA_NAMESPACE}"
  if ! oc delete namespace "${INFRA_NAMESPACE}" --ignore-not-found 2>/tmp/oc_ns_err; then
    ns_failed+=("${INFRA_NAMESPACE}")
    echo "  ✗ Could not delete ${INFRA_NAMESPACE}: $(cat /tmp/oc_ns_err)"
    echo "    You may not have permission — contact your cluster admin to remove it."
  fi
  if [[ ${#ns_failed[@]} -gt 0 ]]; then
    echo ""
    echo "  ⚠ The following namespaces could not be deleted (permission denied):"
    for ns in "${ns_failed[@]}"; do echo "      - ${ns}"; done
    echo "    All other resources were cleaned up successfully."
  else
    ok "Namespaces deleted"
  fi
else
  ok "Namespaces skipped (run with DELETE_NAMESPACES=true to also delete them)"
fi

echo ""
echo "============================================================"
echo " Cleanup complete."
echo "============================================================"
