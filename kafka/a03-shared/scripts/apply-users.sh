#!/usr/bin/env bash
# Create or update the A03 team logins and the staff login.
# team00 is the staff reference team; team01..teamNN are the student teams.
# Usage: apply-users.sh [team_count]   (default 10)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEAM_COUNT="${1:-10}"

kubectl apply -f "${BASE_DIR}/staff-user.yaml"

for i in $(seq 0 "${TEAM_COUNT}"); do
  TEAM="$(printf 'team%02d' "${i}")"
  export TEAM
  envsubst '${TEAM}' < "${BASE_DIR}/users.yaml.tmpl" | kubectl apply -f -
done

kubectl wait kafkauser --all -n a03-kafka --for=condition=Ready --timeout=120s
