#!/usr/bin/env bash
# ingress/portable/prerequisites/install-ingress-nginx.sh
#
# Cluster-admin, one-time script: installs the ingress-nginx controller
# (version pinned in portable/versions.env) using the generic "cloud"
# provider manifest — not Kind's
# NodePort-specific one — so the controller's own Service is a real
# `type: LoadBalancer`, matching how it will actually behave on a real
# cloud (OVH included) later. Item 9 requires exactly one such Service for
# all external routes in this platform.
#
# On Kind specifically, a LoadBalancer Service never gets an external IP on
# its own — Kind has no cloud provider. See
# install-cloud-provider-kind.sh, which must also run (and keep running)
# for the Service below to receive one locally. On a real cloud cluster,
# that script is not needed at all — the cloud's own LoadBalancer
# implementation takes over.
#
# Also disables ExternalName Service backends on the controller itself
# (--disable-svc-external-name) and restricts what hosts a team Ingress
# can claim (team-ingress-host-policy.yaml) — both close ways a team
# could use the shared ingress to reach another team. See each one's own
# comment for why.
#
# Safe to re-run (idempotent — kubectl apply).
#
# Usage: install-ingress-nginx.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
# shellcheck disable=SC1091
source "${REPO_ROOT}/portable/versions.env"

echo "=========================================="
echo "Installing ingress-nginx (${INGRESS_NGINX_VERSION})"
echo "=========================================="

kubectl apply -f "https://raw.githubusercontent.com/kubernetes/ingress-nginx/${INGRESS_NGINX_VERSION}/deploy/static/provider/cloud/deploy.yaml"

echo ""
echo "Disabling ExternalName Service backends..."
# Patched in here, not before, since `kubectl apply` above always resets
# args to upstream's list on every run — this keeps the flag present
# regardless of how many times the script is re-run. Patched before
# waiting for Available below so a fresh install's first rollout already
# carries it, rather than an unpatched pod going Available first.
kubectl patch deployment ingress-nginx-controller -n ingress-nginx \
  --type=json -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--disable-svc-external-name"}]'

echo ""
echo "Waiting for the ingress-nginx controller Deployment to become available..."
kubectl wait deployment ingress-nginx-controller -n ingress-nginx \
  --for=condition=Available --timeout=180s

echo ""
echo "Restricting team Ingress hosts to each team's own namespace..."
kubectl apply -f "${REPO_ROOT}/ingress/portable/manifests/team-ingress-host-policy.yaml"

echo ""
echo "=========================================="
echo "ingress-nginx ${INGRESS_NGINX_VERSION} installed"
echo "=========================================="
kubectl get deployment ingress-nginx-controller -n ingress-nginx
kubectl get service ingress-nginx-controller -n ingress-nginx

has_external_ip() {
  kubectl get service ingress-nginx-controller -n ingress-nginx \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null | grep -q .
}

if ! has_external_ip; then
  # Poll for up to 90s before reporting anything's actually wrong, not just
  # slow — a real cloud's LoadBalancer provisioning commonly takes under a
  # minute, and k3s's own built-in one resolves almost immediately. 90s
  # gives real, normal cases room without hanging indefinitely on one that
  # genuinely never will (see the no-LB-controller-at-all case below).
  echo ""
  echo "No external IP yet — polling for up to 90s..."
  lb_deadline=$(( $(date +%s) + 90 ))
  while (( $(date +%s) < lb_deadline )) && ! has_external_ip; do
    sleep 5
  done
fi

echo ""
if has_external_ip; then
  echo "External IP: $(kubectl get service ingress-nginx-controller -n ingress-nginx -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"
else
  echo "Still no external IP after 90s."
  # cloud-provider-kind is only ever relevant on Kind specifically — its
  # context name is a reliable, accurate signal for this one decision
  # (it's Kind's own hardcoded convention, not a guess).
  if [[ "$(kubectl config current-context 2>/dev/null)" == kind-* ]]; then
    echo "This is Kind — it needs cloud-provider-kind running, which you must"
    echo "start yourself (needs root, can't be done non-interactively):"
    echo "  sudo bash $(dirname "${BASH_SOURCE[0]}")/install-cloud-provider-kind.sh"
  else
    echo "A real cloud's own LoadBalancer provisioning can occasionally take"
    echo "longer than this — check again shortly with: kubectl get service"
    echo "ingress-nginx-controller -n ingress-nginx. If it never resolves,"
    echo "this cluster likely has no LoadBalancer implementation at all"
    echo "(e.g. a bare-metal cluster with no MetalLB or similar installed)."
  fi
fi
