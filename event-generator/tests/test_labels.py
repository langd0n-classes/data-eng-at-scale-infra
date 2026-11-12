"""Tests for the outbreak labels and the history backfill. Kafka is mocked."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

# 1.5 events/s gives about 2900 events per hour averaged over a week.
os.environ['EVENT_RATE_PER_SEC'] = '1.5'
os.environ['REGIONS'] = 'Boston,Cambridge,Somerville,Brookline,Newton'
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import event_generator as eg  # noqa: E402
import backfill_history as bf  # noqa: E402

REGIONS = ['Boston', 'Cambridge', 'Somerville', 'Brookline', 'Newton']


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


class FakeStore:
    """In-memory stand-in for KafkaStateStore. `log` plays the role of the staff topic."""

    def __init__(self, log=None):
        self.log = log if log is not None else []
        self.fail_writes = False
        self.fail_load = False

    def load(self):
        if self.fail_load:
            raise RuntimeError("kafka down")
        states, released = {}, set()
        for rec in self.log:
            eg.KafkaStateStore._apply(json.dumps(rec), states, released)
        return states, released

    def save_state(self, hour, profile, affected, recorded_at):
        if self.fail_writes:
            return False
        self.log.append({'kind': 'state', 'hour': eg.format_hour(hour), 'profile': profile,
                         'affected_regions': list(affected)})
        return True

    def save_released(self, team, hour, released_at):
        if self.fail_writes:
            return False
        self.log.append({'kind': 'released', 'team': team, 'hour': eg.format_hour(hour)})
        return True


def make_service(store, sent, delay=12.0, teams=('team01', 'team02')):
    def send(team, msgs):
        sent.append((team, msgs))
        return True
    return eg.LabelService(store, REGIONS, delay, send, lambda: list(teams))


# ---- label rule -----------------------------------------------------------

@pytest.mark.parametrize("profile,region,expected", [
    ('outbreak', 'Boston', 'ALERT'),
    ('outbreak', 'Newton', 'LOW'),       # outbreak, region not affected
    ('winddown', 'Boston', 'WATCH'),
    ('winddown', 'Newton', 'LOW'),
    ('baseline', 'Boston', 'LOW'),       # baseline never alerts, even if a region is listed
])
def test_label_rule(profile, region, expected):
    assert eg.label_status(profile, ['Boston', 'Cambridge'], region) == expected


def test_label_rule_empty_affected():
    assert eg.label_status('outbreak', [], 'Boston') == 'LOW'


def test_label_message_shape():
    msgs = eg.build_labels(utc(2026, 11, 9, 14, 0), 'outbreak', ['Boston'], REGIONS, utc(2026, 11, 10, 3, 0, 7))
    assert [k for k, _ in msgs] == REGIONS
    key, value = msgs[0]
    assert value == {'region': 'Boston', 'hour': '2026-11-09T14:00:00Z', 'status': 'ALERT',
                     'released_at': '2026-11-10T03:00:07Z'}
    assert key == value['region']
    assert [v['status'] for _, v in msgs[1:]] == ['LOW'] * 4


def test_labels_never_carry_bad_events(monkeypatch):
    monkeypatch.setattr(eg, 'BAD_EVENT_RATE', 1.0)
    monkeypatch.setattr(eg, 'make_bad_event', lambda e: pytest.fail("labels must not be corrupted"))
    sent = []
    svc = make_service(None, sent)
    svc.record(utc(2026, 11, 9, 14, 30), 'outbreak', ['Boston'])
    svc.release_due(utc(2026, 11, 10, 3, 0))
    for _, msgs in sent:
        for _, v in msgs:
            assert set(v) == {'region', 'hour', 'status', 'released_at'}
            assert v['status'] in ('LOW', 'WATCH', 'ALERT')


# ---- hour boundary --------------------------------------------------------

def test_state_is_recorded_at_the_half_hour_and_not_changed():
    svc = make_service(None, [])
    assert not svc.record(utc(2026, 11, 9, 14, 29, 59), 'baseline', [])
    assert svc.record(utc(2026, 11, 9, 14, 30, 0), 'outbreak', ['Boston'])
    # the state flips later in the hour: the half-hour state stays
    assert not svc.record(utc(2026, 11, 9, 14, 45), 'winddown', ['Boston'])
    assert svc.states[utc(2026, 11, 9, 14)] == ('outbreak', ('Boston',))


def test_state_flip_before_half_hour_uses_the_new_state():
    svc = make_service(None, [])
    svc.record(utc(2026, 11, 9, 9, 20), 'baseline', [])  # too early, ignored
    svc.record(utc(2026, 11, 9, 9, 31), 'outbreak', ['Boston'])
    assert svc.states[utc(2026, 11, 9, 9)][0] == 'outbreak'


def test_hour_start_belongs_to_the_new_hour():
    svc = make_service(None, [])
    svc.record(utc(2026, 11, 9, 14, 59, 59), 'outbreak', ['Boston'])
    assert utc(2026, 11, 9, 14) in svc.states
    assert not svc.record(utc(2026, 11, 9, 15, 0, 0), 'outbreak', ['Boston'])  # 15:00 is before 15:30
    assert utc(2026, 11, 9, 15) not in svc.states


# ---- release timing -------------------------------------------------------

def test_release_after_delay_only():
    sent = []
    svc = make_service(None, sent, delay=12)
    svc.record(utc(2026, 11, 9, 14, 30), 'outbreak', ['Boston'])
    # hour ends 15:00; release at 03:00 next day
    assert svc.release_due(utc(2026, 11, 10, 2, 59, 59)) == 0
    assert sent == []
    assert svc.release_due(utc(2026, 11, 10, 3, 0, 0)) == 10  # 5 regions x 2 teams
    assert sorted(t for t, _ in sent) == ['team01', 'team02']


def test_no_duplicate_release():
    sent = []
    svc = make_service(None, sent)
    svc.record(utc(2026, 11, 9, 14, 30), 'outbreak', ['Boston'])
    svc.release_due(utc(2026, 11, 10, 4))
    assert svc.release_due(utc(2026, 11, 10, 5)) == 0
    assert len(sent) == 2


def test_failed_send_is_retried_and_other_team_not_repeated():
    sent = []
    fail = {'team02': True}

    def send(team, msgs):
        if fail.get(team):
            return False
        sent.append(team)
        return True
    svc = eg.LabelService(None, REGIONS, 12, send, lambda: ['team01', 'team02'])
    svc.record(utc(2026, 11, 9, 14, 30), 'outbreak', ['Boston'])
    svc.release_due(utc(2026, 11, 10, 4))
    fail['team02'] = False
    svc.release_due(utc(2026, 11, 10, 4, 1))
    assert sorted(sent) == ['team01', 'team02']


def test_zero_delay():
    sent = []
    svc = make_service(None, sent, delay=0)
    svc.record(utc(2026, 11, 9, 14, 30), 'baseline', [])
    assert svc.release_due(utc(2026, 11, 9, 14, 59)) == 0
    assert svc.release_due(utc(2026, 11, 9, 15, 0)) == 10


# ---- restart recovery -----------------------------------------------------

def test_restart_keeps_pending_and_does_not_rerelease():
    log = []
    sent1 = []
    s1 = make_service(FakeStore(log), sent1, delay=12)
    s1.tick(utc(2026, 11, 9, 14, 30), 'outbreak', ['Boston', 'Newton'])   # records hour 14
    s1.tick(utc(2026, 11, 9, 15, 30), 'outbreak', ['Boston', 'Newton'])   # records hour 15
    s1.tick(utc(2026, 11, 10, 3, 0), 'baseline', [])                       # releases hour 14 only
    assert {t for t, _ in sent1} == {'team01', 'team02'}
    assert [m[0][1]['hour'] for _, m in sent1] == ['2026-11-09T14:00:00Z'] * 2

    # pod restarts: a new service replays the same topic
    sent2 = []
    s2 = make_service(FakeStore(log), sent2, delay=12)
    assert s2.ensure_loaded()
    s2.tick(utc(2026, 11, 10, 3, 1), 'baseline', [])                       # hour 14: already released
    assert sent2 == []
    s2.tick(utc(2026, 11, 10, 4, 0), 'baseline', [])                       # hour 15 now due, from the log
    hours = {m[0][1]['hour'] for _, m in sent2}
    assert hours == {'2026-11-09T15:00:00Z'}
    assert {m[0][1]['status'] for _, m in sent2} == {'ALERT'}              # state from before the restart


def test_restart_does_not_overwrite_recorded_hour():
    log = []
    s1 = make_service(FakeStore(log), [])
    s1.tick(utc(2026, 11, 9, 14, 30), 'outbreak', ['Boston'])
    s2 = make_service(FakeStore(log), [])
    s2.ensure_loaded()
    assert not s2.record(utc(2026, 11, 9, 14, 50), 'winddown', [])
    assert s2.states[utc(2026, 11, 9, 14)] == ('outbreak', ('Boston',))


def test_nothing_released_until_state_loaded():
    sent = []
    store = FakeStore([])
    store.fail_load = True
    svc = make_service(store, sent, delay=0)
    svc.tick(utc(2026, 11, 9, 14, 30), 'outbreak', ['Boston'])
    assert svc.tick(utc(2026, 11, 9, 15, 5), 'outbreak', ['Boston']) == 0
    assert sent == []
    store.fail_load = False
    assert svc.tick(utc(2026, 11, 9, 15, 6), 'outbreak', ['Boston']) > 0
    # the hour recorded while the topic was unreadable is now persisted
    assert any(r['kind'] == 'state' and r['hour'] == '2026-11-09T14:00:00Z' for r in store.log)


def test_unsaved_markers_are_retried():
    store = FakeStore([])
    svc = make_service(store, [], delay=0)
    svc.tick(utc(2026, 11, 9, 14, 30), 'outbreak', ['Boston'])
    store.fail_writes = True
    svc.tick(utc(2026, 11, 9, 15, 1), 'outbreak', ['Boston'])
    assert [r['kind'] for r in store.log] == ['state']
    store.fail_writes = False
    svc.tick(utc(2026, 11, 9, 15, 2), 'outbreak', ['Boston'])
    kinds = sorted(r['kind'] for r in store.log)
    assert kinds == ['released', 'released', 'state']


def test_restorable_regions():
    log = []
    s1 = make_service(FakeStore(log), [])
    s1.tick(utc(2026, 11, 9, 14, 30), 'outbreak', ['Boston', 'Newton'])
    s2 = make_service(FakeStore(log), [])
    s2.ensure_loaded()
    assert s2.restorable_regions(utc(2026, 11, 9, 15, 10)) == ['Boston', 'Newton']
    assert s2.restorable_regions(utc(2026, 11, 9, 23, 0)) == []   # too old
    s3 = make_service(FakeStore([]), [])
    assert s3.restorable_regions(utc(2026, 11, 9, 15, 10)) == []


def test_state_store_replay_skips_bad_records():
    states, released = {}, set()
    for raw in [b'not json', json.dumps({'kind': 'state'}).encode(),
                json.dumps({'kind': 'state', 'hour': '2026-11-09T14:00:00Z', 'profile': 'winddown',
                            'affected_regions': ['Boston']}).encode(),
                json.dumps({'kind': 'released', 'team': 'team01', 'hour': '2026-11-09T14:00:00Z'}).encode()]:
        eg.KafkaStateStore._apply(raw, states, released)
    assert states == {utc(2026, 11, 9, 14): ('winddown', ('Boston',))}
    assert released == {('team01', utc(2026, 11, 9, 14))}


# ---- history simulation ---------------------------------------------------

START = utc(2026, 11, 2, 0)   # a Monday


def collect(start, end, seed):
    return [(h, p, a, ev) for h, p, a, ev in bf.simulate_history(start, end, seed)]


def test_history_is_deterministic():
    a = collect(START, START + timedelta(hours=36), 551)
    b = collect(START, START + timedelta(hours=36), 551)
    assert a == b
    assert collect(START, START + timedelta(hours=6), 552) != a[:6]


def test_history_events_in_range_and_clean():
    end = START + timedelta(hours=48)
    valid_types = {'symptom_report', 'clinic_visit', 'hospital_admission', 'vaccination',
                   'emergency_incident', 'general_health_report'}
    for hour, profile, affected, events in bf.simulate_history(START, end, 551):
        for ev in events:
            ts = datetime.fromisoformat(ev['timestamp'])
            assert hour <= ts < hour + eg.HOUR
            assert ev['event_type'] in valid_types
            assert ev['region'] in REGIONS and isinstance(ev['age'], int) and 0 < ev['age'] < 120
            assert ev['source'] == 'event-generator' and ev['schema_version'] == '1.0'
            json.dumps(ev)  # plain JSON, no corrupted bytes
        assert events == sorted(events, key=lambda e: e['timestamp'])


def test_history_profiles_follow_the_schedule():
    got = {h: p for h, p, _, _ in bf.simulate_history(START, START + timedelta(hours=48), 551)}
    assert got[utc(2026, 11, 2, 8)] == 'baseline'
    assert got[utc(2026, 11, 2, 9)] == 'outbreak'
    assert got[utc(2026, 11, 3, 8)] == 'outbreak'
    assert got[utc(2026, 11, 3, 9)] == 'winddown'
    assert got[utc(2026, 11, 3, 21)] == 'baseline'


def test_history_affected_regions_stable_within_window():
    affected = {}
    for hour, profile, aff, _ in bf.simulate_history(START, START + timedelta(hours=48), 551):
        if profile == 'baseline':
            assert aff == []
        else:
            affected.setdefault('w', aff)
            assert aff == affected['w'] and 2 <= len(aff) <= 4


def test_history_is_identical_for_every_team():
    """Same seed and range give the same stream whichever team it is written for."""
    sent = {}

    class P:
        def __init__(self, team):
            self.team = team

        def send(self, topic, value, key=None):
            sent.setdefault(self.team, []).append((topic.replace(self.team, 'T'), key, json.dumps(value, sort_keys=True)))

            class F:
                def add_errback(self, cb):
                    return self
            return F()

        def flush(self, timeout=None):
            pass

    for team in ('team01', 'team07'):
        bf.run(team, START, START + timedelta(hours=30), 551, P(team), 100, False, utc(2026, 11, 11), out=lambda m: None)
    assert sent['team01'] == sent['team07']
    topics = {t for t, _, _ in sent['team01']}
    assert topics == {'events.T.history', 'events.T.outbreak_status'}
    labels = [json.loads(v) for t, _, v in sent['team01'] if t.endswith('outbreak_status')]
    assert len(labels) == 30 * len(REGIONS)
    assert all(l['released_at'] == '2026-11-11T00:00:00Z' for l in labels)


def test_history_label_matches_rule():
    for hour, profile, affected, _ in bf.simulate_history(START, START + timedelta(hours=40), 551):
        for _, v in eg.build_labels(hour, profile, affected, REGIONS, utc(2026, 11, 11)):
            if profile == 'baseline' or v['region'] not in affected:
                assert v['status'] == 'LOW'
            else:
                assert v['status'] == ('ALERT' if profile == 'outbreak' else 'WATCH')


def test_history_rates_match_live_pacing():
    """Mean hourly count per profile is 3600 / mean live gap (rate-dependent)."""
    base = 1.0 / eg.EVENT_RATE_PER_SEC
    expect = {'outbreak': 3600 / base, 'winddown': 3600 / (base * 2.25), 'baseline': 3600 / (base * 3.0)}
    counts = {'outbreak': [], 'winddown': [], 'baseline': []}
    for _, p, _, ev in bf.simulate_history(START, START + timedelta(hours=48), 551):
        counts[p].append(len(ev))
    for p, c in counts.items():
        assert abs(sum(c) / len(c) - expect[p]) / expect[p] < 0.05


def test_parse_utc_and_cli_rejects_future_and_unaligned():
    assert bf.parse_utc('2026-11-09') == utc(2026, 11, 9)
    assert bf.parse_utc('2026-11-09T00:00:00Z') == utc(2026, 11, 9)
    with pytest.raises(SystemExit):
        bf.main(['--team', 'team01', '--end', '2026-11-09T00:30:00Z', '--dry-run'])
    with pytest.raises(SystemExit):
        bf.main(['--team', 'team01', '--end', '2099-01-01', '--dry-run'])
