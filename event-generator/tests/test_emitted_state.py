"""Labels follow the emitted events; failed sends; caps; topic creation; log reconstruction."""
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

os.environ['EVENT_RATE_PER_SEC'] = '1.5'
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import event_generator as eg  # noqa: E402
import backfill_history as bf  # noqa: E402
import reconstruct_state as rs  # noqa: E402

REGIONS = ['Boston', 'Cambridge', 'Somerville', 'Brookline', 'Newton']


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


class Fut:
    def __init__(self, exc=None):
        self.exc = exc

    def get(self, timeout=None):
        if self.exc:
            raise self.exc

    def add_callback(self, cb, *a, **k):
        return self

    def add_errback(self, cb, *a, **k):
        return self


class Prod:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, topic, value=None, key=None):
        self.sent.append((topic, key, value))
        return Fut(RuntimeError("broker rejected") if self.fail else None)

    def flush(self, timeout=None):
        pass


# ---- labels follow the emitted events (finding 1) --------------------------

def test_pool_items_carry_the_generation_state_and_emitted_json_is_unchanged(monkeypatch):
    monkeypatch.setattr(eg, 'TEAM_KAFKA_MAPPING', {})
    monkeypatch.setattr(eg, 'EVENT_RATE_PER_SEC', 100.0)
    gen = eg.EventGenerator()
    gen.state.profile, gen.state.affected_regions = 'outbreak', ['Boston']
    gen._fill_pool(3)
    gen.state.profile, gen.state.affected_regions = 'baseline', []   # self.state runs ahead
    gen._fill_pool(2)
    assert gen.event_pool.qsize() == 5
    assert gen.emitted_state is None

    prod = Prod()
    gen.producers = {'team01': prod}
    gen.running = True
    t = threading.Thread(target=gen.emit_thread_worker, daemon=True)
    t.start()
    seen = []
    deadline = time.time() + 20
    while len(prod.sent) < 5 and time.time() < deadline:
        seen.append(gen.emitted_state)
        time.sleep(0.001)
    gen.running = False
    assert len(prod.sent) == 5
    # emitted_state moved through the generation states in FIFO order, never to self.state early
    states = [s for i, s in enumerate(seen) if s is not None and (i == 0 or s != seen[i - 1])]
    assert states == [('outbreak', ('Boston',)), ('baseline', ())]
    for _, _, ev in prod.sent:
        assert 'tag' not in ev and 'profile' not in ev and ev['source'] == 'event-generator'
        assert set(ev) >= {'event_type', 'timestamp', 'region', 'schema_version'}


def test_label_thread_tick_uses_the_emitted_state(monkeypatch):
    monkeypatch.setattr(eg, 'TEAM_KAFKA_MAPPING', {})
    monkeypatch.setattr(eg, 'LABEL_TICK_SECONDS', 0)
    gen = eg.EventGenerator()
    gen.labels.record(utc(2026, 11, 9, 14, 30), 'baseline', [])   # sanity: no store, in memory
    gen.state.profile, gen.state.affected_regions = 'outbreak', ['Boston']  # ahead of the emitted events
    gen.emitted_state = ('baseline', ())
    calls = []
    gen.labels.tick = lambda now, profile, affected: calls.append((profile, affected)) or gen.stop_label()
    gen.stop_label = lambda: setattr(gen, 'running', False)
    gen.running = True
    gen.label_thread_worker()
    assert calls == [('baseline', [])]


# ---- send_labels (finding 2) -----------------------------------------------

def test_send_labels_false_on_failed_delivery(monkeypatch):
    monkeypatch.setattr(eg, 'TEAM_KAFKA_MAPPING', {})
    gen = eg.EventGenerator()
    msgs = eg.build_labels(utc(2026, 11, 9, 14), 'outbreak', ['Boston'], REGIONS, utc(2026, 11, 10, 3))
    gen.producers = {'team01': Prod(fail=True)}
    assert gen.send_labels('team01', msgs) is False
    gen.producers = {'team01': Prod()}
    assert gen.send_labels('team01', msgs) is True


def test_failed_delivery_is_not_marked_released(monkeypatch):
    monkeypatch.setattr(eg, 'TEAM_KAFKA_MAPPING', {})
    gen = eg.EventGenerator()
    gen.producers = {'team01': Prod(fail=True)}
    svc = eg.LabelService(None, REGIONS, 12, gen.send_labels, lambda: ['team01'])
    svc.record(utc(2026, 11, 9, 14, 30), 'outbreak', ['Boston'])
    assert svc.release_due(utc(2026, 11, 10, 3)) == 0 and svc.released == set()
    gen.producers['team01'] = Prod()
    assert svc.release_due(utc(2026, 11, 10, 3, 1)) == 5


# ---- caps and topic creation (findings 3 and 4) ----------------------------

class StoreDown:
    def __init__(self):
        self.ensure_calls = 0
        self.fail_load = True

    def ensure_topic(self):
        self.ensure_calls += 1

    def load(self):
        if self.fail_load:
            raise RuntimeError("down")
        return {}, set()

    def save_state(self, *a):
        return False

    def save_released(self, *a):
        return False


def test_unsaved_lists_are_capped():
    store = StoreDown()
    store.fail_load = False
    svc = eg.LabelService(store, REGIONS, 1000, lambda t, m: True, lambda: ['team01'])
    svc.max_unsaved = 3
    for h in range(10):
        svc.tick(utc(2026, 11, 9, h, 30), 'outbreak', ['Boston'])
    assert len(svc._unsaved_states) == 3
    assert len(svc.states) == 10   # still valid in memory


def test_topic_is_ensured_once_even_if_load_keeps_failing():
    store = StoreDown()
    svc = eg.LabelService(store, REGIONS, 12, lambda t, m: True, lambda: [])
    for _ in range(3):
        assert svc.ensure_loaded() is False
    assert store.ensure_calls == 1


def test_ensure_topic_creates_compacted_and_never_raises(monkeypatch):
    created = []

    class Admin:
        def __init__(self, **k):
            pass

        def create_topics(self, topics):
            created.extend(topics)

        def close(self):
            pass
    monkeypatch.setattr(eg, 'KafkaAdminClient', Admin)
    store = eg.KafkaStateStore('k0:9092', 'staff.outbreak_state', lambda: None)
    store.ensure_topic()
    assert created[0].name == 'staff.outbreak_state' and created[0].topic_configs == {'cleanup.policy': 'compact'}

    class Exists(Admin):
        def create_topics(self, topics):
            raise eg.TopicAlreadyExistsError()
    monkeypatch.setattr(eg, 'KafkaAdminClient', Exists)
    store.ensure_topic()

    class Broken(Admin):
        def __init__(self, **k):
            raise RuntimeError("no broker")
    monkeypatch.setattr(eg, 'KafkaAdminClient', Broken)
    store.ensure_topic()


# ---- log reconstruction (finding 5) ----------------------------------------

T0 = utc(2026, 11, 10, 1, 37, 0)


def synthetic_log(restart=False):
    """One pod, 1 event/s. Refills every 6h top the pool up to 40000."""
    def line(t, msg):
        return f"{t.strftime('%Y-%m-%dT%H:%M:%S.123456789Z')} {t.strftime('%Y-%m-%d %H:%M:%S')},123 - __main__ - INFO - {msg}"
    out = [line(T0 - timedelta(seconds=5), "Loaded 2 team Kafka mappings")]
    refills = [(0, 'winddown', ['Boston', 'NYC'], 40000),
               (6, 'baseline', [], 21600),
               (12, 'outbreak', ['Seattle'], 21600)]
    events = []
    for h, profile, affected, n in refills:
        t = T0 + timedelta(hours=h)
        shown = affected if affected else 'all (baseline)'
        events.append((t, line(t, f"[REFILL] Outbreak state updated: profile={profile}, intensity=0.40, affected_regions={shown}")))
        events.append((t, line(t, f"[REFILL] Generating {n} events...")))
        events.append((t, line(t, "[REFILL] Pool now has 40000/50000 events")))
    for k in range(100, 20 * 3600 + 1, 100):
        t = T0 + timedelta(seconds=k)
        count = k if not (restart and k > 36000) else k - 36000
        events.append((t, line(t, f"[EMIT] Produced {count} events (0 bad) → teams: [team01, team02]")))
    events.sort(key=lambda e: e[0])
    return [e[1] for e in events] + ["not a log line"]


def test_reconstruct_follows_the_pool_not_the_refill_time():
    lines = synthetic_log()
    rows, skipped = rs.reconstruct(lines, utc(2026, 11, 10, 2), utc(2026, 11, 10, 21))
    assert skipped == []
    # batch A holds events 1..40000 (to 12:43:40), B to 61600 (18:43:40), then C.
    # A refill-time rule would have flipped at the 08:30 half hour and the 14:30 one.
    assert rows == [
        {'from_time': '2026-11-10T02:30:00Z', 'profile': 'winddown', 'affected_regions': ['Boston', 'NYC']},
        {'from_time': '2026-11-10T13:30:00Z', 'profile': 'baseline', 'affected_regions': []},
        {'from_time': '2026-11-10T19:30:00Z', 'profile': 'outbreak', 'affected_regions': ['Seattle']},
    ]


def test_reconstruct_skips_hours_outside_the_log():
    rows, skipped = rs.reconstruct(synthetic_log(), utc(2026, 11, 10, 1), utc(2026, 11, 10, 23))
    assert [h for h, _ in skipped] == [utc(2026, 11, 10, 1), utc(2026, 11, 10, 22)]   # log spans 01:37 to 21:37


def test_reconstruct_accepts_plain_asctime_lines():
    lines = [l.split(' ', 1)[1] if l[:2] == '20' and 'Z ' in l[:32] else l for l in synthetic_log()]
    rows, _ = rs.reconstruct(lines, utc(2026, 11, 10, 2), utc(2026, 11, 10, 21))
    assert len(rows) == 3


def test_reconstruct_rejects_a_log_with_a_restart():
    with pytest.raises(ValueError):
        rs.reconstruct(synthetic_log(restart=True), utc(2026, 11, 10, 2), utc(2026, 11, 10, 21))


def test_reconstructed_list_feeds_labels_from(tmp_path, monkeypatch):
    monkeypatch.setattr(bf, 'REGIONS', ['Seattle', 'Boston', 'NYC'])
    log = tmp_path / 'pod.log'
    log.write_text('\n'.join(synthetic_log()))
    out = tmp_path / 'states.json'
    assert rs.main([str(log), '--start', '2026-11-10T02:00:00Z', '--end', '2026-11-10T21:00:00Z',
                    '--out', str(out)]) == 0
    rows = bf.load_state_list(str(out))
    prod = Prod()
    n, skipped, errors = bf.run_state_list('team01', utc(2026, 11, 10, 2), utc(2026, 11, 10, 21), rows,
                                           prod, utc(2026, 11, 11), out=lambda m: None)
    assert n == 19 * 3 and skipped == []
    by = {}
    for _, _, v in prod.sent:
        by.setdefault(v['hour'], {})[v['region']] = v['status']
    assert by['2026-11-10T12:00:00Z']['Boston'] == 'WATCH'      # still batch A
    assert by['2026-11-10T13:00:00Z'] == {'Seattle': 'LOW', 'Boston': 'LOW', 'NYC': 'LOW'}
    assert by['2026-11-10T19:00:00Z']['Seattle'] == 'ALERT'
