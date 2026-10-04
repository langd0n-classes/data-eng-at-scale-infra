# Portable Pipeline (Kind / k3s)

A `kubectl`-only variant of Tekton CI/CD for the portable Fall 2026
platform: Tekton Pipelines, a read-only Tekton Dashboard, an in-cluster
image registry, and a per-team deploy pipeline (Kafka + event generator,
deliberately no NiFi) — no OpenShift Pipelines operator, no OpenShift
Routes/BuildConfigs/ImageStreams, no cloud account, no paid service.

This does **not** replace the OpenShift path. `pipeline/setup.sh`,
`pipeline/ops.sh`, and everything under `pipeline/tasks/`,
`pipeline/pipelines/`, and `pipeline/rbac/` are unchanged and remain the
primary CI/CD method for the classroom.

**Out of scope here:** ChatOps, Kafka Console, NiFi's own deployment (NiFi
itself stays fully available and untouched via the OpenShift path — only
the _pipeline's active dependency_ on deploying it is removed here, per
issue #57 item 10).

## Compatibility

Plain `kubectl`, Tekton's own upstream release manifests, no OpenShift-only
resources (no SCC dependency — Tekton Pipelines' own ServiceAccounts run
under standard Pod Security, unlike the OpenShift path's `pipeline`
ServiceAccount, which specifically exists to receive the `pipelines-scc`
auto-binding OpenShift Pipelines grants it). Runs on any conformant
Kubernetes cluster: Kind, k3s, EKS, GKE, AKS, a bare-metal cluster, etc.

## Prerequisites

Everything below is owned and tested by its own component — install and
verify each one there, not here:

- **Strimzi operator** installed — `kafka/portable/README.md` (needed
  because the deploy-kafka Task below runs
  `kafka/portable/scripts/deploy.sh`)
- **ingress-nginx + cloud-provider-kind** installed —
  `ingress/portable/README.md` (needed to expose the Tekton Dashboard)
- **In-cluster registry** deployed — `registry/portable/README.md` (needed
  for the image-build Task below; its own build/push/pull behavior is
  tested there, not here)
- **Team namespaces onboarded** — `onboarding/portable/README.md` (needed
  before the deploy-kafka / deploy-event-generator Tasks below have
  anywhere to deploy into)
- **`config.env` created and filled in** — see below

### Update config.env

Every section below needs some part of this — set it all up once, here,
first:

```bash
# one-time: create your own config.env from the template
cp config.env.example config.env
```

Then edit `config.env` and set: `VOLUME_SIZE`, `REGISTRY_VOLUME_SIZE`,
`REGISTRY_NODE_PORT`, `KUBECTL_CLI_IMAGE`, `EVENT_GENERATOR_NAME`,
`EVENT_GENERATOR_IMAGE`, `EVENT_RATE_PER_SEC`, `TOPIC_PREFIX`,
`TOPIC_SUFFIX`, `REGIONS`, `TEAM_BOOTSTRAP_SERVERS`, and `SPARK_IMAGE` —
plus these, which need a specific value, not just any value:

| Variable                        | Notes                                                                                                                                                     |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `INFRA_NAMESPACE`               | Hard-required by `apply-rbac.sh` below — it exits immediately if this isn't set                                                                           |
| `STORAGE_CLASS`                 | `standard` on Kind                                                                                                                                        |
| `GIT_REPO_URL` + `GIT_BRANCH`   | Point these at `feature/portable-fall-platform` specifically — none of this exists on `main` yet, so cloning `main` leaves every Task with nothing to run |
| `DASHBOARD_HOST`                | Hostname routed to the ingress-nginx LoadBalancer's external IP — needed to expose the Dashboard below                                                    |
| `TEAMn_NAME`, `TEAMn_NAMESPACE` | Set the pairs for however many teams you actually want deployed, leaving every other slot as `"skip"` (see "Every `TEAMn_NAME`..." further below)         |

Separately, `onboarding/cluster.env`'s `NUM_TEAMS` must match how many
teams you're actually onboarding — see `onboarding/portable/README.md`.

### Install Tekton Pipelines + the read-only Dashboard

Validated on: Kubernetes (Kind) v1.34.11, Tekton Pipelines `v1.15.3` (LTS),
Tekton Dashboard `v0.72.0` (read-only).

**1. Install Tekton Pipelines**

```bash
bash pipeline/portable/prerequisites/install-tekton.sh
```

```
deployment.apps/tekton-pipelines-controller condition met
deployment.apps/tekton-pipelines-webhook condition met

NAME                          READY   UP-TO-DATE   AVAILABLE   AGE
tekton-events-controller      1/1     1            1           40s
tekton-pipelines-controller   1/1     1            1           40s
tekton-pipelines-webhook      1/1     1            1           40s
```

**2. Install the read-only Tekton Dashboard**

Confirmed directly in the downloaded manifest (not assumed): the plain
`release.yaml` build passes `--read-only=true` to the dashboard container —
`release-full.yaml` is the read/write variant and is deliberately not used.

```bash
bash pipeline/portable/prerequisites/install-tekton-dashboard.sh
```

```
deployment.apps/tekton-dashboard condition met

NAME               READY   UP-TO-DATE   AVAILABLE   AGE
tekton-dashboard   1/1     1            1           4s
NAME               TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)    AGE
tekton-dashboard   ClusterIP   10.96.139.146   <none>        9097/TCP   4s
```

Not externally reachable yet — exposed through `ingress-nginx` next.

### Expose the Dashboard, and confirm it's reachable + read-only

Requires `ingress/portable/` already installed (ingress-nginx +
cloud-provider-kind) and `DASHBOARD_HOST` already set in `config.env`
(see "Update config.env" above).

**1. Expose it through the shared ingress-nginx LoadBalancer**

Standalone example (`export`, not just an inline prefix, so it's still set
for step 2 below):

```bash
export DASHBOARD_HOST=tekton.local
envsubst '${DASHBOARD_HOST}' \
  < pipeline/portable/manifests/tekton-dashboard-ingress-template.yaml | kubectl apply -f -
```

For a real deploy, use whatever you set `DASHBOARD_HOST` to in `config.env`
instead:

```bash
source config.env
envsubst '${DASHBOARD_HOST}' \
  < pipeline/portable/manifests/tekton-dashboard-ingress-template.yaml | kubectl apply -f -
```

```
ingress.networking.k8s.io/tekton-dashboard created
```

**2. Confirm it's reachable, and confirm read-only is actually enforced**

Use whichever `DASHBOARD_HOST` you actually created the Ingress with in
step 1 — `tekton.local` if you used the standalone example, or
`source config.env`'s value if you used the real `config.env`-driven
command instead (`tekton.apps-crc.testing` for the committed example
value, or whatever you set `EXTERNAL_DOMAIN` to):

```bash
source config.env   # only if you haven't already, in this shell
EXTERNAL_IP=$(kubectl get svc ingress-nginx-controller -n ingress-nginx -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

# Reads succeed
curl -s --resolve "${DASHBOARD_HOST}:80:${EXTERNAL_IP}" "http://${DASHBOARD_HOST}/api/v1/namespaces" -o - -w "\nHTTP %{http_code}\n"
```

```
{"kind":"NamespaceList", ... }
HTTP 200
```

```bash
# Writes are rejected
curl -s -X POST --resolve "${DASHBOARD_HOST}:80:${EXTERNAL_IP}" \
  "http://${DASHBOARD_HOST}/api/v1/namespaces/default/pipelineruns" -d '{}' -w "\nHTTP %{http_code}\n"
```

```
Forbidden - CSRF header not found in request
HTTP 403
```

On a real DNS-backed cluster, `--resolve` isn't needed at all — point
`DASHBOARD_HOST` at a real hostname and just `curl http://${DASHBOARD_HOST}/...`
directly.

**To open the Dashboard in a browser** (not just curl): `--resolve` only
fakes DNS for that one curl call, so a browser needs real name resolution
instead. On Kind (no real DNS), that means a local `/etc/hosts` entry —
just your machine, not usable by anyone else:

```bash
echo "${EXTERNAL_IP} ${DASHBOARD_HOST}" | sudo tee -a /etc/hosts
```

On a real cloud cluster (EKS, GKE, AKS, OVHcloud, any managed Kubernetes),
skip `/etc/hosts` entirely — `ingress-nginx-controller`'s `LoadBalancer`
already has a real, routable public IP there, so instead create a real DNS
A record pointing `DASHBOARD_HOST` at that IP. Once that record exists,
any browser can open `http://${DASHBOARD_HOST}/` directly, with no
per-machine setup.

## Pipeline RBAC

`rbac/` auto-detects "dedicated" vs. "shared" cluster the same way
`onboarding/apply-onboarding.sh` already does (`kubectl auth can-i create
clusterrolebindings`). `apply-rbac.sh` auto-sources `config.env` itself —
`INFRA_NAMESPACE` needs to already be set there (see "Update config.env"
above).

```bash
bash pipeline/portable/rbac/apply-rbac.sh
```

```
serviceaccount/pipeline created
Cluster type: dedicated (cluster-admin available) — using a ClusterRoleBinding
clusterrole.rbac.authorization.k8s.io/pipeline-runner-role-portable created
clusterrolebinding.rbac.authorization.k8s.io/pipeline-runner-binding-portable created
```

Most vanilla-Kubernetes clusters — a self-provisioned cloud cluster, a
bare-metal cluster you stood up yourself, etc. — hand the account that
created the cluster full cluster-admin by default, so this same
dedicated-cluster path is the common case generally, not just on Kind.
The shared-cluster fallback (`role-rolebinding-namespace.yaml` +
`clusterrole-namespaces-read.yaml`, mirroring
`pipeline/rbac/04-role-rolebinding-namespace.yaml`'s NERC scenario) exists
for the less common case of a restricted/shared cluster where that doesn't
hold.

## The full per-team deploy pipeline

Five Tasks chained by one Pipeline: clone → check if the event generator's
source changed → (if changed) build+push its image → deploy every team's
Kafka in parallel → (if changed) deploy the event generator once, after
every team's Kafka is up. Same shape as
`pipeline/pipelines/01-pipeline-deploy-all-teams.yaml`, minus NiFi. It
hardcodes team1 through team15 (Tekton's `matrix` can't derive one param
from another, e.g. building "team-01" out of "team" + "01") — a class with
fewer than 15 teams just leaves the unused `TEAMn_NAME` slots blank or
`"skip"`, and each `deploy-kafka-teamN` Task has its own `when` guard
skipping it.

**1. Apply the Tasks** — three use fixed images (git, kaniko), two need
`${KUBECTL_CLI_IMAGE}` substituted (they run plain kubectl commands):

```bash
source config.env
kubectl apply -f pipeline/portable/tasks/git-clone-task.yaml
kubectl apply -f pipeline/portable/tasks/build-push-image-task.yaml
envsubst '${KUBECTL_CLI_IMAGE}' < pipeline/portable/tasks/check-source-changed-task.yaml | kubectl apply -f -
envsubst '${KUBECTL_CLI_IMAGE}' < pipeline/portable/tasks/deploy-kafka-task.yaml | kubectl apply -f -
envsubst '${KUBECTL_CLI_IMAGE}' < pipeline/portable/tasks/deploy-event-generator-task.yaml | kubectl apply -f -
```

**2. Apply the Pipeline** — its `PipelineRun` needs `hostNetwork: true` on
the pod template (`spec.taskRunTemplate.podTemplate.hostNetwork: true`),
so the build step can reach `localhost:${REGISTRY_NODE_PORT}` — see
`registry/portable/README.md` for why:

```bash
kubectl apply -f pipeline/portable/pipelines/deploy-all-teams-pipeline.yaml
```

**3. Submit a `PipelineRun`** — applying the Pipeline above only registers
the definition; nothing runs until a `PipelineRun` actually submits it.
`runs/run-all-teams-pipeline-run.yaml` always carries all 15 team slots —
leave a team's `TEAMn_NAME` blank or `"skip"` in `config.env` to bypass it
(same `envsubst`-templated pattern as `pipeline/runs/run-all-teams.yaml`
on the OpenShift path):

```bash
envsubst < pipeline/portable/runs/run-all-teams-pipeline-run.yaml | kubectl create -f -
```
```
pipelinerun.tekton.dev/deploy-all-teams-cbdcn created
```

**4. Confirm it succeeded:**

```bash
kubectl get pipelinerun -n infra
```
```
NAME                   SUCCEEDED   REASON      STARTTIME   COMPLETIONTIME
deploy-all-teams-run   True        Succeeded   40s         0s
```

**5. Confirm both teams actually work:**

```bash
kubectl get kafka -n team-01   # Ready
kubectl get kafka -n team-02   # Ready
kubectl logs -n infra -l app=event-generator --tail=3   # confirm it's actually emitting
```
```
[EMIT] Produced 100 events → teams: [team01, team02]
```

Both teams' Kafka independently confirmed to receive real events (same
consume-from-topic check as `event-generator/portable/README.md`, repeated
for `team-02`).

**6. Idempotency** — re-run step 3 a second time with no changes:

```bash
kubectl get kafka kafka-team01 -n team-01 -o jsonpath='{.metadata.resourceVersion}'
# 42057
# ... re-run step 3 ...
kubectl get kafka kafka-team01 -n team-01 -o jsonpath='{.metadata.resourceVersion}'
# 42057 — unchanged
kubectl get pod -n team-01 -l strimzi.io/cluster=kafka-team01 -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}'
# 0 — broker never restarted
```

The event generator is idempotent too: a third re-run with no source
change skipped `build-event-generator-image` and `deploy-event-generator`
entirely (neither `TaskRun` was even created) — see
`event-generator/portable/README.md` for the mechanism.

**7. Unused team slots are skipped, not attempted-and-failed** —
confirmed for real: with `config.env` configured for 2 real teams and 13
slots left as `"skip"`, only 2 `deploy-kafka-teamN` pods were created; the
other 13 were cleanly skipped.

The full 15-team pipeline definition (19 Tasks, 46 params) was confirmed
structurally valid by Tekton's own admission webhook; live validation used
2 teams — each team block is mechanically identical to the two already
proven, and running all 15 Kafka brokers simultaneously wasn't necessary
to prove that.

## The orchestrator: one repeatable deployment path

Now its own top-level `portable/README.md` — the single index for the
whole platform's orchestration commands (`install-prerequisites.sh`,
`deploy-pipeline.sh`, the one-liner to trigger another run,
`status-platform.sh`, `teardown-pipeline.sh`,
`teardown-prerequisites.sh`), not just this component. See there for the
full command list and validated end-to-end evidence.

## Cleanup (prerequisites so far)

```bash
kubectl delete -f pipeline/portable/manifests/tekton-dashboard-ingress-template.yaml --ignore-not-found   # Dashboard Ingress
kubectl delete -f "https://github.com/tektoncd/dashboard/releases/download/v0.72.0/release.yaml" --ignore-not-found   # Dashboard itself
kubectl delete -f "https://github.com/tektoncd/pipeline/releases/download/v1.15.3/release.yaml" --ignore-not-found    # Tekton Pipelines
```
