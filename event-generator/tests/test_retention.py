"""History and label topics get unlimited retention (admin client mocked).

The fake admin returns response objects shaped like kafka-python 2.0.2's:
  CreateTopicsResponse.topic_errors   [(topic, error_code, error_message)]
  AlterConfigsResponse.resources      [(error_code, error_message, resource_type, resource_name)]
  describe_configs(...)               [DescribeConfigsResponse] with
                                      .resources [(error_code, error_message, resource_type, name,
                                                   [(config_name, config_value, ...)])]
Error codes are returned, not raised (create_topics raises code 36 as TopicAlreadyExistsError in
2.0.2, so both ways are tested).
"""
import json
import os
import sys
import threading
from types import SimpleNamespace

import pytest

os.environ['EVENT_RATE_PER_SEC'] = '1.5'
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import event_generator as eg  # noqa: E402
import backfill_history as bf  # noqa: E402

HIST, LABELS = 'events.team01.history', 'events.team01.outbreak_status'


class FakeAdmin:
    """Class-level state so a test can set it up before the client is constructed."""
    topics = {}                 # name -> {config: value}
    created, altered = [], []
    create_codes = {}           # topic -> error code returned by create_topics
    create_raises = False       # raise TopicAlreadyExistsError for an existing topic, as 2.0.2 does
    alter_code = 0
    alter_is_noop = False       # the broker accepts the alter but does not apply it
    describe_code = 0
    init_error = None
    block = None                # threading.Event: list_topics waits on it
    constructed = 0

    def __init__(self, **k):
        FakeAdmin.constructed += 1
        if FakeAdmin.init_error:
            raise FakeAdmin.init_error

    def list_topics(self):
        if FakeAdmin.block is not None:
            FakeAdmin.block.wait(30)
        return list(FakeAdmin.topics)

    def create_topics(self, new_topics):
        errors = []
        for t in new_topics:
            if t.name in FakeAdmin.topics:
                if FakeAdmin.create_raises:
                    raise eg.TopicAlreadyExistsError()
                errors.append((t.name, 36, 'Topic already exists'))
            elif FakeAdmin.create_codes.get(t.name):
                errors.append((t.name, FakeAdmin.create_codes[t.name], 'refused'))
            else:
                FakeAdmin.topics[t.name] = dict(t.topic_configs)
                FakeAdmin.created.append((t.name, t.num_partitions, dict(t.topic_configs)))
                errors.append((t.name, 0, None))
        return SimpleNamespace(topic_errors=errors)

    def alter_configs(self, resources):
        out = []
        for r in resources:
            if FakeAdmin.alter_code == 0 and not FakeAdmin.alter_is_noop:
                FakeAdmin.topics[r.name] = dict(r.configs)
            FakeAdmin.altered.append((r.name, dict(r.configs)))
            out.append((FakeAdmin.alter_code, 'nope' if FakeAdmin.alter_code else None, 2, r.name))
        return SimpleNamespace(resources=out)

    def describe_configs(self, resources):
        out = []
        for r in resources:
            cfg = FakeAdmin.topics.get(r.name, {})
            entries = [('retention.ms', cfg.get('retention.ms', '86400000'), False, 5, False, []),
                       ('retention.bytes', cfg.get('retention.bytes', '104857600'), False, 5, False, [])]
            out.append((FakeAdmin.describe_code, 'err' if FakeAdmin.describe_code else None, 2, r.name, entries))
        return [SimpleNamespace(resources=out)]

    def close(self):
        pass


@pytest.fixture(autouse=True)
def admin(monkeypatch):
    FakeAdmin.topics, FakeAdmin.created, FakeAdmin.altered = {}, [], []
    FakeAdmin.create_codes, FakeAdmin.create_raises, FakeAdmin.alter_code = {}, False, 0
    FakeAdmin.alter_is_noop, FakeAdmin.describe_code = False, 0
    FakeAdmin.init_error, FakeAdmin.block, FakeAdmin.constructed = None, None, 0
    monkeypatch.setattr(eg, 'KafkaAdminClient', FakeAdmin)
    monkeypatch.setattr(eg, '_ensure_inflight', {})


FOREVER = {'retention.ms': '-1', 'retention.bytes': '-1'}


def test_creates_missing_topics_and_reads_back():
    assert eg.ensure_unlimited_topics('k:9092', [HIST, LABELS])
    assert FakeAdmin.created == [(HIST, 1, FOREVER), (LABELS, 1, FOREVER)]
    assert FakeAdmin.altered == []


def test_existing_topic_is_altered():
    FakeAdmin.topics = {HIST: {'retention.ms': '86400000', 'retention.bytes': '104857600'}}
    assert eg.ensure_unlimited_topics('k:9092', [HIST, LABELS])
    assert FakeAdmin.altered == [(HIST, FOREVER)]
    assert [c[0] for c in FakeAdmin.created] == [LABELS]
    assert FakeAdmin.topics[HIST] == FOREVER


def test_code_36_in_the_create_response_goes_on_to_alter(monkeypatch):
    class Racy(FakeAdmin):
        def list_topics(self):
            return []   # not there when listed, there when created
    FakeAdmin.topics = {HIST: {}}
    monkeypatch.setattr(eg, 'KafkaAdminClient', Racy)
    assert eg.ensure_unlimited_topics('k:9092', [HIST])
    assert FakeAdmin.altered == [(HIST, FOREVER)]


def test_already_exists_raised_by_the_client_goes_on_to_alter(monkeypatch):
    class Racy(FakeAdmin):
        def list_topics(self):
            return []
    FakeAdmin.topics, FakeAdmin.create_raises = {HIST: {}}, True
    monkeypatch.setattr(eg, 'KafkaAdminClient', Racy)
    assert eg.ensure_unlimited_topics('k:9092', [HIST])
    assert FakeAdmin.altered == [(HIST, FOREVER)]


def test_create_error_code_is_a_failure_and_stops():
    FakeAdmin.create_codes = {HIST: 29}   # TopicAuthorizationFailed
    assert eg.ensure_unlimited_topics('k:9092', [HIST, LABELS]) is False
    assert FakeAdmin.created == [] and FakeAdmin.altered == []


def test_alter_error_code_is_a_failure():
    FakeAdmin.topics = {HIST: {}}
    FakeAdmin.alter_code = 40             # InvalidConfig
    assert eg.ensure_unlimited_topics('k:9092', [HIST]) is False


def test_read_back_mismatch_is_a_failure():
    FakeAdmin.topics = {HIST: {}}
    FakeAdmin.alter_is_noop = True        # the response says OK, the config did not change
    assert eg.ensure_unlimited_topics('k:9092', [HIST]) is False


def test_describe_error_is_a_failure():
    FakeAdmin.describe_code = 29
    assert eg.ensure_unlimited_topics('k:9092', [HIST]) is False


def test_client_construction_failure_does_not_raise():
    FakeAdmin.init_error = RuntimeError("no broker")
    assert eg.ensure_unlimited_topics('k:9092', [HIST]) is False


def test_timeout_is_a_failure_and_does_not_start_a_second_call():
    FakeAdmin.block = threading.Event()
    try:
        assert eg.ensure_unlimited_topics('k:9092', [HIST], timeout=0.2) is False
        assert FakeAdmin.constructed == 1
        # the first call is still hung: a retry is refused without another thread
        assert eg.ensure_unlimited_topics('k:9092', [HIST], timeout=0.2) is False
        assert FakeAdmin.constructed == 1
    finally:
        FakeAdmin.block.set()
    # once it has finished, a new call works
    for t in list(eg._ensure_inflight.values()):
        t.join(5)
    assert eg.ensure_unlimited_topics('k:9092', [HIST], timeout=5) is True


def test_state_topic_creation_is_bounded_and_checks_codes(monkeypatch):
    store = eg.KafkaStateStore('k0:9092', 'staff.outbreak_state', lambda: None)
    store.ensure_topic()
    assert FakeAdmin.created == [('staff.outbreak_state', 1, {'cleanup.policy': 'compact'})]
    FakeAdmin.create_codes = {'staff.outbreak_state2': 29}
    store2 = eg.KafkaStateStore('k0:9092', 'staff.outbreak_state2', lambda: None)
    store2.ensure_topic()   # error code: logged, no raise
    gate = threading.Event()

    class Hang(FakeAdmin):
        def create_topics(self, new_topics):
            gate.wait(30)
            return SimpleNamespace(topic_errors=[])
    monkeypatch.setattr(eg, 'KafkaAdminClient', Hang)
    monkeypatch.setattr(eg, 'ENSURE_TOPICS_TIMEOUT', 0.2)
    try:
        store.ensure_topic()   # hangs in the client: returns after the timeout
    finally:
        gate.set()


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
    assert gen.send_labels('team01', msgs) is True and sent == [LABELS]
    gen.send_labels('team01', msgs)
    assert calls == [('k1:9092', [LABELS])] * 2   # the second attempt succeeded; the third did not ask


def test_backfill_sets_up_both_topics_before_writing(monkeypatch):
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
    assert order[0] == ('topics', [LABELS, HIST])
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
    assert rc == 1 and seen == [[LABELS]]
