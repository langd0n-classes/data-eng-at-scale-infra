#!/usr/bin/env bash
# ingress/portable/prerequisites/install-cloud-provider-kind.sh
#
# Kind-only, local-only prerequisite — NOT part of the eventual OVH-cloud
# path (a real cloud already provides its own LoadBalancer implementation;
# this exists purely to simulate one for local Kind validation).
#
# Kind has no cloud provider, so a `type: LoadBalancer` Service (like
# ingress-nginx's, see install-ingress-nginx.sh) never gets an external IP
# on its own. cloud-provider-kind (kubernetes-sigs) is a small controller
# that runs as a process on the HOST machine, watches every local Kind
# cluster's LoadBalancer Services, and assigns each one a real external IP
# backed by a small proxy container on the Kind Docker network.
#
# It must keep running for LoadBalancer Services to keep working — this
# script downloads the pinned binary and starts it in the background
# (idempotent: does nothing if an instance is already running).
#
# REQUIRES root: cloud-provider-kind manipulates host networking (iptables /
# network namespaces) to route traffic to its LoadBalancer proxy containers,
# and exits immediately with "please run this again with `sudo`" otherwise.
# Run this script itself with sudo — do not try to sudo only the binary
# invocation inside it, since the download/extract steps need to write to
# this script's own .bin/ directory as the invoking user.
#
# Usage: sudo install-cloud-provider-kind.sh
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: this script must be run with sudo (cloud-provider-kind requires root"
  echo "       to manipulate host networking). Re-run as:"
  echo "         sudo $0"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CPK_VERSION="0.11.1"
BIN_DIR="${SCRIPT_DIR}/.bin"
BIN_PATH="${BIN_DIR}/cloud-provider-kind"
PID_FILE="${BIN_DIR}/cloud-provider-kind.pid"
LOG_FILE="${BIN_DIR}/cloud-provider-kind.log"

mkdir -p "${BIN_DIR}"

echo "=========================================="
echo "Installing cloud-provider-kind (v${CPK_VERSION}, Kind-only)"
echo "=========================================="

if [[ -f "${PID_FILE}" ]] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
  echo "Already running (pid $(cat "${PID_FILE}")) — nothing to do."
  exit 0
fi

if [[ ! -x "${BIN_PATH}" ]]; then
  OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
  ARCH="$(uname -m)"
  case "${ARCH}" in
    x86_64) ARCH="amd64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    *) echo "ERROR: unsupported architecture '${ARCH}'"; exit 1 ;;
  esac

  echo "Downloading cloud-provider-kind v${CPK_VERSION} for ${OS}/${ARCH}..."
  WORKDIR="$(mktemp -d)"
  trap 'rm -rf "${WORKDIR}"' EXIT
  curl -sL -o "${WORKDIR}/cpk.tar.gz" \
    "https://github.com/kubernetes-sigs/cloud-provider-kind/releases/download/v${CPK_VERSION}/cloud-provider-kind_${CPK_VERSION}_${OS}_${ARCH}.tar.gz"
  tar xzf "${WORKDIR}/cpk.tar.gz" -C "${WORKDIR}"
  mv "${WORKDIR}/cloud-provider-kind" "${BIN_PATH}"
  chmod +x "${BIN_PATH}"
else
  echo "Binary already present at ${BIN_PATH}"
fi

echo "Starting cloud-provider-kind in the background (log: ${LOG_FILE})..."
nohup "${BIN_PATH}" >"${LOG_FILE}" 2>&1 &
echo $! > "${PID_FILE}"
sleep 2

if kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
  echo ""
  echo "=========================================="
  echo "cloud-provider-kind running (pid $(cat "${PID_FILE}"))"
  echo "=========================================="
  echo "Stop it with: kill \$(cat ${PID_FILE})"
else
  echo "ERROR: cloud-provider-kind exited immediately — check ${LOG_FILE}"
  exit 1
fi
