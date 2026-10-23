#!/usr/bin/env bash
# portable/scripts/deploy-pipeline.sh
#
# Everything that actually changes if you modify the deploy pipeline
# itself: pipeline RBAC (including the `pipeline` ServiceAccount, applied
# by pipeline/portable/rbac/apply-rbac.sh), the Tekton Tasks, and the
# Pipeline definition — then submits the first PipelineRun.
#
# Assumes install-prerequisites.sh already ran. Safe to re-run (idempotent
# throughout), but after the first time, prefer the plain one-liner below
# to trigger another run without re-applying RBAC/Tasks/Pipeline for
# nothing — same reasoning Tekton itself uses: apply Tasks/Pipeline once,
# submit PipelineRuns repeatedly:
#
#   source config.env && source providers/${PROVIDER:-kind}.env && envsubst < pipeline/portable/runs/run-all-teams-pipeline-run.yaml | kubectl create -f -
#
# Usage: deploy-pipeline.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

info() { echo "▶ $*"; }

if [[ ! -f "${REPO_ROOT}/config.env" ]]; then
  echo "ERROR: config.env not found at ${REPO_ROOT}/config.env"
  echo "       Copy config.env.example to config.env and fill in your values."
  exit 1
fi
source "${REPO_ROOT}/config.env"

# TEAM_BOOTSTRAP_SERVERS is computed in-cluster by check-redeploy-needed-task.yaml
# from TEAM{N}_NAME/TEAM{N}_NAMESPACE — not built here, so there's nothing for
# a shortcut invocation to get wrong.

# Same provider override as install-prerequisites.sh — must be sourced
# here too, independently, since this runs as its own later invocation
# and has no memory of what PROVIDER that earlier run used.
PROVIDER="${PROVIDER:-kind}"
PROVIDER_ENV_FILE="${REPO_ROOT}/providers/${PROVIDER}.env"
if [[ ! -f "${PROVIDER_ENV_FILE}" ]]; then
  echo "ERROR: no providers/${PROVIDER}.env file found for PROVIDER='${PROVIDER}'."
  echo "       Copy providers/TEMPLATE.env.example to providers/${PROVIDER}.env and fill in real values."
  exit 1
fi
source "${PROVIDER_ENV_FILE}"

for var in INFRA_NAMESPACE STORAGE_CLASS VOLUME_SIZE KUBECTL_CLI_IMAGE \
           GIT_REPO_URL GIT_BRANCH EVENT_GENERATOR_NAME EVENT_GENERATOR_IMAGE \
           EVENT_RATE_PER_SEC TOPIC_PREFIX TOPIC_SUFFIX REGIONS \
           TEKTON_WORKSPACE_SIZE; do
  val="${!var:-}"
  if [[ -z "$val" ]]; then
    echo "ERROR: ${var} is not set in config.env"
    exit 1
  fi
done

if ! kubectl get namespace "${INFRA_NAMESPACE}" >/dev/null 2>&1; then
  echo "ERROR: namespace '${INFRA_NAMESPACE}' doesn't exist yet."
  echo "       Run install-prerequisites.sh first."
  exit 1
fi

echo "============================================================"
echo " Deploying the pipeline"
echo "============================================================"

echo ""
info "Step 1 — Pipeline RBAC..."
bash "${REPO_ROOT}/pipeline/portable/rbac/apply-rbac.sh"

echo ""
info "Step 2 — Tekton Tasks + Pipeline..."
envsubst '${KUBECTL_CLI_IMAGE}' < "${REPO_ROOT}/pipeline/portable/tasks/deploy-kafka-task.yaml" | kubectl apply -f -
envsubst '${KUBECTL_CLI_IMAGE}' < "${REPO_ROOT}/pipeline/portable/tasks/deploy-event-generator-task.yaml" | kubectl apply -f -
envsubst '${KUBECTL_CLI_IMAGE}' < "${REPO_ROOT}/pipeline/portable/tasks/check-redeploy-needed-task.yaml" | kubectl apply -f -
kubectl apply -f "${REPO_ROOT}/pipeline/portable/tasks/git-clone-task.yaml"
kubectl apply -f "${REPO_ROOT}/pipeline/portable/pipelines/deploy-all-teams-pipeline.yaml"

echo ""
info "Step 3 — Submitting the deploy-all-teams PipelineRun..."
export RUN_TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
envsubst < "${REPO_ROOT}/pipeline/portable/runs/run-all-teams-pipeline-run.yaml" | kubectl create -f -

echo ""
echo "============================================================"
echo " Pipeline deployed and a run submitted — watch it with:"
echo "   tkn pipelinerun logs --last -f -n ${INFRA_NAMESPACE}"
echo " or check status with: bash portable/scripts/status-platform.sh"
echo ""
echo " Next time, skip re-applying RBAC/Tasks/Pipeline — just submit"
echo " another run directly:"
echo "   source config.env && source providers/${PROVIDER:-kind}.env && envsubst < pipeline/portable/runs/run-all-teams-pipeline-run.yaml | kubectl create -f -"
echo "============================================================"
