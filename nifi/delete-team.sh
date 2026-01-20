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
if [ -z "$1" ]; then
    echo "Usage: $0 <team-name> [namespace]"
    echo "Example: $0 team01"
    echo "Example: $0 team01 ${INFRA_NAMESPACE}"
    exit 1
fi

TEAM_NAME=$1
export TEAM_NAME

# Namespace (default to INFRA_NAMESPACE from config)
if [ -z "$2" ]; then
    TARGET_NAMESPACE=$INFRA_NAMESPACE
else
    TARGET_NAMESPACE=$2
fi
export INFRA_NAMESPACE=$TARGET_NAMESPACE

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

echo ""
echo "✓ Deletion complete for ${TEAM_NAME} in ${TARGET_NAMESPACE}"
echo ""