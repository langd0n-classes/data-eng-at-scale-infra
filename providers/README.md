# Provider Adapter Files

What a target Kubernetes cluster needs that the rest of the portable
platform doesn't know on its own: `StorageClass`, LoadBalancer
behavior, a NetworkPolicy-enforcement check, and the minimum
Kubernetes version (1.30+ — required by `ValidatingAdmissionPolicy`).

## How it's selected

`PROVIDER` picks a file: `providers/${PROVIDER}.env` (defaults to
`kind`). `install-prerequisites.sh`, `apply-onboarding.sh`, and
`deploy-pipeline.sh` source it automatically — nothing to do by hand
for those. Any other command that reads `STORAGE_CLASS` directly
(a raw one-liner, bypassing those scripts) needs
`source providers/${PROVIDER:-kind}.env` added itself.

## What's in here

- **`kind.env`** — validated locally. Nothing to change for local Kind
  testing. Kind needs two things beyond its two settings:
  `cloud-provider-kind` running (Kind has no LoadBalancer of its own)
  and Calico installed (Kind's default CNI doesn't enforce
  NetworkPolicy) — both covered in `onboarding/portable/README.md` and
  `ingress/portable/README.md`.
- **`ovh.env`** — validated on a managed OVHcloud MKS cluster (Free plan,
  `US-EAST-VA-1`, `b2-30` nodes, Kubernetes 1.34.6): `STORAGE_CLASS`
  (`csi-cinder-high-speed-gen2`) provisions real Cinder-backed PVCs;
  `LoadBalancer` gets a real external IP from Octavia automatically, no
  extra component needed; `check-networkpolicy.sh` confirmed Canal CNI
  (the Free plan's default) genuinely enforces NetworkPolicy, same as
  Kind's Calico.
- **`TEMPLATE.env.example`** — copy this for a real cluster (see
  below). No values are asserted in it on purpose.
- **`lib/check-networkpolicy.sh`** — the actual automated
  NetworkPolicy check, not just documentation: proves a deny-all
  policy is genuinely enforced, live, on whatever cluster you're on.
  Called automatically by `apply-onboarding.sh`. Works identically on
  any cluster — nothing Kind-specific in it.

## Adding a new provider

```bash
cp providers/TEMPLATE.env.example providers/<name>.env
export PROVIDER=<name>
```

Fill in:
- `STORAGE_CLASS` — confirm with `kubectl get storageclass`, never guess.
- `MIN_KUBERNETES_VERSION` — usually `1.30`, unless this provider needs more.
- The `LoadBalancer` comment — does its own cloud-controller-manager
  provision a real external IP automatically (most managed clouds do),
  or does it need something extra, the way Kind needs
  `cloud-provider-kind`?
- The `NetworkPolicy enforcement` comment — does its default CNI
  actually enforce NetworkPolicy, or does it need a different one
  installed first, the way Kind needs Calico? Don't assume either way
  — `lib/check-networkpolicy.sh` confirms it live regardless.
