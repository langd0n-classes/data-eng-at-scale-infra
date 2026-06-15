#!/usr/bin/env bash
# tekton/ops.sh — Day-to-day classroom operations
#
# All commands use plain oc calls — no Tekton involvement (except cleanup-runs).
# Run from repo root or from tekton/ — the script finds config.env automatically.
#
# Usage:
#   bash tekton/ops.sh <command> [args...]  [--dry-run]
#
# Flags:
#   --dry-run      Print commands without executing
#   FORCE=true     Skip all confirmation prompts
#   WIPE_PVCS=true Also delete workspace PVCs in teardown-all
#
# Commands:
#   Team lifecycle
#     add-team      <name> <ns> <pwd>   Deploy Kafka + NiFi for a team
#     add-kafka     <name> <ns>          Deploy only Kafka (surgical re-add)
#     add-nifi      <name> <ns> <pwd>   Deploy only NiFi (surgical re-add)
#     remove-team   <name> <ns>          Remove Kafka + NiFi for a team
#     reset-team    <name> <ns> <pwd>   Remove then redeploy a team
#
#   Component operations
#     remove-kafka      <name> <ns>      Delete Kafka StatefulSet + Services + PVC
#     remove-nifi       <name> <ns>      Delete NiFi StatefulSet + Services + PVC + Route + NetworkPolicy
#     wipe-kafka-data   <name> <ns>      Delete Kafka PVC (scale to 0 first, then back to 1)
#     force-update-nifi <name> <ns> <pwd> Clear SHA annotation and redeploy NiFi
#     reset-password    <name> <ns> <pwd> Same as force-update-nifi
#     restart-kafka     <name> <ns>      Delete Kafka pod (triggers restart)
#     restart-nifi      <name> <ns>      Delete NiFi pod (triggers restart)
#
#   Event generator
#     pause-events    Scale event generator to 0 replicas
#     resume-events   Scale event generator to 1 replica
#     remove-events   Delete entire event generator deployment
#
#   Bulk operations
#     remove-all-teams   Remove all configured teams (Kafka + NiFi, no namespace deletion)
#     teardown-all       Cancel runs → remove events → remove all teams
#     reset-all          teardown-all then re-run the reset pipeline
#     cleanup-runs       Keep 3 PipelineRuns + 5 TaskRuns, delete the rest (requires tkn)
#
#   Observability
#     status <name> <ns>   Show pods, services, PVCs, routes, and recent events

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

source "${SCRIPT_DIR}/lib/common.sh"

# ── Argument parsing ───────────────────────────────────────────────────────────
COMMAND=""
ARGS=()
DRY_RUN=false

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    *)
      if [[ -z "$COMMAND" ]]; then
        COMMAND="$arg"
      else
        ARGS+=("$arg")
      fi
      ;;
  esac
done

if [[ -z "$COMMAND" ]]; then
  COMMAND="help"
fi

# ── Load config ────────────────────────────────────────────────────────────────
if [[ "$COMMAND" != "help" ]]; then
  load_config "${REPO_ROOT}"
  EVENT_GENERATOR_NAME="${EVENT_GENERATOR_NAME:-event-generator}"

  if ! oc whoami &>/dev/null; then
    err "Not logged into OpenShift — run 'oc login' first"
    exit 1
  fi
fi

# ── Private helpers ────────────────────────────────────────────────────────────

_do_add_kafka() {
  local name="$1" ns="$2"
  if ! oc get namespace "${ns}" &>/dev/null; then
    err "Namespace '${ns}' does not exist."
    err "Create it first: oc new-project ${ns}"
    exit 1
  fi
  info "Deploying Kafka for ${name} in ${ns}..."
  run "TEAM_NAME='${name}' TEAM_NAMESPACE='${ns}' STORAGE_CLASS='${STORAGE_CLASS}' \
    envsubst '\${TEAM_NAME} \${TEAM_NAMESPACE} \${STORAGE_CLASS}' \
    < '${REPO_ROOT}/kafka/per-team/kafka-per-team-template.yaml' | oc apply -f -"
}

_do_add_nifi() {
  local name="$1" ns="$2" pwd="$3"
  if ! oc get namespace "${ns}" &>/dev/null; then
    err "Namespace '${ns}' does not exist."
    err "Create it first: oc new-project ${ns}"
    exit 1
  fi
  info "Deploying NiFi for ${name} in ${ns}..."
  run "cd '${REPO_ROOT}/nifi' && bash deploy-team.sh '${name}' '${ns}' '${pwd}'"
}

_do_remove_kafka() {
  local name="$1" ns="$2"
  info "Removing Kafka for ${name} in ${ns}..."
  run "oc delete statefulset,svc,pvc \
    -l 'app=kafka-${name}' \
    -n '${ns}' --ignore-not-found"
}

_do_remove_nifi() {
  local name="$1" ns="$2"
  info "Removing NiFi for ${name} in ${ns}..."
  run "oc delete statefulset,svc,pvc \
    -l 'app=nifi-${name}' \
    -n '${ns}' --ignore-not-found"
  run "oc delete route \
    -l 'app=nifi-${name}' \
    -n '${ns}' --ignore-not-found"
  run "oc delete networkpolicy allow-from-openshift-ingress \
    -n '${ns}' --ignore-not-found"
}

_do_remove_events() {
  info "Removing event generator..."
  run "oc delete deployment '${EVENT_GENERATOR_NAME}' \
    -n '${INFRA_NAMESPACE}' --ignore-not-found"
  run "oc delete svc,configmap,buildconfig,imagestream \
    -l 'app=${EVENT_GENERATOR_NAME}' \
    -n '${INFRA_NAMESPACE}' --ignore-not-found"
}

_do_teardown_body() {
  # Step 1: Cancel in-flight PipelineRuns FIRST — a running pipeline recreates
  # resources as you delete them, so cancel before touching anything.
  echo "Cancelling in-flight PipelineRuns..."
  while IFS= read -r pr; do
    [[ -z "$pr" ]] && continue
    run "oc patch pipelinerun '${pr}' -n '${INFRA_NAMESPACE}' \
      --type merge -p '{\"spec\":{\"status\":\"StoppedRunFinally\"}}'"
  done < <(oc get pipelinerun -n "${INFRA_NAMESPACE}" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.conditions[0].reason}{"\n"}{end}' \
    2>/dev/null | awk '/\tRunning/{print $1}' || true)

  echo "Cancelling in-flight TaskRuns..."
  while IFS= read -r tr; do
    [[ -z "$tr" ]] && continue
    run "oc patch taskrun '${tr}' -n '${INFRA_NAMESPACE}' \
      --type merge -p '{\"spec\":{\"status\":\"TaskRunCancelled\"}}'"
  done < <(oc get taskrun -n "${INFRA_NAMESPACE}" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.conditions[0].reason}{"\n"}{end}' \
    2>/dev/null | awk '/\tRunning/{print $1}' || true)

  # Step 2: Remove event generator
  _do_remove_events

  # Step 3: Remove all configured teams
  for i in $(seq 1 15); do
    local ns_var="TEAM${i}_NAMESPACE" name_var="TEAM${i}_NAME"
    local ns="${!ns_var:-skip}" name="${!name_var:-skip}"
    [[ "$ns" == "skip" ]] && continue
    echo "  removing team: ${name} in ${ns}"
    _do_remove_kafka "${name}" "${ns}"
    _do_remove_nifi  "${name}" "${ns}"
  done

  # Step 4: Optionally wipe Tekton workspace PVCs (created per PipelineRun)
  if [[ "${WIPE_PVCS:-false}" == "true" ]]; then
    echo "Deleting workspace PVCs (WIPE_PVCS=true)..."
    run "oc delete pvc \
      -l 'tekton.dev/pipeline=deploy-all-teams' \
      -n '${INFRA_NAMESPACE}' --ignore-not-found"
  else
    warn "Workspace PVCs kept. Run with WIPE_PVCS=true to also delete them."
  fi

  ok "Teardown complete — namespaces, Tekton tasks/pipelines/RBAC untouched."
}

_do_clean_history() {
  # Deletes all PipelineRuns, TaskRuns, and workspace PVCs — full clean slate.
  echo "Deleting all PipelineRuns..."
  run "oc delete pipelinerun --all -n '${INFRA_NAMESPACE}' --ignore-not-found"

  echo "Deleting all TaskRuns..."
  run "oc delete taskrun --all -n '${INFRA_NAMESPACE}' --ignore-not-found"

  echo "Deleting all workspace PVCs..."
  run "oc delete pvc --all -n '${INFRA_NAMESPACE}' --ignore-not-found"

  ok "Pipeline history and workspace PVCs deleted."
}

# ── Command functions ──────────────────────────────────────────────────────────

cmd_add_team() {
  local name="${1:?Usage: add-team <name> <ns> <pwd>}"
  local ns="${2:?Usage: add-team <name> <ns> <pwd>}"
  local pwd="${3:?Usage: add-team <name> <ns> <pwd>}"
  _do_add_kafka "${name}" "${ns}"
  _do_add_nifi  "${name}" "${ns}" "${pwd}"
  ok "Team ${name} deployed in ${ns}"
}

cmd_add_kafka() {
  local name="${1:?Usage: add-kafka <name> <ns>}"
  local ns="${2:?Usage: add-kafka <name> <ns>}"
  _do_add_kafka "${name}" "${ns}"
  ok "Kafka deployed for ${name} in ${ns}"
}

cmd_add_nifi() {
  local name="${1:?Usage: add-nifi <name> <ns> <pwd>}"
  local ns="${2:?Usage: add-nifi <name> <ns> <pwd>}"
  local pwd="${3:?Usage: add-nifi <name> <ns> <pwd>}"
  _do_add_nifi "${name}" "${ns}" "${pwd}"
  ok "NiFi deployed for ${name} in ${ns}"
}

cmd_remove_team() {
  local name="${1:?Usage: remove-team <name> <ns>}"
  local ns="${2:?Usage: remove-team <name> <ns>}"
  confirm "Remove Kafka + NiFi for ${name} in ${ns}?"
  _do_remove_kafka "${name}" "${ns}"
  _do_remove_nifi  "${name}" "${ns}"
  ok "Team ${name} removed from ${ns}"
}

cmd_reset_team() {
  local name="${1:?Usage: reset-team <name> <ns> <pwd>}"
  local ns="${2:?Usage: reset-team <name> <ns> <pwd>}"
  local pwd="${3:?Usage: reset-team <name> <ns> <pwd>}"
  confirm "Remove then redeploy ${name} in ${ns}?"
  _do_remove_kafka "${name}" "${ns}"
  _do_remove_nifi  "${name}" "${ns}"
  _do_add_kafka    "${name}" "${ns}"
  _do_add_nifi     "${name}" "${ns}" "${pwd}"
  ok "Team ${name} reset in ${ns}"
}

cmd_remove_kafka() {
  local name="${1:?Usage: remove-kafka <name> <ns>}"
  local ns="${2:?Usage: remove-kafka <name> <ns>}"
  confirm "Delete Kafka StatefulSet + Services + PVC for ${name} in ${ns}?"
  _do_remove_kafka "${name}" "${ns}"
  ok "Kafka removed for ${name} in ${ns}"
}

cmd_remove_nifi() {
  local name="${1:?Usage: remove-nifi <name> <ns>}"
  local ns="${2:?Usage: remove-nifi <name> <ns>}"
  confirm "Delete NiFi StatefulSet + Services + PVC + Route + NetworkPolicy for ${name} in ${ns}?"
  _do_remove_nifi "${name}" "${ns}"
  ok "NiFi removed for ${name} in ${ns}"
}

cmd_wipe_kafka_data() {
  local name="${1:?Usage: wipe-kafka-data <name> <ns>}"
  local ns="${2:?Usage: wipe-kafka-data <name> <ns>}"

  if ! oc get statefulset "kafka-${name}" -n "${ns}" &>/dev/null; then
    err "StatefulSet kafka-${name} not found in ${ns} — Kafka is not deployed for this team."
    exit 1
  fi

  confirm "Wipe all Kafka data for ${name} in ${ns}? This PERMANENTLY deletes the PVC."
  info "Scaling Kafka to 0..."
  run "oc scale statefulset 'kafka-${name}' --replicas=0 -n '${ns}'"
  # Wait for pod to terminate before deleting PVC (PVC can't be deleted while mounted)
  echo "Waiting for pod to terminate..."
  run "oc wait pod 'kafka-${name}-0' --for=delete --timeout=60s -n '${ns}' 2>/dev/null || true"
  info "Deleting Kafka PVC..."
  run "oc delete pvc -l 'app=kafka-${name}' -n '${ns}' --ignore-not-found"
  info "Scaling Kafka back to 1..."
  run "oc scale statefulset 'kafka-${name}' --replicas=1 -n '${ns}'"
  ok "Kafka data wiped for ${name} in ${ns}. Pod is restarting with fresh storage."
}

cmd_force_update_nifi() {
  local name="${1:?Usage: force-update-nifi <name> <ns> <pwd>}"
  local ns="${2:?Usage: force-update-nifi <name> <ns> <pwd>}"
  local pwd="${3:?Usage: force-update-nifi <name> <ns> <pwd>}"
  # Clear the git SHA annotation so the deploy-nifi task won't skip on next pipeline run
  run "oc annotate statefulset 'nifi-${name}' -n '${ns}' \
    tekton.dev/git-sha- --ignore-not-found 2>/dev/null || true"
  run "cd '${REPO_ROOT}/nifi' && bash deploy-team.sh '${name}' '${ns}' '${pwd}'"
  ok "NiFi force-updated for ${name} in ${ns}"
}

cmd_reset_password() {
  local name="${1:?Usage: reset-password <name> <ns> <pwd>}"
  local ns="${2:?Usage: reset-password <name> <ns> <pwd>}"
  local pwd="${3:?Usage: reset-password <name> <ns> <pwd>}"
  # Clear SHA annotation and redeploy with new password
  run "oc annotate statefulset 'nifi-${name}' -n '${ns}' \
    tekton.dev/git-sha- --ignore-not-found 2>/dev/null || true"
  run "cd '${REPO_ROOT}/nifi' && bash deploy-team.sh '${name}' '${ns}' '${pwd}'"
  ok "Password reset for ${name} in ${ns}"
}

cmd_restart_kafka() {
  local name="${1:?Usage: restart-kafka <name> <ns>}"
  local ns="${2:?Usage: restart-kafka <name> <ns>}"
  run "oc delete pod 'kafka-${name}-0' -n '${ns}'"
  ok "Kafka pod kafka-${name}-0 deleted — StatefulSet will restart it"
}

cmd_restart_nifi() {
  local name="${1:?Usage: restart-nifi <name> <ns>}"
  local ns="${2:?Usage: restart-nifi <name> <ns>}"
  run "oc delete pod 'nifi-${name}-0' -n '${ns}'"
  ok "NiFi pod nifi-${name}-0 deleted — StatefulSet will restart it"
}

cmd_pause_events() {
  run "oc scale deployment '${EVENT_GENERATOR_NAME}' --replicas=0 -n '${INFRA_NAMESPACE}'"
  ok "Event generator paused (0 replicas)"
}

cmd_resume_events() {
  run "oc scale deployment '${EVENT_GENERATOR_NAME}' --replicas=1 -n '${INFRA_NAMESPACE}'"
  ok "Event generator resumed (1 replica)"
}

cmd_remove_events() {
  confirm "Delete the entire event generator deployment (Deployment, ConfigMap, BuildConfig, ImageStream)?"
  _do_remove_events
  ok "Event generator removed"
}

cmd_remove_all_teams() {
  confirm "Remove all configured teams (Kafka + NiFi)? Namespaces are kept."
  for i in $(seq 1 15); do
    local ns_var="TEAM${i}_NAMESPACE" name_var="TEAM${i}_NAME"
    local ns="${!ns_var:-skip}" name="${!name_var:-skip}"
    [[ "$ns" == "skip" ]] && continue
    echo "  removing team: ${name} in ${ns}"
    _do_remove_kafka "${name}" "${ns}"
    _do_remove_nifi  "${name}" "${ns}"
  done
  ok "All teams removed"
}

cmd_teardown_all() {
  local clean=false
  for arg in "${ARGS[@]:-}"; do
    [[ "$arg" == "--clean" ]] && clean=true
  done

  if [[ "$clean" == "true" ]]; then
    confirm "Teardown all apps AND delete pipeline history + workspace PVCs? (full clean slate — Tekton tasks/pipelines/RBAC untouched)"
  else
    confirm "Teardown all deployed apps (events + all teams)? Tekton tasks/pipelines/RBAC and namespaces are untouched."
  fi

  _do_teardown_body

  if [[ "$clean" == "true" ]]; then
    _do_clean_history
  fi
}

cmd_reset_all() {
  confirm "Reset all: teardown all deployed apps then re-run the reset pipeline? Namespaces and Tekton infra are untouched."
  _do_teardown_body
  info "Launching reset pipeline..."
  run "bash '${REPO_ROOT}/tekton/setup.sh' --reset"
}

cmd_cleanup_runs() {
  if ! command -v tkn &>/dev/null; then
    err "'tkn' CLI not found."
    err "Install it: https://tekton.dev/docs/cli/#installation"
    exit 1
  fi
  run "tkn pipelinerun delete --keep=3 -n '${INFRA_NAMESPACE}' --force"
  run "tkn taskrun delete --keep=5 -n '${INFRA_NAMESPACE}' --force"
  ok "Old PipelineRuns and TaskRuns cleaned up"
}

cmd_status() {
  local name="${1:?Usage: status <name> <ns>}"
  local ns="${2:?Usage: status <name> <ns>}"
  # status is read-only — always executes, even in --dry-run mode
  echo "=== Pods (${ns}) ==="
  oc get pods -n "${ns}" \
    -l "app in (kafka-${name},nifi-${name})" \
    --no-headers 2>/dev/null || echo "  (none)"

  echo ""
  echo "=== Services (${ns}) ==="
  oc get svc -n "${ns}" \
    -l "app in (kafka-${name},nifi-${name})" \
    --no-headers 2>/dev/null || echo "  (none)"

  echo ""
  echo "=== PVCs (${ns}) ==="
  oc get pvc -n "${ns}" \
    -l "app in (kafka-${name},nifi-${name})" \
    --no-headers 2>/dev/null || echo "  (none)"

  echo ""
  echo "=== Routes (${ns}) ==="
  oc get route -n "${ns}" \
    -l "app=nifi-${name}" \
    --no-headers 2>/dev/null || echo "  (none)"

  echo ""
  echo "=== Recent Events (last 10) ==="
  oc get events -n "${ns}" \
    --sort-by='.lastTimestamp' \
    --no-headers 2>/dev/null | tail -10 || echo "  (none)"
}

cmd_status_all() {
  # status-all is read-only — always executes, even in --dry-run mode
  echo "======================================================"
  echo "  Cluster-wide status"
  echo "======================================================"

  # ── infra namespace ──────────────────────────────────────
  echo ""
  echo "── infra (${INFRA_NAMESPACE}) ─────────────────────────────────────"

  echo "Event Generator:"
  oc get deployment "${EVENT_GENERATOR_NAME}" -n "${INFRA_NAMESPACE}" \
    --no-headers \
    -o custom-columns='  NAME:.metadata.name,READY:.status.readyReplicas,REPLICAS:.spec.replicas' \
    2>/dev/null || echo "  (not deployed)"

  local chatops_name="${CHATOPS_NAME:-slack-chatops}"
  echo "ChatOps:"
  oc get deployment "${chatops_name}" -n "${INFRA_NAMESPACE}" \
    --no-headers \
    -o custom-columns='  NAME:.metadata.name,READY:.status.readyReplicas,REPLICAS:.spec.replicas' \
    2>/dev/null || echo "  (not deployed)"

  echo "Pipeline runs (last 3):"
  oc get pipelinerun -n "${INFRA_NAMESPACE}" \
    --sort-by='.metadata.creationTimestamp' --no-headers \
    -o custom-columns='  NAME:.metadata.name,STATUS:.status.conditions[0].reason,STARTED:.metadata.creationTimestamp' \
    2>/dev/null | tail -3 || echo "  (none)"

  # ── team namespaces ─────────────────────────────────────
  local namespaces
  namespaces=$(oc get projects --no-headers \
    -o custom-columns='NAME:.metadata.name' 2>/dev/null \
    | grep -v "^${INFRA_NAMESPACE}$" \
    | grep -v "^openshift" \
    | grep -v "^kube" \
    | grep -v "^default$" \
    || true)

  if [[ -z "${namespaces}" ]]; then
    echo ""
    echo "No team namespaces found."
  else
    while IFS= read -r ns; do
      [[ -z "${ns}" ]] && continue
      echo ""
      echo "── ${ns} ─────────────────────────────────────────────────"

      echo "Pods:"
      oc get pods -n "${ns}" --no-headers \
        -o custom-columns='  NAME:.metadata.name,STATUS:.status.phase,READY:.status.containerStatuses[0].ready' \
        2>/dev/null || echo "  (none)"

      echo "PVCs:"
      oc get pvc -n "${ns}" --no-headers \
        -o custom-columns='  NAME:.metadata.name,STATUS:.status.phase,CAPACITY:.status.capacity.storage' \
        2>/dev/null || echo "  (none)"

      echo "Routes:"
      oc get route -n "${ns}" --no-headers \
        -o custom-columns='  NAME:.metadata.name,HOST:.spec.host' \
        2>/dev/null || echo "  (none)"

    done <<< "${namespaces}"
  fi

  echo ""
  echo "======================================================"
}

cmd_help() {
  cat <<'HELP'
tekton/ops.sh — Day-to-day classroom operations

Usage: bash tekton/ops.sh <command> [args...] [--dry-run]
       FORCE=true bash tekton/ops.sh <command>
       WIPE_PVCS=true bash tekton/ops.sh teardown-all

Team lifecycle:
  add-team      <name> <ns> <pwd>   Deploy Kafka + NiFi for a team
  add-kafka     <name> <ns>          Deploy only Kafka (surgical re-add)
  add-nifi      <name> <ns> <pwd>   Deploy only NiFi (surgical re-add)
  remove-team   <name> <ns>          Remove Kafka + NiFi for a team
  reset-team    <name> <ns> <pwd>   Remove then redeploy a team

Component operations:
  remove-kafka      <name> <ns>          Delete Kafka StatefulSet + Services + PVC
  remove-nifi       <name> <ns>          Delete NiFi StatefulSet + Services + PVC + Route + NetworkPolicy
  wipe-kafka-data   <name> <ns>          Delete Kafka PVC (scales to 0 first, then back to 1)
  force-update-nifi <name> <ns> <pwd>   Clear SHA annotation and redeploy NiFi
  reset-password    <name> <ns> <pwd>   Reset NiFi login password
  restart-kafka     <name> <ns>          Delete Kafka pod (StatefulSet restarts it)
  restart-nifi      <name> <ns>          Delete NiFi pod (StatefulSet restarts it)

Event generator:
  pause-events    Scale event generator to 0 replicas
  resume-events   Scale event generator to 1 replica
  remove-events   Delete entire event generator deployment

Bulk operations:
  remove-all-teams      Remove all configured teams (Kafka + NiFi, namespaces kept)
  teardown-all          Cancel runs → remove events → remove all teams
  teardown-all --clean  Same + delete all PipelineRuns, TaskRuns, and workspace PVCs (full clean slate)
  reset-all             teardown-all then re-run the reset pipeline
  cleanup-runs          Keep 3 PipelineRuns + 5 TaskRuns, delete the rest (requires tkn)

Observability:
  status      <name> <ns>   Show pods, services, PVCs, routes, and events for one team
  status-all                Show pods, PVCs, routes, and pipeline runs across all namespaces

Note: namespaces are NEVER deleted by ops.sh.
      For full decommission, use: bash tekton/cleanup.sh
HELP
}

# ── Main dispatch ──────────────────────────────────────────────────────────────
case "$COMMAND" in
  add-team)           cmd_add_team            "${ARGS[@]}" ;;
  add-kafka)          cmd_add_kafka           "${ARGS[@]}" ;;
  add-nifi)           cmd_add_nifi            "${ARGS[@]}" ;;
  remove-team)        cmd_remove_team         "${ARGS[@]}" ;;
  reset-team)         cmd_reset_team          "${ARGS[@]}" ;;
  remove-kafka)       cmd_remove_kafka        "${ARGS[@]}" ;;
  remove-nifi)        cmd_remove_nifi         "${ARGS[@]}" ;;
  wipe-kafka-data)    cmd_wipe_kafka_data     "${ARGS[@]}" ;;
  force-update-nifi)  cmd_force_update_nifi   "${ARGS[@]}" ;;
  reset-password)     cmd_reset_password      "${ARGS[@]}" ;;
  restart-kafka)      cmd_restart_kafka       "${ARGS[@]}" ;;
  restart-nifi)       cmd_restart_nifi        "${ARGS[@]}" ;;
  pause-events)       cmd_pause_events ;;
  resume-events)      cmd_resume_events ;;
  remove-events)      cmd_remove_events ;;
  remove-all-teams)   cmd_remove_all_teams ;;
  teardown-all)       cmd_teardown_all ;;
  reset-all)          cmd_reset_all ;;
  cleanup-runs)       cmd_cleanup_runs ;;
  status)             cmd_status              "${ARGS[@]}" ;;
  status-all)         cmd_status_all ;;
  help|--help|-h)     cmd_help ;;
  *)
    err "Unknown command: ${COMMAND}"
    echo ""
    cmd_help
    exit 1
    ;;
esac
