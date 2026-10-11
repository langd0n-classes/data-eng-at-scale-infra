#!/usr/bin/env bash
# ingress/portable/prerequisites/install-ingress-nginx.sh
#
# Cluster-admin, one-time script: installs the ingress-nginx controller
# (v1.15.1) using the generic "cloud" provider manifest — not Kind's
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
# Safe to re-run (idempotent — kubectl apply).
#
# Usage: install-ingress-nginx.sh
set -euo pipefail

INGRESS_NGINX_VERSION="controller-v1.15.1"

echo "=========================================="
echo "Installing ingress-nginx (${INGRESS_NGINX_VERSION})"
echo "=========================================="

kubectl apply -f "https://raw.githubusercontent.com/kubernetes/ingress-nginx/${INGRESS_NGINX_VERSION}/deploy/static/provider/cloud/deploy.yaml"

echo ""
echo "Waiting for the ingress-nginx controller Deployment to become available..."
kubectl wait deployment ingress-nginx-controller -n ingress-nginx \
  --for=condition=Available --timeout=180s

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
