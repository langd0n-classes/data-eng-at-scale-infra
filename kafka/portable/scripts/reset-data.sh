#!/usr/bin/env bash
# kafka/portable/scripts/reset-data.sh — Wipe a team's persistent Kafka data (portable/kubectl variant)
#
# Mirrors chatops/src/commands.py::cmd_wipe_kafka_data's phase ordering:
#   1. Verify the Kafka CR exists.
#   2. Delete the KafkaNodePool (operator removes the broker pod).
#   3. Wait up to 120s for the pod to disappear; force-delete after timeout
#      (safe here since the data is intentionally being wiped).
#   4. Delete PVCs by label, then wait up to 60s for them to fully disappear —
#      recreating the node pool before PVCs are gone would reuse the same PVC
#      name and could retain the old cluster ID.
#   5. Recreate the KafkaNodePool with fresh storage, then wait for the new
#      broker pod and the Kafka CR to both report Ready. The CR alone is not
#      enough: on Kind, its Ready condition stayed True from before the reset
#      for several minutes while no broker existed.
#
# Usage: reset-data.sh <team_name> <team_namespace> [storage_class] [volume_size]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST_DIR="${SCRIPT_DIR}/../manifests"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

if [ $# -lt 2 ] || [ $# -gt 4 ]; then
  echo "Usage: $0 <team_name> <team_namespace> [storage_class] [volume_size]"
  exit 1
fi

TEAM_NAME="$1"
TEAM_NAMESPACE="$2"
CLI_STORAGE_CLASS="${3:-}"
CLI_VOLUME_SIZE="${4:-}"

_TEAM_NAME_ARG="${TEAM_NAME}"
_TEAM_NAMESPACE_ARG="${TEAM_NAMESPACE}"
if [ -f "${REPO_ROOT}/config.env" ]; then
  source "${REPO_ROOT}/config.env"
fi
TEAM_NAME="${_TEAM_NAME_ARG}"
TEAM_NAMESPACE="${_TEAM_NAMESPACE_ARG}"
STORAGE_CLASS="${CLI_STORAGE_CLASS:-${STORAGE_CLASS:-standard}}"
VOLUME_SIZE="${CLI_VOLUME_SIZE:-${VOLUME_SIZE:-2Gi}}"

LABEL="strimzi.io/cluster=kafka-${TEAM_NAME}"

echo "=========================================="
echo "Resetting Kafka data for ${TEAM_NAME} (portable)"
echo "=========================================="

if ! kubectl get kafka "kafka-${TEAM_NAME}" -n "${TEAM_NAMESPACE}" &>/dev/null; then
  echo "ERROR: Kafka CR kafka-${TEAM_NAME} not found in ${TEAM_NAMESPACE} — Kafka is not deployed for this team."
  exit 1
fi

echo "Phase 1: Deleting KafkaNodePool..."
kubectl delete kafkanodepool dual-role -n "${TEAM_NAMESPACE}" --ignore-not-found

echo "Phase 2: Waiting up to 120s for broker pod to disappear..."
deadline=$(( $(date +%s) + 120 ))
while (( $(date +%s) < deadline )); do
  if [ -z "$(kubectl get pod -n "${TEAM_NAMESPACE}" -l "${LABEL}" --no-headers 2>/dev/null)" ]; then
    break
  fi
  sleep 5
done
if [ -n "$(kubectl get pod -n "${TEAM_NAMESPACE}" -l "${LABEL}" --no-headers 2>/dev/null)" ]; then
  echo "  Pod still present after 120s — force-deleting (safe, data is being wiped)..."
  kubectl delete pod -n "${TEAM_NAMESPACE}" -l "${LABEL}" --grace-period=0 --force --ignore-not-found
  sleep 5
fi

echo "Phase 3: Deleting PVCs..."
# --wait=false: a PVC held by a finalizer would otherwise block here forever,
# before the 60s timeout below can report it.
kubectl delete pvc -n "${TEAM_NAMESPACE}" -l "${LABEL}" --ignore-not-found --wait=false
pvc_deadline=$(( $(date +%s) + 60 ))
while (( $(date +%s) < pvc_deadline )); do
  if [ -z "$(kubectl get pvc -n "${TEAM_NAMESPACE}" -l "${LABEL}" --no-headers 2>/dev/null)" ]; then
    break
  fi
  sleep 3
done
if [ -n "$(kubectl get pvc -n "${TEAM_NAMESPACE}" -l "${LABEL}" --no-headers 2>/dev/null)" ]; then
  echo "ERROR: PVCs for kafka-${TEAM_NAME} still exist after 60s." >&2
  echo "Recreating the node pool now could reuse the old volume and cluster ID." >&2
  echo "Wait for the PVCs to finish deleting, then run this script again." >&2
  exit 1
fi

echo "Phase 4: Recreating KafkaNodePool with fresh storage..."
export TEAM_NAME TEAM_NAMESPACE STORAGE_CLASS VOLUME_SIZE
envsubst < "${MANIFEST_DIR}/kafka-nodepool-template.yaml" | kubectl apply -f -

echo "Phase 5: Waiting up to 600s for the new broker pod to be Ready..."
# Poll the broker pod, not only the Kafka CR: the CR's Ready condition can
# still read True from before the reset while no broker exists. 600s covers
# Strimzi's 300s operation timeout when a reconciliation is still retrying
# the deleted broker.
POD="kafka-${TEAM_NAME}-dual-role-0"
ready_deadline=$(( $(date +%s) + 600 ))
ready=""
while (( $(date +%s) < ready_deadline )); do
  pod_ready=$(kubectl get pod "${POD}" -n "${TEAM_NAMESPACE}" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "")
  kafka_ready=$(kubectl get kafka "kafka-${TEAM_NAME}" -n "${TEAM_NAMESPACE}" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "")
  if [ "${pod_ready}" = "True" ] && [ "${kafka_ready}" = "True" ]; then
    ready="True"
    break
  fi
  sleep 5
done

echo ""
if [ "${ready}" = "True" ]; then
  echo "Kafka data wiped and broker Ready for ${TEAM_NAME} in ${TEAM_NAMESPACE}."
else
  echo "Kafka data wiped for ${TEAM_NAME} in ${TEAM_NAMESPACE}, but the broker is not Ready after 600s." >&2
  echo "Check: kubectl get kafka,pod -n ${TEAM_NAMESPACE} -l ${LABEL}" >&2
  exit 1
fi
