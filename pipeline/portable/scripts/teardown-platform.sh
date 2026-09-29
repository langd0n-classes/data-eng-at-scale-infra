#!/usr/bin/env bash
# pipeline/portable/scripts/teardown-platform.sh
#
# Removes platform and team resources — every team namespace (which
# cascades to delete every Kafka custom resource, PVC, quota, RBAC, and
# NetworkPolicy inside it), the shared event generator, the Spark queue
# controller and admission policy, the Tekton Tasks/Pipeline, and the
# registry (with its PVC).
#
# Deliberately does NOT remove the cluster-wide, one-time prerequisites
# (Strimzi operator, Tekton Pipelines/Dashboard themselves, ingress-nginx)
# — same scoping kafka/portable/README.md already documents for its own
# operator cleanup ("rarely needed... unrelated to any one team's
# lifecycle"), so a redeploy right after this doesn't need to reinstall
# any of them.
#
# Safe to re-run (kubectl delete --ignore-not-found throughout).
#
# Usage: teardown-platform.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

if [[ -f "${REPO_ROOT}/config.env" ]]; then
  # shellcheck disable=SC1090
  source "${REPO_ROOT}/config.env"
fi
INFRA_NAMESPACE="${INFRA_NAMESPACE:-infra}"

info() { echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }

echo "============================================================"
echo " Tearing down the portable Fall 2026 platform"
echo " (cluster-wide prerequisites are left installed — see header comment)"
echo "============================================================"

echo ""
info "Removing team namespaces (cascades: Kafka CRs, PVCs, quota, RBAC, NetworkPolicy)..."
for ns in $(kubectl get namespace -l team --no-headers -o custom-columns=:.metadata.name 2>/dev/null); do
  kubectl delete namespace "${ns}" --ignore-not-found
  ok "${ns}"
done

echo ""
info "Removing the shared event generator..."
kubectl delete deployment "${EVENT_GENERATOR_NAME:-event-generator}" -n "${INFRA_NAMESPACE}" --ignore-not-found
kubectl delete service "${EVENT_GENERATOR_NAME:-event-generator}" -n "${INFRA_NAMESPACE}" --ignore-not-found
kubectl delete configmap "${EVENT_GENERATOR_NAME:-event-generator}-config" -n "${INFRA_NAMESPACE}" --ignore-not-found

echo ""
info "Removing the Spark queue controller and admission policy..."
kubectl delete validatingadmissionpolicybinding spark-job-queue-policy-binding --ignore-not-found
kubectl delete validatingadmissionpolicy spark-job-queue-policy --ignore-not-found
kubectl delete deployment spark-queue-controller -n "${INFRA_NAMESPACE}" --ignore-not-found
kubectl delete clusterrolebinding spark-queue-controller-binding --ignore-not-found
kubectl delete clusterrole spark-queue-controller-role --ignore-not-found
kubectl delete serviceaccount spark-queue-controller -n "${INFRA_NAMESPACE}" --ignore-not-found
kubectl delete configmap spark-queue-controller-script -n "${INFRA_NAMESPACE}" --ignore-not-found

echo ""
info "Removing Tekton Tasks, Pipeline, and PipelineRuns..."
kubectl delete pipelinerun -n "${INFRA_NAMESPACE}" -l app=data-eng-infra --ignore-not-found
kubectl delete pipeline deploy-all-teams -n "${INFRA_NAMESPACE}" --ignore-not-found
kubectl delete task deploy-kafka deploy-event-generator git-clone build-push-image -n "${INFRA_NAMESPACE}" --ignore-not-found
kubectl delete ingress tekton-dashboard -n tekton-pipelines --ignore-not-found

echo ""
info "Removing the pipeline ServiceAccount and RBAC..."
kubectl delete clusterrolebinding pipeline-runner-binding-portable --ignore-not-found
kubectl delete clusterrole pipeline-runner-role-portable --ignore-not-found
kubectl delete clusterrolebinding pipeline-namespaces-reader-binding --ignore-not-found
kubectl delete clusterrole pipeline-namespaces-reader --ignore-not-found
kubectl delete serviceaccount pipeline -n "${INFRA_NAMESPACE}" --ignore-not-found

echo ""
info "Removing the in-cluster registry (and its PVC)..."
kubectl delete deployment registry -n "${INFRA_NAMESPACE}" --ignore-not-found
kubectl delete service registry -n "${INFRA_NAMESPACE}" --ignore-not-found
kubectl delete pvc registry-data -n "${INFRA_NAMESPACE}" --ignore-not-found

echo ""
info "Removing the infra namespace's own resources (quota, limitrange, RBAC)..."
kubectl delete resourcequota infra-quota -n "${INFRA_NAMESPACE}" --ignore-not-found
kubectl delete limitrange infra-limits -n "${INFRA_NAMESPACE}" --ignore-not-found
kubectl delete rolebinding infra-admins-edit -n "${INFRA_NAMESPACE}" --ignore-not-found

echo ""
echo "============================================================"
echo " Teardown complete"
echo " (Strimzi operator, Tekton Pipelines/Dashboard, ingress-nginx, and"
echo "  the infra namespace itself are left installed for the next deploy)"
echo "============================================================"
