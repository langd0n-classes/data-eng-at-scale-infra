#!/usr/bin/env bash
# pipeline/setup.sh — Automated Tekton setup: RBAC + tasks/pipelines + PipelineRun
#
# Usage:
#   bash pipeline/setup.sh                    # full setup: RBAC + tasks + pipelines + run
#   bash pipeline/setup.sh --skip-rbac        # skip RBAC (already applied)
#   bash pipeline/setup.sh --skip-tasks       # skip tasks/pipelines (already applied)
#   bash pipeline/setup.sh --run-only         # only submit the PipelineRun
#   bash pipeline/setup.sh --reset            # use run-reset-all-teams.yaml instead
#   bash pipeline/setup.sh --dry-run          # print all commands without executing
#
# Run from repo root or from pipeline/ — the script finds config.env automatically.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

source "${SCRIPT_DIR}/lib/common.sh"

# ── Parse flags ────────────────────────────────────────────────────────────────
SKIP_RBAC=false
SKIP_TASKS=false
RUN_ONLY=false
USE_RESET=false
DRY_RUN=false

for arg in "$@"; do
  case "$arg" in
    --skip-rbac)   SKIP_RBAC=true ;;
    --skip-tasks)  SKIP_TASKS=true ;;
    --run-only)    RUN_ONLY=true; SKIP_RBAC=true; SKIP_TASKS=true ;;
    --reset)       USE_RESET=true ;;
    --dry-run)     DRY_RUN=true ;;
    *) echo "Unknown flag: $arg"; echo "Usage: $0 [--skip-rbac] [--skip-tasks] [--run-only] [--reset] [--dry-run]"; exit 1 ;;
  esac
done

# ── Step 1: Load & Validate Config ─────────────────────────────────────────────
echo "============================================================"
echo " Tekton Setup"
echo "============================================================"
echo ""
info "Step 1 — Loading config..."

CONFIG_FILE="${REPO_ROOT}/config.env"
load_config "${REPO_ROOT}"

# Validate required variables
MISSING=()
for var in INFRA_NAMESPACE OC_CLI_IMAGE GIT_REPO_URL EXTERNAL_DOMAIN STORAGE_CLASS; do
  val="${!var:-}"
  if [[ -z "$val" ]]; then
    MISSING+=("$var (empty)")
  elif [[ "$val" == *"your-cluster"* || "$val" == *"YOUR-USERNAME"* || "$val" == *"example.com"* ]]; then
    MISSING+=("$var (still has placeholder value: $val)")
  fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "ERROR: The following config.env variables are missing or have placeholder values:"
  for m in "${MISSING[@]}"; do echo "         $m"; done
  echo "       Edit config.env and re-run."
  exit 1
fi

ok "Config loaded — infra namespace: ${INFRA_NAMESPACE}"

# Default for EVENT_GENERATOR_NAME — set in config.env.example, safe to default here
EVENT_GENERATOR_NAME="${EVENT_GENERATOR_NAME:-event-generator}"

# ── Step 2: Prerequisite Checks ────────────────────────────────────────────────
info "Step 2 — Checking prerequisites..."

if ! oc whoami &>/dev/null; then
  warn "Not logged into OpenShift — run 'oc login' first"
else
  ok "Logged in as $(oc whoami)"
fi

if ! oc get pods -n openshift-pipelines &>/dev/null; then
  warn "Cannot list pods in openshift-pipelines — Pipelines operator may not be installed"
else
  ok "OpenShift Pipelines namespace accessible"
fi

if oc get task git-clone -n openshift-pipelines &>/dev/null || oc get clustertask git-clone &>/dev/null 2>/dev/null; then
  ok "git-clone task found"
else
  warn "git-clone task not found — pipeline will fail at clone-repo step"
fi

# ── Step 3: Auto-Detect Cluster Type ───────────────────────────────────────────
info "Step 3 — Detecting cluster type..."

if oc auth can-i create clusterrolebindings &>/dev/null; then
  CLUSTER_TYPE="dedicated"
else
  CLUSTER_TYPE="shared"
fi

ok "Cluster type: ${CLUSTER_TYPE}"

# ── Step 4: Apply RBAC ─────────────────────────────────────────────────────────
if [[ "$SKIP_RBAC" == "false" ]]; then
  echo ""
  info "Step 4 — Applying RBAC (${CLUSTER_TYPE} path)..."

  # Apply SA first — it must exist before the ClusterRoleBinding references it
  run "source '${CONFIG_FILE}' && envsubst '\${INFRA_NAMESPACE} \${TEKTON_SA_NAME}' \
    < '${SCRIPT_DIR}/rbac/01-serviceaccount.yaml' | oc apply -f -"
  ok "ServiceAccount applied"

  if [[ "$CLUSTER_TYPE" == "dedicated" ]]; then
    run "source '${CONFIG_FILE}' && envsubst '\${INFRA_NAMESPACE} \${TEKTON_SA_NAME}' \
      < '${SCRIPT_DIR}/rbac/02-clusterrolebinding.yaml' | oc apply -f -"
    ok "ClusterRoleBinding applied"
  else
    # Shared cluster — apply Role + RoleBinding to every active namespace
    ALL_NS=("${INFRA_NAMESPACE}")
    for i in $(seq 1 15); do
      ns_var="TEAM${i}_NAMESPACE"
      ns="${!ns_var:-skip}"
      [[ "$ns" == "skip" ]] && continue
      ALL_NS+=("$ns")
    done

    for ns in "${ALL_NS[@]}"; do
      echo "         applying to namespace: ${ns}"
      run "source '${CONFIG_FILE}' && INFRA_NAMESPACE='${INFRA_NAMESPACE}' \
        envsubst '\${INFRA_NAMESPACE}' \
        < '${SCRIPT_DIR}/rbac/04-role-rolebinding-namespace.yaml' | oc apply -f - -n '${ns}'"
    done
    ok "Role + RoleBinding applied to ${#ALL_NS[@]} namespaces"
  fi

  # Apply ChatOps RoleBinding per namespace (namespace-scoped, no cluster-admin needed).
  # Binds slack-chatops-sa to the built-in 'admin' ClusterRole in infra + each team namespace.
  if [[ "${CHATOPS_ENABLED:-false}" == "true" ]]; then
    CHATOPS_NAME_VAL="${CHATOPS_NAME:-slack-chatops}"
    ALL_CHATOPS_NS=("${INFRA_NAMESPACE}")
    for i in $(seq 1 15); do
      ns_var="TEAM${i}_NAMESPACE"
      ns="${!ns_var:-skip}"
      [[ "$ns" == "skip" ]] && continue
      ALL_CHATOPS_NS+=("$ns")
    done
    for ns in "${ALL_CHATOPS_NS[@]}"; do
      echo "         chatops binding → namespace: ${ns}"
      run "INFRA_NAMESPACE='${INFRA_NAMESPACE}' CHATOPS_NAME='${CHATOPS_NAME_VAL}' \
        envsubst '\${INFRA_NAMESPACE} \${CHATOPS_NAME}' \
        < '${REPO_ROOT}/chatops/k8s/rbac/rolebinding-namespace.yaml' | oc apply -f - -n '${ns}'"
    done
    ok "ChatOps RoleBindings applied to ${#ALL_CHATOPS_NS[@]} namespaces"
  fi
else
  info "Step 4 — RBAC (skipped)"
fi

# ── Step 5: Apply Tasks and Pipelines ──────────────────────────────────────────
if [[ "$SKIP_TASKS" == "false" ]]; then
  echo ""
  info "Step 5 — Applying tasks..."

  for task_file in "${SCRIPT_DIR}/tasks"/0*.yaml; do
    task_name="$(basename "$task_file")"
    echo "         ${task_name}"
    run "source '${CONFIG_FILE}' && envsubst '\${INFRA_NAMESPACE} \${OC_CLI_IMAGE}' \
      < '${task_file}' | oc apply -f -"
  done
  ok "Tasks applied"

  info "          Applying pipelines..."
  for pipeline_file in "${SCRIPT_DIR}/pipelines"/0*.yaml; do
    pipeline_name="$(basename "$pipeline_file")"
    echo "         ${pipeline_name}"
    run "source '${CONFIG_FILE}' && envsubst '\${INFRA_NAMESPACE}' \
      < '${pipeline_file}' | oc apply -f -"
  done
  ok "Pipelines applied"

  # ── Step 5b: Apply event generator build resources (one-time setup) ──────────
  # ImageStream + BuildConfig are applied here so the Tekton deploy-event-generator
  # task can focus only on ConfigMap + Deployment. The BuildConfig's ConfigChange
  # trigger starts the image build automatically on first apply.
  info "Step 5b — Applying event generator build resources..."
  EG_SOURCE="${REPO_ROOT}/event-generator/k8s"
  run "sed \
    -e 's|\${INFRA_NAMESPACE}|${INFRA_NAMESPACE}|g' \
    -e 's|\${EVENT_GENERATOR_NAME}|${EVENT_GENERATOR_NAME}|g' \
    '${EG_SOURCE}/01-imagestream.yaml' | oc apply -f -"
  run "sed \
    -e 's|\${INFRA_NAMESPACE}|${INFRA_NAMESPACE}|g' \
    -e 's|\${EVENT_GENERATOR_NAME}|${EVENT_GENERATOR_NAME}|g' \
    -e 's|\${GIT_REPO_URL}|${GIT_REPO_URL}|g' \
    -e 's|\${GIT_BRANCH}|${GIT_BRANCH:-main}|g' \
    '${EG_SOURCE}/02-buildconfig.yaml' | oc apply -f -"
  ok "BuildConfig applied — ConfigChange trigger starts build automatically"
else
  info "Step 5 — Tasks/pipelines (skipped)"
fi

# ── Step 5c: Write team-registry ConfigMap + team-passwords Secret ─────────────
_write_team_registry() {
  info "Step 5c — Writing team-registry ConfigMap..."
  local args=()
  for i in $(seq 1 15); do
    local tname tns
    tname=$(eval echo "\${TEAM${i}_NAME:-skip}")
    tns=$(eval echo "\${TEAM${i}_NAMESPACE:-skip}")
    [[ "${tname}" == "skip" || "${tns}" == "skip" ]] && continue
    local bootstrap="kafka-${tname}.${tns}.svc.cluster.local:9092"
    args+=("--from-literal=${tname}=namespace=${tns},bootstrap=${bootstrap}")
  done
  if [[ ${#args[@]} -eq 0 ]]; then
    warn "No teams defined in config.env — team-registry not written"
    return
  fi
  oc create configmap team-registry -n "${INFRA_NAMESPACE}" \
    "${args[@]}" --dry-run=client -o yaml | oc apply -f -
  ok "team-registry updated (${#args[@]} team(s))"
}

_write_team_passwords() {
  info "          Writing team-passwords Secret..."
  local args=()
  for i in $(seq 1 15); do
    local tname tpwd
    tname=$(eval echo "\${TEAM${i}_NAME:-skip}")
    tpwd=$(eval echo "\${TEAM${i}_PASSWORD:-skip}")
    [[ "${tname}" == "skip" || "${tpwd}" == "skip" ]] && continue
    args+=("--from-literal=${tname}=${tpwd}")
  done
  if [[ ${#args[@]} -eq 0 ]]; then
    warn "No team passwords in config.env — team-passwords not written"
    return
  fi
  oc create secret generic team-passwords -n "${INFRA_NAMESPACE}" \
    "${args[@]}" --dry-run=client -o yaml | oc apply -f -
  ok "team-passwords updated (${#args[@]} team(s))"
}

_write_team_registry
_write_team_passwords

# ── Step 6: Auto-Increment Run Number and Submit ───────────────────────────────
echo ""
info "Step 6 — Submitting PipelineRun..."

if [[ "$USE_RESET" == "true" ]]; then
  RUN_YAML="${SCRIPT_DIR}/runs/run-reset-all-teams.yaml"
  BASE_NAME="reset-all-teams-run"
else
  RUN_YAML="${SCRIPT_DIR}/runs/run-all-teams.yaml"
  BASE_NAME="deploy-all-teams-run"
fi

# Use a timestamp suffix — avoids name collisions even when old runs are
# archived (not truly deleted) on shared clusters like NERC where only
# cluster admins can delete PipelineRun objects.
RUN_NUM=$(date +%Y%m%d-%H%M%S)

RUN_NAME="${BASE_NAME}-${RUN_NUM}"
echo "         run name: ${RUN_NAME}"

run "source '${CONFIG_FILE}' && envsubst < '${RUN_YAML}' \
  | sed 's/${BASE_NAME}-001/${RUN_NAME}/' \
  | oc create -f -"

ok "PipelineRun submitted"

# ── Step 7: Print Watch Command ────────────────────────────────────────────────
echo ""
echo "============================================================"
echo " PipelineRun submitted: ${RUN_NAME}"
echo ""
echo " Watch logs:"
echo "   tkn pipelinerun logs ${RUN_NAME} -f -n ${INFRA_NAMESPACE}"
echo ""
echo " Or follow the latest run:"
echo "   tkn pipelinerun logs --last -f -n ${INFRA_NAMESPACE}"
echo "============================================================"
