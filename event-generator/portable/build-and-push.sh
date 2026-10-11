#!/usr/bin/env bash
# event-generator/portable/build-and-push.sh
#
# Standalone build+push for the event generator image, using local Docker
# — for testing a change before/without waiting on
# .github/workflows/build-event-generator.yml. Pushes to the exact same
# GHCR destination that workflow uses, so either one produces an
# identical, interchangeable image.
#
# Needs a WRITE-capable login first (not the cluster's read-only
# ghcr-pull-secret, which can't push): `docker login ghcr.io -u
# <your-github-username>` with a PAT that has write:packages.
#
# Usage:
#   source config.env && ./build-and-push.sh git     # from the pushed branch (GIT_REPO_URL/GIT_BRANCH)
#   ./build-and-push.sh local <path-to-event-generator-dir>
set -euo pipefail

MODE="${1:?usage: build-and-push.sh git|local [path]}"
source config.env

IMAGE="${GHCR_NAMESPACE}/${EVENT_GENERATOR_NAME}:latest"
DEPLOYMENT="${EVENT_GENERATOR_NAME}"

case "$MODE" in
  git)
    : "${GIT_REPO_URL:?GIT_REPO_URL must be set in config.env}"
    : "${GIT_BRANCH:?GIT_BRANCH must be set in config.env}"
    BUILD_DIR="$(mktemp -d)"
    trap 'rm -rf "${BUILD_DIR}"' EXIT
    git clone --depth=1 --branch "${GIT_BRANCH}" --filter=blob:none --sparse \
      "${GIT_REPO_URL}" "${BUILD_DIR}/repo"
    (cd "${BUILD_DIR}/repo" && git sparse-checkout set event-generator)
    CONTEXT="${BUILD_DIR}/repo/event-generator"
    ;;
  local)
    CONTEXT="${2:?local event-generator directory required}"
    ;;
  *)
    echo "unknown mode: ${MODE} (use 'git' or 'local')" >&2
    exit 1
    ;;
esac

docker build -t "${IMAGE}" -f "${CONTEXT}/Dockerfile" "${CONTEXT}"
docker push "${IMAGE}"

if kubectl get deployment "${DEPLOYMENT}" -n "${INFRA_NAMESPACE}" >/dev/null 2>&1; then
  kubectl rollout restart deployment "${DEPLOYMENT}" -n "${INFRA_NAMESPACE}"
  kubectl rollout status deployment "${DEPLOYMENT}" -n "${INFRA_NAMESPACE}" --timeout=120s
else
  echo "No existing ${DEPLOYMENT} Deployment found — deploy it for the first time (see README step 1), then re-run this script on future code changes to auto-redeploy."
fi
