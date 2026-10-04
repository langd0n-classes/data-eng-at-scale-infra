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
# Safe to re-run (kubectl delete --ignore-not-found throughout).
#
# Usage: teardown-pipeline.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [[ -f "${REPO_ROOT}/config.env" ]]; then
  # shellcheck disable=SC1090
  source "${REPO_ROOT}/config.env"
fi
INFRA_NAMESPACE="${INFRA_NAMESPACE:-infra}"

info() { echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
fail() { echo "  ✗ FAILED: $* — see error above"; }

echo "============================================================"
echo " Tearing down the pipeline"
echo " (team namespaces and onboarding are left installed — see header comment)"
echo "============================================================"

echo ""
info "Removing each team's Kafka (CR, KafkaNodePool, PDB, PVCs)..."
for ns in $(kubectl get namespace -l team --no-headers -o custom-columns=:.metadata.name 2>/dev/null); do
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
kubectl delete deployment "${EVENT_GENERATOR_NAME:-event-generator}" -n "${INFRA_NAMESPACE}" --ignore-not-found
kubectl delete service "${EVENT_GENERATOR_NAME:-event-generator}" -n "${INFRA_NAMESPACE}" --ignore-not-found
kubectl delete configmap "${EVENT_GENERATOR_NAME:-event-generator}-config" -n "${INFRA_NAMESPACE}" --ignore-not-found

echo ""
info "Removing Tekton Tasks, Pipeline, and PipelineRuns..."
kubectl delete pipelinerun -n "${INFRA_NAMESPACE}" -l app=data-eng-infra --ignore-not-found
kubectl delete pipeline deploy-all-teams -n "${INFRA_NAMESPACE}" --ignore-not-found
kubectl delete task deploy-kafka deploy-event-generator check-source-changed git-clone build-push-image -n "${INFRA_NAMESPACE}" --ignore-not-found

echo ""
info "Removing the pipeline ServiceAccount and RBAC..."
# dedicated-cluster path (clusterrolebinding.yaml)
kubectl delete clusterrolebinding pipeline-runner-binding-portable --ignore-not-found
kubectl delete clusterrole pipeline-runner-role-portable --ignore-not-found
# shared-cluster path (clusterrole-namespaces-read.yaml + role-rolebinding-namespace.yaml,
# applied to infra AND every team namespace by apply-rbac.sh)
kubectl delete clusterrolebinding pipeline-namespaces-reader-binding --ignore-not-found
kubectl delete clusterrole pipeline-namespaces-reader --ignore-not-found
kubectl delete role pipeline-runner-role -n "${INFRA_NAMESPACE}" --ignore-not-found
kubectl delete rolebinding pipeline-runner-binding -n "${INFRA_NAMESPACE}" --ignore-not-found
for ns in $(kubectl get namespace -l team --no-headers -o custom-columns=:.metadata.name 2>/dev/null); do
  kubectl delete role pipeline-runner-role -n "${ns}" --ignore-not-found
  kubectl delete rolebinding pipeline-runner-binding -n "${ns}" --ignore-not-found
done
kubectl delete serviceaccount pipeline -n "${INFRA_NAMESPACE}" --ignore-not-found

echo ""
echo "============================================================"
echo " Pipeline teardown complete"
echo " (team namespaces, onboarding, registry, Spark queue, and cluster-wide"
echo "  prerequisites are left installed — see teardown-prerequisites.sh)"
echo "============================================================"
