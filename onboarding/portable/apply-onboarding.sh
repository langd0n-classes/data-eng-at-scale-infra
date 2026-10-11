#!/usr/bin/env bash
# onboarding/portable/apply-onboarding.sh — kubectl-only cluster onboarding
#
# kubectl-only equivalent of onboarding/apply-onboarding.sh, for Kind/k3s/any
# conformant Kubernetes cluster. Creates: infra namespace, team namespaces,
# ResourceQuota, LimitRange, ServiceAccount, NetworkPolicy, RoleBindings.
#
# This does NOT replace the OpenShift path. onboarding/apply-onboarding.sh
# and its manifests are unchanged and remain the primary onboarding method
# for the classroom.
#
# Reuses the same onboarding/cluster.env as the OpenShift path (same
# variables — only the CLI and the Group-membership mechanism differ; see
# manifests/08-infra-rbac.yaml's header comment).
#
# Usage:
#   bash onboarding/portable/apply-onboarding.sh                # full onboarding
#   bash onboarding/portable/apply-onboarding.sh --dry-run       # print commands, no changes
#   bash onboarding/portable/apply-onboarding.sh --skip-rbac     # skip human RBAC
#   bash onboarding/portable/apply-onboarding.sh --teams-only    # skip infra ns + infra RBAC
#   bash onboarding/portable/apply-onboarding.sh --infra-only    # only infra ns + infra RBAC
#   bash onboarding/portable/apply-onboarding.sh --from-team=3   # start team loop from team 3
#
# Run from repo root or from onboarding/portable/ — the script finds
# cluster.env automatically.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ONBOARDING_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Parse flags ──────────────────────────────────────────────────────────────
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

# ── Helpers ──────────────────────────────────────────────────────────────────
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

# ── Step 1: Load & Validate Config ──────────────────────────────────────────
echo "============================================================"
echo " Cluster Onboarding (portable / kubectl-only)"
echo "============================================================"
echo ""
info "Step 1 — Loading config..."

CONFIG_FILE="${ONBOARDING_DIR}/cluster.env"
if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "ERROR: cluster.env not found at ${CONFIG_FILE}"
  echo "       Copy onboarding/cluster.env.example to onboarding/cluster.env and fill in your values."
  exit 1
fi

source "$CONFIG_FILE"

ONBOARDING_NUM_TEAMS="${NUM_TEAMS}"

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

# ── Step 2: Prerequisite Checks ─────────────────────────────────────────────
echo ""
info "Step 2 — Checking prerequisites..."

if ! kubectl cluster-info &>/dev/null; then
  echo "ERROR: Not connected to a Kubernetes cluster — check your kubeconfig/context"
  exit 1
fi
ok "Connected to cluster ($(kubectl config current-context))"

if ! kubectl get storageclass "${STORAGE_CLASS}" &>/dev/null; then
  echo ""
  echo "ERROR: Storage class '${STORAGE_CLASS}' not found on this cluster."
  echo "       Available storage classes:"
  kubectl get storageclass --no-headers 2>/dev/null | awk '{print "         " $1}' || true
  echo ""
  echo "       Update STORAGE_CLASS in onboarding/cluster.env and re-run."
  exit 1
fi
ok "Storage class '${STORAGE_CLASS}' found"

# ── Step 3: Create Infra Namespace ──────────────────────────────────────────
if [[ "$TEAMS_ONLY" == "false" ]]; then
  echo ""
  info "Step 3 — Creating infra namespace (${INFRA_NAMESPACE})..."
  run "INFRA_NAMESPACE='${INFRA_NAMESPACE}' \
    envsubst '\${INFRA_NAMESPACE}' \
    < '${ONBOARDING_DIR}/portable/manifests/01-infra-namespace.yaml' | kubectl apply -f -"

  run "INFRA_NAMESPACE='${INFRA_NAMESPACE}' \
    INFRA_LIMIT_POD_CPU_MAX='${INFRA_LIMIT_POD_CPU_MAX}' INFRA_LIMIT_POD_MEM_MAX='${INFRA_LIMIT_POD_MEM_MAX}' \
    INFRA_LIMIT_CONTAINER_CPU_MAX='${INFRA_LIMIT_CONTAINER_CPU_MAX}' INFRA_LIMIT_CONTAINER_MEM_MAX='${INFRA_LIMIT_CONTAINER_MEM_MAX}' \
    INFRA_LIMIT_CONTAINER_CPU_DEFAULT='${INFRA_LIMIT_CONTAINER_CPU_DEFAULT}' INFRA_LIMIT_CONTAINER_MEM_DEFAULT='${INFRA_LIMIT_CONTAINER_MEM_DEFAULT}' \
    INFRA_LIMIT_CONTAINER_CPU_REQUEST='${INFRA_LIMIT_CONTAINER_CPU_REQUEST}' INFRA_LIMIT_CONTAINER_MEM_REQUEST='${INFRA_LIMIT_CONTAINER_MEM_REQUEST}' \
    envsubst '\${INFRA_NAMESPACE} \${INFRA_LIMIT_POD_CPU_MAX} \${INFRA_LIMIT_POD_MEM_MAX} \
              \${INFRA_LIMIT_CONTAINER_CPU_MAX} \${INFRA_LIMIT_CONTAINER_MEM_MAX} \
              \${INFRA_LIMIT_CONTAINER_CPU_DEFAULT} \${INFRA_LIMIT_CONTAINER_MEM_DEFAULT} \
              \${INFRA_LIMIT_CONTAINER_CPU_REQUEST} \${INFRA_LIMIT_CONTAINER_MEM_REQUEST}' \
    < '${ONBOARDING_DIR}/portable/manifests/02-infra-limitrange.yaml' | kubectl apply -f -"

  run "INFRA_NAMESPACE='${INFRA_NAMESPACE}' \
    INFRA_RESOURCE_QUOTA_CPU='${INFRA_RESOURCE_QUOTA_CPU}' \
    INFRA_RESOURCE_QUOTA_MEMORY='${INFRA_RESOURCE_QUOTA_MEMORY}' \
    INFRA_RESOURCE_QUOTA_STORAGE='${INFRA_RESOURCE_QUOTA_STORAGE}' \
    envsubst '\${INFRA_NAMESPACE} \${INFRA_RESOURCE_QUOTA_CPU} \
              \${INFRA_RESOURCE_QUOTA_MEMORY} \${INFRA_RESOURCE_QUOTA_STORAGE}' \
    < '${ONBOARDING_DIR}/portable/manifests/03-infra-resourcequota.yaml' | kubectl apply -f -"

  ok "Infra namespace ready (namespace + limitrange + quota)"
else
  info "Step 3 — Infra namespace (skipped — --teams-only)"
fi

# ── Step 4: Apply Infra Human RBAC ──────────────────────────────────────────
if [[ "$SKIP_RBAC" == "false" && "$TEAMS_ONLY" == "false" ]]; then
  echo ""
  info "Step 4 — Applying infra RBAC (${INFRA_ADMIN_GROUP} → edit in ${INFRA_NAMESPACE})..."
  run "INFRA_NAMESPACE='${INFRA_NAMESPACE}' INFRA_ADMIN_GROUP='${INFRA_ADMIN_GROUP}' \
    envsubst '\${INFRA_NAMESPACE} \${INFRA_ADMIN_GROUP}' \
    < '${ONBOARDING_DIR}/portable/manifests/08-infra-rbac.yaml' | kubectl apply -f -"
  ok "Infra RBAC applied"
else
  info "Step 4 — Infra RBAC (skipped)"
fi

# ── Step 5: Create Team Namespaces ──────────────────────────────────────────
if [[ "$INFRA_ONLY" == "false" ]]; then
  echo ""
  TEAM_COUNT=$(( NUM_TEAMS - FROM_TEAM + 1 ))
  info "Step 5 — Creating ${TEAM_COUNT} team namespace(s) (team $(printf '%02d' "${FROM_TEAM}") to team $(printf '%02d' "${NUM_TEAMS}"))..."

  for i in $(seq "${FROM_TEAM}" "${NUM_TEAMS}"); do
    TEAM_ID="$(printf '%02d' "$i")"
    NS="${TEAM_NAMESPACE_PREFIX}-${TEAM_ID}"
    echo ""
    echo "         ── team ${TEAM_ID} (${NS}) ──"

    run "TEAM_NAMESPACE_PREFIX='${TEAM_NAMESPACE_PREFIX}' TEAM_ID='${TEAM_ID}' \
      envsubst '\${TEAM_NAMESPACE_PREFIX} \${TEAM_ID}' \
      < '${ONBOARDING_DIR}/portable/manifests/04-team-namespace.yaml' | kubectl apply -f -"

    run "TEAM_NAMESPACE_PREFIX='${TEAM_NAMESPACE_PREFIX}' TEAM_ID='${TEAM_ID}' \
      LIMIT_POD_CPU_MAX='${LIMIT_POD_CPU_MAX}' LIMIT_POD_MEM_MAX='${LIMIT_POD_MEM_MAX}' \
      LIMIT_CONTAINER_CPU_MAX='${LIMIT_CONTAINER_CPU_MAX}' LIMIT_CONTAINER_MEM_MAX='${LIMIT_CONTAINER_MEM_MAX}' \
      LIMIT_CONTAINER_CPU_DEFAULT='${LIMIT_CONTAINER_CPU_DEFAULT}' LIMIT_CONTAINER_MEM_DEFAULT='${LIMIT_CONTAINER_MEM_DEFAULT}' \
      LIMIT_CONTAINER_CPU_REQUEST='${LIMIT_CONTAINER_CPU_REQUEST}' LIMIT_CONTAINER_MEM_REQUEST='${LIMIT_CONTAINER_MEM_REQUEST}' \
      envsubst '\${TEAM_NAMESPACE_PREFIX} \${TEAM_ID} \${LIMIT_POD_CPU_MAX} \${LIMIT_POD_MEM_MAX} \
                \${LIMIT_CONTAINER_CPU_MAX} \${LIMIT_CONTAINER_MEM_MAX} \
                \${LIMIT_CONTAINER_CPU_DEFAULT} \${LIMIT_CONTAINER_MEM_DEFAULT} \
                \${LIMIT_CONTAINER_CPU_REQUEST} \${LIMIT_CONTAINER_MEM_REQUEST}' \
      < '${ONBOARDING_DIR}/portable/manifests/05-team-limitrange.yaml' | kubectl apply -f -"

    run "TEAM_NAMESPACE_PREFIX='${TEAM_NAMESPACE_PREFIX}' TEAM_ID='${TEAM_ID}' \
      RESOURCE_QUOTA_CPU='${RESOURCE_QUOTA_CPU}' RESOURCE_QUOTA_MEMORY='${RESOURCE_QUOTA_MEMORY}' \
      RESOURCE_QUOTA_STORAGE='${RESOURCE_QUOTA_STORAGE}' \
      envsubst '\${TEAM_NAMESPACE_PREFIX} \${TEAM_ID} \
                \${RESOURCE_QUOTA_CPU} \${RESOURCE_QUOTA_MEMORY} \${RESOURCE_QUOTA_STORAGE}' \
      < '${ONBOARDING_DIR}/portable/manifests/06-team-resourcequota.yaml' | kubectl apply -f -"

    run "TEAM_NAMESPACE_PREFIX='${TEAM_NAMESPACE_PREFIX}' TEAM_ID='${TEAM_ID}' \
      envsubst '\${TEAM_NAMESPACE_PREFIX} \${TEAM_ID}' \
      < '${ONBOARDING_DIR}/portable/manifests/07-team-serviceaccount.yaml' | kubectl apply -f -"

    if [[ "$SKIP_RBAC" == "false" ]]; then
      run "TEAM_NAMESPACE_PREFIX='${TEAM_NAMESPACE_PREFIX}' TEAM_ID='${TEAM_ID}' \
        INFRA_ADMIN_GROUP='${INFRA_ADMIN_GROUP}' TEAM_GROUP_SUFFIX='${TEAM_GROUP_SUFFIX}' \
        envsubst '\${TEAM_NAMESPACE_PREFIX} \${TEAM_ID} \${INFRA_ADMIN_GROUP} \${TEAM_GROUP_SUFFIX}' \
        < '${ONBOARDING_DIR}/portable/manifests/09-team-rbac.yaml' | kubectl apply -f -"
    fi

    run "TEAM_NAMESPACE_PREFIX='${TEAM_NAMESPACE_PREFIX}' TEAM_ID='${TEAM_ID}' INFRA_NAMESPACE='${INFRA_NAMESPACE}' \
      envsubst '\${TEAM_NAMESPACE_PREFIX} \${TEAM_ID} \${INFRA_NAMESPACE}' \
      < '${ONBOARDING_DIR}/portable/manifests/10-team-networkpolicy.yaml' | kubectl apply -f -"

    ok "${NS} — namespace, limitrange, quota, serviceaccount, rbac, networkpolicy"
  done
else
  info "Step 5 — Team namespaces (skipped — --infra-only)"
fi

# ── Step 6: Summary ──────────────────────────────────────────────────────────
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
echo " Next: establish group membership for teachers/students — see"
echo " onboarding/portable/README.md for how group membership works without"
echo " 'oc adm groups' (X.509 client cert Organization field, or an OIDC"
echo " provider's groups claim, depending on your cluster's auth setup)."
echo ""
echo "============================================================"
