#!/bin/bash
# Deploy a single team's minimal NiFi instance
# Usage: ./deploy-team.sh <team-name> <namespace> <password> [--generate-yaml]

set -e

# Load configuration from parent directory
if [ ! -f ../config.env ]; then
    echo "ERROR: ../config.env not found!"
    echo "Please ensure config.env exists in the parent directory"
    exit 1
fi

source ../config.env

# Export variables for envsubst
export STORAGE_CLASS
export NIFI_IMAGE

# Parse arguments
GENERATE_YAML=false
TEAM_NAME=""
TEAM_NAMESPACE=""
TEAM_PASSWORD=""

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
            elif [ -z "$TEAM_PASSWORD" ]; then
                TEAM_PASSWORD=$1
            else
                echo "ERROR: Too many arguments"
                echo "Usage: $0 <team-name> <namespace> <password> [--generate-yaml]"
                echo "Example: $0 team01 team-01 MySecurePass123"
                echo "Example: $0 team01 team-01 MySecurePass123 --generate-yaml"
                exit 1
            fi
            shift
            ;;
    esac
done

# All required arguments must be provided
if [ -z "$TEAM_NAME" ] || [ -z "$TEAM_NAMESPACE" ] || [ -z "$TEAM_PASSWORD" ]; then
    echo "Usage: $0 <team-name> <namespace> <password> [--generate-yaml]"
    echo ""
    echo "Arguments:"
    echo "  team-name  : Team identifier (e.g., team01)"
    echo "  namespace  : Kubernetes namespace to deploy in"
    echo "  password   : NiFi login password for this team"
    echo ""
    echo "Examples:"
    echo "  $0 team01 team-01 MySecurePass123"
    echo "  $0 team01 ${INFRA_NAMESPACE} \"\$(openssl rand -base64 16)\" --generate-yaml"
    exit 1
fi

# Export for envsubst
export TEAM_NAME
export TEAM_NAMESPACE
export TEAM_PASSWORD

echo "=========================================="
echo "Deploying NiFi for ${TEAM_NAME}"
echo "=========================================="
echo "Team Name: ${TEAM_NAME}"
echo "Namespace: ${TEAM_NAMESPACE}"
echo "NiFi Image: ${NIFI_IMAGE}"
echo "Storage Class: ${STORAGE_CLASS}"
echo ""

# Check if namespace exists
if ! kubectl get namespace "${TEAM_NAMESPACE}" &>/dev/null; then
    echo "ERROR: Namespace ${TEAM_NAMESPACE} does not exist!"
    echo "Please create it first or check the namespace name"
    exit 1
fi

if [ "$GENERATE_YAML" = true ]; then
    # Generate YAML files to /tmp for manual inspection/editing
    OUTPUT_DIR="/tmp/nifi-deploy-${TEAM_NAME}-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$OUTPUT_DIR"

    echo "Generating YAML files to: $OUTPUT_DIR"
    echo ""

    # Generate all manifests
    echo "Generating PVC..."
    envsubst < team-pvc-template.yaml > "$OUTPUT_DIR/nifi-pvc.yaml"

    echo "Generating StatefulSet..."
    envsubst < team-statefulset-template.yaml > "$OUTPUT_DIR/nifi-statefulset.yaml"

    echo "Generating Route/Ingress..."
    envsubst < team-route-template.yaml > "$OUTPUT_DIR/nifi-route.yaml"

    echo ""
    echo "✓ YAML files generated for ${TEAM_NAME}"
    echo "  Output directory: $OUTPUT_DIR"
    echo "  Files:"
    echo "    - $OUTPUT_DIR/nifi-pvc.yaml"
    echo "    - $OUTPUT_DIR/nifi-statefulset.yaml"
    echo "    - $OUTPUT_DIR/nifi-route.yaml"
    echo ""
    echo "You can now edit these files manually and apply them with:"
    echo "  kubectl apply -f $OUTPUT_DIR/ -n ${TEAM_NAMESPACE}"
    echo ""

else
    # Normal deployment mode
    echo "Deploying PVC..."
    envsubst < team-pvc-template.yaml | kubectl apply -f -

    echo "Deploying StatefulSet..."
    envsubst < team-statefulset-template.yaml | kubectl apply -f -

    echo "Deploying Route/Ingress..."
    envsubst < team-route-template.yaml | kubectl apply -f - || echo "Note: Route/Ingress creation may fail on vanilla Kubernetes (OpenShift-specific)"

    echo ""
    echo "=========================================="
    echo "Deployment Complete!"
    echo "=========================================="
    echo ""
    echo "NiFi for ${TEAM_NAME} is being deployed to ${TEAM_NAMESPACE}"
    echo ""
    echo "Access credentials:"
    echo "  Username: ${TEAM_NAME}"
    echo "  Password: [provided via command line]"
    echo ""
    echo "Check status:"
    echo "  kubectl get pods,svc,pvc -n ${TEAM_NAMESPACE} -l app=nifi-${TEAM_NAME}"
    echo ""
    echo "View logs:"
    echo "  kubectl logs -f nifi-${TEAM_NAME}-0 -n ${TEAM_NAMESPACE}"
    echo ""
    echo "Get URL (OpenShift):"
    echo "  kubectl get route nifi-${TEAM_NAME} -n ${TEAM_NAMESPACE} -o jsonpath='{.spec.host}'"
    echo ""
    echo "Port forward (testing):"
    echo "  kubectl port-forward -n ${TEAM_NAMESPACE} nifi-${TEAM_NAME}-0 8443:8443"
    echo "  Access at: https://localhost:8443/nifi"
    echo ""
    echo "It may take 2-3 minutes for NiFi to be fully ready."
    echo ""
fi
