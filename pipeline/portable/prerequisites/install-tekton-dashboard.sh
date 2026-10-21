#!/usr/bin/env bash
# pipeline/portable/prerequisites/install-tekton-dashboard.sh
#
# Cluster-admin, one-time script: installs the Tekton Dashboard (version
# pinned in portable/versions.env) in READ-ONLY mode, using the official
# upstream release manifest.
#
# Confirmed (not assumed) by inspecting the manifest directly: the plain
# `release.yaml` build passes `--read-only=true` to the dashboard container
# — this is the read-only variant. `release-full.yaml` is the read/write
# one and is deliberately NOT used here — item 3 requires read-only.
#
# Requires install-tekton.sh to have already run (the Dashboard is a UI over
# Tekton's own CRDs/API).
#
# Also applies tekton-dashboard-networkpolicy.yaml right after the
# Deployment comes up, before the Ingress step runs later — otherwise the
# Service would be reachable cluster-wide (bypassing the Ingress's basic
# auth/TLS) in the gap between the two.
#
# Safe to re-run (idempotent — kubectl apply).
#
# Usage: install-tekton-dashboard.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
# shellcheck disable=SC1091
source "${REPO_ROOT}/portable/versions.env"

echo "=========================================="
echo "Installing Tekton Dashboard (${TEKTON_DASHBOARD_VERSION}, read-only)"
echo "=========================================="

kubectl apply -f "https://github.com/tektoncd/dashboard/releases/download/${TEKTON_DASHBOARD_VERSION}/release.yaml"

echo ""
echo "Waiting for the Dashboard Deployment to become available..."
kubectl wait deployment tekton-dashboard -n tekton-pipelines \
  --for=condition=Available --timeout=180s

echo ""
echo "Restricting the dashboard Service to the ingress-nginx controller only..."
kubectl apply -f "${REPO_ROOT}/pipeline/portable/manifests/tekton-dashboard-networkpolicy.yaml"

echo ""
echo "=========================================="
echo "Tekton Dashboard ${TEKTON_DASHBOARD_VERSION} installed (read-only)"
echo "=========================================="
kubectl get deployment tekton-dashboard -n tekton-pipelines
kubectl get service tekton-dashboard -n tekton-pipelines
echo ""
echo "Not externally reachable yet — apply"
echo "pipeline/portable/manifests/tekton-dashboard-ingress-template.yaml to expose it"
echo "through the shared ingress-nginx LoadBalancer (see ingress/portable/)."
