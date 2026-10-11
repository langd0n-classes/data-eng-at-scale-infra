#!/usr/bin/env bash
# pipeline/portable/rbac/apply-rbac.sh
#
# Applies the pipeline ServiceAccount and whichever RBAC variant fits this
# cluster, auto-detected the same way onboarding/apply-onboarding.sh already
# detects "dedicated" vs "shared" cluster type:
#   - Dedicated (cluster-admin available, e.g. Kind/k3s locally, OVHcloud's
#     default kubeconfig): one ClusterRoleBinding covering every namespace.
#   - Shared (no cluster-admin, e.g. a restricted managed cluster): a
#     namespace-scoped Role+RoleBinding applied to infra and every team
#     namespace, plus one minimal separate ClusterRole for the one
#     unavoidable cluster-scoped read (see role-rolebinding-namespace.yaml's
#     header comment).
#
# Usage: apply-rbac.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

if [[ -f "${REPO_ROOT}/config.env" ]]; then
  source "${REPO_ROOT}/config.env"
fi
: "${INFRA_NAMESPACE:?INFRA_NAMESPACE must be set (config.env)}"

echo "=========================================="
echo "Applying pipeline RBAC"
echo "=========================================="

envsubst '${INFRA_NAMESPACE}' < "${SCRIPT_DIR}/serviceaccount.yaml" | kubectl apply -f -

if kubectl auth can-i create clusterrolebindings &>/dev/null; then
  echo "Cluster type: dedicated (cluster-admin available) — using a ClusterRoleBinding"
  envsubst '${INFRA_NAMESPACE}' < "${SCRIPT_DIR}/clusterrolebinding.yaml" | kubectl apply -f -
else
  echo "Cluster type: shared (no cluster-admin) — using namespace-scoped Role+RoleBinding"
  echo "Requesting one minimal cluster-scoped read (namespaces) separately..."
  envsubst '${INFRA_NAMESPACE}' < "${SCRIPT_DIR}/clusterrole-namespaces-read.yaml" | kubectl apply -f -

  NAMESPACES="${INFRA_NAMESPACE} $(kubectl get namespace -l app=data-eng-infra,team -o jsonpath='{.items[*].metadata.name}')"
  for ns in ${NAMESPACES}; do
    echo "  → ${ns}"
    envsubst '${INFRA_NAMESPACE}' < "${SCRIPT_DIR}/role-rolebinding-namespace.yaml" | kubectl apply -f - -n "${ns}"
  done
fi

echo ""
echo "=========================================="
echo "Pipeline RBAC applied"
echo "=========================================="
