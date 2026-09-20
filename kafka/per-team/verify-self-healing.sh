#!/usr/bin/env bash
# Verifies Strimzi self-healing: deletes Kafka broker pod, times recovery to Ready.
# Usage: bash scripts/verify-self-healing.sh <team-name> <team-namespace>
# Example: bash scripts/verify-self-healing.sh team01 team-01

set -euo pipefail

TEAM_NAME="${1:?Usage: $0 <team-name> <team-namespace>}"
TEAM_NS="${2:?Usage: $0 <team-name> <team-namespace>}"
POD="kafka-${TEAM_NAME}-dual-role-0"

echo "Deleting pod ${POD} in ${TEAM_NS}..."
oc delete pod "${POD}" -n "${TEAM_NS}"
START=$(date +%s)

# After deletion, pod disappears immediately — must wait for Strimzi to recreate it
# before calling oc wait (oc wait on a non-existent pod exits with error)
echo "Waiting for pod to be recreated..."
for i in $(seq 1 60); do
  if oc get pod "${POD}" -n "${TEAM_NS}" &>/dev/null; then
    break
  fi
  sleep 2
done

echo "Waiting for pod to be Ready..."
oc wait pod "${POD}" -n "${TEAM_NS}" \
  --for=condition=Ready --timeout=300s

END=$(date +%s)
echo "Recovery time: $((END - START)) seconds"
