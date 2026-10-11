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
# ClusterRoleBinding) is removed explicitly. Also removes the Tekton
# Dashboard Ingress and the cluster-wide prerequisites themselves (Strimzi,
# Tekton Pipelines, the Tekton Dashboard, ingress-nginx).
#
# Versions in the cluster-wide section must match
# install-prerequisites.sh's own pinned versions exactly (each
# `kubectl delete -f <url>` targets the same release manifest the
# matching install used).
#
# The cluster-wide teardown is rarely needed — mainly for a real shared
# cloud cluster, where deleting the whole cluster isn't an option the way
# it is on Kind.
#
# Safe to re-run (kubectl delete --ignore-not-found throughout).
#
# Usage: teardown-prerequisites.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [[ -f "${REPO_ROOT}/config.env" ]]; then
  # shellcheck disable=SC1090
  source "${REPO_ROOT}/config.env"
fi
INFRA_NAMESPACE="${INFRA_NAMESPACE:-infra}"
STRIMZI_OPERATOR_NAMESPACE="${STRIMZI_OPERATOR_NAMESPACE:-strimzi-system}"

info() { echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
fail() { echo "  ✗ FAILED: $* — see error above"; }

echo "============================================================"
echo " Tearing down prerequisites"
echo "============================================================"

echo ""
info "Removing onboarded namespaces (team-*, ${INFRA_NAMESPACE}) — cascades Kafka CRs/PVCs, the Spark queue controller, quota, RBAC, NetworkPolicy..."
# --timeout bounds the wait (kubectl's own default is "wait forever" for a stuck
# finalizer — confirmed directly: --wait=true and --timeout=0s are kubectl delete's
# own defaults) — a stuck Calico-related delete hung exactly this way earlier in
# this same validation, with no error, just silence, until manually killed
for ns in $(kubectl get namespace -l team --no-headers -o custom-columns=:.metadata.name 2>/dev/null) "${INFRA_NAMESPACE}"; do
  if kubectl delete namespace "${ns}" --ignore-not-found --timeout=60s; then
    ok "${ns}"
  else
    fail "${ns} (stuck finalizer? check: kubectl get namespace ${ns} -o yaml | grep -A5 finalizers)"
  fi
done

echo ""
info "Removing the Spark queue's cluster-scoped objects (admission policy, RBAC — not namespaced, so a namespace delete can't reach them)..."
kubectl delete validatingadmissionpolicybinding spark-job-queue-policy-binding --ignore-not-found
kubectl delete validatingadmissionpolicy spark-job-queue-policy --ignore-not-found
kubectl delete clusterrolebinding spark-queue-controller-binding --ignore-not-found
kubectl delete clusterrole spark-queue-controller-role --ignore-not-found

echo ""
info "Removing the Tekton Dashboard Ingress..."
kubectl delete ingress tekton-dashboard -n tekton-pipelines --ignore-not-found

echo ""
info "Removing ingress-nginx (controller-v1.15.1)..."
kubectl delete -f "https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.15.1/deploy/static/provider/cloud/deploy.yaml" --ignore-not-found --timeout=60s

echo ""
info "Removing the Tekton Dashboard (v0.72.0)..."
kubectl delete -f "https://github.com/tektoncd/dashboard/releases/download/v0.72.0/release.yaml" --ignore-not-found --timeout=60s

echo ""
info "Removing Tekton Pipelines (v1.15.3)..."
kubectl delete -f "https://github.com/tektoncd/pipeline/releases/download/v1.15.3/release.yaml" --ignore-not-found --timeout=60s

echo ""
info "Removing the Strimzi operator (0.51.0)..."
kubectl delete namespace "${STRIMZI_OPERATOR_NAMESPACE}" --ignore-not-found --timeout=60s
kubectl delete crd -l app=strimzi --ignore-not-found --timeout=60s
kubectl delete clusterrole,clusterrolebinding -l app=strimzi --ignore-not-found
# created directly via `kubectl create` by install-strimzi.sh, so they
# carry no app=strimzi label and aren't caught by the -l selector above
kubectl delete clusterrolebinding \
  strimzi-cluster-operator-namespaced \
  strimzi-cluster-operator-watched \
  strimzi-cluster-operator-entity-operator-delegation \
  --ignore-not-found

echo ""
echo "============================================================"
echo " Prerequisites removed"
echo " (cloud-provider-kind, if you started it yourself, is not managed"
echo "  by kubectl — stop that process separately if it's still running)"
echo "============================================================"
