#!/bin/bash
# Delete a single team's minimal NiFi instance
# Usage: ./delete-team.sh <team-name> [namespace]

set -e

# Load configuration from parent directory
if [ ! -f ../config.env ]; then
    echo "ERROR: ../config.env not found!"
    exit 1
fi

source ../config.env

# Team name from argument
if [ -z "$1" ] || [ -z "$2" ]; then
    echo "Usage: $0 <team-name> <namespace>"
    echo "Example: $0 \${TEAM_NAME} \${TEAM_NAMESPACE}"
    exit 1
fi

TEAM_NAME=$1
TEAM_NAMESPACE=$2
TARGET_NAMESPACE=$2
export TEAM_NAME TEAM_NAMESPACE EXTERNAL_DOMAIN

echo "=========================================="
echo "Deleting NiFi for: ${TEAM_NAME}"
echo "Namespace: ${TARGET_NAMESPACE}"
echo "=========================================="

# Delete Route
echo "Deleting Route..."
envsubst < team-route-template.yaml | kubectl delete -f - --ignore-not-found

# Delete StatefulSet and Service
echo "Deleting StatefulSet and Service..."
envsubst < team-statefulset-template.yaml | kubectl delete -f - --ignore-not-found

# Delete PVC
echo "Deleting PVC..."
envsubst < team-pvc-template.yaml | kubectl delete -f - --ignore-not-found

# Delete NetworkPolicy
echo "Deleting NetworkPolicy..."
envsubst < team-networkpolicy-template.yaml | kubectl delete -f - --ignore-not-found

echo ""
echo "✓ Deletion complete for ${TEAM_NAME} in ${TARGET_NAMESPACE}"
echo ""