#!/usr/bin/env bash
# event-generator/portable/build-and-push.sh
#
# Standalone build+push for the event generator image, with no Tekton
# pipeline and no local Docker daemon required — runs kaniko as a one-shot
# pod inside the cluster, the same way pipeline/portable/'s build Task
# does, just invoked directly. Works identically on a local Kind cluster
# and a real production cluster.
#
# Both modes share one short-lived PVC as the build context, filled
# differently depending on where the source comes from:
#   git   — a shallow (--depth=1), sparse (only event-generator/) clone
#           into the PVC. Deliberately NOT kaniko's own --context=git://,
#           which would clone the entire repo + full history on every
#           build. Reads GIT_REPO_URL/GIT_BRANCH from config.env — the
#           same variables the real pipeline's git-clone Task uses, so
#           this never silently builds a different branch than the
#           pipeline would.
#   local — `kubectl cp` of a local directory into the same PVC, for
#           testing uncommitted changes.
# kaniko itself always reads from the PVC either way.
#
# Skip-if-unchanged + auto-redeploy: only what the Dockerfile actually
# copies (src/ + Dockerfile, not README/CLAUDE.md/portable/) is
# content-hashed (sha256 over every file's path+contents,
# order-independent), combined with a hash of the event generator's own
# runtime config values (EVENT_RATE_PER_SEC, TOPIC_PREFIX/SUFFIX, TOPIC,
# REGIONS, TEAM_BOOTSTRAP_SERVERS, KAFKA_BOOTSTRAP_SERVERS) — so a
# config-only change (e.g. a new team added to TEAM_BOOTSTRAP_SERVERS)
# still triggers a redeploy even when the code itself didn't change.
# Combined hash is compared against the `source-hash` annotation already
# on the running Deployment (the source of truth lives in-cluster, not in
# a local file, so this behaves the same regardless of which machine runs
# the script). Unchanged -> the kaniko build is skipped entirely. Changed
# -> build, push, stamp the new hash on the Deployment, and
# `kubectl rollout restart` it so the change actually goes live.
#
# Usage:
#   source config.env && ./build-and-push.sh git
#   ./build-and-push.sh local <path-to-event-generator-dir>
set -euo pipefail

MODE="${1:?usage: build-and-push.sh git|local [path]}"
source config.env

IMAGE="localhost:${REGISTRY_NODE_PORT}/${EVENT_GENERATOR_NAME}:latest"
PVC_NAME="kaniko-build-context"
DEPLOYMENT="${EVENT_GENERATOR_NAME}"

# Always clean up the build pod/PVC on the way out, success or failure — a
# failed rollout must still fail the script (that's a real signal, not
# something to hide), but it must not leave the PVC/pod behind to collide
# with the next run.
cleanup() {
  kubectl delete pod kaniko-build kaniko-context-clone kaniko-context-holder -n "${INFRA_NAMESPACE}" --ignore-not-found >/dev/null 2>&1
  kubectl delete pvc "${PVC_NAME}" -n "${INFRA_NAMESPACE}" --ignore-not-found >/dev/null 2>&1
}
trap cleanup EXIT

# macOS ships `shasum -a 256`, not `sha256sum` — this runs on the host, not in-cluster
sha256_of_stdin() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | awk '{print $1}'
  else shasum -a 256 | awk '{print $1}'
  fi
}

hash_local_tree() {
  local dir="$1"
  local hasher="sha256sum"
  command -v sha256sum >/dev/null 2>&1 || hasher="shasum -a 256"
  # only what the Dockerfile actually copies — not README/CLAUDE.md/portable/
  # (so unrelated doc edits don't trigger a rebuild) and not __pycache__
  # (gitignored, so a git-cloned source tree never has it, but a local
  # checkout picks one up the moment you've ever run the script locally —
  # confirmed directly: this caused a real, permanent mismatch against the
  # pipeline's git-based hash until excluded here)
  (cd "$dir" && find src Dockerfile -type f ! -path '*/__pycache__/*' ! -name '*.pyc' | LC_ALL=C sort | xargs $hasher | sha256_of_stdin)
}

# must match the pipeline's check-source-changed-task.yaml CONFIG_VALUES
# string exactly (same fields, same order, same "|" delimiter)
config_values_string() {
  printf '%s' "${EVENT_RATE_PER_SEC:-}|${TOPIC_PREFIX:-}|${TOPIC_SUFFIX:-}|${TOPIC:-}|${REGIONS:-}|${TEAM_BOOTSTRAP_SERVERS:-}|${KAFKA_BOOTSTRAP_SERVERS:-}"
}

combined_hash() {
  local code_hash="$1"
  printf '%s' "${code_hash}$(config_values_string)" | sha256_of_stdin
}

deployment_exists() {
  kubectl get deployment "${DEPLOYMENT}" -n "${INFRA_NAMESPACE}" >/dev/null 2>&1
}

deployed_hash() {
  kubectl get deployment "${DEPLOYMENT}" -n "${INFRA_NAMESPACE}" \
    -o jsonpath='{.metadata.annotations.source-hash}' 2>/dev/null || true
}

create_pvc() {
  kubectl apply -n "${INFRA_NAMESPACE}" -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${PVC_NAME}
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: ${STORAGE_CLASS}
  resources:
    requests:
      storage: 1Gi
EOF
}

finish_and_redeploy() {
  local new_hash="$1"
  if deployment_exists; then
    kubectl annotate deployment "${DEPLOYMENT}" -n "${INFRA_NAMESPACE}" "source-hash=${new_hash}" --overwrite
    kubectl rollout restart deployment "${DEPLOYMENT}" -n "${INFRA_NAMESPACE}"
    kubectl rollout status deployment "${DEPLOYMENT}" -n "${INFRA_NAMESPACE}" --timeout=120s
  else
    echo "No existing ${DEPLOYMENT} Deployment found — deploy it for the first time (see README step 1), then re-run this script on future code changes to auto-redeploy."
  fi
}

PREV_HASH="$(deployed_hash)"

case "$MODE" in
  git)
    : "${GIT_REPO_URL:?GIT_REPO_URL must be set in config.env}"
    : "${GIT_BRANCH:?GIT_BRANCH must be set in config.env}"
    create_pvc
    kubectl apply -n "${INFRA_NAMESPACE}" -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: kaniko-context-clone
spec:
  restartPolicy: Never
  containers:
    - name: clone
      image: alpine/git:2.47.2
      command: ["/bin/sh", "-c"]
      args:
        - |
          set -eu
          git clone --depth=1 --branch "${GIT_BRANCH}" --filter=blob:none --sparse "${GIT_REPO_URL}" /tmp/clone
          cd /tmp/clone
          git sparse-checkout set event-generator
          cp -r event-generator/. /workspace/
          cd /workspace
          echo "SOURCE_HASH:\$(find src Dockerfile -type f | sort | xargs sha256sum | sha256sum | awk '{print \$1}')"
      volumeMounts:
        - name: ctx
          mountPath: /workspace
  volumes:
    - name: ctx
      persistentVolumeClaim:
        claimName: ${PVC_NAME}
EOF
    kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/kaniko-context-clone -n "${INFRA_NAMESPACE}" --timeout=120s
    CODE_HASH="$(kubectl logs pod/kaniko-context-clone -n "${INFRA_NAMESPACE}" | grep '^SOURCE_HASH:' | cut -d: -f2)"
    kubectl delete pod kaniko-context-clone -n "${INFRA_NAMESPACE}" --ignore-not-found
    NEW_HASH="$(combined_hash "${CODE_HASH}")"

    if [[ -n "${PREV_HASH}" && "${PREV_HASH}" == "${NEW_HASH}" ]]; then
      echo "Source unchanged (${NEW_HASH:0:12}...) — skipping build."
      exit 0
    fi
    ;;
  local)
    LOCAL_DIR="${2:?local event-generator directory required}"
    NEW_HASH="$(combined_hash "$(hash_local_tree "${LOCAL_DIR}")")"

    if [[ -n "${PREV_HASH}" && "${PREV_HASH}" == "${NEW_HASH}" ]]; then
      echo "Source unchanged (${NEW_HASH:0:12}...) — skipping build."
      exit 0
    fi

    create_pvc
    kubectl apply -n "${INFRA_NAMESPACE}" -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: kaniko-context-holder
spec:
  restartPolicy: Never
  containers:
    - name: holder
      image: busybox:stable
      command: ["sleep", "300"]
      volumeMounts:
        - name: ctx
          mountPath: /workspace
  volumes:
    - name: ctx
      persistentVolumeClaim:
        claimName: ${PVC_NAME}
EOF
    kubectl wait --for=condition=Ready pod/kaniko-context-holder -n "${INFRA_NAMESPACE}" --timeout=60s
    kubectl cp "${LOCAL_DIR}/." "${INFRA_NAMESPACE}/kaniko-context-holder:/workspace"
    kubectl delete pod kaniko-context-holder -n "${INFRA_NAMESPACE}" --ignore-not-found
    ;;
  *)
    echo "unknown mode: ${MODE} (use 'git' or 'local')" >&2
    exit 1
    ;;
esac

# the actual build — identical from here on, regardless of mode
kubectl apply -n "${INFRA_NAMESPACE}" -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: kaniko-build
spec:
  restartPolicy: Never
  hostNetwork: true
  containers:
    - name: kaniko-build
      image: gcr.io/kaniko-project/executor:v1.24.0
      args:
        - --dockerfile=/workspace/Dockerfile
        - --context=dir:///workspace
        - --destination=${IMAGE}
        - --insecure
        - --insecure-pull
      volumeMounts:
        - name: ctx
          mountPath: /workspace
  volumes:
    - name: ctx
      persistentVolumeClaim:
        claimName: ${PVC_NAME}
EOF
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/kaniko-build -n "${INFRA_NAMESPACE}" --timeout=180s
kubectl logs pod/kaniko-build -n "${INFRA_NAMESPACE}" --tail=5

finish_and_redeploy "${NEW_HASH}"
