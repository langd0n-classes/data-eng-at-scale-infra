#!/usr/bin/env bash
# portable/scripts/install-prerequisites.sh
#
# Everything needed before the deploy pipeline can run, grouped together
# because none of it changes when the pipeline's own definition changes:
#   - cluster-wide, one-time installs (Strimzi, Tekton Pipelines, Tekton
#     Dashboard, ingress-nginx)
#   - onboarding (team namespaces, quota, ServiceAccount, RBAC,
#     NetworkPolicy, per-team Spark quota — all tied to
#     onboarding/cluster.env's NUM_TEAMS)
#   - shared infra services (the in-cluster registry, the Spark queue
#     controller + admission policy)
#   - the Tekton Dashboard's Ingress (applied last on purpose — see Step 6)
#
# Deliberately does NOT include pipeline RBAC, Tekton Tasks/Pipeline, or
# submitting a PipelineRun — see deploy-pipeline.sh for that (the part
# that actually changes if you modify the deploy pipeline itself).
#
# On Kind, cloud-provider-kind (ingress/portable/) needs root and must be
# started separately in its own terminal — this script cannot do that for
# you (no way to prompt for a sudo password non-interactively), so it
# checks for it and prints instructions rather than blocking. Everything
# else here works without it; it's only needed for the Tekton Dashboard's
# external reachability, not for the Kafka/event-generator deploy path.
#
# Safe to re-run (idempotent throughout).
#
# Usage: install-prerequisites.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

info() { echo "▶ $*"; }
warn() { echo "  ⚠ $*"; }

if [[ ! -f "${REPO_ROOT}/config.env" ]]; then
  echo "ERROR: config.env not found at ${REPO_ROOT}/config.env"
  echo "       Copy config.env.example to config.env and fill in your values."
  exit 1
fi
source "${REPO_ROOT}/config.env"

for var in INFRA_NAMESPACE STORAGE_CLASS REGISTRY_VOLUME_SIZE \
           REGISTRY_NODE_PORT SPARK_IMAGE DASHBOARD_HOST; do
  val="${!var:-}"
  if [[ -z "$val" ]]; then
    echo "ERROR: ${var} is not set in config.env"
    exit 1
  fi
done

echo "============================================================"
echo " Installing prerequisites"
echo "============================================================"

echo ""
info "Step 1 — Cluster-wide prerequisites (idempotent)..."
bash "${REPO_ROOT}/kafka/portable/prerequisites/install-strimzi.sh"
bash "${REPO_ROOT}/pipeline/portable/prerequisites/install-tekton.sh"
bash "${REPO_ROOT}/pipeline/portable/prerequisites/install-tekton-dashboard.sh"
bash "${REPO_ROOT}/ingress/portable/prerequisites/install-ingress-nginx.sh"
# install-ingress-nginx.sh itself already polls for the external IP and
# reports accurately (Kind vs. a real cloud) when it's still missing —
# nothing more to check here.

echo ""
info "Step 2 — Onboarding (namespaces, quota, ServiceAccount, RBAC, NetworkPolicy)..."
bash "${REPO_ROOT}/onboarding/portable/apply-onboarding.sh"

echo ""
info "Step 3 — Spark per-team ResourceQuota..."
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
info "Step 4 — In-cluster registry..."
envsubst '${INFRA_NAMESPACE} ${STORAGE_CLASS} ${REGISTRY_VOLUME_SIZE} ${REGISTRY_NODE_PORT}' \
  < "${REPO_ROOT}/registry/portable/manifests/registry.yaml" | kubectl apply -f -
kubectl rollout status deployment/registry -n "${INFRA_NAMESPACE}" --timeout=120s

echo ""
info "Step 5 — Spark queue controller + admission policy..."
kubectl create configmap spark-queue-controller-script \
  --from-file=queue-controller.py="${REPO_ROOT}/spark-queue/portable/scripts/queue-controller.py" \
  -n "${INFRA_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
SPARK_IMAGE_REPO="${SPARK_IMAGE%:*}"
export SPARK_IMAGE_REPO
envsubst '${INFRA_NAMESPACE} ${SPARK_IMAGE_REPO}' < "${REPO_ROOT}/spark-queue/portable/manifests/queue-controller-deployment.yaml" | kubectl apply -f -
envsubst '${SPARK_IMAGE_REPO}' < "${REPO_ROOT}/spark-queue/portable/manifests/spark-job-admission-policy.yaml" | kubectl apply -f -

echo ""
info "Step 6 — Tekton Dashboard Ingress..."
# Applying an Ingress before ingress-nginx's admission webhook is actually
# reachable fails with "connection refused" — confirmed directly, even
# when its Endpoints object already looks ready (kube-proxy's own
# iptables/ipvs rules for a freshly-created pod can lag behind the
# Endpoints object updating, so checking Endpoints isn't a reliable
# enough signal either — confirmed directly too: it passed and the apply
# still failed). Retrying the apply itself is the robust fix, not
# pre-checking readiness through an indirect signal.
DASHBOARD_INGRESS_RENDERED="$(envsubst '${DASHBOARD_HOST}' < "${REPO_ROOT}/pipeline/portable/manifests/tekton-dashboard-ingress-template.yaml")"
attempt=1
until echo "${DASHBOARD_INGRESS_RENDERED}" | kubectl apply -f -; do
  if (( attempt >= 6 )); then
    echo "ERROR: Dashboard Ingress still failing after ${attempt} attempts (ingress-nginx's admission webhook unreachable)." >&2
    exit 1
  fi
  warn "Dashboard Ingress apply failed (attempt ${attempt}/6) — ingress-nginx's admission webhook isn't reachable yet, retrying in 5s..."
  sleep 5
  attempt=$(( attempt + 1 ))
done

echo ""
info "Step 7 — Tekton Dashboard link..."
EXTERNAL_IP="$(kubectl get service ingress-nginx-controller -n ingress-nginx -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null)"
# Test the thing that actually matters — does DASHBOARD_HOST already
# resolve to something — rather than guessing from which platform this
# is (Kind vs k3s vs a specific cloud): that only generalizes to the two
# platforms actually tested here, not to every "Kind, k3s, EKS, GKE, AKS,
# bare-metal" target this whole repo is meant to run on.
resolve_host() {
  if command -v getent >/dev/null 2>&1; then
    getent hosts "$1" 2>/dev/null | awk '{print $1; exit}'
  elif command -v dscacheutil >/dev/null 2>&1; then
    dscacheutil -q host -a name "$1" 2>/dev/null | awk '/^ip_address:/{print $2; exit}'
  fi
}
RESOLVED_IP="$(resolve_host "${DASHBOARD_HOST}")"

echo ""
echo "============================================================"
echo " Tekton Dashboard"
echo "============================================================"
if [[ -n "${RESOLVED_IP}" ]]; then
  # Already resolves — real DNS record, or an /etc/hosts entry from a
  # previous run — nothing left to do.
  echo "   http://${DASHBOARD_HOST}/"
elif [[ -n "${EXTERNAL_IP}" ]]; then
  echo " ${DASHBOARD_HOST} doesn't resolve yet. Pick whichever applies:"
  echo "   Local cluster (Kind, k3s, ...), no real DNS:"
  echo "     echo \"${EXTERNAL_IP} ${DASHBOARD_HOST}\" | sudo tee -a /etc/hosts"
  echo "   Real cloud cluster (OVHcloud, EKS, GKE, AKS, ...):"
  echo "     create a DNS A record for ${DASHBOARD_HOST} -> ${EXTERNAL_IP}"
  echo " Then open:"
  echo "   http://${DASHBOARD_HOST}/"
else
  echo " ingress-nginx has no external IP yet — see the warning above"
  echo " (on Kind: start cloud-provider-kind; on a real cloud: the"
  echo " LoadBalancer is still provisioning). Re-run this step once it does."
fi
echo "============================================================"

echo ""
echo "============================================================"
echo " Prerequisites ready — next: deploy-pipeline.sh"
echo "============================================================"
