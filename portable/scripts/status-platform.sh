#!/usr/bin/env bash
# portable/scripts/status-platform.sh
#
# Read-only status report for the whole portable platform: every
# cluster-wide prerequisite, the registry, onboarding, the Spark queue,
# and per-team Kafka + the shared event generator. Mirrors ops.sh's
# cmd_status/cmd_status_all pattern — always safe to run, never changes
# anything.
#
# Usage: status-platform.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [[ -f "${REPO_ROOT}/config.env" ]]; then
  # shellcheck disable=SC1090
  source "${REPO_ROOT}/config.env"
fi
INFRA_NAMESPACE="${INFRA_NAMESPACE:-infra}"

section() { echo ""; echo "── $* ──"; }

section "Cluster-wide prerequisites"
kubectl get deployment strimzi-cluster-operator -n "${STRIMZI_OPERATOR_NAMESPACE:-strimzi-system}" \
  --no-headers 2>/dev/null || echo "Strimzi operator: not found"
kubectl get deployment tekton-pipelines-controller tekton-pipelines-webhook -n tekton-pipelines \
  --no-headers 2>/dev/null || echo "Tekton Pipelines: not found"
kubectl get deployment tekton-dashboard -n tekton-pipelines --no-headers 2>/dev/null \
  || echo "Tekton Dashboard: not found"
kubectl get deployment ingress-nginx-controller -n ingress-nginx --no-headers 2>/dev/null \
  || echo "ingress-nginx: not found"
EXTERNAL_IP="$(kubectl get service ingress-nginx-controller -n ingress-nginx \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null)"
echo "ingress-nginx external IP: ${EXTERNAL_IP:-<pending>}"

section "Registry"
kubectl get deployment registry -n "${INFRA_NAMESPACE}" --no-headers 2>/dev/null \
  || echo "registry: not found"
kubectl get service registry -n "${INFRA_NAMESPACE}" --no-headers 2>/dev/null || true

section "Onboarding (namespaces)"
kubectl get namespace -l app=data-eng-infra --no-headers 2>/dev/null || echo "none found"

section "Spark queue"
kubectl get deployment spark-queue-controller -n "${INFRA_NAMESPACE}" --no-headers 2>/dev/null \
  || echo "spark-queue-controller: not found"
kubectl get validatingadmissionpolicy spark-job-queue-policy --no-headers 2>/dev/null \
  || echo "spark-job-queue-policy: not found"
kubectl get job -A -l queue=spark -o custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name,SUSPEND:.spec.suspend 2>/dev/null

section "Per-team Kafka"
for ns in $(kubectl get namespace -l team --no-headers -o custom-columns=:.metadata.name 2>/dev/null); do
  kubectl get kafka -n "${ns}" --no-headers 2>/dev/null || echo "${ns}: no Kafka found"
done

section "Event generator"
kubectl get deployment "${EVENT_GENERATOR_NAME:-event-generator}" -n "${INFRA_NAMESPACE}" \
  --no-headers 2>/dev/null || echo "event-generator: not found"

section "Latest PipelineRun"
kubectl get pipelinerun -n "${INFRA_NAMESPACE}" \
  --sort-by=.metadata.creationTimestamp --no-headers 2>/dev/null | tail -1 \
  || echo "none found"
