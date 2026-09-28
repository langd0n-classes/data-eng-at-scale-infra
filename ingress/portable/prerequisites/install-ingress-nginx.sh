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
echo ""
echo "EXTERNAL-IP will stay <pending> on Kind until install-cloud-provider-kind.sh"
echo "is installed AND running — see ingress/portable/README.md."
