#!/usr/bin/env bash
# kafka/portable/scripts/restart.sh — Restart a team's Kafka broker pod (portable/kubectl variant)
#
# Deletes the broker pod; Strimzi's operator recreates it. Mirrors
# kafka/per-team/verify-self-healing.sh, translated from oc to kubectl.
#
# Usage: restart.sh <team_name> <team_namespace>
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "Usage: $0 <team_name> <team_namespace>"
  echo "Example: $0 team01 team-portable-test"
  exit 1
fi

TEAM_NAME="$1"
TEAM_NAMESPACE="$2"
POD="kafka-${TEAM_NAME}-dual-role-0"

if ! kubectl get pod "${POD}" -n "${TEAM_NAMESPACE}" &>/dev/null; then
  echo "ERROR: Pod ${POD} not found in ${TEAM_NAMESPACE}"
  exit 1
fi

echo "Deleting pod ${POD} in ${TEAM_NAMESPACE}..."
kubectl delete pod "${POD}" -n "${TEAM_NAMESPACE}"
START=$(date +%s)

# After deletion, the pod disappears immediately — must wait for Strimzi to
# recreate it before calling kubectl wait (kubectl wait on a non-existent pod
# exits with an error instead of waiting for it to appear).
echo "Waiting for pod to be recreated..."
for i in $(seq 1 60); do
  if kubectl get pod "${POD}" -n "${TEAM_NAMESPACE}" &>/dev/null; then
    break
  fi
  sleep 2
done

echo "Waiting for pod to be Ready..."
kubectl wait pod "${POD}" -n "${TEAM_NAMESPACE}" \
  --for=condition=Ready --timeout=300s

END=$(date +%s)
echo ""
echo "Recovery time: $((END - START)) seconds"
