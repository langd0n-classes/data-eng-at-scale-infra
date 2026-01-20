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

# Check if Kafka resources exist
if ! kubectl get statefulset "kafka-${TEAM_NAME}" -n "${TEAM_NAMESPACE}" &>/dev/null; then
    echo "WARNING: Kafka StatefulSet kafka-${TEAM_NAME} not found in ${TEAM_NAMESPACE}"
    echo "Nothing to delete."
    exit 0
fi

echo "⚠️  WARNING: This will delete Kafka and ALL its data!"
echo ""
echo "Resources to be deleted:"
echo "  - StatefulSet: kafka-${TEAM_NAME}"
echo "  - Services: kafka-${TEAM_NAME}, kafka-${TEAM_NAME}-headless"
echo "  - PVC: data-kafka-${TEAM_NAME}-0"
echo ""
read -p "Are you sure you want to continue? (yes/no): " confirm

if [ "${confirm}" != "yes" ]; then
    echo "Deletion cancelled."
    exit 0
fi

echo ""
echo "Deleting Kafka resources..."

# Delete StatefulSet first (this will delete the pod)
echo "  Deleting StatefulSet..."
kubectl delete statefulset "kafka-${TEAM_NAME}" -n "${TEAM_NAMESPACE}" || true

# Delete Services
echo "  Deleting Services..."
kubectl delete service "kafka-${TEAM_NAME}" -n "${TEAM_NAMESPACE}" || true
kubectl delete service "kafka-${TEAM_NAME}-headless" -n "${TEAM_NAMESPACE}" || true

# Delete PVC (this removes the data)
echo "  Deleting PVC..."
kubectl delete pvc "data-kafka-${TEAM_NAME}-0" -n "${TEAM_NAMESPACE}" || true

echo ""
echo "=========================================="
echo "Deletion Complete!"
echo "=========================================="
echo ""
echo "Kafka for ${TEAM_NAME} has been removed from ${TEAM_NAMESPACE}"
echo ""
echo "Verify deletion:"
echo "  kubectl get all,pvc -n ${TEAM_NAMESPACE} -l app=kafka-${TEAM_NAME}"
echo ""
