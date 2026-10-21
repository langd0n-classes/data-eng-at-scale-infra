#!/usr/bin/env bash
# Write one login's connection bundle: a .env file and the broker CA
# certificate. Give the bundle only to that team.
# Usage: team-bundle.sh <user> <output_dir>
#   user: team01..team10, or staff
set -euo pipefail

USER_NAME="${1:?usage: team-bundle.sh <user> <output_dir>}"
OUT_DIR="${2:?usage: team-bundle.sh <user> <output_dir>}"
NS=a03-kafka
CLUSTER=a03

BOOTSTRAP_HOST="$(kubectl get kafka "${CLUSTER}" -n "${NS}" \
  -o jsonpath='{.status.listeners[?(@.name=="external")].addresses[0].host}')"
BOOTSTRAP_PORT="$(kubectl get kafka "${CLUSTER}" -n "${NS}" \
  -o jsonpath='{.status.listeners[?(@.name=="external")].addresses[0].port}')"
if [ -z "${BOOTSTRAP_HOST}" ]; then
  echo "ERROR: the external listener has no address yet." >&2
  exit 1
fi

PASSWORD="$(kubectl get secret "${USER_NAME}" -n "${NS}" \
  -o jsonpath='{.data.password}' | base64 -d)"

mkdir -p "${OUT_DIR}"
chmod 700 "${OUT_DIR}"
kubectl get secret "${CLUSTER}-cluster-ca-cert" -n "${NS}" \
  -o jsonpath='{.data.ca\.crt}' | base64 -d > "${OUT_DIR}/kafka-ca.crt"

umask 077
cat > "${OUT_DIR}/.env" <<EOF
KAFKA_BOOTSTRAP_SERVERS=${BOOTSTRAP_HOST}:${BOOTSTRAP_PORT}
KAFKA_USERNAME=${USER_NAME}
KAFKA_PASSWORD=${PASSWORD}
KAFKA_CA_FILE=kafka-ca.crt
EOF

echo "Wrote ${OUT_DIR}/.env and ${OUT_DIR}/kafka-ca.crt"
