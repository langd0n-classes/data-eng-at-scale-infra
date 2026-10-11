#!/usr/bin/env bash
# pipeline/portable/prerequisites/install-tekton.sh
#
# Cluster-admin, one-time script: installs Tekton Pipelines v1.15.3 (LTS)
# using the official release manifest. Installs into the `tekton-pipelines`
# namespace (upstream default).
#
# This is the portable equivalent of installing the OpenShift Pipelines
# operator via OperatorHub (root README.md Quick Start, step 1) — vanilla
# Kubernetes has no operator-based install path, so this applies Tekton's
# own upstream release manifest directly instead.
#
# Safe to re-run (idempotent — kubectl apply).
#
# Usage: install-tekton.sh
set -euo pipefail

TEKTON_PIPELINES_VERSION="v1.15.3"

echo "=========================================="
echo "Installing Tekton Pipelines (${TEKTON_PIPELINES_VERSION})"
echo "=========================================="

kubectl apply -f "https://github.com/tektoncd/pipeline/releases/download/${TEKTON_PIPELINES_VERSION}/release.yaml"

echo ""
echo "Waiting for the Tekton controller and webhook Deployments to become available..."
kubectl wait deployment tekton-pipelines-controller -n tekton-pipelines \
  --for=condition=Available --timeout=180s
kubectl wait deployment tekton-pipelines-webhook -n tekton-pipelines \
  --for=condition=Available --timeout=180s

echo ""
echo "=========================================="
echo "Tekton Pipelines ${TEKTON_PIPELINES_VERSION} installed"
echo "=========================================="
kubectl get deployment -n tekton-pipelines
kubectl get crd | grep tekton.dev
