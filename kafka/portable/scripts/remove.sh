#!/usr/bin/env bash
# kafka/portable/scripts/remove.sh — Remove a team's Kafka resources and PVCs (portable/kubectl variant)
#
# Mirrors kafka/per-team/delete-team.sh, plus PodDisruptionBudget deletion
# (which delete-team.sh currently omits).
#
# Usage: remove.sh <team_name> <team_namespace>
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "Usage: $0 <team_name> <team_namespace>"
  echo "Example: $0 team01 team-portable-test"
  exit 1
fi

TEAM_NAME="$1"
TEAM_NAMESPACE="$2"

echo "=========================================="
echo "Removing Kafka for ${TEAM_NAME} (portable)"
echo "=========================================="
echo "Team Name: ${TEAM_NAME}"
echo "Namespace: ${TEAM_NAMESPACE}"
echo ""

if ! kubectl get namespace "${TEAM_NAMESPACE}" &>/dev/null; then
  echo "WARNING: Namespace ${TEAM_NAMESPACE} does not exist!"
  echo "Nothing to delete."
  exit 0
fi

if ! kubectl get kafka "kafka-${TEAM_NAME}" -n "${TEAM_NAMESPACE}" &>/dev/null; then
  echo "WARNING: Kafka CR kafka-${TEAM_NAME} not found in ${TEAM_NAMESPACE}"
  echo "Nothing to delete."
  exit 0
fi

echo "WARNING: This will delete Kafka and ALL its data!"
echo ""
echo "Resources to be deleted:"
echo "  - Kafka CR: kafka-${TEAM_NAME}"
echo "  - KafkaNodePool CR: dual-role"
echo "  - PodDisruptionBudget: kafka-${TEAM_NAME}-pdb"
echo "  - (operator cascades: pods, services, PVC)"
echo ""
read -p "Are you sure you want to continue? (yes/no): " confirm

if [ "${confirm}" != "yes" ]; then
  echo "Deletion cancelled."
  exit 0
fi

echo ""
echo "Deleting Kafka resources..."

echo "  Deleting Kafka CR..."
kubectl delete kafka "kafka-${TEAM_NAME}" -n "${TEAM_NAMESPACE}" --ignore-not-found

echo "  Deleting KafkaNodePool CR..."
kubectl delete kafkanodepool dual-role -n "${TEAM_NAMESPACE}" --ignore-not-found

echo "  Deleting PodDisruptionBudget..."
kubectl delete pdb "kafka-${TEAM_NAME}-pdb" -n "${TEAM_NAMESPACE}" --ignore-not-found

echo "  Deleting Strimzi PVCs..."
kubectl delete pvc -l "strimzi.io/cluster=kafka-${TEAM_NAME}" -n "${TEAM_NAMESPACE}" --ignore-not-found

echo ""
echo "=========================================="
echo "Removal Complete!"
echo "=========================================="
echo ""
echo "Kafka for ${TEAM_NAME} has been removed from ${TEAM_NAMESPACE}"
echo ""
echo "Verify removal:"
echo "  kubectl get kafka,kafkanodepool,pdb,pod,pvc -n ${TEAM_NAMESPACE}"
echo ""
