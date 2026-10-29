#!/usr/bin/env bash
# portable/scripts/teardown-pipeline.sh
#
# Undoes deploy-pipeline.sh: every PipelineRun, the Pipeline, the Tasks,
# pipeline RBAC (including the `pipeline` ServiceAccount), the shared
# event generator, and — since this is what a PipelineRun actually
# produces, not onboarding — each team's Kafka (CR, KafkaNodePool, PDB,
# PVCs, via kafka/portable/scripts/remove.sh, the same tool
# kafka/portable/README.md documents for this).
#
# Deliberately does NOT remove team namespaces or anything onboarding
# created (quota, RBAC, NetworkPolicy) — those belong to
# install-prerequisites.sh / teardown-prerequisites.sh. This lets you wipe
# and resubmit a run without redoing onboarding.
#
# Namespaces are selected by the app=data-eng-infra label (this
# platform's own namespaces, not any other project's `team`-labeled
# namespace that happens to share a cluster with this one).
#
# Safe to re-run (kubectl delete --ignore-not-found throughout). Keeps
# running every remaining step even if one fails (set -uo pipefail, not
# set -e), but the script's own exit code now reflects whether anything
# actually failed, instead of always reporting success.
#
# Usage:
#   bash teardown-pipeline.sh              # interactive, asks to confirm
#   FORCE=true bash teardown-pipeline.sh   # skips the confirmation prompt
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [[ -f "${REPO_ROOT}/config.env" ]]; then
  # shellcheck disable=SC1090
  source "${REPO_ROOT}/config.env"
fi
INFRA_NAMESPACE="${INFRA_NAMESPACE:-infra}"

FAILURES=0
info() { echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
fail() { echo "  ✗ FAILED: $* — see error above"; FAILURES=$((FAILURES + 1)); }

confirm() {
  local prompt="$1"
  local ctx
  ctx="$(kubectl config current-context 2>/dev/null || echo 'UNKNOWN')"
  if [[ "${FORCE:-false}" == "true" ]]; then
    echo "  (FORCE=true — skipping prompt: ${prompt})"
    echo "  Current context: ${ctx}"
    return 0
  fi
  read -r -p "${prompt} (current context: ${ctx}) (y/N): " answer
  [[ "$answer" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
}

echo "============================================================"
echo " Tearing down the pipeline"
echo " (team namespaces and onboarding are left installed — see header comment)"
echo "============================================================"

confirm "This will permanently delete every team's Kafka, the event generator, and all Tekton Tasks/Pipeline/PipelineRuns/RBAC. Continue?"

echo ""
info "Removing each team's Kafka (CR, KafkaNodePool, PDB, PVCs)..."
if ! ns_list="$(kubectl get namespace -l app=data-eng-infra --no-headers -o custom-columns=:.metadata.name 2>&1)"; then
  fail "could not list namespaces — check cluster connectivity / current context (${ns_list})"
  ns_list=""
fi
for ns in ${ns_list}; do
  for kafka_cr in $(kubectl get kafka -n "${ns}" -o name 2>/dev/null); do
    team_name="${kafka_cr#kafka.kafka.strimzi.io/kafka-}"
    if bash "${REPO_ROOT}/kafka/portable/scripts/remove.sh" --yes "${team_name}" "${ns}"; then
      ok "${team_name} (${ns})"
    else
      fail "${team_name} (${ns})"
    fi
  done
done

echo ""
info "Removing the shared event generator..."
if kubectl delete deployment "${EVENT_GENERATOR_NAME:-event-generator}" -n "${INFRA_NAMESPACE}" --ignore-not-found \
  && kubectl delete service "${EVENT_GENERATOR_NAME:-event-generator}" -n "${INFRA_NAMESPACE}" --ignore-not-found \
  && kubectl delete configmap "${EVENT_GENERATOR_NAME:-event-generator}-config" -n "${INFRA_NAMESPACE}" --ignore-not-found; then
  ok "event generator"
else
  fail "event generator"
fi

echo ""
info "Removing Tekton Tasks, Pipeline, and PipelineRuns..."
if kubectl delete pipelinerun -n "${INFRA_NAMESPACE}" -l app=data-eng-infra --ignore-not-found \
  && kubectl delete pipeline deploy-all-teams -n "${INFRA_NAMESPACE}" --ignore-not-found \
  && kubectl delete task deploy-kafka deploy-event-generator check-redeploy-needed git-clone -n "${INFRA_NAMESPACE}" --ignore-not-found; then
  ok "Tekton Tasks/Pipeline/PipelineRuns"
else
  fail "Tekton Tasks/Pipeline/PipelineRuns"
fi

echo ""
info "Removing the pipeline ServiceAccount and RBAC..."
rbac_ok=true
# dedicated-cluster path (clusterrolebinding.yaml)
kubectl delete clusterrolebinding pipeline-runner-binding-portable --ignore-not-found || rbac_ok=false
kubectl delete clusterrole pipeline-runner-role-portable --ignore-not-found || rbac_ok=false
# shared-cluster path (clusterrole-namespaces-read.yaml + role-rolebinding-namespace.yaml,
# applied to infra AND every team namespace by apply-rbac.sh)
kubectl delete clusterrolebinding pipeline-namespaces-reader-binding --ignore-not-found || rbac_ok=false
kubectl delete clusterrole pipeline-namespaces-reader --ignore-not-found || rbac_ok=false
kubectl delete role pipeline-runner-role -n "${INFRA_NAMESPACE}" --ignore-not-found || rbac_ok=false
kubectl delete rolebinding pipeline-runner-binding -n "${INFRA_NAMESPACE}" --ignore-not-found || rbac_ok=false
if ! ns_list="$(kubectl get namespace -l app=data-eng-infra --no-headers -o custom-columns=:.metadata.name 2>&1)"; then
  echo "  could not list namespaces — check cluster connectivity / current context (${ns_list})" >&2
  rbac_ok=false
  ns_list=""
fi
for ns in ${ns_list}; do
  kubectl delete role pipeline-runner-role -n "${ns}" --ignore-not-found || rbac_ok=false
  kubectl delete rolebinding pipeline-runner-binding -n "${ns}" --ignore-not-found || rbac_ok=false
done
kubectl delete serviceaccount pipeline -n "${INFRA_NAMESPACE}" --ignore-not-found || rbac_ok=false
if [[ "${rbac_ok}" == "true" ]]; then
  ok "pipeline ServiceAccount and RBAC"
else
  fail "pipeline ServiceAccount and RBAC"
fi

echo ""
echo "============================================================"
if [[ "${FAILURES}" -gt 0 ]]; then
  echo " Pipeline teardown complete with ${FAILURES} failure(s) — see FAILED lines above"
else
  echo " Pipeline teardown complete"
fi
echo " (team namespaces, onboarding, Spark queue, and cluster-wide"
echo "  prerequisites are left installed — see teardown-prerequisites.sh)"
echo "============================================================"

[[ "${FAILURES}" -eq 0 ]]
