#!/usr/bin/env bash
# onboarding/portable/make-kubeconfigs.sh
#
# Writes one kubeconfig per team and one for staff, for a cluster whose
# authentication cannot assert group membership (no OIDC groups claim).
#
#   - Team kubeconfig: the team's `team-workload` ServiceAccount token. It
#     has `edit` and the `team-kafka` Role in the team namespace only.
#   - Staff kubeconfig: a `staff` ServiceAccount in the infra namespace,
#     bound to `edit` and `team-kafka` in every team namespace and to `edit`
#     in the infra namespace.
#
# Tokens are long-lived Secret-based tokens. To revoke one, delete its
# Secret (`<serviceaccount>-token`) and run this script again.
#
# The output files are credentials. Write them outside any Git working
# tree, and share each team's file with that team only.
#
# Reads onboarding/cluster.env (INFRA_NAMESPACE, TEAM_NAMESPACE_PREFIX,
# FIRST_TEAM, NUM_TEAMS). Uses the current kubectl context for the API
# server address and CA.
#
# Usage: make-kubeconfigs.sh OUTPUT_DIR
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ONBOARDING_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

OUT_DIR="${1:?Usage: make-kubeconfigs.sh OUTPUT_DIR}"
source "${ONBOARDING_DIR}/cluster.env"
FIRST_TEAM="${FIRST_TEAM:-0}"

if git -C "${OUT_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
   && ! git -C "${OUT_DIR}" check-ignore -q "${OUT_DIR}"; then
  echo "ERROR: ${OUT_DIR} is inside a Git working tree and is not ignored." >&2
  exit 1
fi
mkdir -p "${OUT_DIR}"
chmod 700 "${OUT_DIR}"

SERVER="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
CA_DATA="$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"

ensure_token_secret() {
  local ns="$1" sa="$2"
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: ${sa}-token
  namespace: ${ns}
  annotations:
    kubernetes.io/service-account.name: ${sa}
type: kubernetes.io/service-account-token
EOF
  for _ in $(seq 1 30); do
    if [[ -n "$(kubectl get secret "${sa}-token" -n "${ns}" -o jsonpath='{.data.token}' 2>/dev/null)" ]]; then
      return 0
    fi
    sleep 1
  done
  echo "ERROR: token for ${ns}/${sa} was not issued." >&2
  exit 1
}

write_kubeconfig() {
  local file="$1" name="$2" ns="$3" sa="$4"
  local token
  token="$(kubectl get secret "${sa}-token" -n "${ns}" -o jsonpath='{.data.token}' | base64 -d)"
  umask 077
  cat > "${file}" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: ds551
    cluster:
      server: ${SERVER}
      certificate-authority-data: ${CA_DATA}
users:
  - name: ${name}
    user:
      token: ${token}
contexts:
  - name: ${name}
    context:
      cluster: ds551
      user: ${name}
      namespace: ${5:-${ns}}
current-context: ${name}
EOF
}

# Staff ServiceAccount and its bindings.
kubectl create serviceaccount staff -n "${INFRA_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl create rolebinding staff-edit --clusterrole=edit \
  --serviceaccount="${INFRA_NAMESPACE}:staff" -n "${INFRA_NAMESPACE}" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

for i in $(seq "${FIRST_TEAM}" "${NUM_TEAMS}"); do
  TEAM_ID="$(printf '%02d' "$i")"
  NS="${TEAM_NAMESPACE_PREFIX}-${TEAM_ID}"
  kubectl create rolebinding staff-edit --clusterrole=edit \
    --serviceaccount="${INFRA_NAMESPACE}:staff" -n "${NS}" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl create rolebinding staff-kafka --role=team-kafka \
    --serviceaccount="${INFRA_NAMESPACE}:staff" -n "${NS}" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  ensure_token_secret "${NS}" team-workload
  write_kubeconfig "${OUT_DIR}/${NS}.kubeconfig" "${NS}" "${NS}" team-workload
  echo "wrote ${OUT_DIR}/${NS}.kubeconfig"
done

ensure_token_secret "${INFRA_NAMESPACE}" staff
FIRST_NS="${TEAM_NAMESPACE_PREFIX}-$(printf '%02d' "${FIRST_TEAM}")"
write_kubeconfig "${OUT_DIR}/staff.kubeconfig" staff "${INFRA_NAMESPACE}" staff "${FIRST_NS}"
echo "wrote ${OUT_DIR}/staff.kubeconfig"
