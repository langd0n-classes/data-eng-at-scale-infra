#!/usr/bin/env bash
# kafka/portable/prerequisites/install-strimzi.sh
#
# Cluster-admin, one-time script: installs the Strimzi 0.51.0 Cluster Operator
# cluster-wide (watches all namespaces) so any newly-created team namespace can
# receive a Ready Kafka CR without a per-team operator install — mirrors how
# the OpenShift path installs Strimzi via OperatorHub "All namespaces" mode.
#
# REQUIRES cluster-admin (or an explicitly-granted equivalent): this creates
# CustomResourceDefinitions and ClusterRole/ClusterRoleBinding objects, which
# are cluster-scoped and are not covered by a namespace-scoped "edit" role.
#
# Run once per cluster, before any team's Kafka is deployed via
# kafka/portable/scripts/deploy.sh. Safe to re-run (idempotent).
#
# Usage: install-strimzi.sh
set -euo pipefail

STRIMZI_VERSION="0.51.0"
OPERATOR_NAMESPACE="strimzi-system"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT

echo "=========================================="
echo "Installing Strimzi ${STRIMZI_VERSION} (cluster-wide)"
echo "=========================================="

echo "Downloading Strimzi ${STRIMZI_VERSION} release bundle..."
curl -sL -o "${WORKDIR}/strimzi.tar.gz" \
  "https://github.com/strimzi/strimzi-kafka-operator/releases/download/${STRIMZI_VERSION}/strimzi-${STRIMZI_VERSION}.tar.gz"
tar xzf "${WORKDIR}/strimzi.tar.gz" -C "${WORKDIR}"
cd "${WORKDIR}/strimzi-${STRIMZI_VERSION}"

echo "Creating namespace ${OPERATOR_NAMESPACE}..."
kubectl create namespace "${OPERATOR_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

echo "Pointing Role/ClusterRoleBinding manifests at ${OPERATOR_NAMESPACE}..."
# sed -i syntax differs between GNU (Linux) and BSD (macOS) — detect and branch.
if sed --version >/dev/null 2>&1; then
  # GNU sed
  sed -i "s/namespace: .*/namespace: ${OPERATOR_NAMESPACE}/" install/cluster-operator/*RoleBinding*.yaml
else
  # BSD sed (macOS) — requires the backup-suffix argument even when empty
  sed -i '' "s/namespace: .*/namespace: ${OPERATOR_NAMESPACE}/" install/cluster-operator/*RoleBinding*.yaml
fi

echo "Switching STRIMZI_NAMESPACE to '*' (watch all namespaces)..."
perl -0777 -pi -e \
  's/- name: STRIMZI_NAMESPACE\n(\s+)valueFrom:\n\s+fieldRef:\n\s+fieldPath: metadata\.namespace/- name: STRIMZI_NAMESPACE\n$1value: "*"/' \
  install/cluster-operator/060-Deployment-strimzi-cluster-operator.yaml

echo "Verifying STRIMZI_NAMESPACE patch:"
grep -A2 STRIMZI_NAMESPACE install/cluster-operator/060-Deployment-strimzi-cluster-operator.yaml

echo "Creating cluster-wide ClusterRoleBindings..."
kubectl create clusterrolebinding strimzi-cluster-operator-namespaced \
  --clusterrole=strimzi-cluster-operator-namespaced \
  --serviceaccount "${OPERATOR_NAMESPACE}:strimzi-cluster-operator" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl create clusterrolebinding strimzi-cluster-operator-watched \
  --clusterrole=strimzi-cluster-operator-watched \
  --serviceaccount "${OPERATOR_NAMESPACE}:strimzi-cluster-operator" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl create clusterrolebinding strimzi-cluster-operator-entity-operator-delegation \
  --clusterrole=strimzi-entity-operator \
  --serviceaccount "${OPERATOR_NAMESPACE}:strimzi-cluster-operator" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Applying CRDs, ServiceAccount, ClusterRoles, ConfigMap, and Deployment..."
kubectl apply -f install/cluster-operator -n "${OPERATOR_NAMESPACE}"

echo ""
echo "Waiting for the operator Deployment to become available..."
kubectl wait deployment strimzi-cluster-operator -n "${OPERATOR_NAMESPACE}" \
  --for=condition=Available --timeout=180s

echo ""
echo "=========================================="
echo "Strimzi ${STRIMZI_VERSION} installed and watching all namespaces"
echo "=========================================="
kubectl get deployment strimzi-cluster-operator -n "${OPERATOR_NAMESPACE}"
kubectl get crd | grep strimzi.io
