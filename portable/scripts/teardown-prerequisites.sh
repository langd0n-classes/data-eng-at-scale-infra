#!/usr/bin/env bash
# portable/scripts/teardown-prerequisites.sh
#
# Undoes install-prerequisites.sh, in full symmetry with
# onboarding/portable/apply-onboarding.sh's own namespace creation: every
# team namespace AND the infra namespace itself (if you only want to wipe
# the Kafka/event-generator state and keep onboarding, use
# teardown-pipeline.sh instead). Deleting infra cascades the Spark queue
# controller and infra's own quota/limitrange/RBAC — all of it lives
# inside that namespace. What a namespace delete can't reach
# (cluster-scoped objects: the admission policy, its ClusterRole/
# ClusterRoleBinding) is removed explicitly, along with the Tekton
# Dashboard Ingress — both uniquely-named, this-platform-only resources,
# always removed regardless of CLUSTER_WIDE_TEARDOWN below.
#
# The cluster-wide prerequisites themselves (Strimzi, Tekton Pipelines,
# the Tekton Dashboard, ingress-nginx) are genuinely shared, cluster-wide
# singletons — on a cluster shared with any other tenant/project, deleting
# them breaks that other workload too, not just this platform's own.
# So that block only runs with CLUSTER_WIDE_TEARDOWN=true (default: skipped
# entirely) — see the Usage section below. Versions for that block come
# from portable/versions.env — the same file install-prerequisites.sh's
# own install scripts source — so there's exactly one place to update,
# not one per script.
#
# Namespaces are selected by the app=data-eng-infra label (this
# platform's own namespaces, not any other project's `team`-labeled
# namespace that happens to share a cluster with this one).
#
# Safe to re-run (kubectl delete --ignore-not-found throughout). Keeps
# running every remaining step even if one fails (set -uo pipefail, not
# set -e) — a single stuck resource shouldn't block cleanup of
# everything else — but the script's own exit code now reflects whether
# anything actually failed, instead of always reporting success.
#
# Usage:
#   bash teardown-prerequisites.sh                                   # interactive, namespaces + this platform's own cluster-scoped objects only
#   FORCE=true bash teardown-prerequisites.sh                        # same scope, no confirmation prompt
#   CLUSTER_WIDE_TEARDOWN=true bash teardown-prerequisites.sh        # also removes ingress-nginx/Tekton/Strimzi, asks twice
#   FORCE=true CLUSTER_WIDE_TEARDOWN=true bash teardown-prerequisites.sh   # full wipe, no prompts (CI/automation)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [[ -f "${REPO_ROOT}/config.env" ]]; then
  # shellcheck disable=SC1090
  source "${REPO_ROOT}/config.env"
fi
# shellcheck disable=SC1091
source "${REPO_ROOT}/portable/versions.env"
INFRA_NAMESPACE="${INFRA_NAMESPACE:-infra}"
STRIMZI_OPERATOR_NAMESPACE="${STRIMZI_OPERATOR_NAMESPACE:-strimzi-system}"

FAILURES=0
info() { echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
fail() { echo "  ✗ FAILED: $* — see error above"; FAILURES=$((FAILURES + 1)); }

confirm() {
  local prompt="$1"
  local ctx
  ctx="$(kubectl config current-context 2>/dev/null || echo 'UNKNOWN')"
  if [[ "${FORCE:-false}" == "true" ]]; then
    echo "  (FORCE=true — skipping prompt: ${prompt})"
    echo "  Current context: ${ctx}"
    return 0
  fi
  read -r -p "${prompt} (current context: ${ctx}) (y/N): " answer
  [[ "$answer" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
}

echo "============================================================"
echo " Tearing down prerequisites"
echo "============================================================"

confirm "This will permanently delete this platform's namespaces (team-*, ${INFRA_NAMESPACE}) and its own cluster-scoped objects. Continue?"

echo ""
info "Removing onboarded namespaces (team-*, ${INFRA_NAMESPACE}) — cascades Kafka CRs/PVCs, the Spark queue controller, quota, RBAC, NetworkPolicy..."
# --timeout bounds the wait (kubectl's own default is "wait forever" for a stuck
# finalizer — confirmed directly: --wait=true and --timeout=0s are kubectl delete's
# own defaults) — a stuck Calico-related delete hung exactly this way earlier in
# this same validation, with no error, just silence, until manually killed
if ! ns_list="$(kubectl get namespace -l app=data-eng-infra --no-headers -o custom-columns=:.metadata.name 2>&1)"; then
  fail "could not list namespaces — check cluster connectivity / current context (${ns_list})"
  ns_list=""
fi
for ns in ${ns_list}; do
  if kubectl delete namespace "${ns}" --ignore-not-found --timeout=60s; then
    ok "${ns}"
  else
    fail "${ns} (stuck finalizer? check: kubectl get namespace ${ns} -o yaml | grep -A5 finalizers)"
  fi
done

echo ""
info "Removing the Spark queue's cluster-scoped objects (admission policy, RBAC — not namespaced, so a namespace delete can't reach them)..."
if kubectl delete validatingadmissionpolicybinding spark-job-queue-policy-binding --ignore-not-found \
  && kubectl delete validatingadmissionpolicy spark-job-queue-policy --ignore-not-found \
  && kubectl delete clusterrolebinding spark-queue-controller-binding --ignore-not-found \
  && kubectl delete clusterrole spark-queue-controller-role --ignore-not-found; then
  ok "Spark queue cluster-scoped objects"
else
  fail "Spark queue cluster-scoped objects"
fi

echo ""
info "Removing the Tekton Dashboard Ingress..."
if kubectl delete ingress tekton-dashboard -n tekton-pipelines --ignore-not-found; then
  ok "Tekton Dashboard Ingress"
else
  fail "Tekton Dashboard Ingress"
fi

echo ""
info "Removing the Tekton Dashboard NetworkPolicy..."
if kubectl delete networkpolicy tekton-dashboard-isolation -n tekton-pipelines --ignore-not-found; then
  ok "Tekton Dashboard NetworkPolicy"
else
  fail "Tekton Dashboard NetworkPolicy"
fi

if [[ "${CLUSTER_WIDE_TEARDOWN:-false}" != "true" ]]; then
  echo ""
  echo "============================================================"
  echo " Skipping cluster-wide teardown (ingress-nginx, Tekton, Strimzi)"
  echo " These are shared, cluster-wide singletons — another tenant on"
  echo " this same cluster may depend on them. Set CLUSTER_WIDE_TEARDOWN=true"
  echo " to also remove them (only do this on a cluster you own entirely)."
  echo "============================================================"
else
  confirm "This will ALSO permanently delete ingress-nginx, Tekton Pipelines, the Tekton Dashboard, and Strimzi — shared cluster-wide infrastructure another tenant on this cluster may depend on. Are you sure?"

  echo ""
  info "Removing ingress-nginx (${INGRESS_NGINX_VERSION})..."
  if kubectl delete -f "https://raw.githubusercontent.com/kubernetes/ingress-nginx/${INGRESS_NGINX_VERSION}/deploy/static/provider/cloud/deploy.yaml" --ignore-not-found --timeout=60s; then
    ok "ingress-nginx"
  else
    fail "ingress-nginx"
  fi

  echo ""
  info "Removing the team Ingress host policy..."
  if kubectl delete -f "${REPO_ROOT}/ingress/portable/manifests/team-ingress-host-policy.yaml" --ignore-not-found; then
    ok "Team Ingress host policy"
  else
    fail "Team Ingress host policy"
  fi

  echo ""
  info "Removing the Tekton Dashboard (${TEKTON_DASHBOARD_VERSION})..."
  if kubectl delete -f "https://github.com/tektoncd/dashboard/releases/download/${TEKTON_DASHBOARD_VERSION}/release.yaml" --ignore-not-found --timeout=60s; then
    ok "Tekton Dashboard"
  else
    fail "Tekton Dashboard"
  fi

  echo ""
  info "Removing Tekton Pipelines (${TEKTON_PIPELINES_VERSION})..."
  if kubectl delete -f "https://github.com/tektoncd/pipeline/releases/download/${TEKTON_PIPELINES_VERSION}/release.yaml" --ignore-not-found --timeout=60s; then
    ok "Tekton Pipelines"
  else
    fail "Tekton Pipelines"
  fi

  echo ""
  info "Removing the Strimzi operator (${STRIMZI_VERSION})..."
  if kubectl delete namespace "${STRIMZI_OPERATOR_NAMESPACE}" --ignore-not-found --timeout=60s \
    && kubectl delete crd -l app=strimzi --ignore-not-found --timeout=60s \
    && kubectl delete clusterrole,clusterrolebinding -l app=strimzi --ignore-not-found \
    && kubectl delete clusterrolebinding \
      strimzi-cluster-operator-namespaced \
      strimzi-cluster-operator-watched \
      strimzi-cluster-operator-entity-operator-delegation \
      --ignore-not-found; then
    # created directly via `kubectl create` by install-strimzi.sh, so the
    # three clusterrolebindings above carry no app=strimzi label and
    # aren't caught by the -l selector — removed explicitly instead
    ok "Strimzi operator"
  else
    fail "Strimzi operator"
  fi
fi

echo ""
echo "============================================================"
if [[ "${FAILURES}" -gt 0 ]]; then
  echo " Prerequisites removed with ${FAILURES} failure(s) — see FAILED lines above"
else
  echo " Prerequisites removed"
fi
echo " (cloud-provider-kind, if you started it yourself, is not managed"
echo "  by kubectl — stop that process separately if it's still running)"
echo "============================================================"

[[ "${FAILURES}" -eq 0 ]]
