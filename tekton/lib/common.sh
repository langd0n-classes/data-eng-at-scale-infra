#!/usr/bin/env bash
# tekton/lib/common.sh — Shared helpers for tekton/*.sh scripts
# Source this file; do not run it directly.
#
# Usage:
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   source "${SCRIPT_DIR}/lib/common.sh"   # from setup.sh / ops.sh (one level up)

info() { echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }
err()  { echo "  ✗ $*" >&2; }

# DRY_RUN-aware command executor.
# Set DRY_RUN=true in the calling script to print commands without running them.
run() {
  if [[ "${DRY_RUN:-false}" == "true" ]]; then
    echo "  [dry-run] $*"
  else
    eval "$@"
  fi
}

# FORCE-aware confirmation prompt.
# Set FORCE=true in the calling script (or environment) to skip all prompts.
confirm() {
  local prompt="$1"
  if [[ "${FORCE:-false}" == "true" ]]; then
    echo "  (FORCE=true — skipping: ${prompt})"
    return 0
  fi
  read -r -p "${prompt} (y/N): " answer
  [[ "$answer" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
}

# Find and source config.env from the given repo root.
# Exits with a clear error message if the file is missing.
load_config() {
  local repo_root="${1}"
  local config_file="${repo_root}/config.env"
  if [[ ! -f "${config_file}" ]]; then
    echo "ERROR: config.env not found at ${config_file}"
    echo "       Copy config.env.example to config.env and fill in your values."
    exit 1
  fi
  source "${config_file}"
}
