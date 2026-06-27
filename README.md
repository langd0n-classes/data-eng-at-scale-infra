# Multi-Tenant Kafka + NiFi Infrastructure

Infrastructure-as-code for classroom-scale, per-team data engineering environments on OpenShift.
Each team gets an isolated Kafka broker and NiFi instance. A shared event generator produces
a synthetic mixed event stream that each team splits, routes, and processes.

## Architecture

```
                     ┌─────────────────────────────┐
                     │   infra namespace            │
                     │   Event Generator            │
                     │   (synthetic mixed stream)   │
                     └────────────┬────────────────┘
                                  │  publishes to each team's raw topic
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
     ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
     │  team-01    │    │  team-02    │    │  team-N     │
     │  Kafka      │    │  Kafka      │    │  Kafka      │
     │  NiFi       │    │  NiFi       │    │  NiFi       │
     └──────┬──────┘    └──────┬──────┘    └──────┬──────┘
            │                  │                  │
            ▼                  ▼                  ▼
     typed topics       typed topics       typed topics
     (per event type)   (per event type)   (per event type)
            │
            ▼
     Student Analytics
     (Spark / DB / notebooks)


  Instructor → Slack /infra → ChatOps (infra namespace) → kubectl API
```

## Three Ways to Operate

| Tool | Who | When | How |
|------|-----|------|-----|
| `bash pipeline/setup.sh` | Instructor | Before class — provision all teams | Fire-and-forget Tekton pipeline, runs in-cluster |
| `bash pipeline/ops.sh` | Instructor | During class — fix one team | Direct `oc` commands, instant results, no Tekton |
| `/infra <command>` in Slack | Instructor | Anytime — no terminal needed | ChatOps bot in infra namespace |

### Day-2 Team Registry

A `team-registry` ConfigMap and `team-passwords` Secret in the `infra` namespace act as
the cluster-side source of truth for team mappings and NiFi passwords. Written by
`setup.sh` on every run; kept in sync by ops.sh and ChatOps whenever Kafka is added or removed.

Adding or removing a team from Slack or the terminal automatically updates the event-generator
— no manual ConfigMap edits needed.

**Three config.env sync flows:**

| Flow | When to use | Steps |
|------|-------------|-------|
| Local-first | You know the team before class | Edit config.env → `ops.sh add-team` → done |
| Cluster-first (manual) | Team added via Slack, paste yourself | `/infra add-team` → `/infra export-config` → paste into config.env |
| Cluster-first (auto) | Team added via Slack, sync automatically | `/infra add-team` → `ops.sh sync-config` → config.env updated |

---

## Quick Start

### 1. Install OpenShift Pipelines Operator

Install via OperatorHub as kubeadmin. Required for `setup.sh` (ops.sh and ChatOps work without it).

### 2. Configure Both Env Files

```bash
cp config.env.example config.env
cp onboarding/cluster.env.example onboarding/cluster.env
# Edit both files — set cluster domain, storage class, team names, passwords
```

### 3. Create Namespaces and RBAC (kubeadmin)

```bash
bash onboarding/apply-onboarding.sh --dry-run   # verify first
bash onboarding/apply-onboarding.sh
```

### 4. Add Users to Groups (kubeadmin)

```bash
oc adm groups add-users infra-admins <instructor-username>
oc adm groups add-users team-01-devs <student-username>
```

### 5. Deploy Everything (instructor)

```bash
source config.env
bash pipeline/setup.sh
```

Deploys Kafka + NiFi for all configured teams in parallel, then the event generator, then ChatOps.
Watch progress:

```bash
tkn pipelinerun logs --last -f -n ${INFRA_NAMESPACE}
```

### 6. Verify

```bash
# All team resources
oc get pods,svc,pvc -n team-01

# NiFi UI (printed in pipeline output, or):
oc get route -n team-01

# Events flowing (consume 5 messages)
oc run kafka-consumer --rm -it \
  --image=confluentinc/cp-kafka:7.5.0 -n team-01 -- \
  kafka-console-consumer \
  --bootstrap-server kafka-team01.team-01.svc.cluster.local:9092 \
  --topic events.team01.raw --max-messages 5
```

### 7. Operate from Slack

Set up the `/infra` slash command (see [chatops/README.md](chatops/README.md)), then:

```
/infra status-all
/infra pause-events
/infra reset-password team01 team-01 NewPass2026!!
```

---

## Directory Structure

```
data-eng-at-scale-infra/
├── config.env.example          Runtime config template — copy to config.env
├── onboarding/                 One-time cluster setup (kubeadmin): namespaces, quotas,
│                               LimitRanges, RBAC groups — run before anything else
├── pipeline/                   Two tools for classroom operations (run after onboarding):
│   ├── setup.sh                  Fire-and-forget provisioning for all teams via Tekton
│   ├── ops.sh                    Surgical day-to-day fixes via raw oc (instant, no Tekton)
│   ├── tasks/                    Tekton Task definitions (deploy-kafka, deploy-nifi, etc.)
│   ├── pipelines/                Tekton Pipeline definitions
│   └── runs/                     PipelineRun and TaskRun templates
├── chatops/                    Slack /infra slash command handler
│   ├── src/                      FastAPI app (commands.py, settings.py, main.py)
│   └── k8s/                      Deployment, BuildConfig, Route, RBAC
├── kafka/                      Kafka broker templates and per-team deploy scripts
├── nifi/                       NiFi instance templates and per-team deploy scripts
├── event-generator/            Synthetic event producer (Python, outbreak simulation)
├── storage/                    Optional MinIO (S3) and PostgreSQL templates
└── learning/                   16 step-by-step student guides
```

---

## Key Technologies

- **OpenShift** — Container orchestration (`oc` CLI, Routes, BuildConfigs)
- **Apache Kafka 3.6.x** — KRaft mode (no ZooKeeper), one broker per team
- **Apache NiFi 1.24.x** — Visual data flow, one instance per team
- **Python 3.x** — Event generator (outbreak simulation)
- **Tekton** — CI/CD pipelines (OpenShift Pipelines operator)
- **FastAPI** — ChatOps Slack bot

---

## Component Documentation

- [Pipeline (setup.sh + ops.sh)](pipeline/README.md) — Full provisioning and day-2 reference
- [ChatOps (Slack /infra)](chatops/README.md) — Slash command setup and command reference
- [Kafka](kafka/README.md) — Kafka deployment options and configuration
- [NiFi](nifi/README.md) — NiFi installation and access
- [Event Generator](event-generator/README.md) — Synthetic data producer
- [Onboarding](onboarding/README.md) — Namespace, quota, and RBAC setup

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for scope and code style guidelines.
See [SECURITY.md](SECURITY.md) for secrets management — never commit real credentials.
