"""Scheduled false-alarm bursts and AI-sourced labels with staff corrections. Kafka is mocked."""
import json
import os
from collections import Counter
import sys
from datetime import datetime, timedelta, timezone

import pytest

os.environ['EVENT_RATE_PER_SEC'] = '1.5'
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import event_generator as eg  # noqa: E402

REGIONS = ['Boston', 'Cambridge', 'Somerville', 'Brookline', 'Newton']
TEAMS = ('team01', 'team02')


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


class Store:
    """In-memory staff topic: an append-only log of JSON records, replayed like KafkaStateStore.load."""

    def __init__(self, log=None):
        self.log = log if log is not None else []
        self.fail_writes = False

    def load(self):
        states, released, records = {}, set(), {}
        for raw in self.log:
            eg.KafkaStateStore._apply(raw, states, released, records)
        return states, released, records

    def _write(self, value):
        if self.fail_writes:
            return False
        self.log.append(json.dumps(value))
        return True

    def save_state(self, hour, profile, affected, recorded_at):
        return self._write({'kind': 'state', 'hour': eg.format_hour(hour), 'profile': profile,
                            'affected_regions': list(affected), 'recorded_at': eg.format_ts(recorded_at)})

    def save_released(self, team, hour, released_at, source=None):
        value = {'kind': 'released', 'team': team, 'hour': eg.format_hour(hour),
                 'released_at': eg.format_ts(released_at)}
        if source is not None:
            value['source'] = source
        return self._write(value)

    def save_record(self, value):
        return self._write(value)

    def records(self, kind):
        latest = {}
        for raw in self.log:
            rec = json.loads(raw)
            if rec['kind'] == kind:
                latest[eg.record_key(rec)] = rec
        return latest


def ai_cfg(start=utc(2026, 12, 3), **kw):
    kw.setdefault('seed', '551')
    return eg.AiLabelConfig(start, **kw)


def service(store, sent, ai=None, delay=12.0, teams=TEAMS):
    def send(team, msgs):
        sent.extend((team, v) for _, v in msgs)
        return True
    return eg.LabelService(store, REGIONS, delay, send, lambda: list(teams), ai=ai)


def schedule_state(t):
    """Emitted state at t: outbreak in Boston/Newton on the Monday/Thursday windows, else baseline."""
    active, profile, _, _ = eg.is_in_outbreak_window(t)
    return (profile, ['Boston', 'Newton']) if active else ('baseline', [])


def run(svc, start, end, step=timedelta(seconds=15)):
    t = start
    while t < end:
        profile, affected = schedule_state(t)
        svc.tick(t, profile, affected)
        t += step
    return t


# ---- off by default --------------------------------------------------------

def test_both_behaviors_off_by_default(monkeypatch):
    for name in ('FALSE_ALARM_START', 'AI_LABELS_START'):
        assert os.getenv(name) is None
    assert eg.FALSE_ALARM_START is None and eg.AI_LABELS_START is None
    assert eg.ai_config_from_env() is None and eg.false_alarm_config_from_env() is None
    monkeypatch.setattr(eg, 'TEAM_KAFKA_MAPPING', {})
    gen = eg.EventGenerator()
    assert gen.labels.ai is None and gen.false_alarms is None


def test_defaults_and_env_parsing(monkeypatch):
    assert (eg.CR_SEED, eg.FALSE_ALARM_PROBABILITY, eg.FALSE_ALARM_MINUTES, eg.FALSE_ALARM_EVENTS_PER_MINUTE,
            eg.AI_LABEL_DELAY_HOURS, eg.AI_LABEL_ERROR_RATE, eg.STAFF_CORRECTION_DELAY_HOURS) == \
        ('551', 0.04, 15.0, 6.0, 1.0, 0.12, 18.0)
    monkeypatch.setenv('X_START', '2026-12-03T00:00:00Z')
    assert eg._env_time('X_START') == utc(2026, 12, 3)
    monkeypatch.setenv('X_START', '2026-12-03T05:00:00-05:00')
    assert eg._env_time('X_START') == utc(2026, 12, 3, 10)
    monkeypatch.setattr(eg, 'CONFIG_ERRORS', [])
    monkeypatch.setenv('X_START', 'soon')
    assert eg._env_time('X_START') is None and len(eg.CONFIG_ERRORS) == 1
    monkeypatch.setenv('X_RATE', '1.5')
    assert eg._env_prob('X_RATE', 0.12) == 0.12


def test_no_source_without_ai():
    sent = []
    svc = service(None, sent)
    run(svc, utc(2026, 11, 30, 0), utc(2026, 12, 1, 12))
    assert sent and all('source' not in v for _, v in sent)


# ---- AI labels ---------------------------------------------------------------

def test_ai_status_deterministic_and_rate_within_tolerance():
    hours = [utc(2026, 12, 3) + i * eg.HOUR for i in range(24 * 200)]
    wrong = 0
    total = 0
    for h in hours:
        for r in REGIONS:
            true = 'ALERT' if r == 'Boston' else 'LOW'
            a = eg.ai_status('551', h, r, true, 0.12)
            assert a == eg.ai_status('551', h, r, true, 0.12)
            assert a in eg.STATUSES
            wrong += a != true
            total += 1
    assert abs(wrong / total - 0.12) < 0.01            # 24,000 draws: 3 sigma is about 0.0063
    assert eg.ai_status('551', hours[0], 'Boston', 'LOW', 0.0) == 'LOW'
    assert eg.ai_status('551', hours[0], 'Boston', 'LOW', 1.0) != 'LOW'
    # a higher rate only adds errors
    for h in hours[:500]:
        if eg.ai_status('551', h, 'Newton', 'LOW', 0.12) != 'LOW':
            assert eg.ai_status('551', h, 'Newton', 'LOW', 0.3) != 'LOW'


def released_rows(sent):
    rows = {}
    for team, v in sent:
        rows.setdefault((team, v['region'], v['hour']), []).append(v)
    return rows


def test_every_wrong_ai_status_gets_exactly_one_correction():
    sent = []
    start = utc(2026, 12, 3)
    svc = service(None, sent, ai=ai_cfg(start, delay_hours=1, error_rate=0.12, correction_delay_hours=18))
    run(svc, utc(2026, 12, 2, 12), utc(2026, 12, 6, 12))
    rows = released_rows(sent)
    n_ai = n_wrong = 0
    for (team, region, hour), vs in rows.items():
        h = eg.parse_hour(hour)
        if h < start:
            continue
        true = svc.ai_decisions(h)[region][0]
        ai = [v for v in vs if v['source'] == 'ai']
        staff = [v for v in vs if v['source'] == 'staff']
        assert len(ai) == 1
        n_ai += 1
        # released AI_LABEL_DELAY_HOURS after the hour ends (to the 15 s tick)
        assert timedelta(0) <= eg.parse_ts(ai[0]['released_at']) - (h + eg.HOUR + timedelta(hours=1)) \
            < timedelta(seconds=15)
        if ai[0]['status'] == true:
            assert staff == []
            continue
        if h + timedelta(hours=2 + 18, seconds=30) > utc(2026, 12, 6, 12):
            assert staff == []                                   # correction not due yet
            continue
        n_wrong += 1
        assert len(staff) == 1 and staff[0]['status'] == true
        gap = eg.parse_ts(staff[0]['released_at']) - eg.parse_ts(ai[0]['released_at'])
        assert timedelta(hours=18) <= gap < timedelta(hours=18, seconds=15)
        latest = max(vs, key=lambda v: v['released_at'])
        assert latest['status'] == true
    assert n_ai > 500 and 0.05 < n_wrong / n_ai < 0.2


def test_source_absent_before_start_and_present_after():
    sent = []
    start = utc(2026, 12, 3)
    svc = service(None, sent, ai=ai_cfg(start, correction_delay_hours=2), delay=12)
    run(svc, utc(2026, 12, 1, 0), utc(2026, 12, 4, 0))
    before = [v for _, v in sent if eg.parse_ts(v['released_at']) < start]
    after = [v for _, v in sent if eg.parse_ts(v['released_at']) >= start]
    assert before and after
    assert all('source' not in v for v in before)
    assert all(v['source'] in ('ai', 'staff') for v in after)
    # hours before the start that are released after it come through the old path as staff, true status
    legacy = [v for v in after if eg.parse_hour(v['hour']) < start]
    assert legacy and all(v['source'] == 'staff' for v in legacy)
    for v in legacy:
        p, a = svc.states[eg.parse_hour(v['hour'])]
        assert v['status'] == eg.label_status(p, a, v['region'])
    # AI hours use the AI delay, not LABEL_DELAY_HOURS
    assert any(v['source'] == 'ai' and v['hour'] == '2026-12-03T00:00:00Z' for v in after)


def test_ai_label_records_for_grading():
    store = Store()
    sent = []
    svc = service(store, sent, ai=ai_cfg(correction_delay_hours=2), teams=('team01',))
    run(svc, utc(2026, 12, 3), utc(2026, 12, 3, 12))
    recs = store.records('ai_label')
    assert len(recs) == len(REGIONS) * 10             # AI out for hours 00..09 (hour end + 1 h)
    for rec in recs.values():
        assert rec['wrong'] == (rec['true_status'] != rec['ai_status'])
        assert set(rec['ai_released_at']) == {'team01'}
        if rec['wrong'] and rec['hour'] <= '2026-12-03T08:00:00Z':   # correction due (+ 2 h)
            assert set(rec['correction_released_at']) == {'team01'}
            assert rec['correction_released_at']['team01'] > rec['ai_released_at']['team01']
        elif not rec['wrong']:
            assert rec['correction_released_at'] == {}


def test_restart_keeps_pending_corrections_and_does_not_duplicate():
    cfg = ai_cfg(correction_delay_hours=18)
    store = Store()
    sent1 = []
    s1 = service(store, sent1, ai=cfg)
    mid = run(s1, utc(2026, 12, 3), utc(2026, 12, 3, 20))   # AI out for hours 0..18, no correction yet
    assert not any(v['source'] == 'staff' for _, v in sent1)
    wrong_before = {(t, v['region'], v['hour']) for t, v in sent1
                    if v['status'] != s1.ai_decisions(eg.parse_hour(v['hour']))[v['region']][0]}
    assert wrong_before

    # restart 1, with a different error rate: decisions are read back, not drawn again
    sent2 = []
    s2 = service(store, sent2, ai=ai_cfg(correction_delay_hours=18, error_rate=0.9))
    assert s2.ensure_loaded()
    mid = run(s2, mid, utc(2026, 12, 4, 16))
    corr2 = [(t, v['region'], v['hour']) for t, v in sent2 if v['source'] == 'staff']
    assert len(corr2) == len(set(corr2))
    ai2 = [v for _, v in sent2 if v['source'] == 'ai']
    assert not any(v['hour'] < '2026-12-03T18:00:00Z' for v in ai2)     # hours 0..17 were out before
    assert wrong_before <= set(corr2)                                     # none lost

    # restart 2: nothing already corrected is sent again
    sent3 = []
    s3 = service(store, sent3, ai=cfg)
    assert s3.ensure_loaded()
    run(s3, mid, utc(2026, 12, 5, 16))
    corr3 = [(t, v['region'], v['hour']) for t, v in sent3 if v['source'] == 'staff']
    assert not set(corr2) & set(corr3)
    allcorr = corr2 + corr3 + [(t, v['region'], v['hour']) for t, v in sent1 if v['source'] == 'staff']
    assert len(allcorr) == len(set(allcorr))
    # every wrong AI status across the three runs got its correction exactly once
    ai_all = [(t, v) for t, v in sent1 + sent2 + sent3 if v['source'] == 'ai']
    wrong_all = {(t, v['region'], v['hour']) for t, v in ai_all
                 if v['status'] != s3.ai_decisions(eg.parse_hour(v['hour']))[v['region']][0]
                 and v['hour'] <= '2026-12-04T20:00:00Z'}
    assert wrong_all <= set(allcorr)


def test_correction_written_before_restart_window_closes():
    """The corrected marker is written as soon as the send is acknowledged."""
    store = Store()
    s = service(store, [], ai=ai_cfg(correction_delay_hours=0.5), teams=('team01',))
    run(s, utc(2026, 12, 3), utc(2026, 12, 3, 6))
    markers = store.records('corrected')
    assert markers
    for m in markers.values():
        ai_label = store.records('ai_label')
        wrong = [r for r in REGIONS if ai_label[f"ai_label|{m['hour']}|{r}"]['wrong']]
        assert m['regions'] == wrong


def test_ai_labels_deterministic_from_seed():
    def stream(seed):
        sent = []
        svc = service(None, sent, ai=ai_cfg(seed=seed, correction_delay_hours=3))
        run(svc, utc(2026, 12, 3), utc(2026, 12, 4, 6))
        return sent
    a, b, c = stream('551'), stream('551'), stream('552')
    assert a == b
    assert a != c


# ---- false alarms ------------------------------------------------------------

def make_gen(monkeypatch):
    monkeypatch.setattr(eg, 'TEAM_KAFKA_MAPPING', {})
    monkeypatch.setattr(eg, 'BAD_EVENT_RATE', 0.0)

    class Prod:
        def __init__(self):
            self.sent = []

        def send(self, topic, value=None, key=None):
            self.sent.append((topic, value))

    gen = eg.EventGenerator()
    gen.producers = {'team01': Prod()}
    gen.labels.teams_fn = lambda: []        # this test looks at raw events only
    return gen


def fa_cfg(**kw):
    kw.setdefault('start', utc(2026, 11, 30))
    kw.setdefault('probability', 0.5)
    kw.setdefault('minutes', 20)
    kw.setdefault('events_per_minute', 6)
    kw.setdefault('seed', '551')
    return eg.FalseAlarmConfig(**kw)


def drive_bursts(gen, fa, start, end, step=timedelta(seconds=5)):
    t = start
    while t < end:
        st = schedule_state(t)
        gen.emitted_state = (st[0], tuple(st[1]))
        gen.labels.tick(t, st[0], st[1])
        fa.step(t, gen.emitted_state)
        t += step
    return t


def drive(labels, fa, start, end, state=('baseline', ()), step=timedelta(seconds=5)):
    """Label tick then burst step every `step`, with a fixed emitted state."""
    t = start
    while t < end:
        labels.tick(t, state[0], list(state[1]))
        fa.step(t, state)
        t += step
    return t


def test_bursts_never_change_state_or_labels(monkeypatch):
    gen = make_gen(monkeypatch)
    gen.state.profile, gen.state.affected_regions, gen.state.symptom_burden = 'outbreak', ['Boston'], 12.5
    gen._fill_pool(10)
    before_state = (gen.state.profile, list(gen.state.affected_regions), gen.state.symptom_burden,
                    gen.event_pool.qsize())
    fa = eg.FalseAlarmService(fa_cfg(), REGIONS, gen.labels, gen.send_false_alarm_event)
    gen.false_alarms = fa
    start, end = utc(2026, 11, 30), utc(2026, 12, 1, 12)   # Mon 00:00 to Tue 12:00: baseline, outbreak, winddown

    # the same run with no bursts gives the reference labels
    ref = service(None, [], teams=())
    run(ref, start, end, step=timedelta(seconds=5))

    drive_bursts(gen, fa, start, end)
    events = [v for _, v in gen.producers['team01'].sent]
    assert len(events) > 100
    assert (gen.state.profile, list(gen.state.affected_regions), gen.state.symptom_burden,
            gen.event_pool.qsize()) == before_state
    assert gen.labels.states == ref.states                    # labels unchanged
    bursts = [r for r in gen.labels.records_of_kind('false_alarm') if r['status'] == 'ended']
    assert bursts and sum(b['events_added'] for b in bursts) == len(events)
    assert any(ref.states[eg.hour_floor(eg.parse_ts(b['start']))][0] == 'outbreak' for b in bursts)
    for ev in events:
        assert ev['event_type'] == 'symptom_report' and ev['source'] == 'event-generator'
    for b in bursts:
        s, e = eg.parse_ts(b['start']), eg.parse_ts(b['end'])
        hour = eg.hour_floor(s)
        # inside one hour, after its HH:30 state, and the hour's recorded label is LOW for the region
        assert hour + timedelta(minutes=31) <= s and e < hour + eg.HOUR
        assert eg.parse_ts(b['planned_end']) <= hour + eg.HOUR
        p, a = ref.states[hour]
        assert eg.label_status(p, a, b['region']) == 'LOW'
        assert timedelta(minutes=15) - timedelta(seconds=10) <= e - s <= timedelta(minutes=20)
        assert abs(b['events_added'] - 6 * (e - s).total_seconds() / 60) <= 2
    # at a probability of 0.5 about half the hours have a plan, and every plan has a record
    hours = (end - start) / eg.HOUR
    planned = sum(1 for i in range(int(hours)) if eg.false_alarm_plan('551', start + i * eg.HOUR, 0.5))
    assert 0.35 * hours < planned < 0.65 * hours
    assert len(gen.labels.records_of_kind('false_alarm')) == planned


def test_burst_is_capped_at_the_top_of_the_hour():
    labels = service(None, [], teams=())
    fa = eg.FalseAlarmService(fa_cfg(probability=1.0, minutes=50), REGIONS, labels, lambda r: True)
    hour = utc(2026, 11, 30, 3)
    plan_start, _ = eg.false_alarm_plan('551', hour, 1.0)
    drive(labels, fa, hour, hour + timedelta(hours=1, minutes=5))
    rec = labels.get_record(f"false_alarm|{eg.format_ts(plan_start)}")
    assert rec['planned_end'] == eg.format_ts(hour + eg.HOUR)
    assert rec['status'] == 'ended' and rec['end_reason'] == 'planned'
    assert eg.parse_ts(rec['end']) < hour + eg.HOUR
    assert eg.parse_ts(rec['last_event_at']) < hour + eg.HOUR


def test_burst_region_is_low_and_ends_when_status_changes():
    labels = service(None, [], teams=())
    sent = []
    fa = eg.FalseAlarmService(fa_cfg(probability=1.0, minutes=15), REGIONS, labels,
                              lambda r: sent.append(r) or True)
    hour = utc(2026, 11, 30, 3)
    plan_start, _ = eg.false_alarm_plan('551', hour, 1.0)
    # the hour's recorded state: Boston affected; the emitted state at the start: four regions affected
    labels.record(hour + timedelta(minutes=30), 'outbreak', ['Boston'])
    state = ('outbreak', ('Boston', 'Cambridge', 'Somerville', 'Brookline'))
    fa.step(plan_start, state)
    assert fa.active['region'] == 'Newton'                   # the only region LOW in both
    for i in range(5, 65, 5):
        fa.step(plan_start + timedelta(seconds=i), state)
    assert set(sent) == {'Newton'} and len(sent) == 7          # 6 per minute, from second 0 to 60
    fa.step(plan_start + timedelta(minutes=2), ('outbreak', ('Newton',)))   # Newton is no longer LOW
    assert fa.active is None
    rec = labels.get_record(f"false_alarm|{eg.format_ts(plan_start)}")
    assert rec['status'] == 'ended' and rec['end_reason'] == 'status_changed' and rec['events_added'] == 7

    # a region LOW in the emitted state but not in the hour's recorded label is never chosen
    h2 = hour + eg.HOUR
    plan2, _ = eg.false_alarm_plan('551', h2, 1.0)
    labels.record(h2 + timedelta(minutes=30), 'outbreak', list(REGIONS))
    fa.step(plan2, ('baseline', ()))
    rec2 = labels.get_record(f"false_alarm|{eg.format_ts(plan2)}")
    assert rec2['status'] == 'skipped' and rec2['end_reason'] == 'no_low_region' and len(sent) == 7

    # no recorded state for the hour: skipped
    h3 = hour + 2 * eg.HOUR
    plan3, _ = eg.false_alarm_plan('551', h3, 1.0)
    fa.step(plan3, ('baseline', ()))
    assert labels.get_record(f"false_alarm|{eg.format_ts(plan3)}")['end_reason'] == 'no_hour_state'


def test_bursts_off_before_start():
    labels = service(None, [], teams=())
    sent = []
    fa = eg.FalseAlarmService(fa_cfg(start=utc(2026, 12, 1), probability=1.0), REGIONS, labels,
                              lambda r: sent.append(r) or True)
    drive(labels, fa, utc(2026, 11, 30), utc(2026, 12, 1), step=timedelta(seconds=30))
    assert sent == [] and labels.records == {}


def test_burst_events_use_the_baseline_mix(monkeypatch):
    gen = make_gen(monkeypatch)
    gen.state.profile, gen.state.affected_regions = 'outbreak', ['Boston', 'Cambridge']
    for _ in range(3000):
        gen.send_false_alarm_event('Newton')
    events = [v for _, v in gen.producers['team01'].sent]
    assert {e['region'] for e in events} == {'Newton'}
    sev = Counter(e['severity'] for e in events)
    # baseline weights 60/30/10; outbreak would be 20/40/40
    assert abs(sev['mild'] / 3000 - 0.6) < 0.04 and abs(sev['severe'] / 3000 - 0.1) < 0.03
    durations = [e['duration_days'] for e in events if e['severity'] == 'mild']
    assert max(durations) <= 5


def burst_then_restart(store, cfg, minutes_run):
    """Run a burst for minutes_run minutes; return (plan_start, region, events sent)."""
    sent = []
    l1 = service(store, [], teams=())
    fa1 = eg.FalseAlarmService(cfg, REGIONS, l1, lambda r: sent.append(r) or True, checkpoint_seconds=60)
    hour = utc(2026, 11, 30, 2)
    plan_start, _ = eg.false_alarm_plan(cfg.seed, hour, 1.0)
    drive(l1, fa1, hour + timedelta(minutes=29), plan_start + timedelta(minutes=minutes_run, seconds=1))
    return plan_start, fa1.active['region'], sent


def test_burst_restart_resumes_in_same_region_with_count():
    store = Store()
    cfg = fa_cfg(probability=1.0, minutes=12)
    plan_start, region, sent = burst_then_restart(store, cfg, 5)
    n_before = len(sent)
    rec = store.records('false_alarm')[f"false_alarm|{eg.format_ts(plan_start)}"]
    assert rec['status'] == 'running' and rec['events_added'] >= n_before - 6   # flushed at start and each minute

    l2 = service(store, [], teams=())
    assert l2.ensure_loaded()
    fa2 = eg.FalseAlarmService(cfg, REGIONS, l2, lambda r: sent.append(r) or True)
    drive(l2, fa2, plan_start + timedelta(minutes=6), plan_start + timedelta(minutes=13))
    rec = store.records('false_alarm')[f"false_alarm|{eg.format_ts(plan_start)}"]
    assert rec['status'] == 'ended' and rec['end_reason'] == 'planned'
    assert rec['region'] == region and set(sent) == {region}
    assert len(sent) - 6 <= rec['events_added'] <= len(sent)   # at most one checkpoint interval lost


@pytest.mark.parametrize("change", [
    {'seed': '552'},                # another plan
    {'probability': 0.0},           # no plan
    {'minutes': 2},                 # the plan's window has closed
    {'start': utc(2026, 12, 15)},   # start moved later
])
def test_restart_with_changed_config_closes_running_burst(change):
    store = Store()
    cfg = fa_cfg(probability=1.0, minutes=12)
    plan_start, region, _ = burst_then_restart(store, cfg, 4)
    key = f"false_alarm|{eg.format_ts(plan_start)}"
    assert store.records('false_alarm')[key]['status'] == 'running'

    l2 = service(store, [], teams=())
    assert l2.ensure_loaded()
    cfg2 = fa_cfg(**{**dict(probability=1.0, minutes=12), **change})
    fa2 = eg.FalseAlarmService(cfg2, REGIONS, l2, lambda r: True)
    t = plan_start + timedelta(minutes=5)
    l2.tick(t, 'baseline', [])
    fa2.step(t, ('baseline', ()))
    rec = store.records('false_alarm')[key]
    assert rec['status'] == 'ended' and rec['end_reason'] == 'interrupted' and rec['region'] == region
    assert not [r for r in store.records('false_alarm').values() if r['status'] == 'running' and
                r['planned_start'] == eg.format_ts(plan_start)]


def test_burst_closed_when_window_ended_during_downtime():
    store = Store()
    cfg = fa_cfg(probability=1.0, minutes=12)
    plan_start, _, _ = burst_then_restart(store, cfg, 2)
    l2 = service(store, [], teams=())
    l2.ensure_loaded()
    fa2 = eg.FalseAlarmService(cfg, REGIONS, l2, lambda r: True)
    t = plan_start + timedelta(hours=2)
    l2.tick(t, 'baseline', [])
    fa2.step(t, ('baseline', ()))
    rec = store.records('false_alarm')[f"false_alarm|{eg.format_ts(plan_start)}"]
    assert rec['status'] == 'ended' and rec['end_reason'] == 'interrupted'


def test_false_alarm_plan_deterministic():
    hours = [utc(2026, 11, 30) + i * eg.HOUR for i in range(500)]
    a = [eg.false_alarm_plan('551', h, 0.04) for h in hours]
    assert a == [eg.false_alarm_plan('551', h, 0.04) for h in hours]
    assert a != [eg.false_alarm_plan('552', h, 0.04) for h in hours]
    starts = [p for p in a if p]
    assert 5 <= len(starts) <= 40                              # about 20 at 4 % per hour
    for h, p in zip(hours, a):
        if p:
            assert h + timedelta(minutes=31) <= p[0] < h + timedelta(minutes=45) and 0 <= p[1] < 1


# ---- corrections without the AI config, config errors -------------------------

@pytest.mark.parametrize("ai_after", [None, 'later'])
def test_corrections_continue_when_ai_start_removed_or_moved(ai_after):
    store = Store()
    sent1 = []
    s1 = service(store, sent1, ai=ai_cfg(correction_delay_hours=18))
    mid = run(s1, utc(2026, 12, 3), utc(2026, 12, 3, 20))
    wrong = {(t, v['region'], v['hour']) for t, v in sent1
             if v['status'] != s1.ai_decisions(eg.parse_hour(v['hour']))[v['region']][0]}
    assert wrong and set(s1.pending_corrections) == {(t, eg.parse_hour(v['hour'])) for t, v in sent1}
    ai2 = None if ai_after is None else ai_cfg(start=utc(2026, 12, 10), correction_delay_hours=18)
    sent2 = []
    s2 = service(store, sent2, ai=ai2)
    assert s2.ensure_loaded()
    assert set(s2.pending_corrections) == set(s1.pending_corrections)
    run(s2, mid, utc(2026, 12, 4, 16))
    corr = [(t, v['region'], v['hour']) for t, v in sent2 if v.get('source') == 'staff'
            and eg.parse_hour(v['hour']) < utc(2026, 12, 3, 19)]
    assert sorted(corr) == sorted(wrong)                      # each wrong AI status corrected exactly once
    assert not s2.pending_corrections                         # all done; nothing left to scan


def test_pending_corrections_are_cleared():
    sent = []
    s = service(None, sent, ai=ai_cfg(correction_delay_hours=1), teams=('team01',))
    run(s, utc(2026, 12, 3), utc(2026, 12, 3, 6))
    # hours 0..3 have AI out (hour end + 1 h) and their corrections (+1 h) are past: nothing pending
    assert all(h >= utc(2026, 12, 3, 3) for _, h in s.pending_corrections)


def test_unparseable_start_time_fails_loudly(monkeypatch):
    monkeypatch.setattr(eg, 'CONFIG_ERRORS', [])
    monkeypatch.setenv('AI_LABELS_START', '2026-12-3 midnight')
    assert eg._env_time('AI_LABELS_START') is None
    assert len(eg.CONFIG_ERRORS) == 1 and 'AI_LABELS_START' in eg.CONFIG_ERRORS[0]
    assert eg.main() == 2
    monkeypatch.setattr(eg, 'CONFIG_ERRORS', [])
    monkeypatch.setenv('FALSE_ALARM_START', 'soon')
    eg._env_time('FALSE_ALARM_START')
    assert 'FALSE_ALARM_START' in eg.CONFIG_ERRORS[0] and eg.main() == 2


def test_state_records_replay_with_new_kinds():
    states, released, records = {}, set(), {}
    raws = [
        {'kind': 'released', 'team': 'team01', 'hour': '2026-12-03T00:00:00Z', 'released_at': '2026-12-03T02:00:05Z',
         'source': 'ai'},
        {'kind': 'corrected', 'team': 'team01', 'hour': '2026-12-03T00:00:00Z', 'released_at': '2026-12-03T20:00:05Z',
         'regions': ['Boston']},
        {'kind': 'false_alarm', 'planned_start': '2026-11-30T03:10:00Z', 'planned_end': '2026-11-30T03:40:00Z',
         'status': 'ended'},
    ]
    for r in raws:
        eg.KafkaStateStore._apply(json.dumps(r), states, released, records)
    assert released == {('team01', utc(2026, 12, 3))}
    assert set(records) == {'released|team01|2026-12-03T00:00:00Z', 'corrected|team01|2026-12-03T00:00:00Z',
                            'false_alarm|2026-11-30T03:10:00Z'}
    svc = eg.LabelService(Store([json.dumps(r) for r in raws]), REGIONS, 12, lambda t, m: True, lambda: [],
                          ai=ai_cfg())
    assert svc.ensure_loaded()
    assert svc.released_meta[('team01', utc(2026, 12, 3))] == (utc(2026, 12, 3, 2, 0, 5), 'ai')
    assert ('team01', utc(2026, 12, 3)) in svc.corrected


def test_kafka_state_store_load_returns_other_records(monkeypatch):
    """load() against a fake consumer: every kind is replayed (the poll loop must not shadow the result)."""
    raws = [
        {'kind': 'state', 'hour': '2026-12-03T00:00:00Z', 'profile': 'baseline', 'affected_regions': []},
        {'kind': 'released', 'team': 'team12', 'hour': '2026-12-03T00:00:00Z', 'released_at': '2026-12-03T01:03:05Z',
         'source': 'ai'},
        {'kind': 'ai_label', 'hour': '2026-12-03T00:00:00Z', 'region': 'Boston', 'true_status': 'LOW',
         'ai_status': 'WATCH', 'wrong': True, 'ai_released_at': {}, 'correction_released_at': {}},
    ]

    class Rec:
        def __init__(self, v):
            self.value = json.dumps(v).encode()

    class Consumer:
        def __init__(self, *a, **k):
            self.pos = 0

        def partitions_for_topic(self, topic):
            return {0}

        def assign(self, tps):
            pass

        def seek_to_beginning(self, *tps):
            pass

        def end_offsets(self, tps):
            return {tp: len(raws) for tp in tps}

        def position(self, tp):
            return self.pos

        def poll(self, timeout_ms=None):
            self.pos = len(raws)
            return {'tp': [Rec(r) for r in raws]}

        def close(self):
            pass

    monkeypatch.setattr(eg, 'KafkaConsumer', Consumer)
    states, released, records = eg.KafkaStateStore('b:9092', 'staff.outbreak_state', lambda: None).load()
    assert states == {utc(2026, 12, 3): ('baseline', ())}
    assert released == {('team12', utc(2026, 12, 3))}
    assert set(records) == {'released|team12|2026-12-03T00:00:00Z', 'ai_label|2026-12-03T00:00:00Z|Boston'}
