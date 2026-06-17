#!/usr/bin/env bash
# onboarding/apply-onboarding.sh — Cluster onboarding: namespaces, quotas, RBAC
#
# Run this ONCE as kubeadmin before running pipeline/setup.sh.
# Creates: infra namespace, team namespaces, ResourceQuota, LimitRange,
#          ConfigMap/Secret per team, OpenShift Groups, RoleBindings.
#
# Usage:
#   bash onboarding/apply-onboarding.sh                        # full onboarding
#   bash onboarding/apply-onboarding.sh --dry-run              # print commands, no changes
#   bash onboarding/apply-onboarding.sh --skip-rbac            # skip human RBAC (re-run quotas/configmaps)
#   bash onboarding/apply-onboarding.sh --teams-only           # skip infra ns + infra RBAC
#   bash onboarding/apply-onboarding.sh --infra-only           # only infra ns + infra RBAC
#   bash onboarding/apply-onboarding.sh --from-team=3          # start team loop from team 3 (add new teams only)
#
# Adding a new team (e.g. team-03 when team-01 and team-02 already exist):
#   1. Edit cluster.env: set NUM_TEAMS=3
#   2. bash onboarding/apply-onboarding.sh --teams-only --from-team=3
#
# Run from repo root or from onboarding/ — the script finds cluster.env automatically.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Parse flags ────────────────────────────────────────────────────────────────
SKIP_RBAC=false
TEAMS_ONLY=false
INFRA_ONLY=false
DRY_RUN=false
FROM_TEAM=1

for arg in "$@"; do
  case "$arg" in
    --skip-rbac)   SKIP_RBAC=true ;;
    --teams-only)  TEAMS_ONLY=true ;;
    --infra-only)  INFRA_ONLY=true ;;
    --dry-run)     DRY_RUN=true ;;
    --from-team=*) FROM_TEAM="${arg#--from-team=}" ;;
    *) echo "Unknown flag: $arg"
       echo "Usage: $0 [--dry-run] [--skip-rbac] [--teams-only] [--infra-only] [--from-team=N]"
       exit 1 ;;
  esac
done

# ── Helpers ────────────────────────────────────────────────────────────────────
info() { echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }

run() {
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "  [dry-run] $*"
  else
    eval "$@"
  fi
}

# ── Step 1: Load & Validate Config ─────────────────────────────────────────────
echo "============================================================"
echo " Cluster Onboarding"
echo "============================================================"
echo ""
info "Step 1 — Loading config..."

CONFIG_FILE="${SCRIPT_DIR}/cluster.env"
if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "ERROR: cluster.env not found at ${CONFIG_FILE}"
  echo "       Copy onboarding/cluster.env.example to onboarding/cluster.env and fill in your values."
  exit 1
fi

source "$CONFIG_FILE"

# Save NUM_TEAMS now — sourcing config.env later (for configmap step) would overwrite it
ONBOARDING_NUM_TEAMS="${NUM_TEAMS}"

# Validate required variables
MISSING=()
for var in INFRA_NAMESPACE TEAM_NAMESPACE_PREFIX NUM_TEAMS STORAGE_CLASS \
           INFRA_RESOURCE_QUOTA_CPU INFRA_RESOURCE_QUOTA_MEMORY INFRA_RESOURCE_QUOTA_STORAGE \
           INFRA_LIMIT_POD_CPU_MAX INFRA_LIMIT_POD_MEM_MAX \
           INFRA_LIMIT_CONTAINER_CPU_MAX INFRA_LIMIT_CONTAINER_MEM_MAX \
           INFRA_LIMIT_CONTAINER_CPU_DEFAULT INFRA_LIMIT_CONTAINER_MEM_DEFAULT \
           INFRA_LIMIT_CONTAINER_CPU_REQUEST INFRA_LIMIT_CONTAINER_MEM_REQUEST \
           RESOURCE_QUOTA_CPU RESOURCE_QUOTA_MEMORY RESOURCE_QUOTA_STORAGE \
           INFRA_ADMIN_GROUP TEAM_GROUP_SUFFIX; do
  val="${!var:-}"
  if [[ -z "$val" ]]; then
    MISSING+=("$var")
  fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "ERROR: The following cluster.env variables are missing or empty:"
  for m in "${MISSING[@]}"; do echo "         $m"; done
  echo "       Edit onboarding/cluster.env and re-run."
  exit 1
fi

ok "Config loaded — infra: ${INFRA_NAMESPACE}, teams: ${NUM_TEAMS}, storage: ${STORAGE_CLASS}"

# ── Step 2: Prerequisite Checks ────────────────────────────────────────────────
echo ""
info "Step 2 — Checking prerequisites..."

# Must be logged in
if ! oc whoami &>/dev/null; then
  echo "ERROR: Not logged into OpenShift — run 'oc login' first"
  exit 1
fi
ok "Logged in as $(oc whoami)"

# OpenShift Pipelines operator must be installed before onboarding
if ! oc get pods -n openshift-pipelines &>/dev/null; then
  echo ""
  echo "ERROR: OpenShift Pipelines operator is not installed."
  echo "       Install it before running this script:"
  echo "       See onboarding/README.md → Prerequisites → Install OpenShift Pipelines"
  echo ""
  echo "       Quick install via CLI:"
  echo "         oc apply -f - <<EOF"
  echo "         apiVersion: operators.coreos.com/v1alpha1"
  echo "         kind: Subscription"
  echo "         metadata:"
  echo "           name: openshift-pipelines-operator"
  echo "           namespace: openshift-operators"
  echo "         spec:"
  echo "           channel: latest"
  echo "           name: openshift-pipelines-operator-rh"
  echo "           source: redhat-operators"
  echo "           sourceNamespace: openshift-marketplace"
  echo "         EOF"
  echo "         oc get pods -n openshift-pipelines   # verify install"
  exit 1
fi
ok "OpenShift Pipelines operator found"

# Storage class must exist — prevents silent PVC Pending failures later
if ! oc get storageclass "${STORAGE_CLASS}" &>/dev/null; then
  echo ""
  echo "ERROR: Storage class '${STORAGE_CLASS}' not found on this cluster."
  echo "       Available storage classes:"
  oc get storageclass --no-headers 2>/dev/null | awk '{print "         " $1}' || true
  echo ""
  echo "       Update STORAGE_CLASS in onboarding/cluster.env and re-run."
  exit 1
fi
ok "Storage class '${STORAGE_CLASS}' found"

# Auto-detect cluster type 
if oc auth can-i create clusterrolebindings &>/dev/null; then
  CLUSTER_TYPE="dedicated"
else
  CLUSTER_TYPE="shared"
fi
ok "Cluster type: ${CLUSTER_TYPE}"

# ── Step 3: Create Infra Namespace ─────────────────────────────────────────────
if [[ "$TEAMS_ONLY" == "false" ]]; then
  echo ""
  info "Step 3 — Creating infra namespace (${INFRA_NAMESPACE})..."
  run "INFRA_NAMESPACE='${INFRA_NAMESPACE}' \
    envsubst '\${INFRA_NAMESPACE}' \
    < '${SCRIPT_DIR}/01-infra-namespace.yaml' | oc apply -f -"

  run "INFRA_NAMESPACE='${INFRA_NAMESPACE}' \
    INFRA_LIMIT_POD_CPU_MAX='${INFRA_LIMIT_POD_CPU_MAX}' INFRA_LIMIT_POD_MEM_MAX='${INFRA_LIMIT_POD_MEM_MAX}' \
    INFRA_LIMIT_CONTAINER_CPU_MAX='${INFRA_LIMIT_CONTAINER_CPU_MAX}' INFRA_LIMIT_CONTAINER_MEM_MAX='${INFRA_LIMIT_CONTAINER_MEM_MAX}' \
    INFRA_LIMIT_CONTAINER_CPU_DEFAULT='${INFRA_LIMIT_CONTAINER_CPU_DEFAULT}' INFRA_LIMIT_CONTAINER_MEM_DEFAULT='${INFRA_LIMIT_CONTAINER_MEM_DEFAULT}' \
    INFRA_LIMIT_CONTAINER_CPU_REQUEST='${INFRA_LIMIT_CONTAINER_CPU_REQUEST}' INFRA_LIMIT_CONTAINER_MEM_REQUEST='${INFRA_LIMIT_CONTAINER_MEM_REQUEST}' \
    envsubst '\${INFRA_NAMESPACE} \${INFRA_LIMIT_POD_CPU_MAX} \${INFRA_LIMIT_POD_MEM_MAX} \
              \${INFRA_LIMIT_CONTAINER_CPU_MAX} \${INFRA_LIMIT_CONTAINER_MEM_MAX} \
              \${INFRA_LIMIT_CONTAINER_CPU_DEFAULT} \${INFRA_LIMIT_CONTAINER_MEM_DEFAULT} \
              \${INFRA_LIMIT_CONTAINER_CPU_REQUEST} \${INFRA_LIMIT_CONTAINER_MEM_REQUEST}' \
    < '${SCRIPT_DIR}/01b-infra-limitrange.yaml' | oc apply -f -"

  run "INFRA_NAMESPACE='${INFRA_NAMESPACE}' \
    INFRA_RESOURCE_QUOTA_CPU='${INFRA_RESOURCE_QUOTA_CPU}' \
    INFRA_RESOURCE_QUOTA_MEMORY='${INFRA_RESOURCE_QUOTA_MEMORY}' \
    INFRA_RESOURCE_QUOTA_STORAGE='${INFRA_RESOURCE_QUOTA_STORAGE}' \
    envsubst '\${INFRA_NAMESPACE} \${INFRA_RESOURCE_QUOTA_CPU} \
              \${INFRA_RESOURCE_QUOTA_MEMORY} \${INFRA_RESOURCE_QUOTA_STORAGE}' \
    < '${SCRIPT_DIR}/01c-infra-resourcequota.yaml' | oc apply -f -"

  ok "Infra namespace ready (namespace + limitrange + quota)"
else
  info "Step 3 — Infra namespace (skipped — --teams-only)"
fi

# ── Step 4: Create OpenShift Groups ────────────────────────────────────────────
if [[ "$SKIP_RBAC" == "false" ]]; then
  echo ""
  info "Step 4 — Creating OpenShift Groups..."

  # infra-admins group (teachers/TAs)
  run "oc adm groups new '${INFRA_ADMIN_GROUP}' 2>/dev/null || true"
  ok "Group: ${INFRA_ADMIN_GROUP}"

  # Per-team student groups (only for teams being created — respects --from-team)
  for i in $(seq "${FROM_TEAM}" "${NUM_TEAMS}"); do
    TEAM_ID="$(printf '%02d' "$i")"
    GROUP_NAME="${TEAM_NAMESPACE_PREFIX}-${TEAM_ID}-${TEAM_GROUP_SUFFIX}"
    run "oc adm groups new '${GROUP_NAME}' 2>/dev/null || true"
    ok "Group: ${GROUP_NAME}"
  done
else
  info "Step 4 — Groups (skipped — --skip-rbac)"
fi

# ── Step 5: Apply Infra Human RBAC ─────────────────────────────────────────────
if [[ "$SKIP_RBAC" == "false" && "$TEAMS_ONLY" == "false" ]]; then
  echo ""
  info "Step 5 — Applying infra RBAC (${INFRA_ADMIN_GROUP} → edit in ${INFRA_NAMESPACE})..."
  run "INFRA_NAMESPACE='${INFRA_NAMESPACE}' INFRA_ADMIN_GROUP='${INFRA_ADMIN_GROUP}' \
    envsubst '\${INFRA_NAMESPACE} \${INFRA_ADMIN_GROUP}' \
    < '${SCRIPT_DIR}/06-infra-rbac.yaml' | oc apply -f -"
  ok "Infra RBAC applied"
else
  info "Step 5 — Infra RBAC (skipped)"
fi

# ── Step 6: Create Team Namespaces ─────────────────────────────────────────────
if [[ "$INFRA_ONLY" == "false" ]]; then
  echo ""
  TEAM_COUNT=$(( NUM_TEAMS - FROM_TEAM + 1 ))
  info "Step 6 — Creating ${TEAM_COUNT} team namespace(s) (team $(printf '%02d' "${FROM_TEAM}") to team $(printf '%02d' "${NUM_TEAMS}"))..."

  for i in $(seq "${FROM_TEAM}" "${NUM_TEAMS}"); do
    TEAM_ID="$(printf '%02d' "$i")"
    NS="${TEAM_NAMESPACE_PREFIX}-${TEAM_ID}"
    echo ""
    echo "         ── team ${TEAM_ID} (${NS}) ──"

    # Namespace
    run "TEAM_NAMESPACE_PREFIX='${TEAM_NAMESPACE_PREFIX}' TEAM_ID='${TEAM_ID}' \
      envsubst '\${TEAM_NAMESPACE_PREFIX} \${TEAM_ID}' \
      < '${SCRIPT_DIR}/02-team-namespace.yaml' | oc apply -f -"

    # LimitRange
    run "TEAM_NAMESPACE_PREFIX='${TEAM_NAMESPACE_PREFIX}' TEAM_ID='${TEAM_ID}' \
      LIMIT_POD_CPU_MAX='${LIMIT_POD_CPU_MAX}' LIMIT_POD_MEM_MAX='${LIMIT_POD_MEM_MAX}' \
      LIMIT_CONTAINER_CPU_MAX='${LIMIT_CONTAINER_CPU_MAX}' LIMIT_CONTAINER_MEM_MAX='${LIMIT_CONTAINER_MEM_MAX}' \
      LIMIT_CONTAINER_CPU_DEFAULT='${LIMIT_CONTAINER_CPU_DEFAULT}' LIMIT_CONTAINER_MEM_DEFAULT='${LIMIT_CONTAINER_MEM_DEFAULT}' \
      LIMIT_CONTAINER_CPU_REQUEST='${LIMIT_CONTAINER_CPU_REQUEST}' LIMIT_CONTAINER_MEM_REQUEST='${LIMIT_CONTAINER_MEM_REQUEST}' \
      envsubst '\${TEAM_NAMESPACE_PREFIX} \${TEAM_ID} \${LIMIT_POD_CPU_MAX} \${LIMIT_POD_MEM_MAX} \
                \${LIMIT_CONTAINER_CPU_MAX} \${LIMIT_CONTAINER_MEM_MAX} \
                \${LIMIT_CONTAINER_CPU_DEFAULT} \${LIMIT_CONTAINER_MEM_DEFAULT} \
                \${LIMIT_CONTAINER_CPU_REQUEST} \${LIMIT_CONTAINER_MEM_REQUEST}' \
      < '${SCRIPT_DIR}/03-limitrange.yaml' | oc apply -f -"

    # ResourceQuota
    run "TEAM_NAMESPACE_PREFIX='${TEAM_NAMESPACE_PREFIX}' TEAM_ID='${TEAM_ID}' \
      RESOURCE_QUOTA_CPU='${RESOURCE_QUOTA_CPU}' RESOURCE_QUOTA_MEMORY='${RESOURCE_QUOTA_MEMORY}' \
      RESOURCE_QUOTA_STORAGE='${RESOURCE_QUOTA_STORAGE}' \
      envsubst '\${TEAM_NAMESPACE_PREFIX} \${TEAM_ID} \
                \${RESOURCE_QUOTA_CPU} \${RESOURCE_QUOTA_MEMORY} \${RESOURCE_QUOTA_STORAGE}' \
      < '${SCRIPT_DIR}/04-resourcequota.yaml' | oc apply -f -"

    # ConfigMap + Secret (needs KAFKA_CLUSTER_NAME, NIFI_CLUSTER_NAME, EXTERNAL_DOMAIN from config.env)
    # Source config.env if it exists — gracefully skip configmap if not available
    MAIN_CONFIG="${REPO_ROOT}/config.env"
    if [[ -f "$MAIN_CONFIG" ]]; then
      # Use a subshell so config.env variables don't leak into the current shell
      run "(source '${MAIN_CONFIG}' && \
        TEAM_NAMESPACE_PREFIX='${TEAM_NAMESPACE_PREFIX}' TEAM_ID='${TEAM_ID}' \
        envsubst '\${TEAM_NAMESPACE_PREFIX} \${TEAM_ID} \${KAFKA_CLUSTER_NAME} \
                  \${INFRA_NAMESPACE} \${EXTERNAL_DOMAIN} \${NIFI_CLUSTER_NAME}' \
        < '${SCRIPT_DIR}/05-team-configmap.yaml' | oc apply -f -)"
    else
      warn "config.env not found at ${MAIN_CONFIG} — skipping team-shared-config ConfigMap"
      warn "  Run: cp config.env.example config.env && edit config.env, then re-run with --skip-rbac"
    fi

    # Team RBAC (teachers + students)
    if [[ "$SKIP_RBAC" == "false" ]]; then
      run "TEAM_NAMESPACE_PREFIX='${TEAM_NAMESPACE_PREFIX}' TEAM_ID='${TEAM_ID}' \
        INFRA_ADMIN_GROUP='${INFRA_ADMIN_GROUP}' TEAM_GROUP_SUFFIX='${TEAM_GROUP_SUFFIX}' \
        envsubst '\${TEAM_NAMESPACE_PREFIX} \${TEAM_ID} \${INFRA_ADMIN_GROUP} \${TEAM_GROUP_SUFFIX}' \
        < '${SCRIPT_DIR}/07-team-rbac.yaml' | oc apply -f -"
    fi

    ok "${NS} — namespace, limitrange, quota, configmap, rbac"
  done
else
  info "Step 6 — Team namespaces (skipped — --infra-only)"
fi

# ── Step 7: Summary ────────────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo " Onboarding complete"
echo ""

if [[ "$DRY_RUN" == "false" ]]; then
  echo " Namespaces created:"
  [[ "$TEAMS_ONLY" == "false" ]] && echo "   ${INFRA_NAMESPACE}"
  for i in $(seq 1 "${ONBOARDING_NUM_TEAMS}"); do
    echo "   ${TEAM_NAMESPACE_PREFIX}-$(printf '%02d' "$i")"
  done
fi

echo ""
echo " Next: add users to groups (kubeadmin):"
echo "   # Teachers/TAs:"
echo "   oc adm groups add-users ${INFRA_ADMIN_GROUP} <teacher-username>"
echo ""
echo "   # Students (one per student per team):"
for i in $(seq "${FROM_TEAM}" "${ONBOARDING_NUM_TEAMS}"); do
  TEAM_ID="$(printf '%02d' "$i")"
  echo "   oc adm groups add-users ${TEAM_NAMESPACE_PREFIX}-${TEAM_ID}-${TEAM_GROUP_SUFFIX} <student-username>"
done
echo ""
echo "   # On CRC (only 'developer' user available):"
echo "   oc adm groups add-users ${INFRA_ADMIN_GROUP} developer"
echo "   oc adm groups add-users ${TEAM_NAMESPACE_PREFIX}-01-${TEAM_GROUP_SUFFIX} developer"
echo ""
echo " IMPORTANT — Grant namespace-scoped admin so 'developer' can run pipeline/setup.sh:"
echo "   (groups give 'edit'; Tekton RBAC setup requires 'admin' — same as NERC instructors grant)"
echo ""
if [[ "$TEAMS_ONLY" == "false" ]]; then
  echo "   oc adm policy add-role-to-user admin developer -n ${INFRA_NAMESPACE}"
fi
for i in $(seq "${FROM_TEAM}" "${ONBOARDING_NUM_TEAMS}"); do
  TEAM_ID="$(printf '%02d' "$i")"
  echo "   oc adm policy add-role-to-user admin developer -n ${TEAM_NAMESPACE_PREFIX}-${TEAM_ID}"
done
echo ""
echo "============================================================"
