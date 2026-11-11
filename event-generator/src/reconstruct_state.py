#!/usr/bin/env python3
"""
Reconstruct the emitted-state timeline of an event-generator pod from its log.

Staff tool for one case: hours the live generator emitted before the label feature
existed, so no state was recorded. It needs only the Python standard library, so it
runs on a host as well as in the image.

The state of the events being emitted is not the generator's current state. Each
[REFILL] adds a batch of events generated under the state it logged
("Outbreak state updated: profile=..., affected_regions=[...]" then
"Generating N events..."). The pool is FIFO, so the k-th event emitted since the pod
started belongs to the batch that covers k when the batches are laid end to end.
The [EMIT] "Produced N events" lines (every 100 events, N cumulative since the pod
started) give N against time; linear interpolation gives the count at HH:30, and so
the batch, and so the state, of each hour (the live rule: state at the half hour).

Output is a state list for `backfill_history.py --labels-from`: one row per change,
from_time = the HH:30 of the first hour in the new state.

Usage:
  kubectl logs --timestamps deploy/event-generator -n infra > pod.log
  python3 reconstruct_state.py pod.log --start 2026-11-10T02:00:00Z --end 2026-11-11T14:00:00Z --out states.json

Limits:
  - The log must cover one pod from its start (the first [REFILL] line). A log in which
    the cumulative count goes down (a restart) is rejected. Events before the pod started
    are not covered.
  - Hours whose HH:30 is before the first [REFILL] line, or after the last [EMIT] line,
    are skipped and reported.
  - Timestamps come from `kubectl logs --timestamps` (UTC). Without it the container's
    asctime is read as UTC.
  - The count at HH:30 is interpolated between two [EMIT] lines, about 100 events apart
    (a minute or two at the live rate), so a state change within that margin of HH:30 can fall
    on the other side.
  - A batch is taken as N events as logged. The pool is never full at refill, because
    N = threshold - queue size and the threshold is below the pool size.
"""

import argparse
import ast
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone

HOUR = timedelta(hours=1)

_TS = re.compile(r'^(?:(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z )?'
                 r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3})')
_STATE = re.compile(r'\[REFILL\] Outbreak state updated: profile=(\w+), .*?affected_regions=(.*)$')
_GEN = re.compile(r'\[REFILL\] Generating (\d+) events')
_PRODUCED = re.compile(r'\[EMIT\] Produced (\d+) events')


def parse_utc(text: str) -> datetime:
    dt = datetime.fromisoformat(text.strip().replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _line_time(line: str):
    m = _TS.match(line)
    if not m:
        return None
    if m.group(1):  # kubectl --timestamps prefix, RFC3339 in UTC
        frac = (m.group(2) or '')[:7]
        return datetime.strptime(m.group(1) + frac, '%Y-%m-%dT%H:%M:%S' + ('.%f' if frac else '')
                                 ).replace(tzinfo=timezone.utc)
    return datetime.strptime(m.group(3) + ',' + m.group(4), '%Y-%m-%d %H:%M:%S,%f').replace(tzinfo=timezone.utc)


def parse_log(lines):
    """Return (batches, points).

    batches: [(time, profile, affected_tuple, n_events)] in log order.
    points:  [(time, cumulative_emitted)], starting with (first refill time, 0).
    """
    batches, points = [], []
    for line in lines:
        t = _line_time(line)
        if t is None:
            continue
        m = _STATE.search(line)
        if m:
            raw = m.group(2).strip()
            affected = tuple(ast.literal_eval(raw)) if raw.startswith('[') else ()
            batches.append([t, m.group(1), affected, 0])
            continue
        m = _GEN.search(line)
        if m and batches:
            batches[-1][3] += int(m.group(1))
            continue
        m = _PRODUCED.search(line)
        if m:
            points.append((t, int(m.group(1))))
    if not batches:
        raise ValueError("no [REFILL] 'Outbreak state updated' line in the log")
    if not points:
        raise ValueError("no [EMIT] 'Produced N events' line in the log")
    counts = [c for _, c in points]
    if any(b < a for a, b in zip(counts, counts[1:])):
        raise ValueError("the cumulative [EMIT] count goes down: the log spans a restart; supply one pod's log")
    points.insert(0, (batches[0][0], 0))
    return [tuple(b) for b in batches], points


def emitted_count(points, when: datetime):
    """Cumulative emitted count at `when`, interpolated; None outside the logged span."""
    if when < points[0][0] or when > points[-1][0]:
        return None
    for (t0, c0), (t1, c1) in zip(points, points[1:]):
        if t0 <= when <= t1:
            if t1 == t0:
                return float(c1)
            return c0 + (c1 - c0) * (when - t0).total_seconds() / (t1 - t0).total_seconds()
    return float(points[-1][1])


def state_for_count(batches, count: float):
    """(profile, affected) of the batch that holds the count-th emitted event, or None."""
    k = max(1, math.ceil(count))
    total = 0
    for _, profile, affected, n in batches:
        total += n
        if k <= total:
            return profile, affected
    return None


def reconstruct(lines, start: datetime, end: datetime):
    """Return (rows, skipped). rows: state-list dicts; skipped: [(hour, reason)]."""
    batches, points = parse_log(lines)
    rows, skipped = [], []
    hour = start
    while hour < end:
        half = hour + timedelta(minutes=30)
        count = emitted_count(points, half)
        if count is None:
            skipped.append((hour, 'HH:30 is outside the logged span'))
        else:
            st = state_for_count(batches, count)
            if st is None:
                skipped.append((hour, 'emitted count is beyond the generated batches'))
            elif not rows or (rows[-1]['profile'], tuple(rows[-1]['affected_regions'])) != st:
                rows.append({'from_time': format_ts(half), 'profile': st[0], 'affected_regions': list(st[1])})
        hour += HOUR
    return rows, skipped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reconstruct emitted states per hour from an event-generator pod log.")
    ap.add_argument('logfile')
    ap.add_argument('--start', required=True, help="first hour, UTC, on the hour")
    ap.add_argument('--end', required=True, help="end of range, exclusive, UTC, on the hour")
    ap.add_argument('--out', help="write the state list here (default: stdout)")
    args = ap.parse_args(argv)
    start, end = parse_utc(args.start), parse_utc(args.end)
    if start != start.replace(minute=0, second=0, microsecond=0) or end != end.replace(minute=0, second=0, microsecond=0):
        ap.error("--start and --end must be on the hour")
    with open(args.logfile) as f:
        rows, skipped = reconstruct(f, start, end)
    text = json.dumps(rows, indent=2) + '\n'
    if args.out:
        with open(args.out, 'w') as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    for r in rows:
        print(f"{r['from_time']} {r['profile']:9} {','.join(r['affected_regions']) or '-'}", file=sys.stderr)
    for hour, reason in skipped:
        print(f"skipped {format_ts(hour)}: {reason}", file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
