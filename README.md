# Multi-Tenant Kafka + NiFi Infrastructure

Infrastructure manifests and tooling for classroom-scale, per-team Kafka + NiFi deployments. Designed to support DS-551 style projects where a shared event generator produces a mixed raw stream that teams split and process downstream.

## Overview

This repository provides infrastructure-as-code for deploying:

- **Per-team Kafka brokers** - Isolated Kafka instances using KRaft mode (no ZooKeeper)
- **Per-team NiFi instances** - Dedicated Apache NiFi deployments for data workflow orchestration
- **Event generator** - Synthetic data producer for testing and demonstrations
- **Optional storage** - MinIO (S3-compatible) and PostgreSQL templates

### Architecture

Supports two patterns:

1. **Shared Infrastructure**: Single Kafka cluster, topic-level isolation
2. **Per-Team Isolation**: Dedicated Kafka and NiFi per team/tenant (recommended for classes)

#### Classroom Data Flow (typical)

```
Event Generator (infra namespace)
  ↓
Kafka: teamXX.raw (mixed events)
  ↓
NiFi (team namespace): route by event_type
  ↓
Kafka: typed topics (symptom_report, clinic_visit, environmental_conditions)
  ↓
Spark / DB (team namespace): analytics + storage
```

## Prerequisites

- Kubernetes 1.24+ or OpenShift 4.10+
- kubectl or oc CLI
- envsubst (from gettext package)
- Cluster admin or namespace admin privileges
- Sufficient cluster resources (CPU, memory, storage)

### OpenShift-Specific Requirements

- **NiFi requires anyuid SCC** - The Apache NiFi image runs as a specific UID
- Routes are used for external access (can be replaced with Ingress for vanilla Kubernetes)

## Quick Start

### 1. Clone and Configure

```bash
git clone <this-repo>
cd public-infra

# Create your configuration
cp config.env.example config.env
# Edit config.env with your namespace names and settings
```

### 2. Create Namespaces

```bash
source config.env

# Create infrastructure namespace
kubectl create namespace ${INFRA_NAMESPACE}

# Create team namespaces (adjust count as needed)
for i in {01..10}; do
  kubectl create namespace ${TEAM_NAMESPACE_PREFIX}-$i
done
```

### 3. Deploy Components

```bash
source config.env

# Deploy per-team Kafka (per team)
cd kafka/per-team
./deploy-team.sh team01 team-01
cd ../..

# Deploy NiFi (per team)
cd nifi
./deploy-team.sh team01 ${INFRA_NAMESPACE} MySecurePassword123
cd ..

# Deploy event generator (shared, produces mixed events)
kubectl apply -f event-generator/configmap.yaml
kubectl apply -f event-generator/deployment.yaml
```

## Directory Structure

```
public-infra/
├── kafka/
│   ├── per-team/                  # Isolated Kafka per team
│   │   ├── kafka-per-team-template.yaml
│   │   ├── deploy-team.sh
│   │   └── delete-team.sh
│   └── shared-deployment/         # Shared Kafka cluster option
│       ├── kafka-statefulset.yaml
│       ├── kafka-nodeport.yaml
│       └── kafka-route.yaml
├── nifi/
│   ├── team-statefulset-template.yaml
│   ├── team-pvc-template.yaml
│   ├── team-route-template.yaml
│   ├── deploy-team.sh
│   └── delete-team.sh
├── event-generator/               # Synthetic event producer
│   ├── Dockerfile
│   ├── event_generator.py
│   ├── deployment.yaml
│   └── configmap.yaml
├── storage/                       # Optional storage services
│   ├── minio.yaml
│   └── postgres.yaml
├── onboarding/                    # Team namespace templates
│   ├── namespace-template.yaml
│   └── team-config-template.yaml
└── docs/                          # Additional documentation
```

## Configuration

All manifests use environment variable substitution via `envsubst`:

```bash
# Edit configuration
vi config.env

# Source it
source config.env

# Deploy with substitution
envsubst < kafka/per-team/kafka-per-team-template.yaml | kubectl apply -f -
```

### Key Variables

- `INFRA_NAMESPACE`: Shared infra namespace (event generator, optional shared Kafka)
- `TEAM_NAMESPACE_PREFIX`: Prefix for team namespaces (e.g., `team-01`)
- `KAFKA_CLUSTER_NAME`: Kafka cluster name per team or shared
- `NIFI_IMAGE`: NiFi container image
- `STORAGE_CLASS`: Storage class for PVCs
- `NUM_TEAMS`: Number of teams/tenants

## Use Cases

### Educational Environments

- Isolated environments for students or training participants
- Reproducible data engineering exercises
- Hands-on Kafka and NiFi learning

### Development & Testing

- Multi-tenant development clusters
- Integration testing with isolated resources
- Proof-of-concept deployments

### Demonstrations

- Conference talks and workshops
- Product demonstrations
- Architecture prototypes

## Component Documentation

- [Kafka Setup](kafka/README.md) - Kafka deployment options and configuration
- [NiFi Setup](nifi/README.md) - NiFi installation and access
- [Event Generator](event-generator/README.md) - Synthetic data producer (mixed events → raw topic)
- [Contributing](CONTRIBUTING.md) - How to contribute
- [Security](SECURITY.md) - Security considerations and secrets management

## Monitoring

```bash
# Check all resources
kubectl get all -n ${INFRA_NAMESPACE}

# Check specific team
kubectl get pods,svc -n team-01

# View logs
kubectl logs -f <pod-name> -n <namespace>
```

## Cleanup

```bash
# Delete specific team resources
cd kafka/per-team
./delete-team.sh team01 team-01
cd ../..

# Delete entire namespace
kubectl delete namespace team-01
```

## License

This infrastructure code is provided as-is for educational and demonstration purposes.

## Support

For classroom deployments, instructors typically:
- Create infra + team namespaces
- Deploy per-team Kafka + NiFi using this repo
- Configure the event generator to publish mixed events into each team’s raw topic
- Provide students with topic names and access URLs

For issues or questions:

- Check component-specific README files
- Review Kubernetes/OpenShift documentation
- Consult Apache Kafka and NiFi official documentation
