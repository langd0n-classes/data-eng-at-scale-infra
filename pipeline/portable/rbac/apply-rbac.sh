#!/usr/bin/env bash
# pipeline/portable/rbac/apply-rbac.sh
#
# Applies the pipeline ServiceAccount and the least-privilege RBAC that's
# sufficient to run the pipeline: a namespace-scoped Role+RoleBinding applied
# to infra and every team namespace, plus one minimal separate ClusterRole
# for the one unavoidable cluster-scoped read (see
# role-rolebinding-namespace.yaml's header comment). This is the default
# regardless of whether the cluster would also allow something broader —
# unrestricted cluster-wide pod/ServiceAccount creation would reach
# kube-system (which, unlike this platform's own team namespaces, has no Pod
# Security restriction), so it's never picked just because it's available.
#
# PREFER_CLUSTERROLEBINDING=true opts into the broader alternative instead:
# one ClusterRoleBinding covering every namespace via clusterrolebinding.yaml
# (still requires cluster-admin — gated by the same `can-i` check below).
#
# Usage:
#   apply-rbac.sh                                   # namespace-scoped Role (default)
#   PREFER_CLUSTERROLEBINDING=true apply-rbac.sh    # cluster-wide ClusterRoleBinding (opt-in)
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

if [[ "${PREFER_CLUSTERROLEBINDING:-false}" == "true" ]] && kubectl auth can-i create clusterrolebindings &>/dev/null; then
  echo "PREFER_CLUSTERROLEBINDING=true — using a cluster-wide ClusterRoleBinding"
  envsubst '${INFRA_NAMESPACE}' < "${SCRIPT_DIR}/clusterrolebinding.yaml" | kubectl apply -f -
else
  echo "Using namespace-scoped Role+RoleBinding (least privilege, the default)"
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
