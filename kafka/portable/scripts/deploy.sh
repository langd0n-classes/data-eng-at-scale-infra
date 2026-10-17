#!/usr/bin/env bash
# kafka/portable/scripts/deploy.sh — Deploy Kafka for one team (portable/kubectl variant)
#
# Usage: deploy.sh <team_name> <team_namespace> [storage_class] [volume_size]
# Example: deploy.sh team01 team-portable-test
# Example: deploy.sh team01 team-portable-test standard 2Gi
#
# storage_class/volume_size, if given, override any value sourced from the
# repo-root config.env. If neither is set, falls back to standard/2Gi.
# config.env is optional here — this package does not require it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST_DIR="${SCRIPT_DIR}/../manifests"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

if [ $# -lt 2 ] || [ $# -gt 4 ]; then
  echo "Usage: $0 <team_name> <team_namespace> [storage_class] [volume_size]"
  echo "Example: $0 team01 team-portable-test"
  echo "Example: $0 team01 team-portable-test standard 2Gi"
  exit 1
fi

TEAM_NAME="$1"
TEAM_NAMESPACE="$2"
CLI_STORAGE_CLASS="${3:-}"
CLI_VOLUME_SIZE="${4:-}"

# Preserve CLI args across sourcing config.env — config.env exports its own
# TEAM_NAME/TEAM_NAMESPACE/STORAGE_CLASS which would otherwise clobber ours.
_TEAM_NAME_ARG="${TEAM_NAME}"
_TEAM_NAMESPACE_ARG="${TEAM_NAMESPACE}"
if [ -f "${REPO_ROOT}/config.env" ]; then
  source "${REPO_ROOT}/config.env"
fi
TEAM_NAME="${_TEAM_NAME_ARG}"
TEAM_NAMESPACE="${_TEAM_NAMESPACE_ARG}"
STORAGE_CLASS="${CLI_STORAGE_CLASS:-${STORAGE_CLASS:-standard}}"
VOLUME_SIZE="${CLI_VOLUME_SIZE:-${VOLUME_SIZE:-2Gi}}"
INFRA_NAMESPACE="${INFRA_NAMESPACE:-infra}"

echo "=========================================="
echo "Deploying Kafka for ${TEAM_NAME} (portable)"
echo "=========================================="
echo "Team Name:     ${TEAM_NAME}"
echo "Namespace:     ${TEAM_NAMESPACE}"
echo "Storage Class: ${STORAGE_CLASS}"
echo "Volume Size:   ${VOLUME_SIZE}"
echo "Infra NS:      ${INFRA_NAMESPACE}"
echo ""

if ! kubectl get namespace "${TEAM_NAMESPACE}" &>/dev/null; then
  echo "ERROR: Namespace ${TEAM_NAMESPACE} does not exist!"
  echo "Create it first: kubectl create namespace ${TEAM_NAMESPACE}"
  exit 1
fi

export TEAM_NAME TEAM_NAMESPACE STORAGE_CLASS VOLUME_SIZE INFRA_NAMESPACE

echo "Applying KafkaNodePool + Kafka CR + PodDisruptionBudget..."
envsubst < "${MANIFEST_DIR}/kafka-nodepool-template.yaml" | kubectl apply -f -
envsubst < "${MANIFEST_DIR}/kafka-template.yaml"          | kubectl apply -f -
envsubst < "${MANIFEST_DIR}/pdb-template.yaml"             | kubectl apply -f -

echo ""
echo "Waiting for Kafka to be ready..."
kubectl wait kafka "kafka-${TEAM_NAME}" \
  -n "${TEAM_NAMESPACE}" \
  --for=condition=Ready \
  --timeout=300s

echo ""
echo "=========================================="
echo "Deployment Complete!"
echo "=========================================="
echo ""
echo "Kafka for ${TEAM_NAME} is deployed to ${TEAM_NAMESPACE}"
echo ""
echo "Check status:"
echo "  kubectl get kafka,pod -n ${TEAM_NAMESPACE} -l strimzi.io/cluster=kafka-${TEAM_NAME}"
echo ""
echo "Bootstrap endpoint (from within the cluster):"
echo "  kafka-${TEAM_NAME}-kafka-bootstrap.${TEAM_NAMESPACE}.svc.cluster.local:9092"
echo ""
