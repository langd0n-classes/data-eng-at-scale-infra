#!/usr/bin/env bash
# pipeline/portable/scripts/deploy-platform.sh
#
# The single documented command for the whole portable Fall 2026 platform:
# installs every cluster-wide prerequisite (idempotent, safe to re-run),
# then onboards teams and submits one PipelineRun that deploys every
# team's Kafka + the shared event generator, with no NiFi anywhere.
#
# On Kind, cloud-provider-kind (ingress/portable/) needs root and must be
# started separately in its own terminal — this script cannot do that for
# you (no way to prompt for a sudo password non-interactively), so it
# checks for it and prints instructions rather than blocking. Everything
# else here works without it; it's only needed for the Tekton Dashboard's
# external reachability, not for the Kafka/event-generator deploy path.
#
# Usage: deploy-platform.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

info() { echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }

if [[ ! -f "${REPO_ROOT}/config.env" ]]; then
  echo "ERROR: config.env not found at ${REPO_ROOT}/config.env"
  echo "       Copy config.env.example to config.env and fill in your values."
  exit 1
fi
source "${REPO_ROOT}/config.env"

for var in INFRA_NAMESPACE STORAGE_CLASS VOLUME_SIZE REGISTRY_VOLUME_SIZE \
           REGISTRY_NODE_PORT KUBECTL_CLI_IMAGE GIT_REPO_URL GIT_BRANCH \
           EVENT_GENERATOR_NAME EVENT_GENERATOR_IMAGE EVENT_RATE_PER_SEC \
           TOPIC_PREFIX TOPIC_SUFFIX REGIONS TEAM_BOOTSTRAP_SERVERS \
           SPARK_IMAGE DASHBOARD_HOST; do
  val="${!var:-}"
  if [[ -z "$val" ]]; then
    echo "ERROR: ${var} is not set in config.env"
    exit 1
  fi
done

echo "============================================================"
echo " Deploying the portable Fall 2026 platform"
echo "============================================================"

echo ""
info "Step 1 — Cluster-wide prerequisites (idempotent)..."
bash "${REPO_ROOT}/kafka/portable/prerequisites/install-strimzi.sh"
bash "${REPO_ROOT}/pipeline/portable/prerequisites/install-tekton.sh"
bash "${REPO_ROOT}/pipeline/portable/prerequisites/install-tekton-dashboard.sh"
bash "${REPO_ROOT}/ingress/portable/prerequisites/install-ingress-nginx.sh"

if ! kubectl get service ingress-nginx-controller -n ingress-nginx \
     -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null | grep -q .; then
  warn "ingress-nginx has no external IP yet."
  warn "On Kind, run this yourself in another terminal (needs root):"
  warn "  sudo bash ${REPO_ROOT}/ingress/portable/prerequisites/install-cloud-provider-kind.sh"
  warn "Not required for Kafka/event-generator deployment — only for the"
  warn "Tekton Dashboard's external reachability. Continuing."
fi

echo ""
info "Step 2 — In-cluster registry..."
envsubst '${INFRA_NAMESPACE} ${STORAGE_CLASS} ${REGISTRY_VOLUME_SIZE} ${REGISTRY_NODE_PORT}' \
  < "${REPO_ROOT}/registry/portable/manifests/registry.yaml" | kubectl apply -f -
kubectl rollout status deployment/registry -n "${INFRA_NAMESPACE}" --timeout=120s

echo ""
info "Step 3 — Onboarding (namespaces, quota, ServiceAccount, RBAC, NetworkPolicy)..."
bash "${REPO_ROOT}/onboarding/portable/apply-onboarding.sh"

echo ""
info "Step 4 — Spark per-team ResourceQuota..."
ONBOARDING_CONFIG="${REPO_ROOT}/onboarding/cluster.env"
if [[ -f "${ONBOARDING_CONFIG}" ]]; then
  # shellcheck disable=SC1090
  ( source "${ONBOARDING_CONFIG}"
    for i in $(seq 1 "${NUM_TEAMS}"); do
      TEAM_ID="$(printf '%02d' "$i")"
      TEAM_NAMESPACE_PREFIX="${TEAM_NAMESPACE_PREFIX}" TEAM_ID="${TEAM_ID}" \
        envsubst '${TEAM_NAMESPACE_PREFIX} ${TEAM_ID}' \
        < "${REPO_ROOT}/spark-queue/portable/manifests/team-jobs-resourcequota.yaml" | kubectl apply -f -
    done )
else
  warn "onboarding/cluster.env not found — skipping Spark ResourceQuota (run onboarding first)"
fi

echo ""
info "Step 5 — Spark queue controller + admission policy..."
kubectl create configmap spark-queue-controller-script \
  --from-file=queue-controller.py="${REPO_ROOT}/spark-queue/portable/scripts/queue-controller.py" \
  -n "${INFRA_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
envsubst '${INFRA_NAMESPACE}' < "${REPO_ROOT}/spark-queue/portable/manifests/queue-controller-deployment.yaml" | kubectl apply -f -
envsubst '${SPARK_IMAGE}' < "${REPO_ROOT}/spark-queue/portable/manifests/spark-job-admission-policy.yaml" | kubectl apply -f -

echo ""
info "Step 6 — Pipeline RBAC..."
bash "${REPO_ROOT}/pipeline/portable/rbac/apply-rbac.sh"

echo ""
info "Step 7 — Tekton Tasks + Pipeline..."
envsubst '${KUBECTL_CLI_IMAGE}' < "${REPO_ROOT}/pipeline/portable/tasks/deploy-kafka-task.yaml" | kubectl apply -f -
envsubst '${KUBECTL_CLI_IMAGE}' < "${REPO_ROOT}/pipeline/portable/tasks/deploy-event-generator-task.yaml" | kubectl apply -f -
kubectl apply -f "${REPO_ROOT}/pipeline/portable/tasks/git-clone-task.yaml"
kubectl apply -f "${REPO_ROOT}/pipeline/portable/tasks/build-push-image-task.yaml"
kubectl apply -f "${REPO_ROOT}/pipeline/portable/pipelines/deploy-all-teams-pipeline.yaml"

echo ""
info "Step 8 — Tekton Dashboard Ingress..."
envsubst '${DASHBOARD_HOST}' \
  < "${REPO_ROOT}/pipeline/portable/manifests/tekton-dashboard-ingress-template.yaml" | kubectl apply -f -

echo ""
info "Step 9 — Submitting the deploy-all-teams PipelineRun..."

kubectl create -f - <<EOF
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  generateName: deploy-all-teams-
  namespace: ${INFRA_NAMESPACE}
spec:
  pipelineRef:
    name: deploy-all-teams
  taskRunTemplate:
    serviceAccountName: pipeline
    podTemplate:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
  params:
    - name: GIT_URL
      value: "${GIT_REPO_URL}"
    - name: GIT_REVISION
      value: "${GIT_BRANCH}"
    - name: INFRA_NAMESPACE
      value: "${INFRA_NAMESPACE}"
    - name: STORAGE_CLASS
      value: "${STORAGE_CLASS}"
    - name: VOLUME_SIZE
      value: "${VOLUME_SIZE}"
$(for i in $(seq 1 15); do
  name_var="TEAM${i}_NAME"; ns_var="TEAM${i}_NAMESPACE"
  echo "    - name: TEAM${i}_NAME
      value: \"${!name_var:-}\""
  echo "    - name: TEAM${i}_NAMESPACE
      value: \"${!ns_var:-}\""
done)
    - name: EVENT_GENERATOR_NAME
      value: "${EVENT_GENERATOR_NAME}"
    - name: EVENT_GENERATOR_IMAGE
      value: "${EVENT_GENERATOR_IMAGE}"
    - name: EVENT_RATE_PER_SEC
      value: "${EVENT_RATE_PER_SEC}"
    - name: TOPIC_PREFIX
      value: "${TOPIC_PREFIX}"
    - name: TOPIC_SUFFIX
      value: "${TOPIC_SUFFIX}"
    - name: REGIONS
      value: "${REGIONS}"
    - name: TEAM_BOOTSTRAP_SERVERS
      value: "${TEAM_BOOTSTRAP_SERVERS}"
  workspaces:
    - name: source
      volumeClaimTemplate:
        spec:
          accessModes: ["ReadWriteOnce"]
          storageClassName: ${STORAGE_CLASS}
          resources:
            requests:
              storage: ${TEKTON_WORKSPACE_SIZE:-100Mi}
EOF

echo ""
echo "============================================================"
echo " Platform deploy submitted — watch it with:"
echo "   tkn pipelinerun logs --last -f -n ${INFRA_NAMESPACE}"
echo " or check status with: bash pipeline/portable/scripts/status-platform.sh"
echo "============================================================"
