#!/usr/bin/env bash
# pipeline/ops.sh — Day-to-day classroom operations
#
# All commands use plain oc calls — no Tekton involvement (except cleanup-runs).
# Run from repo root or from pipeline/ — the script finds config.env automatically.
#
# Usage:
#   bash pipeline/ops.sh <command> [args...]  [--dry-run]
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
#   ChatOps
#     rebuild-chatops          Trigger ChatOps rebuild from Git (cluster must reach GitHub)
#     rebuild-chatops --local  Trigger binary build from local repo root (offline / CRC)
#
#   Config sync
#     export-config   Print team registry as config.env block (shows passwords from cluster)
#     sync-config     Auto-update config.env in-place from cluster registry + passwords
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
  CONFIG_ENV_PATH="${REPO_ROOT}/config.env"

  if ! oc whoami &>/dev/null; then
    err "Not logged into OpenShift — run 'oc login' first"
    exit 1
  fi
fi

# ── Private helpers ────────────────────────────────────────────────────────────

_upsert_team_registry() {
  local name="$1" ns="$2"
  local bootstrap="kafka-${name}.${ns}.svc.cluster.local:9092"
  local value="namespace=${ns},bootstrap=${bootstrap}"
  oc get configmap team-registry -n "${INFRA_NAMESPACE}" &>/dev/null \
    || oc create configmap team-registry -n "${INFRA_NAMESPACE}"
  run "oc patch configmap team-registry -n '${INFRA_NAMESPACE}' \
    --type merge -p '{\"data\":{\"${name}\":\"${value}\"}}'"
}

_remove_from_team_registry() {
  local name="$1"
  # JSON Merge Patch: setting a key to null removes it (RFC 7386)
  run "oc patch configmap team-registry -n '${INFRA_NAMESPACE}' \
    --type merge -p '{\"data\":{\"${name}\":null}}' 2>/dev/null || true"
}

_upsert_team_password() {
  local name="$1" pwd="$2"
  oc get secret team-passwords -n "${INFRA_NAMESPACE}" &>/dev/null \
    || oc create secret generic team-passwords -n "${INFRA_NAMESPACE}"
  # base64-encode the password so special chars (!, $, ", etc.) are safe in JSON
  local encoded
  encoded=$(printf '%s' "${pwd}" | base64)
  run "oc patch secret team-passwords -n '${INFRA_NAMESPACE}' \
    --type merge -p '{\"data\":{\"${name}\":\"${encoded}\"}}'"
}

_remove_team_password() {
  local name="$1"
  run "oc patch secret team-passwords -n '${INFRA_NAMESPACE}' \
    --type merge -p '{\"data\":{\"${name}\":null}}' 2>/dev/null || true"
}

_patch_event_generator() {
  # Rebuild TEAM_BOOTSTRAP_SERVERS from team-registry and patch + restart EG.
  # Always patches ConfigMap (clears stale entries even when registry is empty).
  # Skips rollout restart if registry is empty — avoids crash-loop with no Kafka.
  local bootstrap_str=""
  if oc get configmap team-registry -n "${INFRA_NAMESPACE}" &>/dev/null; then
    bootstrap_str=$(oc get configmap team-registry \
      -n "${INFRA_NAMESPACE}" -o json \
      | python3 -c "
import sys, json
data = json.load(sys.stdin).get('data', {})
parts = []
for name, val in sorted(data.items()):
    entry = dict(kv.split('=', 1) for kv in val.split(',') if '=' in kv)
    if 'bootstrap' in entry:
        parts.append(f'{name}={entry[\"bootstrap\"]}')
print(','.join(parts))
" 2>/dev/null || echo "")
  fi

  local eg_cm
  eg_cm=$(oc get configmap -n "${INFRA_NAMESPACE}" \
    -l "app=${EVENT_GENERATOR_NAME}" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
  if [[ -z "${eg_cm}" ]]; then
    warn "Event-generator ConfigMap not found — skipping EG patch"
    return
  fi
  run "oc patch configmap '${eg_cm}' -n '${INFRA_NAMESPACE}' \
    --type merge -p '{\"data\":{\"TEAM_BOOTSTRAP_SERVERS\":\"${bootstrap_str}\"}}'"

  if [[ -z "${bootstrap_str}" ]]; then
    info "Registry empty — EG ConfigMap cleared, restart skipped"
    return
  fi
  run "oc rollout restart deployment/'${EVENT_GENERATOR_NAME}' -n '${INFRA_NAMESPACE}'"
  ok "Event-generator patched and restarted"
}

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
  info "Waiting for kafka-${name}-0 to be Ready (max 2 min)..."
  oc wait pod "kafka-${name}-0" \
    --for=condition=Ready --timeout=120s -n "${ns}" 2>/dev/null \
    || warn "Kafka pod not ready yet — EG may not connect to ${name} on first try"
  _upsert_team_registry "${name}" "${ns}"
  _upsert_team_password "${name}" "${pwd}"
  _patch_event_generator
  _do_add_nifi "${name}" "${ns}" "${pwd}"
  ok "Team ${name} deployed in ${ns}"
}

cmd_add_kafka() {
  local name="${1:?Usage: add-kafka <name> <ns>}"
  local ns="${2:?Usage: add-kafka <name> <ns>}"
  _do_add_kafka "${name}" "${ns}"
  info "Waiting for kafka-${name}-0 to be Ready (max 2 min)..."
  oc wait pod "kafka-${name}-0" \
    --for=condition=Ready --timeout=120s -n "${ns}" 2>/dev/null \
    || warn "Kafka pod not ready yet — EG may not connect to ${name} on first try"
  _upsert_team_registry "${name}" "${ns}"
  _patch_event_generator
  ok "Kafka deployed for ${name} in ${ns}"
}

cmd_add_nifi() {
  local name="${1:?Usage: add-nifi <name> <ns> <pwd>}"
  local ns="${2:?Usage: add-nifi <name> <ns> <pwd>}"
  local pwd="${3:?Usage: add-nifi <name> <ns> <pwd>}"
  _do_add_nifi "${name}" "${ns}" "${pwd}"
  _upsert_team_password "${name}" "${pwd}"
  ok "NiFi deployed for ${name} in ${ns}"
}

cmd_remove_team() {
  local name="${1:?Usage: remove-team <name> <ns>}"
  local ns="${2:?Usage: remove-team <name> <ns>}"
  confirm "Remove Kafka + NiFi for ${name} in ${ns}?"
  _do_remove_kafka "${name}" "${ns}"
  _remove_from_team_registry "${name}"
  _remove_team_password "${name}"
  _patch_event_generator
  _do_remove_nifi "${name}" "${ns}"
  ok "Team ${name} removed from ${ns}"
}

cmd_reset_team() {
  local name="${1:?Usage: reset-team <name> <ns> <pwd>}"
  local ns="${2:?Usage: reset-team <name> <ns> <pwd>}"
  local pwd="${3:?Usage: reset-team <name> <ns> <pwd>}"
  confirm "Remove then redeploy ${name} in ${ns}?"
  # Remove phase
  _do_remove_kafka "${name}" "${ns}"
  _remove_from_team_registry "${name}"
  _remove_team_password "${name}"
  _patch_event_generator
  _do_remove_nifi "${name}" "${ns}"
  # Redeploy phase
  _do_add_kafka "${name}" "${ns}"
  info "Waiting for kafka-${name}-0 to be Ready (max 2 min)..."
  oc wait pod "kafka-${name}-0" \
    --for=condition=Ready --timeout=120s -n "${ns}" 2>/dev/null \
    || warn "Kafka pod not ready yet — EG may not connect to ${name} on first try"
  _upsert_team_registry "${name}" "${ns}"
  _upsert_team_password "${name}" "${pwd}"
  _patch_event_generator
  _do_add_nifi "${name}" "${ns}" "${pwd}"
  ok "Team ${name} reset in ${ns}"
}

cmd_remove_kafka() {
  local name="${1:?Usage: remove-kafka <name> <ns>}"
  local ns="${2:?Usage: remove-kafka <name> <ns>}"
  confirm "Delete Kafka StatefulSet + Services + PVC for ${name} in ${ns}?"
  _do_remove_kafka "${name}" "${ns}"
  _remove_from_team_registry "${name}"
  _patch_event_generator
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
  _upsert_team_password "${name}" "${pwd}"
  ok "NiFi force-updated for ${name} in ${ns}"
}

cmd_reset_password() {
  local name="${1:?Usage: reset-password <name> <ns> <pwd>}"
  local ns="${2:?Usage: reset-password <name> <ns> <pwd>}"
  local pwd="${3:?Usage: reset-password <name> <ns> <pwd>}"

  if [[ ${#pwd} -lt 12 ]]; then
    err "Password must be at least 12 characters (NiFi requirement)."
    exit 1
  fi

  if ! oc get pod "nifi-${name}-0" -n "${ns}" &>/dev/null; then
    err "Pod nifi-${name}-0 not found in ${ns} — is NiFi deployed?"
    exit 1
  fi

  # This NiFi image regenerates the bcrypt hash from SINGLE_USER_CREDENTIALS_PASSWORD
  # on every pod start, overwriting anything written by nifi.sh set-single-user-credentials.
  # The only reliable way to change the password is to update the env var in the
  # StatefulSet spec, then delete the pod so it restarts with the new value.
  info "Patching SINGLE_USER_CREDENTIALS_PASSWORD in StatefulSet..."
  run "oc set env statefulset/'nifi-${name}' \
    SINGLE_USER_CREDENTIALS_PASSWORD='${pwd}' -n '${ns}'"

  info "Restarting NiFi pod to pick up new password..."
  run "oc delete pod 'nifi-${name}-0' -n '${ns}'"
  _upsert_team_password "${name}" "${pwd}"
  ok "Password reset for ${name} in ${ns}. NiFi pod restarting — ready in ~2 min."
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
    _remove_from_team_registry "${name}"
    _remove_team_password "${name}"
  done
  _patch_event_generator
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
  run "bash '${REPO_ROOT}/pipeline/setup.sh' --reset"
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
  local issues=()

  echo "${name} / ${ns}"
  echo ""

  # All active pods — any app, any naming convention (skip Succeeded)
  local pod_out
  pod_out=$(oc get pods -n "${ns}" --no-headers \
    -o custom-columns='NAME:.metadata.name,PHASE:.status.phase' 2>/dev/null \
    | grep -v "Succeeded" || echo "")
  if [[ -z "${pod_out}" ]]; then
    echo "  [?]  no active pods"
    issues+=("No pods running in ${ns}")
  else
    while IFS= read -r pod_line; do
      [[ -z "${pod_line}" ]] && continue
      local pname pphase
      pname=$(echo "${pod_line}" | awk '{print $1}')
      pphase=$(echo "${pod_line}" | awk '{print $2}')
      if [[ "${pphase}" == "Running" ]]; then
        printf "  [OK]  %s  Running\n" "${pname}"
      else
        printf "  [X]   %s  %s\n" "${pname}" "${pphase}"
        issues+=("${pname} is ${pphase}")
      fi
    done <<< "${pod_out}"
  fi

  echo ""

  # All PVCs
  local pvc_out
  pvc_out=$(oc get pvc -n "${ns}" --no-headers \
    -o custom-columns='NAME:.metadata.name,PHASE:.status.phase' 2>/dev/null || echo "")
  if [[ -z "${pvc_out}" ]]; then
    echo "  PVCs    [?]  none"
  else
    local pvc_summary="  PVCs    "
    local first=true
    while IFS= read -r pvc_line; do
      [[ -z "${pvc_line}" ]] && continue
      local pname pphase
      pname=$(echo "${pvc_line}" | awk '{print $1}')
      pphase=$(echo "${pvc_line}" | awk '{print $2}')
      [[ "${first}" == "true" ]] || pvc_summary+="  "
      if [[ "${pphase}" == "Bound" ]]; then
        pvc_summary+="[OK] ${pname}"
      else
        pvc_summary+="[X] ${pname}(${pphase})"
        issues+=("PVC ${pname} is ${pphase}")
      fi
      first=false
    done <<< "${pvc_out}"
    echo "${pvc_summary}"
  fi

  # All routes
  local route_out
  route_out=$(oc get route -n "${ns}" --no-headers \
    -o custom-columns='NAME:.metadata.name,HOST:.spec.host' 2>/dev/null || echo "")
  if [[ -n "${route_out}" ]]; then
    while IFS= read -r route_line; do
      [[ -z "${route_line}" ]] && continue
      local rname rhost
      rname=$(echo "${route_line}" | awk '{print $1}')
      rhost=$(echo "${route_line}" | awk '{print $2}')
      [[ -n "${rhost}" ]] && echo "  Route   ${rname}  https://${rhost}"
    done <<< "${route_out}"
  fi

  # Issues
  if [[ ${#issues[@]} -gt 0 ]]; then
    echo ""
    echo "Action needed:"
    for issue in "${issues[@]}"; do
      echo "  [X] ${issue}"
    done
  fi
}

cmd_status_all() {
  # status-all is read-only — always executes, even in --dry-run mode
  local issues=()
  local chatops_name="${CHATOPS_NAME:-slack-chatops}"

  echo "Cluster Overview"
  echo ""

  # ── Infra services — all Deployments + StatefulSets (excludes Tekton/build pods) ──
  echo "Infra"

  local workload_out
  workload_out=$(oc get deployment,statefulset -n "${INFRA_NAMESPACE}" --no-headers \
    -o custom-columns='NAME:.metadata.name,DESIRED:.spec.replicas,READY:.status.readyReplicas' \
    2>/dev/null || echo "")
  if [[ -z "${workload_out}" ]]; then
    echo "  (no deployments found)"
  else
    while IFS= read -r wl; do
      [[ -z "${wl}" ]] && continue
      local wname wdesired wready
      wname=$(echo "${wl}" | awk '{print $1}')
      wdesired=$(echo "${wl}" | awk '{print $2}')
      wready=$(echo "${wl}" | awk '{print $3}')
      wdesired="${wdesired:-1}"
      wready="${wready:-0}"
      if [[ "${wready}" == "${wdesired}" ]]; then
        printf "  %-28s [OK] Running (%s/%s)\n" "${wname}" "${wready}" "${wdesired}"
      elif [[ "${wready}" -gt 0 ]]; then
        printf "  %-28s [!]  Degraded (%s/%s ready)\n" "${wname}" "${wready}" "${wdesired}"
        issues+=("infra/${wname}: degraded (${wready}/${wdesired} ready)")
      else
        printf "  %-28s [X]  Down (0/%s ready)\n" "${wname}" "${wdesired}"
        issues+=("infra/${wname} is down")
      fi
    done <<< "${workload_out}"
  fi

  # Last pipeline run
  local pr_line
  pr_line=$(oc get pipelinerun -n "${INFRA_NAMESPACE}" \
    --sort-by='.metadata.creationTimestamp' --no-headers \
    -o custom-columns='NAME:.metadata.name,STATUS:.status.conditions[0].reason,DATE:.metadata.creationTimestamp' \
    2>/dev/null | tail -1 || echo "")
  if [[ -z "${pr_line}" ]]; then
    printf "  %-26s [-]  no runs found\n" "pipeline"
  else
    local pr_name pr_status pr_date
    pr_name=$(echo "${pr_line}" | awk '{print $1}')
    pr_status=$(echo "${pr_line}" | awk '{print $2}')
    pr_date=$(echo "${pr_line}" | awk '{print $3}' | cut -c1-10)
    local pr_icon="[OK]"
    [[ "${pr_status}" == "Failed" || "${pr_status}" == "PipelineRunCancelled" ]] && pr_icon="[X]"
    [[ "${pr_status}" == "Running" ]] && pr_icon="[>>]"
    printf "  %-26s %s %s  %s  %s\n" "pipeline" "${pr_icon}" "${pr_status}" "${pr_name: -30}" "${pr_date}"
    [[ "${pr_icon}" == "[X]" ]] && issues+=("Last pipeline run failed: ${pr_name}")
  fi

  # ── Teams ───────────────────────────────────────────────
  echo ""
  echo "Teams"

  local namespaces
  namespaces=$(oc get projects --no-headers \
    -o custom-columns='NAME:.metadata.name' 2>/dev/null \
    | grep -v "^${INFRA_NAMESPACE}$" \
    | grep -v "^openshift" \
    | grep -v "^kube" \
    | grep -v "^default$" \
    | grep -v "^kube-public$" \
    | grep -v "^kube-node-lease$" \
    || true)

  if [[ -z "${namespaces}" ]]; then
    echo "  (no team namespaces found)"
  else
    while IFS= read -r ns; do
      [[ -z "${ns}" ]] && continue
      # Derive team name from namespace: team-01 → team01
      local team_name="${ns//-/}"

      echo "  ${ns}"

      # All active pods — any app, any naming convention
      local pod_out pod_summary="    Pods    "
      pod_out=$(oc get pods -n "${ns}" --no-headers \
        -o custom-columns='NAME:.metadata.name,PHASE:.status.phase' 2>/dev/null \
        | grep -v "Succeeded" || echo "")
      if [[ -z "${pod_out}" ]]; then
        pod_summary+="[?] none running"
        issues+=("${ns}: no active pods")
      else
        local first_pod=true
        while IFS= read -r pod_line_raw; do
          [[ -z "${pod_line_raw}" ]] && continue
          local pname pphase
          pname=$(echo "${pod_line_raw}" | awk '{print $1}')
          pphase=$(echo "${pod_line_raw}" | awk '{print $2}')
          [[ "${first_pod}" == "true" ]] || pod_summary+=",  "
          if [[ "${pphase}" == "Running" ]]; then
            pod_summary+="[OK] ${pname}"
          else
            pod_summary+="[X] ${pname}(${pphase})"
            issues+=("${ns}: ${pname} is ${pphase}")
          fi
          first_pod=false
        done <<< "${pod_out}"
      fi
      echo "${pod_summary}"

      # All PVCs
      local pvc_out pvc_summary="    PVCs    "
      pvc_out=$(oc get pvc -n "${ns}" --no-headers \
        -o custom-columns='NAME:.metadata.name,PHASE:.status.phase' 2>/dev/null || echo "")
      if [[ -z "${pvc_out}" ]]; then
        pvc_summary+="(none)"
      else
        local first_pvc=true
        while IFS= read -r pvc_line_raw; do
          [[ -z "${pvc_line_raw}" ]] && continue
          local pvc_name pvc_phase
          pvc_name=$(echo "${pvc_line_raw}" | awk '{print $1}')
          pvc_phase=$(echo "${pvc_line_raw}" | awk '{print $2}')
          [[ "${first_pvc}" == "true" ]] || pvc_summary+=",  "
          if [[ "${pvc_phase}" == "Bound" ]]; then
            pvc_summary+="[OK] ${pvc_name}"
          else
            pvc_summary+="[X] ${pvc_name}(${pvc_phase})"
            issues+=("${ns}: PVC ${pvc_name} is ${pvc_phase}")
          fi
          first_pvc=false
        done <<< "${pvc_out}"
      fi
      echo "${pvc_summary}"

      # All routes
      local route_out
      route_out=$(oc get route -n "${ns}" --no-headers \
        -o custom-columns='HOST:.spec.host' 2>/dev/null || echo "")
      if [[ -n "${route_out}" ]]; then
        local route_summary="    Routes  "
        local first_route=true
        while IFS= read -r rhost; do
          [[ -z "${rhost}" ]] && continue
          [[ "${first_route}" == "true" ]] || route_summary+=",  "
          route_summary+="https://${rhost}"
          first_route=false
        done <<< "${route_out}"
        echo "${route_summary}"
      else
        echo "    Routes  (none)"
      fi

    done <<< "${namespaces}"
  fi

  # ── Issues summary ──────────────────────────────────────
  echo ""
  if [[ ${#issues[@]} -gt 0 ]]; then
    echo "Issues (${#issues[@]})"
    for issue in "${issues[@]}"; do
      echo "  [X] ${issue}"
    done
  else
    echo "All systems healthy [OK]"
  fi
  echo ""
}

cmd_rebuild_chatops() {
  local local_build=false
  for arg in "${ARGS[@]:-}"; do
    [[ "$arg" == "--local" ]] && local_build=true
  done

  local chatops_name="${CHATOPS_NAME:-slack-chatops}"

  if [[ "$local_build" == "true" ]]; then
    info "Starting binary build of ${chatops_name} from local repo root..."
    info "(Use this on clusters without GitHub access, e.g. CRC)"
    run "oc start-build '${chatops_name}' \
      --from-dir='${REPO_ROOT}' \
      --follow \
      -n '${INFRA_NAMESPACE}'"
  else
    info "Starting git-based build of ${chatops_name} from ${GIT_REPO_URL} (${GIT_BRANCH})..."
    run "oc start-build '${chatops_name}' \
      --follow \
      -n '${INFRA_NAMESPACE}'"
  fi

  ok "Build complete — new pod rolling out"
  info "Check pod status: oc get pod -l app=${chatops_name} -n ${INFRA_NAMESPACE}"
}

cmd_export_config() {
  if ! oc get configmap team-registry -n "${INFRA_NAMESPACE}" &>/dev/null; then
    warn "team-registry ConfigMap not found. Run setup.sh first."
    return 1
  fi
  local registry_json passwords_json
  registry_json=$(oc get configmap team-registry -n "${INFRA_NAMESPACE}" -o json)
  passwords_json=$(oc get secret team-passwords -n "${INFRA_NAMESPACE}" -o json 2>/dev/null \
    || echo '{"data":{}}')

  python3 -c "
import sys, json, base64
registry = json.loads(sys.argv[1]).get('data', {})
pwd_data = json.loads(sys.argv[2]).get('data', {})
if not registry:
    print('# team-registry is empty. Nothing to export.')
    sys.exit(0)
teams = sorted(registry.items())
print('# Team config from cluster — paste into config.env')
print()
for i, (name, val) in enumerate(teams, 1):
    entry = dict(kv.split('=', 1) for kv in val.split(',') if '=' in kv)
    ns = entry.get('namespace', 'unknown')
    pwd_b64 = pwd_data.get(name, '')
    pwd = base64.b64decode(pwd_b64).decode() if pwd_b64 else '<set-manually>'
    print(f'export TEAM{i}_NAME={name}')
    print(f'export TEAM{i}_NAMESPACE={ns}')
    print(f'export TEAM{i}_PASSWORD={pwd}')
    print()
for i in range(len(teams) + 1, 16):
    print(f'export TEAM{i}_NAME=skip')
    print(f'export TEAM{i}_NAMESPACE=skip')
    print(f'export TEAM{i}_PASSWORD=skip')
    print()
parts = []
for name, val in teams:
    entry = dict(kv.split('=', 1) for kv in val.split(',') if '=' in kv)
    if 'bootstrap' in entry:
        parts.append(f'{name}={entry[\"bootstrap\"]}')
print(f'export TEAM_BOOTSTRAP_SERVERS=\"{\",\".join(parts)}\"')
" "${registry_json}" "${passwords_json}"
}

cmd_sync_config() {
  local config_file="${CONFIG_ENV_PATH:-config.env}"
  if [[ ! -f "${config_file}" ]]; then
    warn "config.env not found at ${config_file}"
    return 1
  fi
  if ! oc get configmap team-registry -n "${INFRA_NAMESPACE}" &>/dev/null; then
    warn "team-registry ConfigMap not found. Run setup.sh first."
    return 1
  fi

  local registry_json passwords_json
  registry_json=$(oc get configmap team-registry -n "${INFRA_NAMESPACE}" -o json)
  passwords_json=$(oc get secret team-passwords -n "${INFRA_NAMESPACE}" -o json 2>/dev/null \
    || echo '{"data":{}}')

  python3 -c "
import sys, json, base64, re

config_path = sys.argv[1]
registry = json.loads(sys.argv[2]).get('data', {})
pwd_data = json.loads(sys.argv[3]).get('data', {})

if not registry:
    print('team-registry is empty. Nothing to sync.')
    sys.exit(0)

teams = sorted(registry.items())

# Build per-index lookup (1-based)
team_lookup = {}
for i, (name, val) in enumerate(teams, 1):
    entry = dict(kv.split('=', 1) for kv in val.split(',') if '=' in kv)
    ns = entry.get('namespace', 'unknown')
    pwd_b64 = pwd_data.get(name, '')
    pwd = base64.b64decode(pwd_b64).decode() if pwd_b64 else '<set-manually>'
    team_lookup[i] = (name, ns, pwd)
for i in range(len(teams) + 1, 16):
    team_lookup[i] = ('skip', 'skip', 'skip')

# Build bootstrap value
parts = []
for name, val in teams:
    entry = dict(kv.split('=', 1) for kv in val.split(',') if '=' in kv)
    if 'bootstrap' in entry:
        parts.append(f'{name}={entry[\"bootstrap\"]}')
bootstrap_val = ','.join(parts)

name_pat = re.compile(r'^export TEAM(\d+)_NAME=')
ns_pat   = re.compile(r'^export TEAM(\d+)_NAMESPACE=')
pwd_pat  = re.compile(r'^export TEAM(\d+)_PASSWORD=')
bs_pat   = re.compile(r'^export TEAM_BOOTSTRAP_SERVERS=')

with open(config_path) as f:
    lines = f.read().split('\n')

out = []
for line in lines:
    m = name_pat.match(line)
    if m:
        i = int(m.group(1))
        name, ns, pwd = team_lookup.get(i, ('skip', 'skip', 'skip'))
        out.append(f'export TEAM{i}_NAME={name}')
        continue
    m = ns_pat.match(line)
    if m:
        i = int(m.group(1))
        name, ns, pwd = team_lookup.get(i, ('skip', 'skip', 'skip'))
        out.append(f'export TEAM{i}_NAMESPACE={ns}')
        continue
    m = pwd_pat.match(line)
    if m:
        i = int(m.group(1))
        name, ns, pwd = team_lookup.get(i, ('skip', 'skip', 'skip'))
        out.append(f'export TEAM{i}_PASSWORD=\"{pwd}\"')
        continue
    if bs_pat.match(line):
        out.append(f'export TEAM_BOOTSTRAP_SERVERS=\"{bootstrap_val}\"')
        continue
    out.append(line)

with open(config_path, 'w') as f:
    f.write('\n'.join(out))
print(f'config.env updated in-place with {len(teams)} team(s)')
" "${config_file}" "${registry_json}" "${passwords_json}"
  ok "config.env synced from cluster"
}

cmd_help() {
  cat <<'HELP'
pipeline/ops.sh — Day-to-day classroom operations

Usage: bash pipeline/ops.sh <command> [args...] [--dry-run]
       FORCE=true bash pipeline/ops.sh <command>
       WIPE_PVCS=true bash pipeline/ops.sh teardown-all

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

ChatOps:
  rebuild-chatops           Trigger git-based build (cluster must reach GitHub)
  rebuild-chatops --local   Binary build from local repo root (for CRC / offline clusters)

Config sync:
  export-config   Print team registry as config.env block (shows passwords from cluster)
  sync-config     Auto-update config.env in-place from cluster registry + passwords

Observability:
  status      <name> <ns>   Show pods, services, PVCs, routes, and events for one team
  status-all                Show pods, PVCs, routes, and pipeline runs across all namespaces

Note: namespaces are NEVER deleted by ops.sh.
      For full decommission, use: bash pipeline/cleanup.sh
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
  rebuild-chatops)    cmd_rebuild_chatops ;;
  export-config)      cmd_export_config ;;
  sync-config)        cmd_sync_config ;;
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
