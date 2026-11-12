"""Labels from a supplied state list, and a non-default REGIONS list."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

os.environ['EVENT_RATE_PER_SEC'] = '1.5'
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import event_generator as eg  # noqa: E402
import backfill_history as bf  # noqa: E402

LIVE_REGIONS = ['Seattle', 'Boston', 'NYC']


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def live_regions(monkeypatch):
    monkeypatch.setattr(bf, 'REGIONS', LIVE_REGIONS)
    monkeypatch.setattr(eg, 'REGIONS', LIVE_REGIONS)


class P:
    def __init__(self):
        self.sent = []

    def send(self, topic, value=None, key=None):
        self.sent.append((topic, key, value))

        class F:
            def add_errback(self, cb):
                return self
        return F()

    def flush(self, timeout=None):
        pass


JSON_ROWS = [
    {"from_time": "2026-11-10T01:37:00Z", "profile": "winddown", "affected_regions": ["Boston", "NYC"]},
    {"from_time": "2026-11-11T01:37:00Z", "profile": "baseline", "affected_regions": []},
]


def test_state_list_json_and_csv_agree(tmp_path):
    j = tmp_path / 's.json'
    j.write_text(json.dumps(JSON_ROWS))
    c = tmp_path / 's.csv'
    c.write_text("from_time,profile,affected_regions\n"
                 "2026-11-10T01:37:00Z,winddown,Boston;NYC\n"
                 "2026-11-11T01:37:00Z,baseline,\n")
    assert bf.load_state_list(str(j)) == bf.load_state_list(str(c))


def test_state_list_rejects_bad_rows(tmp_path):
    p = tmp_path / 's.json'
    p.write_text(json.dumps([{"from_time": "2026-11-10T01:37:00Z", "profile": "panic", "affected_regions": []}]))
    with pytest.raises(ValueError):
        bf.load_state_list(str(p))
    p.write_text(json.dumps([{"from_time": "2026-11-10T01:37:00Z", "profile": "outbreak",
                              "affected_regions": ["Newton"]}]))
    with pytest.raises(ValueError):   # Newton is not in the live REGIONS
        bf.load_state_list(str(p))


def test_labels_follow_supplied_state_not_the_schedule(tmp_path):
    # 2026-11-10 is a Tuesday: the schedule says winddown until 21:00, the supplied state
    # says the generator stayed in winddown until 2026-11-11 01:37.
    p = tmp_path / 's.json'
    p.write_text(json.dumps(JSON_ROWS))
    rows = bf.load_state_list(str(p))
    prod = P()
    n, skipped, errors = bf.run_state_list('team01', utc(2026, 11, 10, 2), utc(2026, 11, 11, 4), rows, prod,
                                           utc(2026, 11, 11, 12), out=lambda m: None)
    assert n == 26 * 3 and skipped == [] and errors == []
    by_hour = {}
    for topic, key, v in prod.sent:
        assert topic == 'events.team01.outbreak_status' and key == v['region'].encode()
        assert set(v) == {'region', 'hour', 'status', 'released_at'}
        assert v['released_at'] == '2026-11-11T12:00:00Z'
        by_hour.setdefault(v['hour'], {})[v['region']] = v['status']
    assert by_hour['2026-11-10T23:00:00Z'] == {'Seattle': 'LOW', 'Boston': 'WATCH', 'NYC': 'WATCH'}
    # 01:00 hour: half hour 01:30 is before the 01:37 change, so still winddown
    assert by_hour['2026-11-11T01:00:00Z']['Boston'] == 'WATCH'
    # 02:00 hour: baseline
    assert set(by_hour['2026-11-11T02:00:00Z'].values()) == {'LOW'}


def test_hours_before_first_row_are_skipped():
    rows = [(utc(2026, 11, 10, 1, 37), 'winddown', ('Boston',))]
    prod = P()
    n, skipped, _ = bf.run_state_list('team01', utc(2026, 11, 10, 0), utc(2026, 11, 10, 3), rows, prod,
                                      utc(2026, 11, 11), out=lambda m: None)
    assert skipped == [utc(2026, 11, 10, 0), utc(2026, 11, 10, 1)]   # 00:30 and 01:30 precede 01:37
    assert n == 1 * 3


def test_cli_labels_from_dry_run(tmp_path, capsys):
    p = tmp_path / 's.json'
    p.write_text(json.dumps(JSON_ROWS))
    assert bf.main(['--team', 'team01', '--labels-from', str(p), '--start', '2026-11-10T02:00:00Z',
                    '--end', '2026-11-10T05:00:00Z', '--dry-run']) == 0
    assert 'Dry run: 9 labels' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        bf.main(['--team', 'team01', '--labels-from', str(p), '--dry-run'])  # needs start and end


def test_live_labels_use_the_configured_regions():
    sent = []
    svc = eg.LabelService(None, eg.REGIONS, 12, lambda t, m: sent.append(m) or True, lambda: ['team01'])
    svc.record(utc(2026, 11, 9, 14, 30), 'outbreak', ['Seattle'])
    svc.release_due(utc(2026, 11, 10, 3))
    assert {v['region']: v['status'] for _, v in sent[0]} == {'Seattle': 'ALERT', 'Boston': 'LOW', 'NYC': 'LOW'}


def test_history_uses_the_configured_regions():
    for hour, profile, affected, events in bf.simulate_history(utc(2026, 11, 2, 8), utc(2026, 11, 2, 11), 551, LIVE_REGIONS):
        assert set(affected) <= set(LIVE_REGIONS)
        assert {e['region'] for e in events} <= set(LIVE_REGIONS)
