# Shared A03 Kafka Broker

One Kafka broker for all A03 teams, reachable from outside the cluster.
Each team has a SCRAM-SHA-512 login over TLS.
A team login can create, use, and delete only topics and consumer groups
whose names start with its team name and a dot, such as `team03.events.raw`.
`team00` is the staff reference team; it has the same limits as a student team.

This broker is separate from the per-team final-project brokers.

## Requirements

- A Kubernetes cluster that can provision `LoadBalancer` Services.
- The Strimzi Cluster Operator 0.51.0 watching the `a03-kafka` namespace,
  for example from `kafka/portable/prerequisites/install-strimzi.sh`.
- `kubectl`, `envsubst`, and `base64`.
- Python with `confluent-kafka` for the access check.

Validated on OVHcloud Managed Kubernetes 1.34.6 with Strimzi 0.51.0 and
Kafka 4.2.0, 2026-10-21.

## Deploy

```bash
kubectl apply -f kafka.yaml
kubectl wait kafka/a03 -n a03-kafka --for=condition=Ready --timeout=900s
bash scripts/apply-users.sh 10
```

The two load balancers can take several minutes on a new cluster.
The broker pod starts after both have an address.

## DNS Names

Students connect to `kafka.ds551.langd0n.com:9094`, an A record for the
bootstrap load balancer. Each broker has its own load balancer and a lettered
name: node 0 is `kafka-a.ds551.langd0n.com`, node 1 would be `kafka-b`.
`kafka.yaml` puts these names in the broker certificate and advertises them.
If a load balancer address changes, update its A record.

## Give a Team Its Login

```bash
bash scripts/team-bundle.sh team03 <output_dir>
```

The bundle holds `.env` (address, username, password) and `kafka-ca.crt`.
Send `.env` only to that team.
The certificate is the same for every team.

## Check a Login

```bash
python scripts/check-team-access.py <bundle_dir> team02
```

The check creates a topic with the team prefix, produces, consumes, replays,
confirms that the other team's prefix is refused, and deletes its topic.

## Limits

- One broker, one replica: a node drain or broker restart stops A03 for a
  few minutes.
- Retention is 21 days or 100 MiB per partition.
- Each login is limited to 1 MiB/s produce, 2 MiB/s consume, and 25% of
  request time.
