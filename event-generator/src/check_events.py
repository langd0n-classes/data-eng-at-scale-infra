#!/usr/bin/env python3
"""
Check that the event generator is producing to all team Kafka topics.
Runs inside the event generator pod via:
  oc exec <pod> -n ds551-2026-spring-9ab13b -- python3 /dev/stdin < check_events.py

Or pipe directly:
  oc exec $(oc get pod -n ds551-2026-spring-9ab13b -l app=ds551-event-generator -o jsonpath='{.items[0].metadata.name}') \
     -n ds551-2026-spring-9ab13b -- python3 - < check_events.py
"""

import os
import json
from kafka import KafkaConsumer, KafkaProducer
from datetime import datetime

TOPIC_PREFIX = "ds551-s26."
TOPIC_SUFFIX = ".raw"
TIMEOUT_MS = 5000

TEAM_BOOTSTRAP_SERVERS = os.getenv("TEAM_BOOTSTRAP_SERVERS", "")

teams = {}
for entry in TEAM_BOOTSTRAP_SERVERS.split(","):
    if "=" in entry:
        team_id, bootstrap = entry.split("=", 1)
        teams[team_id.strip()] = bootstrap.strip()

print(f"Checking {len(teams)} teams...\n")

OK = []
EMPTY = []
FAIL = []

for team_id, bootstrap in sorted(teams.items()):
    topic = f"{TOPIC_PREFIX}{team_id}{TOPIC_SUFFIX}"
    try:
        consumer = KafkaConsumer(
            topic,
            bootstrap_servers=[bootstrap],
            auto_offset_reset="latest",
            consumer_timeout_ms=TIMEOUT_MS,
            group_id=None,
            enable_auto_commit=False,
        )
        # Get end offsets to check total message count
        partitions = consumer.partitions_for_topic(topic)
        if partitions is None:
            FAIL.append((team_id, "topic does not exist"))
            consumer.close()
            continue

        from kafka import TopicPartition
        tps = [TopicPartition(topic, p) for p in partitions]
        end_offsets = consumer.end_offsets(tps)
        begin_offsets = consumer.beginning_offsets(tps)

        total = sum(end_offsets[tp] - begin_offsets[tp] for tp in tps)
        end = sum(end_offsets[tp] for tp in tps)
        consumer.close()

        if total > 0:
            OK.append((team_id, total, end))
            status = f"✓  {total:>6} messages (end offset: {end})"
        else:
            EMPTY.append((team_id, bootstrap))
            status = f"⚠  0 messages — topic exists but empty"

        print(f"  {team_id:<15} {status}")

    except Exception as e:
        FAIL.append((team_id, str(e)))
        print(f"  {team_id:<15} ✗  FAILED: {e}")

print(f"\n{'='*55}")
print(f"  OK:    {len(OK)}/{len(teams)} teams have messages")
print(f"  Empty: {len(EMPTY)} teams (topic exists, no messages yet)")
print(f"  Fail:  {len(FAIL)} teams (connection or topic error)")

if EMPTY:
    print(f"\n  Empty teams: {', '.join(t for t, _ in EMPTY)}")
if FAIL:
    print(f"\n  Failed teams: {', '.join(t for t, _ in FAIL)}")
