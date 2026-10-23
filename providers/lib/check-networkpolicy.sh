#!/usr/bin/env bash
# providers/lib/check-networkpolicy.sh
#
# Proves NetworkPolicy is actually enforced on the current cluster,
# rather than relying on a human reading a warning. Kind's default CNI
# (kindnet) applies a NetworkPolicy successfully but silently does
# nothing — this catches exactly that, on any cluster, before onboarding
# proceeds.
#
# Creates a disposable namespace with two pods and a deny-all-ingress
# NetworkPolicy, confirms the blocked pod genuinely can't reach the
# other, then deletes the namespace. Aborts with a clear error if traffic
# wasn't actually blocked.
#
# Cleanup is explicit at every return path, not a `trap ... RETURN` —
# confirmed directly that `RETURN` traps are unreliable on macOS's
# stock bash 3.2 (still common locally, even though every script here
# otherwise only assumes POSIX-ish bash), so this avoids that pitfall
# entirely rather than depending on a trap signal that doesn't portably
# fire inside a sourced function.
#
# Usage: source this file, then call check_networkpolicy_enforcement
set -uo pipefail

check_networkpolicy_enforcement() {
  local ns="np-enforcement-check"

  _np_check_cleanup() {
    kubectl delete namespace "${ns}" --ignore-not-found >/dev/null 2>&1
  }

  _np_check_cleanup

  kubectl create namespace "${ns}" >/dev/null
  kubectl run np-check-server --image=nginx:alpine -n "${ns}" \
    --labels=role=np-check-server --port=80 --restart=Never >/dev/null
  kubectl expose pod np-check-server -n "${ns}" --port=80 --name=np-check-server >/dev/null
  kubectl run np-check-client --image=busybox:stable -n "${ns}" \
    --restart=Never --command -- sleep 60 >/dev/null

  kubectl apply -n "${ns}" -f - >/dev/null <<'EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: deny-all-ingress
spec:
  podSelector:
    matchLabels:
      role: np-check-server
  policyTypes:
    - Ingress
  ingress: []
EOF

  if ! kubectl wait pod np-check-server np-check-client -n "${ns}" \
      --for=condition=Ready --timeout=60s >/dev/null 2>&1; then
    echo "ERROR: NetworkPolicy enforcement check couldn't even start (test pods never became Ready) — investigate before continuing." >&2
    _np_check_cleanup
    return 1
  fi

  if kubectl exec np-check-client -n "${ns}" -- \
      wget -qO- --timeout=4 http://np-check-server >/dev/null 2>&1; then
    echo "ERROR: NetworkPolicy is NOT enforced on this cluster — a deny-all-ingress policy had no effect. Install a NetworkPolicy-enforcing CNI (e.g. Calico) before continuing; see onboarding/portable/README.md's walkthrough step 1." >&2
    _np_check_cleanup
    return 1
  fi

  echo "NetworkPolicy enforcement check passed — a deny-all-ingress policy was genuinely enforced."
  _np_check_cleanup
  return 0
}
