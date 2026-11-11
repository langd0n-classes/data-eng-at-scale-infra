#!/usr/bin/env python3
"""
DS-551 labeled history backfill (staff command, run on demand for one team).

Simulates the event generator over a past UTC date range with a simulated clock
and a fixed seed, and writes, to ONE team's Kafka:

  events.<team>.history          the simulated events (same JSON schema, rates and
                                 outbreak/winddown/baseline distributions as live;
                                 no bad events)
  events.<team>.outbreak_status  the label of every region-hour in the range
                                 (released_at = time of this run)

The simulation depends only on --seed and the range, never on the team, so every
team that gets the same seed and range gets identical events and labels. It does
not read or write the live state topic and does not touch events.<team>.raw.

Usage (inside the event-generator image):
  python backfill_history.py --team team01                  # last 14 days before 2026-11-09T00:00:00Z
  python backfill_history.py --team team01 --dry-run        # simulate, print counts, write nothing

Topics are created the way the generator already relies on: the first send
creates a missing topic if the broker allows auto-creation. See README.md.
"""

import argparse
import random
import sys
from threading import Lock
from datetime import datetime, timedelta, timezone

import event_generator as eg
from event_generator import (
    HOUR, EventGenerator, OutbreakState, build_labels, format_hour, is_in_outbreak_window,
    label_topic, logger, BASE_BEDS, BED_PRESSURE_FACTOR, EVENT_RATE_PER_SEC, REGIONS,
    SYMPTOM_BURDEN_DECAY, TEAM_KAFKA_MAPPING, TOPIC_PREFIX, HISTORY_TOPIC_SUFFIX,
)

DEFAULT_END = datetime(2026, 11, 9, 0, 0, 0, tzinfo=timezone.utc)
DEFAULT_DAYS = 14
DEFAULT_SEED = 551
DEFAULT_BATCH = 5000


def history_topic(team_id: str) -> str:
    return f"{TOPIC_PREFIX}{team_id}{HISTORY_TOPIC_SUFFIX}"


def parse_utc(text: str) -> datetime:
    """Accept 2026-11-09 or 2026-11-09T00:00:00Z (UTC)."""
    text = text.strip().replace('Z', '+00:00')
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def next_gap_seconds(profile: str) -> float:
    """Seconds to the next event: the same pacing as the live emit thread."""
    base = 1.0 / EVENT_RATE_PER_SEC
    if profile == 'outbreak':
        return random.uniform(base * 0.5, base * 1.5)
    if profile == 'winddown':
        return random.uniform(base * 1.5, base * 3.0)
    return random.uniform(base * 2.0, base * 4.0)


def simulate_history(start: datetime, end: datetime, seed: int, regions=None):
    """Yield (hour, profile, affected_regions, events) for each UTC hour in [start, end).

    start and end must be on hour boundaries. Event timestamps lie in [hour, hour + 1h).
    State is evaluated at the start of each hour from the same schedule the live
    generator uses (is_in_outbreak_window), so it is constant inside an hour and the
    label state is the state of the whole hour. Affected regions are sampled when an
    outbreak window starts, as live. Seeds the module-level random: run it in its own
    process, never inside the live generator.
    """
    regions = list(regions or REGIONS)
    random.seed(seed)
    gen = EventGenerator.__new__(EventGenerator)  # no Kafka, no queues: only the event methods
    gen.state = OutbreakState()
    gen.state_lock = Lock()
    burden_cap = BASE_BEDS / BED_PRESSURE_FACTOR

    was_active = False
    hour = start
    next_t = start + timedelta(seconds=next_gap_seconds('baseline'))
    last_t = start
    while hour < end:
        is_active, profile, intensity, _ = is_in_outbreak_window(hour)
        st = gen.state
        st.profile, st.intensity = profile, intensity
        if is_active:
            if not was_active:
                k = min(random.randint(2, 4), len(regions))
                st.affected_regions = random.sample(regions, k)
        else:
            st.affected_regions = []
            st.symptom_burden = 0.0
        was_active = is_active

        hour_end = hour + HOUR
        events = []
        while next_t < hour_end:
            st.symptom_burden *= SYMPTOM_BURDEN_DECAY ** (next_t - last_t).total_seconds()
            last_t = next_t
            event = gen.generate_event()
            event['timestamp'] = next_t.isoformat()
            event['source'] = 'event-generator'
            event['schema_version'] = '1.0'
            if event['event_type'] in ('symptom_report', 'hospital_admission'):
                st.symptom_burden = min(st.symptom_burden + 1, burden_cap)
            events.append(event)
            next_t = next_t + timedelta(seconds=next_gap_seconds(profile))
        yield hour, profile, list(st.affected_regions), events
        hour = hour_end


def run(team_id, start, end, seed, producer, batch_size, dry_run, release_time, out=print):
    """Simulate and write. producer may be None when dry_run. Returns (events, labels, send_errors)."""
    errors = []

    def on_error(exc):
        errors.append(exc)

    n_events = n_labels = 0
    day_events = 0
    total_hours = int((end - start) / HOUR)
    for i, (hour, profile, affected, events) in enumerate(simulate_history(start, end, seed), 1):
        labels = build_labels(hour, profile, affected, REGIONS, release_time)
        if not dry_run:
            for ev in events:
                producer.send(history_topic(team_id), value=ev).add_errback(on_error)
                if (n_events + 1) % batch_size == 0:
                    producer.flush(timeout=60)
                n_events += 1
            for key, value in labels:
                producer.send(label_topic(team_id), key=key.encode('utf-8'), value=value).add_errback(on_error)
        else:
            n_events += len(events)
        n_labels += len(labels)
        day_events += len(events)
        if hour.hour == 23 or i == total_hours:
            out(f"[{i}/{total_hours}h] {hour.date()} done: {day_events} events, profile at last hour={profile}, "
                f"total {n_events} events, {n_labels} labels")
            day_events = 0
    if not dry_run:
        producer.flush(timeout=120)
    return n_events, n_labels, errors


VALID_PROFILES = ('baseline', 'outbreak', 'winddown')


def load_state_list(path: str) -> list:
    """Read a state list: rows of (from_time, profile, affected_regions), sorted by time.

    JSON: [{"from_time": "2026-11-10T01:37:00Z", "profile": "winddown",
            "affected_regions": ["Boston", "NYC"]}, ...]
    CSV with header from_time,profile,affected_regions; regions separated by ';' or '|'.
    A row holds from its from_time until the next row's from_time.
    """
    import csv
    import json
    with open(path, newline='') as f:
        text = f.read()
    if path.lower().endswith('.json') or text.lstrip().startswith(('[', '{')):
        raw = json.loads(text)
    else:
        raw = list(csv.DictReader(text.splitlines()))
    rows = []
    for r in raw:
        profile = str(r['profile']).strip().lower()
        if profile not in VALID_PROFILES:
            raise ValueError(f"unknown profile {profile!r} in {r}")
        regions = r.get('affected_regions') or []
        if isinstance(regions, str):
            regions = [x.strip() for x in regions.replace('|', ';').split(';') if x.strip()]
        unknown = [x for x in regions if x not in REGIONS]
        if unknown:
            raise ValueError(f"regions {unknown} are not in REGIONS {REGIONS} (row {r})")
        rows.append((parse_utc(str(r['from_time'])), profile, tuple(regions)))
    if not rows:
        raise ValueError("state list is empty")
    return sorted(rows, key=lambda x: x[0])


def state_at(rows: list, when: datetime):
    """(profile, affected) in force at `when`, or None before the first row."""
    current = None
    for t, profile, affected in rows:
        if t <= when:
            current = (profile, affected)
        else:
            break
    return current


def run_state_list(team_id, start, end, rows, producer, release_time, out=print):
    """Write labels only, for each hour in [start, end), from a supplied state list.

    Same rule as live: the state of an hour is the state at HH:30. Hours before the
    first row have no known state and are skipped. Returns (labels, skipped_hours, errors).
    """
    errors = []
    n_labels, skipped = 0, []
    hour = start
    while hour < end:
        st = state_at(rows, hour + timedelta(minutes=30))
        if st is None:
            skipped.append(hour)
        else:
            profile, affected = st
            for key, value in build_labels(hour, profile, affected, REGIONS, release_time):
                n_labels += 1
                if producer is not None:
                    producer.send(label_topic(team_id), key=key.encode('utf-8'),
                                  value=value).add_errback(errors.append)
            out(f"{format_hour(hour)} {profile:9} {','.join(affected) or '-'}")
        hour += HOUR
    if producer is not None:
        producer.flush(timeout=60)
    return n_labels, skipped, errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Write labeled history for one team.")
    ap.add_argument('--labels-from', metavar='FILE',
                    help="write labels only, from a JSON or CSV state list (see load_state_list); "
                         "needs --start and --end; writes no events")
    ap.add_argument('--team', required=True, help="team id, e.g. team01")
    ap.add_argument('--end', default=format_hour(DEFAULT_END), help="end of range, exclusive (UTC, hour boundary)")
    ap.add_argument('--start', help="start of range, inclusive (UTC, hour boundary); default end minus --days")
    ap.add_argument('--days', type=int, default=DEFAULT_DAYS)
    ap.add_argument('--seed', type=int, default=DEFAULT_SEED)
    ap.add_argument('--batch-size', type=int, default=DEFAULT_BATCH, help="events per producer flush")
    ap.add_argument('--bootstrap', help="Kafka bootstrap server; default from TEAM_BOOTSTRAP_SERVERS")
    ap.add_argument('--dry-run', action='store_true', help="simulate and print, write nothing")
    args = ap.parse_args(argv)

    if args.labels_from and not (args.start and args.end != format_hour(DEFAULT_END)):
        ap.error("--labels-from needs explicit --start and --end")
    end = parse_utc(args.end)
    start = parse_utc(args.start) if args.start else end - timedelta(days=args.days)
    for name, dt in (('start', start), ('end', end)):
        if dt != eg.hour_floor(dt):
            ap.error(f"--{name} must be on an hour boundary")
    if end <= start:
        ap.error("end must be after start")
    if end > datetime.now(timezone.utc):
        ap.error("the range must be in the past")

    producer = None
    if not args.dry_run:
        bootstrap = args.bootstrap or TEAM_KAFKA_MAPPING.get(args.team)
        if not bootstrap:
            ap.error(f"no bootstrap server for {args.team}: set TEAM_BOOTSTRAP_SERVERS or pass --bootstrap")
        producer = EventGenerator.connect_kafka_for_team(None, args.team, bootstrap)
        if producer is None:
            logger.error("Could not connect to Kafka")
            return 1

    if args.labels_from:
        rows = load_state_list(args.labels_from)
        print(f"Labels for {args.team} from {args.labels_from}: {format_hour(start)} to {format_hour(end)} "
              f"(exclusive), regions {REGIONS}, topic {label_topic(args.team)}"
              f"{' [dry run]' if args.dry_run else ''}", flush=True)
        n_labels, skipped, errors = run_state_list(args.team, start, end, rows, producer,
                                                   datetime.now(timezone.utc), out=lambda m: print(m, flush=True))
        if producer:
            producer.close(timeout=10)
        if skipped:
            print(f"Skipped {len(skipped)} hours before the first state row, from {format_hour(skipped[0])}", flush=True)
        print(f"{'Dry run: ' if args.dry_run else 'Wrote '}{n_labels} labels", flush=True)
        if errors:
            print(f"FAILED: {len(errors)} send errors; first: {errors[0]}", file=sys.stderr)
            return 1
        return 0

    print(f"Backfill for {args.team}: {format_hour(start)} to {format_hour(end)} (exclusive), "
          f"seed={args.seed}, topics {history_topic(args.team)} and {label_topic(args.team)}"
          f"{' [dry run]' if args.dry_run else ''}", flush=True)
    n_events, n_labels, errors = run(args.team, start, end, args.seed, producer, args.batch_size,
                                     args.dry_run, datetime.now(timezone.utc), out=lambda m: print(m, flush=True))
    if producer:
        producer.close(timeout=10)
    print(f"Wrote {n_events} events and {n_labels} labels" if not args.dry_run
          else f"Dry run: {n_events} events and {n_labels} labels", flush=True)
    if errors:
        print(f"FAILED: {len(errors)} send errors; first: {errors[0]}", file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
