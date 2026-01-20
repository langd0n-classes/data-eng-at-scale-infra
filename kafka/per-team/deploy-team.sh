#!/bin/bash
# Deploy Kafka for a single team
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Parse arguments
GENERATE_YAML=false
TEAM_NAME=""
TEAM_NAMESPACE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --generate-yaml)
            GENERATE_YAML=true
            shift
            ;;
        *)
            if [ -z "$TEAM_NAME" ]; then
                TEAM_NAME=$1
            elif [ -z "$TEAM_NAMESPACE" ]; then
                TEAM_NAMESPACE=$1
            else
                echo "ERROR: Too many arguments"
                echo "Usage: $0 <team_name> <namespace> [--generate-yaml]"
                echo "Example: $0 team01 team01-namespace"
                echo "Example: $0 team01 team01-namespace --generate-yaml"
                exit 1
            fi
            shift
            ;;
    esac
done

# Check required arguments
if [ -z "$TEAM_NAME" ] || [ -z "$TEAM_NAMESPACE" ]; then
    echo "Usage: $0 <team_name> <namespace> [--generate-yaml]"
    echo "Example: $0 team01 team01-namespace"
    echo "Example: $0 team01 team01-namespace --generate-yaml"
    exit 1
fi

# Load config for STORAGE_CLASS
if [ ! -f "${SCRIPT_DIR}/../config.env" ]; then
    echo "ERROR: ../config.env not found!"
    echo "Please ensure config.env exists in the infra directory"
    exit 1
fi

source "${SCRIPT_DIR}/../config.env"

echo "=========================================="
echo "Deploying Kafka for ${TEAM_NAME}"
echo "=========================================="
echo "Team Name: ${TEAM_NAME}"
echo "Namespace: ${TEAM_NAMESPACE}"
echo "Storage Class: ${STORAGE_CLASS}"
echo ""

# Check if namespace exists
if ! kubectl get namespace "${TEAM_NAMESPACE}" &>/dev/null; then
    echo "ERROR: Namespace ${TEAM_NAMESPACE} does not exist!"
    echo "Please create it first or check the namespace name"
    exit 1
fi

# Export variables for envsubst
export TEAM_NAME
export TEAM_NAMESPACE
export STORAGE_CLASS

if [ "$GENERATE_YAML" = true ]; then
    # Generate YAML files to /tmp for manual inspection/editing
    OUTPUT_DIR="/tmp/kafka-deploy-${TEAM_NAME}-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$OUTPUT_DIR"

    echo "Generating YAML files to: $OUTPUT_DIR"
    echo ""

    # Generate Kafka StatefulSet YAML
    echo "Generating Kafka StatefulSet YAML..."
    envsubst < "${SCRIPT_DIR}/team-kafka-template.yaml" > "$OUTPUT_DIR/kafka-statefulset.yaml"

    echo ""
    echo "✓ YAML files generated for ${TEAM_NAME}"
    echo "  Output directory: $OUTPUT_DIR"
    echo "  Files:"
    echo "    - $OUTPUT_DIR/kafka-statefulset.yaml"
    echo ""
    echo "You can now edit these files manually and apply them with:"
    echo "  kubectl apply -n ${TEAM_NAMESPACE} -f $OUTPUT_DIR/kafka-statefulset.yaml"
    echo ""

else
    # Normal deployment mode
    # Apply the Kafka StatefulSet
    echo "Deploying Kafka StatefulSet..."
    envsubst < "${SCRIPT_DIR}/team-kafka-template.yaml" | kubectl apply -f -

    echo ""
    echo "=========================================="
    echo "Deployment Complete!"
    echo "=========================================="
    echo ""
    echo "Kafka for ${TEAM_NAME} is being deployed to ${TEAM_NAMESPACE}"
    echo ""
    echo "Check status:"
    echo "  kubectl get pods,svc -n ${TEAM_NAMESPACE} -l component=kafka"
    echo ""
    echo "View logs:"
    echo "  kubectl logs -f kafka-${TEAM_NAME}-0 -n ${TEAM_NAMESPACE}"
    echo ""
    echo "Test connection (from within namespace):"
    echo "  kafka-${TEAM_NAME}:9092"
    echo ""
    echo "Test connection (from other namespaces):"
    echo "  kafka-${TEAM_NAME}.${TEAM_NAMESPACE}.svc.cluster.local:9092"
    echo ""
    echo "It may take 1-2 minutes for Kafka to be fully ready."
    echo ""
fi
