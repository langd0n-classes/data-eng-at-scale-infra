"""Check one A03 team login against the shared broker.

The check creates a topic with the team prefix, produces and consumes
three messages, replays them, and confirms that a topic with another
team's prefix is refused. It deletes its own topic at the end.

Usage: python check-team-access.py <bundle_dir> <other_team>
  bundle_dir: a folder written by team-bundle.sh (.env and kafka-ca.crt)
  other_team: a different team name, such as team02
"""

import os
import sys
import time
import uuid

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer
from confluent_kafka.admin import AdminClient, NewTopic


def read_env(bundle_dir):
    values = {}
    with open(os.path.join(bundle_dir, ".env")) as env_file:
        for line in env_file:
            line = line.strip()
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value
    return values


def client_config(env, bundle_dir):
    return {
        "bootstrap.servers": env["KAFKA_BOOTSTRAP_SERVERS"],
        "security.protocol": "SASL_SSL",
        "sasl.mechanism": "SCRAM-SHA-512",
        "sasl.username": env["KAFKA_USERNAME"],
        "sasl.password": env["KAFKA_PASSWORD"],
        "ssl.ca.location": os.path.join(bundle_dir, env["KAFKA_CA_FILE"]),
    }


def create_topic(admin, name):
    futures = admin.create_topics([NewTopic(name, num_partitions=3, replication_factor=1)])
    futures[name].result(timeout=30)


def consume_all(config, topic, group, expected):
    consumer_config = dict(config)
    consumer_config["group.id"] = group
    consumer_config["auto.offset.reset"] = "earliest"
    consumer_config["enable.auto.commit"] = False
    consumer = Consumer(consumer_config)
    consumer.subscribe([topic])
    received = []
    deadline = time.time() + 60
    while len(received) < expected and time.time() < deadline:
        message = consumer.poll(1.0)
        if message is None:
            continue
        if message.error():
            raise KafkaException(message.error())
        received.append(message.value().decode())
    consumer.commit(asynchronous=False)
    consumer.close()
    return received


def main():
    bundle_dir = sys.argv[1]
    other_team = sys.argv[2]
    env = read_env(bundle_dir)
    config = client_config(env, bundle_dir)
    prefix = env["KAFKA_USERNAME"] + "."
    run_id = uuid.uuid4().hex[:8]
    topic = prefix + "check-" + run_id
    admin = AdminClient(config)

    create_topic(admin, topic)
    print("PASS create own topic", topic)

    errors = []

    def on_delivery(error, message):
        if error is not None:
            errors.append(error)

    producer = Producer(config)
    for number in range(3):
        producer.produce(topic, key=str(number), value="event-" + str(number),
                         on_delivery=on_delivery)
    remaining = producer.flush(30)
    if remaining != 0 or errors:
        raise SystemExit("FAIL produce: " + str(remaining) + " not confirmed, errors " + str(errors))
    print("PASS produce 3 messages")

    expected = ["event-0", "event-1", "event-2"]
    first = consume_all(config, topic, prefix + "check-" + run_id, 3)
    if sorted(first) != expected:
        raise SystemExit("FAIL consume: got " + str(first))
    print("PASS consume 3 messages")

    replay = consume_all(config, topic, prefix + "replay-" + run_id, 3)
    if sorted(replay) != expected:
        raise SystemExit("FAIL replay: different messages")
    print("PASS replay with a new group")

    other_topic = other_team + ".check-" + run_id
    try:
        create_topic(admin, other_topic)
        print("FAIL created another team's topic", other_topic)
        admin.delete_topics([other_topic])
        sys.exit(1)
    except KafkaException as error:
        code = error.args[0].code()
        if code == KafkaError.TOPIC_AUTHORIZATION_FAILED:
            print("PASS another team's prefix is refused")
        else:
            raise

    admin.delete_topics([topic])[topic].result(timeout=30)
    print("PASS delete own topic")


if __name__ == "__main__":
    main()
