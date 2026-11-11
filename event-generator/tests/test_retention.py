"""History and label topics get unlimited retention (admin client mocked)."""
import json
import os
import sys

import pytest

os.environ['EVENT_RATE_PER_SEC'] = '1.5'
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import event_generator as eg  # noqa: E402
import backfill_history as bf  # noqa: E402

FOREVER = {'retention.ms': '-1', 'retention.bytes': '-1'}


class FakeAdmin:
    existing = set()
    created, altered = [], []
    fail_with = None

    def __init__(self, bootstrap_servers=None, **k):
        if FakeAdmin.fail_with:
            raise FakeAdmin.fail_with

    def list_topics(self):
        return list(FakeAdmin.existing)

    def create_topics(self, topics):
        for t in topics:
            if t.name in FakeAdmin.existing:
                raise eg.TopicAlreadyExistsError()
            FakeAdmin.created.append((t.name, t.num_partitions, t.topic_configs))

    def alter_configs(self, resources):
        for r in resources:
            FakeAdmin.altered.append((r.name, r.configs))

    def close(self):
        pass


@pytest.fixture(autouse=True)
def admin(monkeypatch):
    FakeAdmin.existing, FakeAdmin.created, FakeAdmin.altered, FakeAdmin.fail_with = set(), [], [], None
    monkeypatch.setattr(eg, 'KafkaAdminClient', FakeAdmin)


def test_creates_missing_topics_with_unlimited_retention():
    assert eg.ensure_unlimited_topics('k:9092', ['events.team01.history', 'events.team01.outbreak_status'])
    assert FakeAdmin.created == [('events.team01.history', 1, FOREVER),
                                 ('events.team01.outbreak_status', 1, FOREVER)]
    assert FakeAdmin.altered == []


def test_alters_existing_topics():
    FakeAdmin.existing = {'events.team01.history'}
    assert eg.ensure_unlimited_topics('k:9092', ['events.team01.history', 'events.team01.outbreak_status'])
    assert FakeAdmin.altered == [('events.team01.history', FOREVER)]
    assert [c[0] for c in FakeAdmin.created] == ['events.team01.outbreak_status']


def test_topic_created_in_between_is_altered(monkeypatch):
    class Racy(FakeAdmin):
        def list_topics(self):
            return []          # not there when listed, there when created

        def create_topics(self, topics):
            raise eg.TopicAlreadyExistsError()
    monkeypatch.setattr(eg, 'KafkaAdminClient', Racy)
    assert eg.ensure_unlimited_topics('k:9092', ['events.team01.history'])
    assert FakeAdmin.altered == [('events.team01.history', FOREVER)]


def test_failure_returns_false_and_does_not_raise():
    FakeAdmin.fail_with = RuntimeError("no broker")
    assert eg.ensure_unlimited_topics('k:9092', ['t']) is False


def test_live_generator_holds_labels_until_topic_is_ready_then_never_asks_again(monkeypatch):
    monkeypatch.setattr(eg, 'TEAM_KAFKA_MAPPING', {'team01': 'k1:9092'})
    calls, ok = [], {'v': False}
    monkeypatch.setattr(eg, 'ensure_unlimited_topics', lambda b, t: calls.append((b, t)) or ok['v'])
    sent = []

    class P:
        def send(self, topic, value=None, key=None):
            sent.append(topic)
            return None

        def flush(self, timeout=None):
            pass
    gen = eg.EventGenerator()
    gen.producers = {'team01': P()}
    msgs = [('Boston', {'region': 'Boston'})]
    assert gen.send_labels('team01', msgs) is False and sent == []     # held
    ok['v'] = True
    assert gen.send_labels('team01', msgs) is True and sent == ['events.team01.outbreak_status']
    gen.send_labels('team01', msgs)
    assert calls == [('k1:9092', ['events.team01.outbreak_status'])] * 2   # second attempt succeeded, third skipped


def test_backfill_sets_up_both_topics_before_writing(monkeypatch):
    monkeypatch.setattr(eg, 'TEAM_KAFKA_MAPPING', {'team01': 'k1:9092'})
    monkeypatch.setattr(bf, 'TEAM_KAFKA_MAPPING', {'team01': 'k1:9092'})
    order = []

    class P:
        def send(self, *a, **k):
            order.append('send')

            class F:
                def add_errback(self, cb):
                    return self
            return F()

        def flush(self, timeout=None):
            pass

        def close(self, timeout=None):
            pass
    monkeypatch.setattr(eg, 'ensure_unlimited_topics', lambda b, t: order.append(('topics', t)) or True)
    monkeypatch.setattr(eg.EventGenerator, 'connect_kafka_for_team', lambda self, team, bs: P())
    assert bf.main(['--team', 'team01', '--start', '2026-11-02T08:00:00Z', '--end', '2026-11-02T09:00:00Z']) == 0
    assert order[0] == ('topics', ['events.team01.outbreak_status', 'events.team01.history'])
    assert 'send' in order[1:]


def test_backfill_labels_only_sets_up_the_label_topic_and_aborts_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(bf, 'TEAM_KAFKA_MAPPING', {'team01': 'k1:9092'})
    seen = []
    monkeypatch.setattr(eg, 'ensure_unlimited_topics', lambda b, t: seen.append(t) or False)
    states = tmp_path / 's.json'
    states.write_text(json.dumps([{"from_time": "2026-11-02T00:00:00Z", "profile": "baseline",
                                   "affected_regions": []}]))
    rc = bf.main(['--team', 'team01', '--labels-from', str(states),
                  '--start', '2026-11-02T08:00:00Z', '--end', '2026-11-02T09:00:00Z'])
    assert rc == 1 and seen == [['events.team01.outbreak_status']]
