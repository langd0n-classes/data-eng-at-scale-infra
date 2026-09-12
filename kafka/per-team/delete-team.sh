#!/bin/bash
# Delete Kafka for a single team
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check arguments
if [ $# -ne 2 ]; then
    echo "Usage: $0 <team_name> <namespace>"
    echo "Example: $0 team01 team01-namespace"
    exit 1
fi

TEAM_NAME=$1
TEAM_NAMESPACE=$2

echo "=========================================="
echo "Deleting Kafka for ${TEAM_NAME}"
echo "=========================================="
echo "Team Name: ${TEAM_NAME}"
echo "Namespace: ${TEAM_NAMESPACE}"
echo ""

# Check if namespace exists
if ! kubectl get namespace "${TEAM_NAMESPACE}" &>/dev/null; then
    echo "WARNING: Namespace ${TEAM_NAMESPACE} does not exist!"
    echo "Nothing to delete."
    exit 0
fi

# Check if Kafka CR exists
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
echo "  - (operator cascades: pods, services, PVC)"
echo ""
read -p "Are you sure you want to continue? (yes/no): " confirm

if [ "${confirm}" != "yes" ]; then
    echo "Deletion cancelled."
    exit 0
fi

echo ""
echo "Deleting Kafka operator CRs..."

# Delete Kafka CR — operator cascades cleanup of pods, services, PVC
echo "  Deleting Kafka CR..."
kubectl delete kafka "kafka-${TEAM_NAME}" -n "${TEAM_NAMESPACE}" || true

# Delete KafkaNodePool CR
echo "  Deleting KafkaNodePool CR..."
kubectl delete kafkanodepool dual-role -n "${TEAM_NAMESPACE}" || true

# Delete Strimzi-created PVCs — must delete or re-add crashes with cluster.id mismatch
echo "  Deleting Strimzi PVCs..."
kubectl delete pvc -l "strimzi.io/cluster=kafka-${TEAM_NAME}" -n "${TEAM_NAMESPACE}" || true

echo ""
echo "=========================================="
echo "Deletion Complete!"
echo "=========================================="
echo ""
echo "Kafka for ${TEAM_NAME} has been removed from ${TEAM_NAMESPACE}"
echo ""
echo "Verify deletion:"
echo "  kubectl get kafka,kafkanodepool,pods,pvc -n ${TEAM_NAMESPACE}"
echo ""
