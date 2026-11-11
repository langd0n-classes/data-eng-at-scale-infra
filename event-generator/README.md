# Event Generator

Synthetic data producer that generates realistic event streams and publishes them to Kafka topics for testing, demos, and learning.

For a `kubectl`-only deployment variant (Kind/k3s, no OpenShift ImageStream/BuildConfig required), see [portable/README.md](portable/README.md).

## What It Does

Produces synthetic health events with outbreak-aware scheduling, published to per-team
Kafka topics. Event type distribution shifts automatically during Mon/Thu outbreak windows.

Event types:
- **Symptom Reports** — patient symptoms, includes available bed count
- **Clinic Visits** — clinical encounters, includes available bed count
- **Hospital Admissions** — inpatient records with severity, O2 level, expected LOS
- **Vaccination** — vaccination events (placeholder, real fields coming soon)
- **Emergency Incidents** — emergency events (placeholder)
- **General Health Reports** — general reports (placeholder)

All events include: `source: "event-generator"` and `schema_version: "1.0"`

---

## Architecture

Two threads handle generation and emission separately:

**RefillThread** — wakes every 6 hours
- Checks Mon 09:00–Tue 21:00 UTC and Thu 09:00–Fri 21:00 UTC outbreak windows
- Sets state: `baseline` | `outbreak` | `winddown`
- Fills the event pool (up to EVENT_POOL_REFILL_THRESHOLD events per batch)
- Fires immediately on startup — no 6-hour wait for the first batch

**EmitThread** — runs continuously
- Drains pool to all team Kafka topics at once (every team gets the identical event)
- Pace: outbreak = 0.1–0.5s/event, winddown = 0.5–1.5s, baseline = 1.0–2.0s

**Bed Pressure Model**
- `symptom_burden` increments with each symptom_report/hospital_admission
- Decays exponentially each cycle (SYMPTOM_BURDEN_DECAY)
- `available_beds = BASE_BEDS − (symptom_burden × BED_PRESSURE_FACTOR)`
- Field injected into: symptom_report, clinic_visit, hospital_admission

**Outbreak event distribution** (approximate, intensity = 0.75):
| Phase | hospital_admission | emergency_incident | symptom_report | clinic_visit |
|-------|-------------------|-------------------|----------------|--------------|
| Baseline | 7% | 3% | 25% | 30% |
| Outbreak | 33% | 38% | 5% | 1% |
| Winddown | interpolated between outbreak and baseline |

---

## CLI Convention

All commands in this README use `kubectl`, which works on both plain Kubernetes and OpenShift (since `oc` is a superset of `kubectl`).

Commands that use OpenShift-specific resources — `ImageStream`, `BuildConfig`, `oc new-project`, `oc start-build` — are marked with **`# OpenShift only`** and must use `oc`.

## Prerequisites

1. **Kubernetes/OpenShift cluster** with access to create namespaces (or pre-provisioned by your platform/cloud provider) and deployments
2. **kubectl** (or `oc` on OpenShift) installed and configured
3. **Kafka cluster(s)** deployed and accessible — two supported modes:
   - **Per-Team Isolation** (`kafka/per-team/`) - Separate Kafka instance per namespace
   - **Shared Cluster** (`kafka/shared-deployment/`) - Single Kafka cluster with topic-based multitenancy
4. **infra namespace** created (if not already provisioned):
   ```bash
   # Kubernetes
   kubectl create namespace ${INFRA_NAMESPACE}

   # OpenShift only
   oc new-project ${INFRA_NAMESPACE}
   ```

5. **Configuration file** set up:
   ```bash
   # From project root
   cp config.env.example config.env
   vi config.env  # Edit with your Kafka bootstrap servers and settings
   ```

---

## Kafka Bootstrap Server Reference

When configuring `TEAM_BOOTSTRAP_SERVERS`, use the appropriate address based on your deployment mode:

**Per-team Kafka** (Kafka runs in each team's namespace; event generator connects cross-namespace from `infra`):
```
kafka-${TEAM_NAME}.${TEAM_NAMESPACE}.svc.cluster.local:9092
```

**Shared Kafka cluster** (Kafka runs in the same `${INFRA_NAMESPACE}` namespace as the event generator):
```
kafka.${INFRA_NAMESPACE}.svc.cluster.local:9092
```

---

## Initial Deployment (First Time)

Run these commands **once** to build and deploy the event generator.

```bash
# 1. Load configuration
source config.env

# 2. Create ImageStream (tracks container images) — OpenShift only
envsubst < event-generator/k8s/01-imagestream.yaml | oc apply -f -

# 3. Create BuildConfig (defines build process) — OpenShift only
envsubst < event-generator/k8s/02-buildconfig.yaml | oc apply -f -

# 3a. Verify BuildConfig was created successfully — OpenShift only
oc get bc ${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}
# Expected output: NAME   TYPE   FROM   LATEST
# If not found, check for errors: oc describe bc ${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}

# 4. Build the container image — OpenShift only
# BuildConfig auto-triggers a build on creation (ConfigChange trigger) — no manual start needed
# Uncomment below only if you need to trigger a rebuild manually later:
# oc start-build ${EVENT_GENERATOR_NAME} --follow -n ${INFRA_NAMESPACE}

# 5. Deploy ConfigMap (Kafka settings, event rate, etc.)
envsubst < event-generator/k8s/03-configmap.yaml | kubectl apply -f -

# 6. Deploy the application
envsubst < event-generator/k8s/04-deployment.yaml | kubectl apply -f -
```

**Note for plain Kubernetes users**: Steps 2–4 use OpenShift's built-in image build system. On plain Kubernetes, build and push the image manually with Docker/Podman first. See [Building with Docker/Podman](#building-with-dockerpodman).

---

## View Build Progress (Without Rebuilding)

To check the status of an existing build **without starting a new one** — OpenShift only:

```bash
# View build logs from the latest build
oc logs -f build/${EVENT_GENERATOR_NAME}-1 -n ${INFRA_NAMESPACE}

# List all builds
oc get builds -n ${INFRA_NAMESPACE}

# View specific build logs (replace '2' with your build number)
oc logs -f build/${EVENT_GENERATOR_NAME}-2 -n ${INFRA_NAMESPACE}
```

---

## Verify Deployment

```bash
# Check pod status
kubectl get pods -n ${INFRA_NAMESPACE} -l app=${EVENT_GENERATOR_NAME}

# View application logs (should show "Produced N events to M teams")
kubectl logs -f deployment/${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}
```

**Expected log output:**
```
INFO - Connected to Kafka for team00-test: kafka-team00-test...svc.cluster.local:9092
INFO - Connected to 1/1 team Kafka instances
INFO - RefillThread started (generates batch every 6h, checks outbreak schedule)
INFO - [REFILL] Outbreak state updated: active=False, profile=baseline, intensity=0.00
INFO - [REFILL] Generating 40000 events...
INFO - [REFILL] Pool now has 40000/50000 events
INFO - EmitThread started (continuous emission at variable pace)
INFO - [EMIT] Produced 100 events to 1 teams
```

---

## Redeployment (After Configuration Changes)

When you update settings in `config.env` or need to redeploy:

```bash
# 1. Load updated configuration
source config.env

# 2. Update ConfigMap
envsubst < event-generator/k8s/03-configmap.yaml | kubectl apply -f -

# 3. Update Deployment (only if you changed 04-deployment.yaml)
# envsubst < event-generator/k8s/04-deployment.yaml | kubectl apply -f -

# 4. Restart to pick up ConfigMap changes
# (always needed if you changed config.env — skip if you only changed deployment.yaml)
kubectl rollout restart deployment/${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}
```

**Quick one-liner (config.env changes only):**
```bash
source config.env && \
envsubst < event-generator/k8s/03-configmap.yaml | kubectl apply -f - && \
kubectl rollout restart deployment/${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}
```

---

## Verify Events in Kafka

### Check events are being produced:

```bash
# Connect to Kafka pod and consume events
# Topic name pattern: ${TOPIC_PREFIX}${TEAM_NAME}${TOPIC_SUFFIX} (e.g. events.team01.raw)
kubectl exec -it kafka-${TEAM_NAME}-0 -n ${TEAM_NAMESPACE} -- kafka-console-consumer \
  --bootstrap-server localhost:9092 \
  --topic ${TOPIC_PREFIX}${TEAM_NAME}${TOPIC_SUFFIX} \
  --from-beginning \
  --max-messages 10
```

You should see JSON events like:
```json
{"event_type":"symptom_report","timestamp":"2024-01-15T10:30:00Z","patient_id":"P12345","source":"event-generator","schema_version":"1.0",...}
{"event_type":"clinic_visit","timestamp":"2024-01-15T10:30:15Z","visit_id":"V98765","source":"event-generator","schema_version":"1.0",...}
{"event_type":"hospital_admission","timestamp":"2024-01-15T10:30:30Z","admission_id":"HA234567","source":"event-generator","schema_version":"1.0",...}
```

---

## Troubleshooting

### No events produced

```bash
# Check pod logs for errors
kubectl logs deployment/${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}

# Common issues:
# - Kafka bootstrap servers incorrect in config.env
# - Kafka pods not running
# - Network connectivity issues
```

### Pod not starting

```bash
# Check pod status and events
kubectl describe pod -l app=${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}

# Check if ConfigMap loaded correctly
kubectl get configmap ${EVENT_GENERATOR_NAME}-config -n ${INFRA_NAMESPACE} -o yaml
```

### Connection errors to Kafka

```bash
# Verify Kafka service exists
kubectl get svc -n ${TEAM_NAMESPACE}

# Test DNS resolution
kubectl run dns-test --rm -it --image=busybox -n ${INFRA_NAMESPACE} \
  -- nslookup kafka-${TEAM_NAME}.${TEAM_NAMESPACE}.svc.cluster.local

# Test TCP connectivity from event-generator pod
kubectl exec deployment/${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE} -- \
  nc -zv kafka-${TEAM_NAME}.${TEAM_NAMESPACE}.svc.cluster.local 9092
```

### Topics not created automatically

```bash
# Exec into Kafka pod and inspect
kubectl exec -it kafka-${TEAM_NAME}-0 -n ${TEAM_NAMESPACE} -- bash

# List existing topics
kafka-topics --bootstrap-server localhost:9092 --list

# Create topic manually if needed
kafka-topics --bootstrap-server localhost:9092 \
  --create --topic ${TOPIC_PREFIX}${TEAM_NAME}${TOPIC_SUFFIX} --partitions 1 --replication-factor 1
```

### Events to some teams but not others

```bash
# Check TEAM_BOOTSTRAP_SERVERS in ConfigMap
kubectl get configmap ${EVENT_GENERATOR_NAME}-config -n ${INFRA_NAMESPACE} -o yaml | grep TEAM_BOOTSTRAP_SERVERS

# Verify all team Kafka pods are running
kubectl get pods -A | grep kafka-team
```

---

## Generated Event Types

### Symptom Report
```json
{
  "event_type": "symptom_report",
  "timestamp": "2024-01-15T10:30:00.000Z",
  "patient_id": "P12345",
  "age": 35,
  "region": "Boston",
  "symptoms": ["toasty_ears", "wobbly_knees", "sleepy_socks"],
  "severity": "moderate",
  "duration_days": 3,
  "reported_via": "mobile_app",
  "available_beds": 342,
  "source": "event-generator",
  "schema_version": "1.0"
}
```

### Clinic Visit
```json
{
  "event_type": "clinic_visit",
  "timestamp": "2024-01-15T10:30:00.000Z",
  "visit_id": "V123456",
  "patient_id": "P12345",
  "clinic_id": "C15",
  "region": "Boston",
  "visit_type": "emergency",
  "primary_complaint": "shortness_of_breath",
  "toastiness_level": 101.2,
  "diagnosis_code": "ZDX456",
  "prescribed_medication": true,
  "follow_up_required": false,
  "available_beds": 342,
  "source": "event-generator",
  "schema_version": "1.0"
}
```

### Hospital Admission
```json
{
  "event_type": "hospital_admission",
  "timestamp": "2024-01-15T10:30:00.000Z",
  "admission_id": "HA234567",
  "patient_id": "P12345",
  "hospital_id": "H3",
  "region": "Boston",
  "admission_reason": "toasty_ears",
  "severity": "severe",
  "toastiness_level": 103.2,
  "fizz_level": 91.5,
  "expected_los_days": 4,
  "available_beds": 342,
  "source": "event-generator",
  "schema_version": "1.0"
}
```

### Placeholder Event Types

`vaccination`, `emergency_incident`, and `general_health_report` are generated with minimal fields
(`event_type`, `timestamp`, `region`, `data: "placeholder"`). Full schemas will be added in a future update.

---

## Configuration Options

All configuration is done via `config.env`. Key settings:

| Variable | Description | Default |
|----------|-------------|---------|
| `INFRA_NAMESPACE` | Namespace for event generator | `infra` |
| `EVENT_RATE_PER_SEC` | Events per second (total across all teams) | `10` |
| `TEAM_BOOTSTRAP_SERVERS` | Multi-team Kafka mappings | — |
| `KAFKA_BOOTSTRAP_SERVERS` | Single-cluster Kafka bootstrap (Mode 2) | — |
| `TOPIC_PREFIX` | Topic name prefix | `events.` |
| `TOPIC_SUFFIX` | Topic name suffix | `.raw` |
| `REGIONS` | Geographic regions for events | `Boston,Cambridge,...` |
| `EVENT_POOL_SIZE` | Max events in pool | `50000` |
| `EVENT_POOL_REFILL_THRESHOLD` | Refill when pool drops below this | `40000` |
| `BASE_BEDS` | Hospital bed capacity for pressure model | `500` |
| `BED_PRESSURE_FACTOR` | Symptom burden effect on available beds | `0.8` |
| `SYMPTOM_BURDEN_DECAY` | Exponential decay rate for symptom burden | `0.995` |
| `BAD_EVENT_RATE` | Share of sent events that break the event contract (bad JSON, missing field, wrong type, unknown `event_type`, out-of-range age, bad timestamp); `0` turns it off. To change it on a running cluster, add the key to the generator ConfigMap and restart the Deployment | `0.0175` |

| `LABEL_DELAY_HOURS` | Hours after a UTC hour ends before its labels are released to `events.<team>.outbreak_status`. Not in the ConfigMap template; add the key by hand and restart the Deployment | `12` |
| `LABEL_STATE_TEAM` | Team whose Kafka holds the staff-only state topic (must be a key of `TEAM_BOOTSTRAP_SERVERS`) | `team00` |
| `LABEL_STATE_TOPIC` | Name of the staff-only state topic | `staff.outbreak_state` |

**Topic naming pattern:**
- Multi-team: `${TOPIC_PREFIX}${TEAM_ID}${TOPIC_SUFFIX}` → `events.team01.raw`
- Single cluster: `${TOPIC_PREFIX}${TOPIC_SUFFIX}` → `events.raw`

---

## Outbreak Labels and Labeled History

Students train a model that decides LOW, WATCH, or ALERT per region per hour. They get
the ground truth late, on `events.<team>.outbreak_status`, and labeled history to train
a first model on `events.<team>.history`.

### Delayed labels (live)

One JSON message per region per completed UTC hour, key = region:

```json
{"region": "Boston", "hour": "2026-11-09T14:00:00Z", "status": "ALERT", "released_at": "2026-11-10T03:00:04Z"}
```

`hour` is the start of the hour. Labels for an hour are released `LABEL_DELAY_HOURS` after
the hour ends. Labels never carry bad events (`BAD_EVENT_RATE` does not apply to them).

**Rule** (`label_status()` in `event_generator.py`): ALERT if the generator's profile for
the hour was `outbreak` and the region was in `affected_regions`; WATCH if the profile was
`winddown` and the region was in `affected_regions`; LOW otherwise.

**Label = state of the events being emitted.** The generator's state changes only when the
refill thread runs (every 6 hours, not at the schedule boundary), and the refill tops a FIFO
pool up to `EVENT_POOL_REFILL_THRESHOLD` events that the emit thread drains at the profile's
pace. The events on the wire can therefore be several hours older than the current state. Each
pooled event carries, internally, the state it was generated under (profile, affected
regions); the emitted JSON is unchanged. The emit thread records the state of the event it
last took from the pool, and the label uses that.

**Mid-hour rule.** For an hour, the state is the emitted state at the half hour (HH:30): the
first emitted state the label thread sees at or after HH:30 is recorded and kept for the hour.

The history backfill follows the same rule (label = state of the events being written), because it
emits each event as it generates it. One difference remains: live `available_beds` is computed from
the symptom burden when the batch is generated (frozen per refill batch), while the history computes
it per event on the simulated clock. Live event content is not changed.

**Restart safety.** The label thread writes each hour's state (`hour`, `profile`,
`affected_regions`) and each release (`team`, `hour`) to `staff.outbreak_state` in the Kafka of
`LABEL_STATE_TEAM` (`team00`). Students cannot read it, because each team only has access to its
own Kafka. At startup the generator replays that topic: pending hours stay pending, released hours
are not released again, and an outbreak that was running keeps its `affected_regions`. Nothing is
released until the replay succeeds. If the pod dies between sending labels and writing the
release marker, that hour is sent again. Consumers must upsert on `(region, hour)`. An hour in
which the generator was down for the whole second half has no label. Run one replica.

A restart empties the pool, so after a restart the emitted state is the new first refill's state.

If `LABEL_STATE_TEAM` is not in `TEAM_BOOTSTRAP_SERVERS`, state is kept in memory only and a
restart loses pending labels; the log says so. If the replay fails at startup, the log warns
("The state topic could not be replayed") when the first refill samples affected regions anew; a
restart in the middle of an outbreak can then give labels and events that disagree. If the state
team's Kafka never accepts writes, at most 2000 unsaved states and 2000 unsaved release markers are
kept for retry; older ones are dropped with a warning, and a restart then loses them.

**Staff topic config.** At startup the generator tries to create `staff.outbreak_state` with
`cleanup.policy=compact`, 1 partition, replication factor 1, through the Kafka admin client (no
CLI in the broker). It never changes an existing topic. If the topic already exists, or the
create is refused, staff must set the config. With Strimzi:

```yaml
apiVersion: kafka.strimzi.io/v1beta2
kind: KafkaTopic
metadata:
  name: staff.outbreak-state       # a resource name cannot contain "_" or "."; set spec.topicName
  namespace: team-00               # the namespace of team00's Kafka
  labels:
    strimzi.io/cluster: kafka-team00   # adjust to team00's Kafka cluster name
spec:
  topicName: staff.outbreak_state
  partitions: 1
  replicas: 1
  config:
    cleanup.policy: compact
```

Without compaction the broker default (`cleanup.policy=delete`, 7 days) applies. That is safe while
`LABEL_DELAY_HOURS` plus a few hours stays well below the retention: older records are only
history. If retention is shorter than the delay, a restart loses the states of hours that are still
pending, and those hours get no label. Without compaction and with infinite retention the topic grows by
about 24 + 24 x teams records a day, which is small.

### Retention of the history and label topics

The team brokers default to `log.retention.hours=24` and `log.retention.bytes=104857600` (100 MB per
partition) and auto-create topics. The raw topic is set to `retention.ms=-1`, `retention.bytes=-1` by the Kafka
deploy task. `events.<team>.history` (about 300 MB) and `events.<team>.outbreak_status` get the same
unlimited retention, so students can re-read both for the rest of the term:

- `backfill_history.py` creates each topic it writes (1 partition, `retention.ms=-1`, `retention.bytes=-1`) or,
  if it exists, sets both to `-1`, before it writes anything, through the Kafka admin client (no CLI in a
  broker). If that fails it writes nothing and exits 1. `--no-topic-setup` skips the step; then set the two
  configs yourself.
- The live generator does the same for `events.<team>.outbreak_status` before its first label send to each team.
  If it fails, the labels for that team are held and retried on the next tick (15 s) with a warning in the log;
  event streaming is not affected. If it keeps failing, set the two configs by hand.
- For an existing topic, the admin `alter_configs` call sets the topic's overrides to exactly these two. Other
  overrides on these tool-owned topics are not expected.
- Every create and alter response is checked for error codes (kafka-python 2.0.2 does not raise them for
  `alter_configs`), and the topic's config is read back; the step counts as done only if `retention.ms` and
  `retention.bytes` both read `-1`. The whole call is limited to 60 seconds, because the admin client can wait
  for good on an unreachable broker; a timeout counts as a failure. The same limit applies to the creation of
  the compacted state topic at startup, so a broker that does not answer cannot hold up the generator.
- Topics that students create (for example their decisions topic) keep the broker default: 24 hours and
  100 MB per partition.

### Labeled history (staff, on demand, one team)

`backfill_history.py` is in the same image. It simulates the generator over a past UTC range
with a simulated clock and a fixed seed, and writes to one team's Kafka:

- `events.<team>.history`: the events (same schema, same pacing and distributions as live, no bad events)
- `events.<team>.outbreak_status`: the label of every region-hour in the range, `released_at` = time of the run

Defaults: the 14 days ending `2026-11-09T00:00:00Z` (about 980,000 events at 1.5 events/s), seed 551.
The simulation does not depend on the team, so the same seed and range give every team identical
events and labels. It does not read or write `staff.outbreak_state` or `events.<team>.raw`.
`REGIONS`, `EVENT_RATE_PER_SEC`, and the bed-pressure settings come from the same environment as the
generator, so run it with the generator's ConfigMap. The simulation applies the schedule (Mon/Thu 09:00)
exactly at the hour; it does not copy the 6-hourly lag of the live state. Running it twice writes the
data twice. It sends in batches (flush every 5000 events) and prints progress per day.

As a one-off Job in the cluster, using the image of the running Deployment (`TEAM` is one team id;
on OpenShift remove the `imagePullSecrets` lines):

```bash
source config.env
TEAM=team01
IMAGE=$(kubectl get deployment ${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE} \
  -o jsonpath='{.spec.template.spec.containers[0].image}')
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: history-${TEAM}
  namespace: ${INFRA_NAMESPACE}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 86400
  template:
    spec:
      restartPolicy: Never
      imagePullSecrets:
        - name: ghcr-pull-secret
      containers:
        - name: history
          image: ${IMAGE}
          command: ["python", "backfill_history.py", "--team", "${TEAM}"]
          envFrom:
            - configMapRef:
                name: ${EVENT_GENERATOR_NAME}-config
          resources:
            requests: {memory: 256Mi, cpu: 200m}
            limits: {memory: 1Gi, cpu: 1}
EOF
kubectl logs -f job/history-${TEAM} -n ${INFRA_NAMESPACE}
```

Options: `--start`, `--end` (UTC, hour boundaries, end exclusive), `--days`, `--seed`, `--batch-size`,
`--bootstrap`, and `--dry-run` (simulate and print counts, write nothing). The topics are created by the
first write if the broker allows auto-creation; otherwise create them first (see "Topics not created
automatically").

### Reconstructing the gap from the generator log (staff, one-off repair)

For hours the live generator emitted before this feature existed, no state was recorded. The state
of the emitted events can be rebuilt from the pod log, because the pool is FIFO. `reconstruct_state.py`
(standard library only; it runs on a host or in the image) reads:

- `[REFILL] Outbreak state updated: profile=..., affected_regions=[...]` and `[REFILL] Generating N events...`:
  one batch of N events under that state, in log order (the first batch is the pool filled at startup);
- `[EMIT] Produced N events`: the cumulative count emitted since the pod started, against time.

It lays the batches end to end, interpolates the emitted count at each HH:30, and takes the state of
the batch that holds that event. Output is a state list for `--labels-from`:

```bash
kubectl logs --timestamps deploy/${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE} > pod.log
python3 reconstruct_state.py pod.log --start 2026-11-10T02:00:00Z --end 2026-11-11T14:00:00Z --out states.json
python3 backfill_history.py --team team01 --labels-from states.json \
  --start 2026-11-10T02:00:00Z --end 2026-11-11T14:00:00Z --dry-run
```

Limits: the log covers one pod from its start (a log that spans a restart is rejected), so events before the
pod started (2026-11-10T01:37Z for the current pod) are not covered. Hours before the first `[REFILL]` line
or after the last `[EMIT]` line are skipped and listed. The count at HH:30 is interpolated between `[EMIT]`
lines about 100 events apart, so a state change within about a minute or two of HH:30 can fall on the other
side. Without `--timestamps` the log's own timestamps are read as UTC. Check the printed rows against the
`[REFILL]` times before writing.

### Labels from a supplied state list (staff, one-off repair)

To write labels for past hours from a known state history (the file `reconstruct_state.py` writes, or one
made by hand), give it as a file and run:

```bash
python backfill_history.py --team team01 --labels-from states.json \
  --start 2026-11-10T02:00:00Z --end 2026-11-11T14:00:00Z
```

This writes labels only (no events) to `events.<team>.outbreak_status`, with `released_at` = time of the run.
`--start` and `--end` are required. `states.json`:

```json
[
  {"from_time": "2026-11-10T01:37:00Z", "profile": "winddown", "affected_regions": ["Boston", "NYC"]},
  {"from_time": "2026-11-11T01:37:00Z", "profile": "baseline", "affected_regions": []}
]
```

CSV works too: header `from_time,profile,affected_regions`, regions separated by `;`. A row holds from its
`from_time` until the next row. Each hour takes the state in force at its half hour, as in the live rule.
Hours before the first row are skipped and reported. Regions must be in `REGIONS`. Use `--dry-run` first.
Run it as the Job above with `command: ["python", "backfill_history.py", "--team", "team01", "--labels-from", ...]`
and the file in a ConfigMap mounted into the pod.

---

## Advanced Usage

### Health Check Endpoints

```bash
# Port forward to access health endpoints
kubectl port-forward deployment/${EVENT_GENERATOR_NAME} 8000:8000 -n ${INFRA_NAMESPACE}

# Check health status
curl http://localhost:8000/health

# Check readiness (shows team count and rate)
curl http://localhost:8000/ready
```

Expected response:
```json
{"status":"ready","teams":2,"rate":10.0}
```

### Scale for Higher Throughput

```bash
# Run multiple replicas
kubectl scale deployment ${EVENT_GENERATOR_NAME} --replicas=3 -n ${INFRA_NAMESPACE}
```

### Change Event Rate

Edit `config.env`:
```bash
EVENT_RATE_PER_SEC="100"  # 100 events/second
```

Then redeploy:
```bash
source config.env && \
envsubst < event-generator/k8s/03-configmap.yaml | kubectl apply -f - && \
kubectl rollout restart deployment/${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}
```

### Connecting to SSL-Enabled Kafka (Shared Cluster)

If using the shared Kafka cluster with SSL/TLS enabled, update `config.env` to point to the SSL listener and ensure the appropriate certificates are available in the cluster:

```bash
# SSL bootstrap server (shared cluster)
KAFKA_BOOTSTRAP_SERVERS="kafka.${INFRA_NAMESPACE}.svc.cluster.local:9093"
```

Refer to `kafka/shared-deployment/` for certificate generation and SSL setup.

---

## Customization

### Adding New Event Types

The new architecture selects event types via outbreak-aware weight functions. To add a new type:

**Step 1: Add a generator method to EventGenerator class:**

```python
def generate_custom_event(self) -> dict:
    return {
        'event_type': 'custom_event',
        'timestamp': datetime.utcnow().isoformat(),
        'region': random.choice(REGIONS),
        # add your fields here
    }
```

**Step 2: Register it in `generate_event()`:**

```python
elif event_type == 'custom_event':
    return self.generate_custom_event()
```

**Step 3: Add it to the weight functions** (`baseline_event_weights`, `outbreak_event_weights`,
`winddown_event_weights`) with appropriate weights. Make sure weights still sum to 1.0
after normalization.

**Step 4: Rebuild and redeploy** (same as any code change).

---

## Performance Tuning

### For High Event Rates

**1. Scale Replicas**

Run multiple generator pods in parallel:

```bash
kubectl scale deployment ${EVENT_GENERATOR_NAME} --replicas=3 -n ${INFRA_NAMESPACE}
```

**2. Increase Event Rate**

Edit `config.env`:
```bash
EVENT_RATE_PER_SEC="100"  # 100 events/second total or per team
```

**3. Adjust Resources**

Edit `k8s/04-deployment.yaml` to allocate more CPU/memory:

```yaml
resources:
  requests:
    memory: "256Mi"
    cpu: "250m"
  limits:
    memory: "512Mi"
    cpu: "500m"
```

**4. Tune the Event Pool**

The new architecture uses a pre-generated event pool. For high-throughput scenarios, increase:

```bash
# in config.env (all optional — defaults shown):
# export EVENT_POOL_SIZE="50000"           # increase if pool drains faster than RefillThread fills it
# export EVENT_POOL_REFILL_THRESHOLD="40000"  # lower to trigger refills more aggressively
```

**5. Monitoring**

For observing producer throughput and Kafka broker health, consider adding a Prometheus JMX exporter sidecar to the Kafka StatefulSet (see `kafka/shared-deployment/` for configuration options).

### Resource Recommendations

| Event Rate | Replicas | CPU Request | Memory Request |
|------------|----------|-------------|----------------|
| < 50/sec   | 1        | 100m        | 128Mi          |
| 50-200/sec | 1-2      | 250m        | 256Mi          |
| 200-500/sec| 2-3      | 500m        | 512Mi          |
| > 500/sec  | 3-5      | 1000m       | 1Gi            |

---

## Building with Docker/Podman

If using plain Kubernetes (not OpenShift), build and push the image manually:

```bash
cd event-generator

# Build
podman build -t your-registry.io/event-generator:latest .

# Push to registry
podman push your-registry.io/event-generator:latest

# Update k8s/04-deployment.yaml with your image
# Then deploy ConfigMap and Deployment
kubectl apply -f k8s/03-configmap.yaml -n ${INFRA_NAMESPACE}
kubectl apply -f k8s/04-deployment.yaml -n ${INFRA_NAMESPACE}
```

---

## Use Cases

- **Development** - Test Kafka consumers without real data sources
- **Demos** - Showcase real-time streaming pipelines
- **Load Testing** - Stress test Kafka clusters and downstream systems
- **Training** - Provide consistent data for learning exercises
- **CI/CD** - Generate test data for automated pipeline testing

---

## Diagnostics

`check_events.py` verifies all configured team topics are receiving events.
Run it from your local machine — it pipes into the pod via stdin:

```bash
source config.env
oc exec $(oc get pod -n ${INFRA_NAMESPACE} -l app=${EVENT_GENERATOR_NAME} \
   -o jsonpath='{.items[0].metadata.name}') \
   -n ${INFRA_NAMESPACE} -- python3 - < event-generator/src/check_events.py
```

Output shows per-team message counts, empty topics, and connection failures.
TEAM_BOOTSTRAP_SERVERS is read from the pod's ConfigMap environment automatically.

---

## Cleanup

```bash
# Delete event generator (OpenShift — single command)
oc delete deployment/${EVENT_GENERATOR_NAME} configmap/${EVENT_GENERATOR_NAME}-config imagestream/${EVENT_GENERATOR_NAME} buildconfig/${EVENT_GENERATOR_NAME} -n ${INFRA_NAMESPACE}

# OR with kubectl (if using Docker/Podman build — single command)
kubectl delete deployment/${EVENT_GENERATOR_NAME} configmap/${EVENT_GENERATOR_NAME}-config -n ${INFRA_NAMESPACE}
```

---

## Directory Structure

```
event-generator/
├── src/                      # Python source code
│   ├── event_generator.py    # Main application (outbreak scheduler + Kafka producer)
│   ├── backfill_history.py   # Staff command: labeled history and label repair for one team
│   ├── reconstruct_state.py  # Staff tool: emitted-state timeline from a pod log
│   ├── check_events.py       # Diagnostic script (run via oc exec)
│   └── requirements.txt      # Python dependencies
├── k8s/                      # Kubernetes manifests
│   ├── 01-imagestream.yaml   # Image tracking (OpenShift)
│   ├── 02-buildconfig.yaml   # Build configuration (OpenShift)
│   ├── 03-configmap.yaml     # Runtime configuration template
│   └── 04-deployment.yaml    # Deployment + Service
├── Dockerfile                # Container image definition
└── README.md
```

---

## Further Reading

### Apache Kafka
- [Kafka Documentation](https://kafka.apache.org/documentation/)
- [Kafka Producer Configuration](https://kafka.apache.org/documentation/#producerconfigs)
- [Kafka Performance Tuning](https://kafka.apache.org/documentation/#maximizingefficiency)
- [KRaft Mode (no ZooKeeper)](https://kafka.apache.org/documentation/#kraft)

### Kafka Python Client
- [kafka-python Documentation](https://kafka-python.readthedocs.io/)
- [kafka-python Producer API](https://kafka-python.readthedocs.io/en/master/apidoc/KafkaProducer.html)

### Kubernetes/OpenShift
- [Kubernetes Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)
- [ConfigMaps](https://kubernetes.io/docs/concepts/configuration/configmap/)
- [OpenShift Builds and BuildConfigs](https://docs.openshift.com/container-platform/latest/cicd/builds/understanding-buildconfigs.html)
- [Kubernetes StatefulSets](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/)

### Project Resources
- [Project Root README](../README.md) - Main project documentation
- [Kafka Deployment Guide](../kafka/README.md) - Deploy Kafka clusters (per-team and shared)
- [Configuration Reference](../config.env.example) - All available settings
