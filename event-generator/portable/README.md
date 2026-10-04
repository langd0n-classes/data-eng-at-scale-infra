# Portable Event Generator (Kind / k3s)

A `kubectl`-only variant of the event generator deployment, for a no-cost
local Kubernetes cluster — no OpenShift ImageStream/BuildConfig required.

This does **not** replace the OpenShift path. `event-generator/k8s/` is
unchanged and remains the primary deployment method for the classroom.

**Out of scope here:** the event generator's own source code
(`event-generator/src/*.py`) — nothing there needed to change at all. Both
`event_generator.py` and `check_events.py` are already fully portable
(env-var driven Kafka connectivity, a plain Flask/waitress health server,
no OpenShift API calls anywhere in the application logic).

## Compatibility

Plain `kubectl`, a pre-built image pulled from an in-cluster registry
(`registry/portable/`) instead of an `ImageStream`/`BuildConfig` pair. Runs
on any conformant Kubernetes cluster: Kind, k3s, EKS, GKE, AKS, a
bare-metal cluster, etc.

## Prerequisites

- **Team namespaces onboarded** — `onboarding/portable/README.md` (needed
  because this Deployment targets the `infra` namespace, which onboarding
  creates)
- **Kafka deployed for each team** — `kafka/portable/README.md` (needed
  for `TEAM_BOOTSTRAP_SERVERS` below — there's nothing to produce events
  to otherwise)
- **An image already pushed to the registry** — see "Prerequisite: an
  image in the registry" below for the two ways to get there
- **`config.env` created and filled in** — see below

## Before you start: update config.env

```bash
# one-time: create your own config.env from the template
cp config.env.example config.env
```
Then edit `config.env` and set: `INFRA_NAMESPACE`, `EVENT_GENERATOR_NAME`,
`EVENT_RATE_PER_SEC`, `TOPIC_PREFIX`, `TOPIC_SUFFIX`, and `REGIONS` — plus
these, which need a specific value, not just any value:

| Variable | Notes |
|---|---|
| `EVENT_GENERATOR_IMAGE` | Must already exist in the registry under this exact reference — either pushed by the full pipeline (`pipeline/portable/`), or by `build-and-push.sh` below |
| `TEAM_BOOTSTRAP_SERVERS` | One `team_id=bootstrap_server` entry per team you've deployed Kafka for — see `kafka/portable/README.md` |
| `TOPIC`, `KAFKA_BOOTSTRAP_SERVERS` | Leave commented out/empty — single-cluster mode only, not the multi-team mode used throughout this validation |

## Prerequisite: an image in the registry

This Deployment only ever *runs* an already-built image — it never builds
one itself. `EVENT_GENERATOR_IMAGE` must already be pushed to the registry
(`registry/portable/`) before step 1 below will work, either via the full
pipeline (`pipeline/portable/`), or standalone:

```bash
source config.env
bash event-generator/portable/build-and-push.sh git                    # pick one: from the pushed git branch
# OR
bash event-generator/portable/build-and-push.sh local event-generator  # pick one: from your local checkout
```
```
INFO Pushing image to localhost:30500/event-generator:latest
INFO Pushed localhost:30500/event-generator@sha256:1a3c5d60e157070bee46b7b9f07413b6834bc2379f49e643f3a5d8b21dc04e44
```

Runs kaniko as a plain Pod — no Docker daemon needed, works the same on a
real cluster as on Kind. Both modes build from a small, short-lived 1Gi PVC
(`git`: shallow+sparse clone; `local`: `kubectl cp`), never the whole repo,
and the script deletes it when done.

**Idempotent:** the script hashes only `src/` + `Dockerfile` (not docs) and
compares it to a `source-hash` annotation already on the Deployment.
Unchanged — it skips the build entirely. Changed — it builds, pushes,
stamps the new hash, and runs `kubectl rollout restart` so the change
actually goes live (plain `kubectl apply` alone won't — the image tag never
changes, so nothing tells Kubernetes to restart the pod). This needs
`imagePullPolicy: Always` on the Deployment (already set).

The real pipeline (`pipeline/portable/`) does the identical check via
`tasks/check-source-changed-task.yaml`, so a pipeline run that didn't touch
the event generator skips rebuilding/redeploying it too.

## Validation walkthrough

Validated on: Kubernetes (Kind) v1.34.11, using the image built and pushed
in `pipeline/portable/` (`localhost:30500/event-generator:test2`) and Kafka
for `team01` deployed via `kafka/portable/`.

**1. Deploy the ConfigMap and Deployment**

The `export` lines below are a standalone, copy-pasteable version of the
same values you just set in `config.env` — for a real deploy, run
`source config.env` instead of retyping them:

```bash
export INFRA_NAMESPACE=infra
export EVENT_GENERATOR_NAME=event-generator
export EVENT_RATE_PER_SEC=5
export TOPIC_PREFIX="events." TOPIC_SUFFIX=".raw" TOPIC=""
export REGIONS="Boston,Worcester"
export TEAM_BOOTSTRAP_SERVERS="team01=kafka-team01-kafka-bootstrap.team-01.svc.cluster.local:9092"
export KAFKA_BOOTSTRAP_SERVERS=""
export EVENT_GENERATOR_IMAGE="localhost:30500/event-generator:test2"

envsubst < event-generator/portable/k8s/configmap.yaml | kubectl apply -f -   # team bootstrap servers, topic names, etc.
envsubst < event-generator/portable/k8s/deployment.yaml | kubectl apply -f -  # the actual Deployment + Service
```
```
configmap/event-generator-config created
deployment.apps/event-generator created
service/event-generator created
```

**2. Confirm it connects and starts emitting**

```bash
kubectl logs -n infra -l app=event-generator --tail=5
```
```
Connected to Kafka for team01: kafka-team01-kafka-bootstrap.team-01.svc.cluster.local:9092
Connected to 1/1 team Kafka instances
Successfully connected teams: team01
Starting emission to 1 teams
Serving on http://0.0.0.0:8000
```

**3. Confirm the team's Kafka actually receives real events**

```bash
# a disposable pod with Kafka's own CLI tools built in
kubectl run kafka-consumer-test -n team-01 --image=confluentinc/cp-kafka:7.5.0 --restart=Never --command -- sleep 300
# read directly from the team's own topic
kubectl exec kafka-consumer-test -n team-01 -- kafka-console-consumer \
  --bootstrap-server kafka-team01-kafka-bootstrap.team-01.svc.cluster.local:9092 \
  --topic events.team01.raw --from-beginning --max-messages 3 --timeout-ms 20000
```
```
{"event_type": "hospital_admission", ..., "region": "Boston", ..., "source": "event-generator", "schema_version": "1.0"}
{"event_type": "symptom_report", ..., "region": "Worcester", ..., "source": "event-generator", "schema_version": "1.0"}
{"event_type": "hospital_admission", ..., "region": "Boston", ..., "source": "event-generator", "schema_version": "1.0"}
Processed a total of 3 messages
```

```bash
kubectl delete pod kafka-consumer-test -n team-01 --ignore-not-found
```

## Cleanup

```bash
source config.env
envsubst < event-generator/portable/k8s/deployment.yaml | kubectl delete -f - --ignore-not-found
envsubst < event-generator/portable/k8s/configmap.yaml | kubectl delete -f - --ignore-not-found
```
