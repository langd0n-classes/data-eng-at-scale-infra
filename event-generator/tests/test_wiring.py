"""Wiring of the label service into EventGenerator. Kafka is mocked."""
import os
import sys
from datetime import datetime, timezone

os.environ['EVENT_RATE_PER_SEC'] = '1.5'
os.environ['REGIONS'] = 'Boston,Cambridge,Somerville,Brookline,Newton'
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import event_generator as eg  # noqa: E402

REGIONS = ['Boston', 'Cambridge', 'Somerville', 'Brookline', 'Newton']


class RecordingProducer:
    def __init__(self):
        self.sent = []

    def send(self, topic, value=None, key=None):
        self.sent.append((topic, key, value))

    def flush(self, timeout=None):
        pass


def test_labels_go_to_the_team_topic_and_state_to_team00(monkeypatch):
    monkeypatch.setattr(eg, 'TEAM_KAFKA_MAPPING', {'team00': 'k0:9092', 'team01': 'k1:9092'})
    monkeypatch.setattr(eg, 'ensure_unlimited_topics', lambda bootstrap, topics: True)
    gen = eg.EventGenerator()
    assert gen.labels.store.bootstrap == 'k0:9092'
    assert gen.labels.store.topic == 'staff.outbreak_state'
    gen.producers = {'team01': RecordingProducer()}
    hour = datetime(2026, 11, 9, 14, tzinfo=timezone.utc)
    msgs = eg.build_labels(hour, 'winddown', ['Boston'], REGIONS, hour)
    assert gen.send_labels('team01', msgs)
    topic, key, value = gen.producers['team01'].sent[0]
    assert topic == 'events.team01.outbreak_status'
    assert key == b'Boston' and value['status'] == 'WATCH'
    assert not gen.send_labels('team02', msgs)  # no producer: not counted as sent


def test_no_state_team_means_memory_only(monkeypatch):
    monkeypatch.setattr(eg, 'TEAM_KAFKA_MAPPING', {'team01': 'k1:9092'})
    assert eg.EventGenerator().labels.store is None
