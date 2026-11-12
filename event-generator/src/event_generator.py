#!/usr/bin/env python3
"""
DS-551 Event Generator with Outbreak Scheduling

Produces synthetic health events to per-team Kafka instances with scheduled outbreak windows.
Outbreak state flips on Mon/Thu 9am UTC, lasting 24h active + 12h winddown (36h total).

Architecture:
  - Flask health thread (immediate)
  - RefillThread (every 6h): checks outbreak schedule, generates batch
  - EmitThread (continuous): drains pool to Kafka at variable pace

Outbreak-aware event and severity distributions are defined inline in this file.

Delayed labels: each team also gets events.<team>.outbreak_status, one message per
region per completed UTC hour, released LABEL_DELAY_HOURS after the hour ends.
See label_status() for the rule and LabelService for restart safety.
Labeled history for training is written by backfill_history.py (separate entry point).
"""

import os
import sys
import json
import time
import random
import logging
import queue
import signal
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Thread, Lock
from flask import Flask
from waitress import serve
from kafka import KafkaProducer, KafkaConsumer, TopicPartition
from kafka.admin import KafkaAdminClient, NewTopic, ConfigResource, ConfigResourceType
from kafka.errors import TopicAlreadyExistsError
import kafka.errors as kafka_errors

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration from environment
EVENT_RATE_PER_SEC = max(0.1, float(os.getenv('EVENT_RATE_PER_SEC', '10')))
TOPIC_PREFIX = os.getenv('TOPIC_PREFIX', 'events.')
TOPIC_SUFFIX = os.getenv('TOPIC_SUFFIX', '.raw')
PATHOGEN_NAME = 'Zorbovian Sniffles'
_DEFAULT_REGIONS = 'Boston,Cambridge,Somerville,Brookline,Newton'
REGIONS = [r.strip() for r in os.getenv('REGIONS', _DEFAULT_REGIONS).split(',') if r.strip()] \
          or _DEFAULT_REGIONS.split(',')

# Pool configuration
EVENT_POOL_SIZE = max(1, int(os.getenv('EVENT_POOL_SIZE', '50000')))
EVENT_POOL_REFILL_THRESHOLD = int(os.getenv('EVENT_POOL_REFILL_THRESHOLD', '40000'))
if EVENT_POOL_REFILL_THRESHOLD > EVENT_POOL_SIZE:
    logger.warning(
        f"EVENT_POOL_REFILL_THRESHOLD ({EVENT_POOL_REFILL_THRESHOLD}) > "
        f"EVENT_POOL_SIZE ({EVENT_POOL_SIZE}), clamping threshold to pool size"
    )
    EVENT_POOL_REFILL_THRESHOLD = EVENT_POOL_SIZE

# Bed pressure parameters
BASE_BEDS = max(1, int(os.getenv('BASE_BEDS', '500')))
BED_PRESSURE_FACTOR = max(0.01, float(os.getenv('BED_PRESSURE_FACTOR', '0.8')))
SYMPTOM_BURDEN_DECAY = max(0.001, min(0.9999, float(os.getenv('SYMPTOM_BURDEN_DECAY', '0.995'))))

# Share of sent events that break the event contract, so that teams can test
# their quarantine and error handling on live data. 0 turns it off.
def _bad_event_rate() -> float:
    raw = os.getenv('BAD_EVENT_RATE', '0.0175')
    try:
        rate = float(raw)
    except ValueError:
        rate = float('nan')
    if not 0.0 <= rate <= 1.0:  # also rejects NaN
        logger.warning(f"BAD_EVENT_RATE={raw!r} is not a number from 0 to 1; using 0.0175")
        rate = 0.0175
    return rate


BAD_EVENT_RATE = _bad_event_rate()
BAD_EVENT_KINDS = ['not_json', 'missing_field', 'wrong_type', 'unknown_type',
                   'out_of_range', 'bad_timestamp']


# Delayed outbreak labels (see label_status / LabelService below).
def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name, '')
    if raw.strip() == '':
        return default
    try:
        value = float(raw)
    except ValueError:
        value = float('nan')
    if not value >= minimum:  # also rejects NaN
        logger.warning(f"{name}={raw!r} is not a number >= {minimum}; using {default}")
        return default
    return value


# Hours between the end of a UTC hour and the release of that hour's labels.
LABEL_DELAY_HOURS = _env_float('LABEL_DELAY_HOURS', 12.0)
LABEL_TOPIC_SUFFIX = os.getenv('LABEL_TOPIC_SUFFIX', '.outbreak_status')
HISTORY_TOPIC_SUFFIX = os.getenv('HISTORY_TOPIC_SUFFIX', '.history')
# Staff-only topic that records the per-hour state so pending labels survive a
# restart. It lives in the Kafka of LABEL_STATE_TEAM (team00), which students
# cannot read because each team only has access to its own Kafka.
LABEL_STATE_TOPIC = os.getenv('LABEL_STATE_TOPIC', 'staff.outbreak_state')
LABEL_STATE_TEAM = os.getenv('LABEL_STATE_TEAM', 'team00')
LABEL_TICK_SECONDS = 15


def _env_prob(name: str, default: float) -> float:
    value = _env_float(name, default)
    if value > 1.0:
        logger.warning(f"{name}={value} is more than 1; using {default}")
        return default
    return value


def _env_time(name: str):
    """A UTC time from the environment (ISO 8601, 'Z' or an offset; no offset means UTC), or None if unset."""
    raw = os.getenv(name, '').strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        logger.warning(f"{name}={raw!r} is not an ISO 8601 time; the feature stays off")
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# Scheduled behaviors. Each is off unless its start time is set. Every choice (when a
# burst starts, its region, which AI statuses are wrong and what they say) is drawn from
# a random.Random seeded with CR_SEED plus the hour (and region), so staff can reproduce it.
CR_SEED = os.getenv('CR_SEED', '551')
# False-alarm bursts: extra symptom_report events in one region whose true status is LOW.
FALSE_ALARM_START = _env_time('FALSE_ALARM_START')
FALSE_ALARM_PROBABILITY = _env_prob('FALSE_ALARM_PROBABILITY', 0.04)   # chance that a burst starts in a UTC hour
FALSE_ALARM_MINUTES = _env_float('FALSE_ALARM_MINUTES', 30.0, minimum=1.0)
FALSE_ALARM_EVENTS_PER_MINUTE = _env_float('FALSE_ALARM_EVENTS_PER_MINUTE', 6.0, minimum=0.1)
# AI-sourced statuses with staff corrections on events.<team>.outbreak_status.
AI_LABELS_START = _env_time('AI_LABELS_START')
AI_LABEL_DELAY_HOURS = _env_float('AI_LABEL_DELAY_HOURS', 1.0)
AI_LABEL_ERROR_RATE = _env_prob('AI_LABEL_ERROR_RATE', 0.12)
STAFF_CORRECTION_DELAY_HOURS = _env_float('STAFF_CORRECTION_DELAY_HOURS', 18.0)

# Parse team bootstrap servers from environment
TEAM_KAFKA_MAPPING = {}
team_bootstrap_env = os.getenv('TEAM_BOOTSTRAP_SERVERS', '')
if team_bootstrap_env:
    for entry in team_bootstrap_env.split(','):
        if '=' in entry:
            team_id, bootstrap = entry.split('=', 1)
            TEAM_KAFKA_MAPPING[team_id.strip()] = bootstrap.strip()

# Single shared cluster (Mode 2 — used when TEAM_BOOTSTRAP_SERVERS is not set)
SINGLE_BOOTSTRAP = os.getenv('KAFKA_BOOTSTRAP_SERVERS')
EXPLICIT_TOPIC = os.getenv('TOPIC')

logger.info(f"Loaded {len(TEAM_KAFKA_MAPPING)} team Kafka mappings")

# Health check Flask app
app = Flask(__name__)

_generator = None          # set in main() after generator.start()
_waiting_for_config = False  # True when pod is running but no Kafka config is present

@app.route('/health')
def health():
    if _waiting_for_config:
        body = {'status': 'waiting', 'reason': 'no Kafka clusters configured',
                'timestamp': datetime.now(timezone.utc).isoformat()}
        return body, 200
    alive = _generator is not None and _generator.running
    body = {'status': 'healthy' if alive else 'unhealthy',
            'timestamp': datetime.now(timezone.utc).isoformat()}
    return body, (200 if alive else 503)

@app.route('/ready')
def ready():
    if _generator is None:
        return {'status': 'not ready', 'reason': 'generator not started'}, 503
    connected = len(_generator.producers)
    ok = connected > 0
    return ({
        'status': 'ready' if ok else 'not ready',
        'teams_connected': connected,
        'teams_configured': len(TEAM_KAFKA_MAPPING),
        'rate': EVENT_RATE_PER_SEC,
    }, 200 if ok else 503)


# ============================================================================
# Outbreak State & Event Distribution
# ============================================================================

@dataclass
class OutbreakState:
    """Shared outbreak state for all threads."""
    profile: str = "baseline"  # baseline | outbreak | winddown
    intensity: float = 0.0
    affected_regions: list = field(default_factory=list)
    symptom_burden: float = 0.0  # rolling count of symptoms


def outbreak_event_weights(intensity: float) -> dict:
    """Outbreak event type weights — skews toward emergency and hospital events at high intensity."""
    emergency = 0.26 + 0.16 * intensity
    admissions = 0.24 + 0.12 * intensity
    routine = 0.07 - 0.03 * intensity
    vaccination = 0.06 - 0.02 * intensity
    health_mention = 0.18 - 0.04 * intensity
    general = 1.0 - (emergency + admissions + routine + vaccination + health_mention)
    return {
        "hospital_admission": admissions,
        "vaccination": max(0.01, vaccination),
        "symptom_report": max(0.05, health_mention),
        "emergency_incident": emergency,
        "general_health_report": max(0.05, general),
        "clinic_visit": max(0.01, routine),
    }


def winddown_event_weights(intensity: float) -> dict:
    """Winddown event type weights."""
    baseline = {
        "hospital_admission": 0.07, "vaccination": 0.15, "symptom_report": 0.25,
        "emergency_incident": 0.03, "general_health_report": 0.20, "clinic_visit": 0.30,
    }
    target = {
        "hospital_admission": 0.14, "vaccination": 0.11, "symptom_report": 0.24,
        "emergency_incident": 0.11, "general_health_report": 0.20, "clinic_visit": 0.20,
    }
    alpha = min(0.35 + 0.25 * intensity, 0.7)
    alpha = max(0.3, alpha)
    return {k: (1 - alpha) * baseline.get(k, 0.0) + alpha * target.get(k, 0.0) for k in baseline}


def baseline_event_weights() -> dict:
    """Baseline event type weights (no outbreak)."""
    return {
        "hospital_admission": 0.07, "vaccination": 0.15, "symptom_report": 0.25,
        "emergency_incident": 0.03, "general_health_report": 0.20, "clinic_visit": 0.30,
    }


def make_bad_event(event: dict):
    """Return a copy of the event that breaks the contract in one way.

    Returns bytes for an event that is not valid JSON, and a dict otherwise.
    The bad event carries no marker: a consumer must find it by validation.
    """
    kind = random.choice(BAD_EVENT_KINDS)
    bad = dict(event)
    if kind == 'not_json':
        text = json.dumps(bad)
        return text[:random.randint(1, len(text) - 1)].encode('utf-8')
    if kind == 'missing_field':
        bad.pop(random.choice(['event_type', 'timestamp', 'region']), None)
    elif kind == 'wrong_type':
        bad['age'] = random.choice(['N/A', 'unknown', 'forty'])
    elif kind == 'unknown_type':
        bad['event_type'] = random.choice(['symptom_reprot', 'clinic visit', 'lab_result', 'TEST'])
    elif kind == 'out_of_range':
        bad['age'] = random.choice([-7, -1, 212, 999])
    else:  # bad_timestamp
        bad['timestamp'] = random.choice(['yesterday', '2026-13-45T25:61:00Z', 'not a time', ''])
    return bad


def normalize_weights(weights: dict) -> dict:
    """Normalize weights to sum to 1.0."""
    total = sum(weights.values())
    return {k: v / total for k, v in weights.items()} if total > 0 else weights


# ============================================================================
# Outbreak Scheduling
# ============================================================================

def is_in_outbreak_window(now: datetime) -> tuple:
    """
    Check if current UTC time is in an outbreak window.
    Returns (is_active, profile, intensity, duration_hours_remaining)

    Mon 09:00-Tue 09:00 UTC = 24h active, Tue 09:00-21:00 = 12h winddown (36h total)
    Thu 09:00-Fri 09:00 UTC = 24h active, Fri 09:00-21:00 = 12h winddown (36h total)
    """
    weekday = now.weekday()  # 0=Mon, 3=Thu
    hour = now.hour

    # Monday outbreak window: 09:00 Mon - 09:00 Tue active, 09:00-21:00 Tue winddown (36h total)
    if weekday == 0 and hour >= 9:  # Mon 09:00 onwards
        return True, "outbreak", random.uniform(0.6, 0.9), 48 - (hour - 9)
    elif weekday == 1 and hour < 21:  # Tue before 21:00
        if hour < 9:
            return True, "outbreak", random.uniform(0.6, 0.9), 21 - hour
        else:
            return True, "winddown", random.uniform(0.2, 0.5), 21 - hour

    # Thursday outbreak window: 09:00 Thu - 09:00 Fri active, 09:00-21:00 Fri winddown (36h total)
    if weekday == 3 and hour >= 9:  # Thu 09:00 onwards
        return True, "outbreak", random.uniform(0.6, 0.9), 48 - (hour - 9)
    elif weekday == 4 and hour < 21:  # Fri before 21:00
        if hour < 9:
            return True, "outbreak", random.uniform(0.6, 0.9), 21 - hour
        else:
            return True, "winddown", random.uniform(0.2, 0.5), 21 - hour

    return False, "baseline", 0.0, 0


# ============================================================================
# Outbreak Labels
# ============================================================================

HOUR = timedelta(hours=1)


def hour_floor(dt: datetime) -> datetime:
    """Start of the UTC hour that contains dt."""
    return dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def format_hour(hour: datetime) -> str:
    """'YYYY-MM-DDTHH:00:00Z' for the start of an hour."""
    return hour_floor(hour).strftime('%Y-%m-%dT%H:00:00Z')


def parse_hour(text: str) -> datetime:
    return datetime.strptime(text, '%Y-%m-%dT%H:00:00Z').replace(tzinfo=timezone.utc)


def parse_ts(text: str) -> datetime:
    """Inverse of format_ts."""
    return datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)


def format_ts(dt: datetime) -> str:
    """ISO UTC timestamp with a Z suffix, whole seconds."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')


def label_status(profile: str, affected_regions, region: str) -> str:
    """THE label rule. One function; the live service and the backfill both call it.

    ALERT if the profile was "outbreak" and the region was in affected_regions.
    WATCH if the profile was "winddown" and the region was in affected_regions.
    LOW otherwise (baseline, or a region the outbreak did not touch).

    profile and affected_regions are the state that held for the hour. When the
    state changes inside an hour, the hour uses the state at the half hour
    (HH:30:00): see LabelService.record().
    """
    if region in (affected_regions or ()):
        if profile == 'outbreak':
            return 'ALERT'
        if profile == 'winddown':
            return 'WATCH'
    return 'LOW'


def build_labels(hour: datetime, profile: str, affected_regions, regions, released_at: datetime,
                 source: str = None) -> list:
    """One (key, value) pair per region for the hour. Labels never carry bad events.

    `source` ("ai" or "staff") is added only when given: records released before
    AI_LABELS_START have no source field.
    """
    return build_status_messages(
        hour, {r: label_status(profile, affected_regions, r) for r in regions}, regions, released_at, source)


def build_status_messages(hour: datetime, statuses: dict, regions, released_at: datetime,
                          source: str = None) -> list:
    """(key, value) pairs for the given regions, with the status given per region."""
    out = []
    for region in regions:
        value = {
            'region': region,
            'hour': format_hour(hour),
            'status': statuses[region],
            'released_at': format_ts(released_at),
        }
        if source is not None:
            value['source'] = source
        out.append((region, value))
    return out


STATUSES = ('LOW', 'WATCH', 'ALERT')


def ai_status(seed, hour: datetime, region: str, true_status: str, error_rate: float) -> str:
    """The status the AI labeler gives a region-hour. Deterministic from (seed, hour, region).

    With probability error_rate it is one of the other two statuses (each equally likely),
    otherwise the true status. The first draw decides wrong/right, so a higher error rate
    only adds errors: every region-hour wrong at a lower rate is also wrong at a higher one.
    """
    rng = random.Random(f"{seed}|ai|{format_hour(hour)}|{region}")
    if rng.random() < error_rate:
        return rng.choice([s for s in STATUSES if s != true_status])
    return true_status


def false_alarm_plan(seed, hour: datetime, probability: float):
    """(planned_start, pick) of the burst that starts in this UTC hour, or None. Deterministic.

    pick (0..1) chooses the region among the regions whose status is LOW when the burst starts.
    """
    rng = random.Random(f"{seed}|false_alarm|{format_hour(hour)}")
    if rng.random() >= probability:
        return None
    start = hour_floor(hour) + timedelta(seconds=int(rng.uniform(0, 3600)))
    return start, rng.random()


def record_key(rec: dict) -> str:
    """Kafka key of a staff-topic record. A compacted topic keeps the newest record per key."""
    kind = rec['kind']
    if kind == 'state':
        return f"state|{rec['hour']}"
    if kind in ('released', 'corrected'):
        return f"{kind}|{rec['team']}|{rec['hour']}"
    if kind == 'ai_label':
        return f"ai_label|{rec['hour']}|{rec['region']}"
    if kind == 'false_alarm':
        return f"false_alarm|{rec['planned_start']}"
    raise ValueError(f"unknown record kind {kind!r}")


def raw_topic(team_id: str) -> str:
    """The raw event topic of a team, as the emit thread names it."""
    if team_id == 'shared':
        return EXPLICIT_TOPIC if EXPLICIT_TOPIC else f"{TOPIC_PREFIX}{TOPIC_SUFFIX.lstrip('.')}"
    return f"{TOPIC_PREFIX}{team_id}{TOPIC_SUFFIX}"


def label_topic(team_id: str) -> str:
    if team_id == 'shared':
        return f"{TOPIC_PREFIX}{LABEL_TOPIC_SUFFIX.lstrip('.')}"
    return f"{TOPIC_PREFIX}{team_id}{LABEL_TOPIC_SUFFIX}"


# Team brokers default to log.retention.hours=24 and log.retention.bytes=100 MB per partition,
# and auto-create topics. The history and label topics must stay readable for the whole term,
# so they get the same unlimited retention as the raw topic (deploy-kafka-task.yaml).
UNLIMITED_RETENTION = {'retention.ms': '-1', 'retention.bytes': '-1'}


TOPIC_EXISTS_CODE = 36        # TOPIC_ALREADY_EXISTS
ENSURE_TOPICS_TIMEOUT = 60.0  # seconds for the whole call; the admin client has no overall timeout
_ensure_inflight = {}         # (bootstrap, topics) -> worker thread of a call that timed out


def _error_name(code: int) -> str:
    try:
        return f"{kafka_errors.for_code(code).__name__} ({code})"
    except Exception:
        return f"error code {code}"


def _check_topic_errors(response, what: str, allow_exists: bool = False) -> bool:
    """Check the per-topic error codes of a CreateTopics response.

    kafka-python 2.0.2 raises for some of these and returns the response for others, so both
    are handled. Returns True if the topic already exists (code 36, when allowed); raises on any
    other non-zero code.
    """
    exists = False
    for entry in (getattr(response, 'topic_errors', None) or []):
        code, message = entry[1], (entry[2] if len(entry) > 2 else None)
        if code == 0:
            continue
        if code == TOPIC_EXISTS_CODE and allow_exists:
            exists = True
            continue
        raise RuntimeError(f"{what}: {_error_name(code)} {message or ''}".strip())
    return exists


def _check_resource_errors(response, what: str):
    """Check the per-resource error codes of an AlterConfigs response (not raised by kafka-python 2.0.2)."""
    for entry in (getattr(response, 'resources', None) or []):
        code, message = entry[0], entry[1]
        if code != 0:
            raise RuntimeError(f"{what}: {_error_name(code)} {message or ''}".strip())


def _retention_is_unlimited(admin, topic: str) -> bool:
    """Read the topic's config back and check that both retention settings are -1."""
    for response in admin.describe_configs([ConfigResource(ConfigResourceType.TOPIC, topic)]):
        for res in response.resources:
            code, message, name, entries = res[0], res[1], res[3], res[4]
            if name != topic:
                continue
            if code != 0:
                raise RuntimeError(f"describe {topic}: {_error_name(code)} {message or ''}".strip())
            values = {e[0]: e[1] for e in entries}
            return all(str(values.get(k)) == v for k, v in UNLIMITED_RETENTION.items())
    return False


def _ensure_unlimited_topics(bootstrap: str, topics) -> bool:
    admin = None
    try:
        admin = KafkaAdminClient(bootstrap_servers=[bootstrap], api_version=(2, 5, 0),
                                 request_timeout_ms=30000, api_version_auto_timeout_ms=10000)
        existing = set(admin.list_topics())
        for topic in topics:
            needs_alter = topic in existing
            if not needs_alter:
                try:
                    response = admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1,
                                                             topic_configs=dict(UNLIMITED_RETENTION))])
                    needs_alter = _check_topic_errors(response, f"create {topic}", allow_exists=True)
                except TopicAlreadyExistsError:
                    needs_alter = True  # raised by kafka-python 2.0.2 for code 36
            if needs_alter:
                response = admin.alter_configs([ConfigResource(ConfigResourceType.TOPIC, topic,
                                                               configs=dict(UNLIMITED_RETENTION))])
                _check_resource_errors(response, f"alter {topic}")
            if not _retention_is_unlimited(admin, topic):
                raise RuntimeError(f"{topic}: retention.ms and retention.bytes are not both -1 on read-back")
            logger.info(f"[TOPICS] {topic} has unlimited retention")
        return True
    except Exception as e:
        logger.warning(f"[TOPICS] Could not set up {list(topics)} with unlimited retention: {e}")
        return False
    finally:
        if admin is not None:
            try:
                admin.close()
            except Exception:
                pass


def ensure_unlimited_topics(bootstrap: str, topics, timeout: float = None) -> bool:
    """Create each topic if missing (1 partition, unlimited retention); set unlimited retention on one that exists.

    Uses the Kafka admin client; no CLI in a broker. Returns True only if every topic was created or
    altered without an error code AND a read-back shows retention.ms and retention.bytes both -1.
    Never raises. The admin client can block for good on an unreachable broker, so the work runs in
    a daemon thread and a call that exceeds `timeout` (default ENSURE_TOPICS_TIMEOUT) counts as a
    failure. A timed-out call still running blocks a new call for the same topics (also a failure),
    so threads do not pile up. For an existing topic, alter_configs sets the topic's overrides to
    exactly these two; these topics are owned by this tool, so nothing else is expected on them.
    """
    timeout = ENSURE_TOPICS_TIMEOUT if timeout is None else timeout
    key = (bootstrap, tuple(topics))
    previous = _ensure_inflight.get(key)
    if previous is not None and previous.is_alive():
        logger.warning(f"[TOPICS] An earlier attempt for {list(topics)} is still running; not starting another")
        return False
    result = [False]

    def work():
        result[0] = _ensure_unlimited_topics(bootstrap, topics)

    worker = Thread(target=work, daemon=True)
    _ensure_inflight[key] = worker
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        logger.warning(f"[TOPICS] Setting up {list(topics)} timed out after {timeout:.0f}s")
        return False
    _ensure_inflight.pop(key, None)
    return result[0]


class KafkaStateStore:
    """Durable per-hour state and released-hour markers, in one staff topic.

    Two kinds of JSON record, keyed so that a compacted topic keeps the newest of each:
      {"kind": "state", "hour": H, "profile": P, "affected_regions": [...], "recorded_at": T}
      {"kind": "released", "team": TEAM, "hour": H, "released_at": T[, "source": "ai"|"staff"]}
    and, when the scheduled behaviors are on (see LabelService and FalseAlarmService):
      {"kind": "corrected", "team": TEAM, "hour": H, "released_at": T, "regions": [...]}
      {"kind": "ai_label", "hour": H, "region": R, "true_status", "ai_status", "wrong",
       "ai_released_at": {TEAM: T}, "correction_released_at": {TEAM: T}}
      {"kind": "false_alarm", "planned_start", "planned_end", "region", "start", "end",
       "events_added", "status", "end_reason"}
    record_key() gives the key of each. load() replays the whole topic. The topic is
    small (per day: 24 state records, one released record per team per hour, and with
    AI labels on, one ai_label record per region-hour, rewritten at each release).
    """

    def __init__(self, bootstrap: str, topic: str, get_producer):
        self.bootstrap = bootstrap
        self.topic = topic
        self.get_producer = get_producer  # callable -> KafkaProducer or None

    def ensure_topic(self):
        """Create the topic as compacted if it does not exist. Best effort; never raises.

        An existing topic is left as it is (see README: staff must set cleanup.policy=compact on it).
        """
        def work():
            admin = None
            try:
                admin = KafkaAdminClient(bootstrap_servers=[self.bootstrap], api_version=(2, 5, 0),
                                         request_timeout_ms=30000, api_version_auto_timeout_ms=10000)
                response = admin.create_topics([NewTopic(self.topic, num_partitions=1, replication_factor=1,
                                                         topic_configs={'cleanup.policy': 'compact'})])
                if not _check_topic_errors(response, f"create {self.topic}", allow_exists=True):
                    logger.info(f"[LABELS] Created compacted topic {self.topic}")
            except TopicAlreadyExistsError:
                pass
            except Exception as e:
                logger.warning(f"[LABELS] Could not create {self.topic} as compacted ({e}); the first write "
                               f"creates it with the broker defaults. See README for the topic config.")
            finally:
                if admin is not None:
                    try:
                        admin.close()
                    except Exception:
                        pass

        # The admin client can block for good on an unreachable broker; never hold up startup.
        worker = Thread(target=work, daemon=True)
        worker.start()
        worker.join(ENSURE_TOPICS_TIMEOUT)
        if worker.is_alive():
            logger.warning(f"[LABELS] Creating {self.topic} timed out after {ENSURE_TOPICS_TIMEOUT:.0f}s")

    def load(self):
        """Return (states, released, records).

        states: {hour_dt: (profile, affected_tuple)}; released: {(team, hour_dt)};
        records: {record_key: newest record} for every kind except state.
        """
        states, released, records = {}, set(), {}
        consumer = KafkaConsumer(bootstrap_servers=[self.bootstrap], group_id=None,
                                 enable_auto_commit=False, auto_offset_reset='earliest')
        try:
            partitions = consumer.partitions_for_topic(self.topic)
            if not partitions:  # topic does not exist yet: first run
                return states, released, records
            tps = [TopicPartition(self.topic, p) for p in sorted(partitions)]
            consumer.assign(tps)
            consumer.seek_to_beginning(*tps)
            end = consumer.end_offsets(tps)
            deadline = time.monotonic() + 60
            while any(consumer.position(tp) < end[tp] for tp in tps):
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not read all of {self.topic} within 60s")
                for records in consumer.poll(timeout_ms=2000).values():
                    for rec in records:
                        self._apply(rec.value, states, released, records)
        finally:
            consumer.close()
        return states, released, records

    @staticmethod
    def _apply(raw, states: dict, released: set, records: dict = None):
        try:
            rec = json.loads(raw)
            kind = rec['kind']
            if kind == 'state':
                states[parse_hour(rec['hour'])] = (rec['profile'], tuple(rec.get('affected_regions') or ()))
                return
            if kind == 'released':
                released.add((rec['team'], parse_hour(rec['hour'])))
            if records is not None:
                records[record_key(rec)] = rec
        except Exception as e:  # skip a malformed record; never block startup on it
            logger.warning(f"[LABELS] Skipping unreadable state record: {e}")

    def _send(self, key: str, value: dict) -> bool:
        producer = self.get_producer()
        if producer is None:
            return False
        try:
            producer.send(self.topic, key=key.encode('utf-8'), value=value).get(timeout=10)
            return True
        except Exception as e:
            logger.error(f"[LABELS] Could not write to {self.topic}: {e}")
            return False

    def save_state(self, hour: datetime, profile: str, affected_regions, recorded_at: datetime) -> bool:
        return self._send(f"state|{format_hour(hour)}", {
            'kind': 'state', 'hour': format_hour(hour), 'profile': profile,
            'affected_regions': list(affected_regions), 'recorded_at': format_ts(recorded_at)})

    def save_released(self, team: str, hour: datetime, released_at: datetime, source: str = None) -> bool:
        value = {'kind': 'released', 'team': team, 'hour': format_hour(hour),
                 'released_at': format_ts(released_at)}
        if source is not None:
            value['source'] = source
        return self._send(f"released|{team}|{format_hour(hour)}", value)

    def save_record(self, value: dict) -> bool:
        return self._send(record_key(value), value)


@dataclass
class AiLabelConfig:
    """AI-sourced statuses with staff corrections. Applies to every hour from hour_floor(start) on."""
    start: datetime
    delay_hours: float = 1.0
    error_rate: float = 0.12
    correction_delay_hours: float = 18.0
    seed: str = '551'

    @property
    def first_hour(self) -> datetime:
        return hour_floor(self.start)


def ai_config_from_env():
    if AI_LABELS_START is None:
        return None
    return AiLabelConfig(AI_LABELS_START, AI_LABEL_DELAY_HOURS, AI_LABEL_ERROR_RATE,
                         STAFF_CORRECTION_DELAY_HOURS, CR_SEED)


class LabelService:
    """Records the state of each UTC hour and releases its labels after a delay.

    The state fed to tick() is the state of the events being emitted (the tag of the
    newest event taken from the FIFO pool), not the generator's current state, which
    runs ahead of the emitted events by the pool depth.

    Mid-hour rule: the state of an hour is the emitted state at the half hour. tick()
    records the first state it sees at or after HH:30:00 and never changes it. The
    emitted state can change inside an hour; the half-hour state is the state that held
    at the midpoint of the hour, which is the state that held for most of the hour
    whenever the hour has one change.

    Restart safety: every recorded state and every release is also written to
    the staff topic (store). On startup, load() replays it, so pending hours
    stay pending and released hours stay released. Nothing is released before
    load() has succeeded. A crash between sending labels and writing the
    released marker re-sends that hour on restart: consumers should upsert on
    (region, hour).

    An hour with no recorded state (the generator was down for the whole second
    half of the hour) gets no label.

    AI labels (ai = AiLabelConfig, off when None). Every hour from ai.first_hour on is
    released ai.delay_hours after it ends, with source "ai"; for each region the AI status
    is ai_status() (wrong with probability ai.error_rate). For each team and hour, the
    regions whose AI status was wrong get a source "staff" record with the true status
    ai.correction_delay_hours after the AI release to that team. Hours before
    ai.first_hour keep the LABEL_DELAY_HOURS path; those released at or after ai.start
    carry source "staff". Nothing released before ai.start carries a source.

    Restart safety of AI labels: the AI decision of each region-hour is a pure function of
    (seed, hour, region, true status); it is also written to the staff topic (ai_label)
    before its first send and read back on load, so a restart reuses it even if the error
    rate was changed. The released marker carries released_at and source, so the due time
    of a pending correction survives a restart. A corrected marker per (team, hour) is
    written right after the correction is acknowledged and stops a second correction; a
    crash in between re-sends that correction once (same status, later released_at).
    """

    def __init__(self, store, regions, delay_hours: float, send_labels, teams_fn, ai: AiLabelConfig = None):
        self.store = store                  # object with load/save_state/save_released, or None
        self.regions = list(regions)
        self.delay = timedelta(hours=delay_hours)
        self.send_labels = send_labels      # (team_id, [(key, value)]) -> bool
        self.teams_fn = teams_fn            # () -> iterable of team ids that can receive labels now
        self.states = {}                    # hour_dt -> (profile, affected tuple)
        self.released = set()               # (team, hour_dt)
        self.loaded = store is None         # with no store there is nothing to load
        self._unsaved_states = set()
        self._unsaved_released = []         # [(team, hour_dt, released_at)]
        self._topic_checked = False
        self.max_unsaved = 2000             # bound when the state team's Kafka never accepts writes
        self.ai = ai
        self.released_meta = {}             # (team, hour_dt) -> (released_at, source or None)
        self.corrected = set()              # (team, hour_dt) whose AI errors have been corrected
        self.records = {}                   # record_key -> newest ai_label / false_alarm / ... record
        self._unsaved_records = {}          # record_key -> record not yet written
        self._records_lock = Lock()         # the false-alarm thread also writes records

    def queue_record(self, rec: dict):
        """Keep rec as the newest record for its key and write it to the staff topic on the next flush."""
        key = record_key(rec)
        with self._records_lock:
            self.records[key] = rec
            self._unsaved_records[key] = rec

    def get_record(self, key: str):
        with self._records_lock:
            return self.records.get(key)

    def records_of_kind(self, kind: str) -> list:
        with self._records_lock:
            return [r for r in self.records.values() if r.get('kind') == kind]

    def _cap_unsaved(self):
        """Drop the oldest unsaved entries past max_unsaved, with a warning. They stay valid in memory."""
        if len(self._unsaved_states) > self.max_unsaved:
            drop = sorted(self._unsaved_states)[:len(self._unsaved_states) - self.max_unsaved]
            self._unsaved_states.difference_update(drop)
            logger.warning(f"[LABELS] State topic unwritable: dropped {len(drop)} unsaved hour states "
                           f"(a restart now would lose them)")
        if len(self._unsaved_released) > self.max_unsaved:
            n = len(self._unsaved_released) - self.max_unsaved
            self._unsaved_released = self._unsaved_released[n:]
            logger.warning(f"[LABELS] State topic unwritable: dropped {n} unsaved release markers "
                           f"(a restart now could re-send those hours)")
        with self._records_lock:
            if len(self._unsaved_records) > self.max_unsaved:
                drop = list(self._unsaved_records)[:len(self._unsaved_records) - self.max_unsaved]
                for key in drop:
                    del self._unsaved_records[key]
                logger.warning(f"[LABELS] State topic unwritable: dropped {len(drop)} unsaved records "
                               f"(a restart now would lose them)")

    def ensure_loaded(self) -> bool:
        if self.loaded:
            return True
        if not self._topic_checked:
            self._topic_checked = True
            ensure = getattr(self.store, 'ensure_topic', None)
            if ensure:
                ensure()
        try:
            result = self.store.load()
        except Exception as e:
            logger.warning(f"[LABELS] State topic not readable yet (will retry): {e}")
            return False
        states, released = result[0], result[1]
        records = dict(result[2]) if len(result) > 2 else {}
        for hour, st in self.states.items():  # states recorded before the load succeeded
            if hour not in states:
                states[hour] = st
                self._unsaved_states.add(hour)
        self.states = states
        self.released |= released
        for rec in records.values():
            try:
                hour = parse_hour(rec['hour']) if 'hour' in rec else None
                if rec['kind'] == 'released':
                    at = parse_ts(rec['released_at']) if rec.get('released_at') else None
                    self.released_meta.setdefault((rec['team'], hour), (at, rec.get('source')))
                elif rec['kind'] == 'corrected':
                    self.corrected.add((rec['team'], hour))
            except Exception as e:
                logger.warning(f"[LABELS] Skipping unreadable state record: {e}")
        with self._records_lock:
            records.update(self.records)   # anything written in this run before the load is newer
            self.records = records
        self.loaded = True
        logger.info(f"[LABELS] Loaded {len(states)} hour states, {len(released)} released markers "
                    f"and {len(records)} other records")
        return True

    def record(self, now: datetime, profile: str, affected_regions) -> bool:
        """Record the state of the current hour if it is at or after HH:30 and not yet recorded."""
        hour = hour_floor(now)
        if now < hour + timedelta(minutes=30) or hour in self.states:
            return False
        self.states[hour] = (profile, tuple(affected_regions))
        self._unsaved_states.add(hour)
        return True

    def restorable_regions(self, now: datetime, max_age_hours: float = 6.0) -> list:
        """Affected regions of the newest recorded state, if it is recent and active.

        Lets a restarted generator carry on an outbreak with the same regions,
        so the labels match the events students saw before the restart.
        """
        if not self.states:
            return []
        hour = max(self.states)
        profile, affected = self.states[hour]
        if profile == 'baseline' or now - (hour + HOUR) > timedelta(hours=max_age_hours):
            return []
        return [r for r in affected if r in self.regions]

    def _flush_unsaved(self):
        if self.store is None:
            return
        for hour in sorted(self._unsaved_states):
            profile, affected = self.states[hour]
            if self.store.save_state(hour, profile, affected, hour + timedelta(minutes=30)):
                self._unsaved_states.discard(hour)
        still = []
        for team, hour, at, source in self._unsaved_released:
            ok = (self.store.save_released(team, hour, at, source) if source is not None
                  else self.store.save_released(team, hour, at))
            if not ok:
                still.append((team, hour, at, source))
        self._unsaved_released = still
        self._flush_records()

    def _flush_records(self):
        if self.store is None or not self.loaded:
            return
        with self._records_lock:
            pending = list(self._unsaved_records.items())
        for key, rec in pending:
            if self.store.save_record(rec):
                with self._records_lock:
                    if self._unsaved_records.get(key) is rec:   # not replaced meanwhile
                        del self._unsaved_records[key]

    def is_ai_hour(self, hour: datetime) -> bool:
        return self.ai is not None and hour >= self.ai.first_hour

    def _delay_for(self, hour: datetime) -> timedelta:
        return timedelta(hours=self.ai.delay_hours) if self.is_ai_hour(hour) else self.delay

    def due_hours(self, now: datetime) -> list:
        """Hours whose labels may be released: hour end + delay has passed."""
        return sorted(h for h in self.states if h + HOUR + self._delay_for(h) <= now)

    def ai_decisions(self, hour: datetime) -> dict:
        """{region: (true_status, ai_status)} for an AI hour. Drawn once, then read from the staff topic.

        A new decision is queued as ai_label records before any AI status of the hour is sent.
        """
        profile, affected = self.states[hour]
        out, drawn = {}, False
        for region in self.regions:
            rec = self.get_record(f"ai_label|{format_hour(hour)}|{region}")
            if rec is not None:
                out[region] = (rec['true_status'], rec['ai_status'])
                continue
            true = label_status(profile, affected, region)
            ai = ai_status(self.ai.seed, hour, region, true, self.ai.error_rate)
            out[region] = (true, ai)
            drawn = True
            self.queue_record({'kind': 'ai_label', 'hour': format_hour(hour), 'region': region,
                               'true_status': true, 'ai_status': ai, 'wrong': ai != true,
                               'ai_released_at': {}, 'correction_released_at': {}})
        if drawn:
            self._flush_records()   # best effort: record the decision before the first send
        return out

    def _note_release(self, hour: datetime, regions, field_name: str, team: str, at: datetime):
        """Add team -> time to field_name of the ai_label records of these regions (grading record)."""
        for region in regions:
            rec = self.get_record(f"ai_label|{format_hour(hour)}|{region}")
            if rec is None:
                continue
            rec = dict(rec)
            rec[field_name] = {**(rec.get(field_name) or {}), team: format_ts(at)}
            self.queue_record(rec)

    def release_due(self, now: datetime) -> int:
        """Send labels for every due (team, hour) not yet released. Returns messages sent."""
        if not self.loaded:
            return 0
        sent = 0
        teams = sorted(self.teams_fn())
        for hour in self.due_hours(now):
            profile, affected = self.states[hour]
            ai_hour = self.is_ai_hour(hour)
            if ai_hour:
                if not any((t, hour) not in self.released for t in teams):
                    continue
                decisions = self.ai_decisions(hour)
            for team in teams:
                if (team, hour) in self.released:
                    continue
                if ai_hour:
                    source = 'ai'
                    msgs = build_status_messages(hour, {r: d[1] for r, d in decisions.items()},
                                                 self.regions, now, source)
                else:
                    source = 'staff' if self.ai is not None and now >= self.ai.start else None
                    msgs = build_labels(hour, profile, affected, self.regions, now, source)
                if self.send_labels(team, msgs):
                    self.released.add((team, hour))
                    self.released_meta[(team, hour)] = (now, source)
                    self._unsaved_released.append((team, hour, now, source))
                    if ai_hour:
                        self._note_release(hour, self.regions, 'ai_released_at', team, now)
                    sent += len(msgs)
                    logger.info(f"[LABELS] Released {len(msgs)} labels for {format_hour(hour)} to {team}"
                                + (f" (source {source})" if source else ""))
        sent += self.release_corrections(now, teams)
        return sent

    def release_corrections(self, now: datetime, teams) -> int:
        """Send a staff record with the true status for each wrong AI status whose correction is due."""
        if self.ai is None:
            return 0
        delay = timedelta(hours=self.ai.correction_delay_hours)
        sent = 0
        teams = set(teams)
        for (team, hour), (at, source) in sorted(self.released_meta.items()):
            if source != 'ai' or team not in teams or (team, hour) in self.corrected or at is None:
                continue
            # Due after the delay, and strictly after the AI record's released_at (whole seconds),
            # so the newest released_at of a region-hour is the true status.
            if now < at + delay or format_ts(now) <= format_ts(at):
                continue
            if hour not in self.states:
                logger.warning(f"[LABELS] No state for {format_hour(hour)}; cannot correct its AI statuses")
                self.corrected.add((team, hour))
                continue
            decisions = self.ai_decisions(hour)
            wrong = [r for r in self.regions if decisions[r][0] != decisions[r][1]]
            if not wrong:
                self.corrected.add((team, hour))   # nothing to correct; no marker needed
                continue
            msgs = build_status_messages(hour, {r: decisions[r][0] for r in wrong}, wrong, now, 'staff')
            if self.send_labels(team, msgs):
                self.corrected.add((team, hour))
                self.queue_record({'kind': 'corrected', 'team': team, 'hour': format_hour(hour),
                                   'released_at': format_ts(now), 'regions': wrong})
                self._note_release(hour, wrong, 'correction_released_at', team, now)
                self._flush_records()   # write the marker at once: a restart must not send it again
                sent += len(msgs)
                logger.info(f"[LABELS] Released {len(msgs)} staff corrections for {format_hour(hour)} to {team}")
        return sent

    def tick(self, now: datetime, profile: str, affected_regions) -> int:
        self.ensure_loaded()
        self.record(now, profile, affected_regions)
        if self.loaded:
            self._flush_unsaved()
        sent = self.release_due(now)
        if self.loaded:
            self._flush_unsaved()
        self._cap_unsaved()
        return sent


@dataclass
class FalseAlarmConfig:
    start: datetime
    probability: float = 0.04        # chance that a burst starts in a given UTC hour
    minutes: float = 30.0            # planned length of a burst
    events_per_minute: float = 6.0   # extra symptom_report events per minute during a burst
    seed: str = '551'


def false_alarm_config_from_env():
    if FALSE_ALARM_START is None:
        return None
    return FalseAlarmConfig(FALSE_ALARM_START, FALSE_ALARM_PROBABILITY, FALSE_ALARM_MINUTES,
                            FALSE_ALARM_EVENTS_PER_MINUTE, CR_SEED)


class FalseAlarmService:
    """Bursts of extra symptom_report events in one region whose true status is LOW.

    The burst events go straight to the raw topics. They never enter the pool, never set
    the emitted state, and do not add to the symptom burden, so the true outbreak state
    and the labels do not change.

    Schedule: false_alarm_plan() per UTC hour (from cfg.start on). At the planned start the
    region is chosen with the plan's pick among the regions whose status (label rule on the
    emitted state) is LOW, in REGIONS order; if none is LOW the burst is recorded as
    skipped. A burst ends at its planned end, or earlier if its region stops being LOW.
    A new burst starts only within START_GRACE (2 minutes) of its planned start; a plan
    missed by more (another burst was running, or the generator was down or had not yet
    replayed the staff topic) is recorded as skipped with end_reason "missed". So bursts
    never overlap.

    Each burst is a false_alarm record in the staff topic (through LabelService.queue_record),
    written at the start, every checkpoint_seconds, and at the end. After a restart a burst
    whose window is still open resumes with its recorded region and count; one whose window
    closed while the generator was down is closed at its last event (end_reason "interrupted").
    Up to checkpoint_seconds of the count can be lost in a crash. Nothing starts before the
    staff topic has been replayed.
    """

    START_GRACE = timedelta(minutes=2)

    def __init__(self, cfg: FalseAlarmConfig, regions, labels: LabelService, send_event,
                 checkpoint_seconds: float = 60.0):
        self.cfg = cfg
        self.regions = list(regions)
        self.labels = labels
        self.send_event = send_event          # (region) -> bool
        self.checkpoint = timedelta(seconds=checkpoint_seconds)
        self.interval = timedelta(seconds=60.0 / cfg.events_per_minute)
        self.length = timedelta(minutes=cfg.minutes)
        self.active = None                    # the running burst record
        self._next_at = None
        self._last_saved = None
        self._closed_stale = False

    def plans_covering(self, now: datetime) -> list:
        """(planned_start, pick) of every plan whose window [start, start + length) contains now."""
        out = []
        back = int(self.length / HOUR) + 1
        for i in range(back, -1, -1):
            hour = hour_floor(now) - i * HOUR
            plan = false_alarm_plan(self.cfg.seed, hour, self.cfg.probability)
            if plan and plan[0] >= self.cfg.start and plan[0] <= now < plan[0] + self.length:
                out.append(plan)
        return out

    def _save(self, now: datetime):
        self.labels.queue_record(dict(self.active))
        self._last_saved = now

    def _end(self, now: datetime, reason: str):
        self.active.update(status='ended', end=self.active.get('last_event_at') or format_ts(now),
                           end_reason=reason)
        self._save(now)
        logger.info(f"[FALSE_ALARM] Burst in {self.active['region']} ended ({reason}): "
                    f"{self.active['events_added']} events added")
        self.active = None

    def _close_stale(self, now: datetime):
        for rec in self.labels.records_of_kind('false_alarm'):
            if rec.get('status') == 'running' and parse_ts(rec['planned_end']) <= now:
                rec = dict(rec, status='ended', end=rec.get('last_event_at') or rec.get('start'),
                           end_reason='interrupted')
                self.labels.queue_record(rec)

    def _maybe_start(self, now: datetime, profile: str, affected):
        for planned_start, pick in self.plans_covering(now):
            key_start = format_ts(planned_start)
            rec = self.labels.get_record(f"false_alarm|{key_start}")
            if rec is not None and rec.get('status') != 'running':
                continue                         # ended or skipped
            if rec is not None:                  # resume after a restart
                if label_status(profile, affected, rec['region']) != 'LOW':
                    self.active = dict(rec)
                    self._end(now, 'status_changed')
                    continue
                self.active = dict(rec)
                logger.info(f"[FALSE_ALARM] Resuming burst in {rec['region']} ({rec['events_added']} events so far)")
            else:
                low = [r for r in self.regions if label_status(profile, affected, r) == 'LOW']
                base = {'kind': 'false_alarm', 'planned_start': key_start,
                        'planned_end': format_ts(planned_start + self.length)}
                if now - planned_start > self.START_GRACE:
                    # Not started on time (another burst was running, or the generator was down
                    # or had not replayed the staff topic yet): record it as missed.
                    self.labels.queue_record(dict(base, status='skipped', region=None, start=None, end=None,
                                                  events_added=0, end_reason='missed'))
                    continue
                if not low:
                    self.labels.queue_record(dict(base, status='skipped', region=None, start=None, end=None,
                                                  events_added=0, end_reason='no_low_region'))
                    continue
                region = low[min(int(pick * len(low)), len(low) - 1)]
                self.active = dict(base, region=region, start=format_ts(now), end=None, events_added=0,
                                   last_event_at=None, status='running', end_reason=None)
                logger.info(f"[FALSE_ALARM] Burst started in {region} until {base['planned_end']}")
            self._next_at = now
            self._save(now)
            return

    def step(self, now: datetime, emitted_state) -> int:
        """Advance to now. Returns the number of burst events sent."""
        if now < self.cfg.start or emitted_state is None or not self.labels.loaded:
            return 0
        if not self._closed_stale:
            self._closed_stale = True
            self._close_stale(now)
        profile, affected = emitted_state
        if self.active is None:
            self._maybe_start(now, profile, affected)
            if self.active is None:
                return 0
        if now >= parse_ts(self.active['planned_end']):
            self._end(now, 'planned')
            return 0
        if label_status(profile, affected, self.active['region']) != 'LOW':
            self._end(now, 'status_changed')
            return 0
        if self._next_at < now - timedelta(seconds=5):   # the thread stalled: do not flood
            self._next_at = now
        sent = 0
        while self._next_at <= now:
            if self.send_event(self.active['region']):
                sent += 1
                self.active['events_added'] += 1
                self.active['last_event_at'] = format_ts(now)
            self._next_at += self.interval
        if now - self._last_saved >= self.checkpoint:
            self._save(now)
        return sent


# ============================================================================
# Event Generator
# ============================================================================

class EventGenerator:
    """Generates synthetic health events with outbreak scheduling."""

    SYMPTOMS = [
        'toasty_ears', 'brain_fizz', 'sleepy_socks', 'uncontrollable_hiccups',
        'temporary_purple_toenails', 'sudden_craving_for_pickles',
        'spontaneous_sneezing_fits', 'mild_grumpiness',
        'excessive_yawning', 'random_giggling', 'wobbly_knees', 'dramatic_sighing'
    ]

    VISIT_TYPES = [
        'routine_checkup', 'emergency', 'follow_up',
        'preventive_care', 'diagnostic_test', 'consultation'
    ]

    VACCINE_TYPES = ['anti_wobble_serum', 'giggle_pox_booster', 'zorbovian_flu_shot',
                     'purple_toe_prevention', 'hiccup_guard', 'grumpiness_inhibitor']
    INCIDENT_TYPES = ['acute_wobble_syndrome', 'spontaneous_pickle_craving', 'extreme_hiccup_episode',
                      'purple_toe_emergency', 'uncontrolled_giggling_fit', 'dramatic_sigh_collapse',
                      'zorbovian_sniffles_outbreak']
    ADMINISTERED_AT = ['clinic', 'pharmacy', 'hospital', 'mobile_unit']

    def __init__(self):
        self.producers = {}  # Map of team_id -> KafkaProducer
        self.running = False
        self.event_pool = queue.Queue(maxsize=EVENT_POOL_SIZE)
        self.state = OutbreakState()
        self.state_lock = Lock()
        # (profile, affected_regions tuple) of the newest event taken from the pool; None until one is.
        self.emitted_state = None
        self._topics_ready = set()   # teams whose label topic has unlimited retention
        self._topic_failures = {}
        self.labels = self._build_label_service()
        fa = false_alarm_config_from_env()
        self.false_alarms = (FalseAlarmService(fa, REGIONS, self.labels, self.send_false_alarm_event)
                             if fa is not None else None)

    def _build_label_service(self) -> LabelService:
        """Labels go to each team's Kafka; the durable state goes to the staff team's Kafka."""
        if TEAM_KAFKA_MAPPING:
            state_team = LABEL_STATE_TEAM if LABEL_STATE_TEAM in TEAM_KAFKA_MAPPING else None
            bootstrap = TEAM_KAFKA_MAPPING.get(state_team)
        else:
            state_team, bootstrap = 'shared', SINGLE_BOOTSTRAP
        store = None
        if state_team and bootstrap:
            store = KafkaStateStore(bootstrap, LABEL_STATE_TOPIC,
                                    lambda: self.producers.get(state_team))
        else:
            logger.warning(
                f"[LABELS] No Kafka entry for LABEL_STATE_TEAM={LABEL_STATE_TEAM!r}: label state is "
                f"kept in memory only, and a restart loses pending labels.")
        return LabelService(store, REGIONS, LABEL_DELAY_HOURS, self.send_labels,
                            lambda: list(self.producers.keys()), ai=ai_config_from_env())

    def send_false_alarm_event(self, region: str) -> bool:
        """Send one extra symptom_report for region to every team's raw topic, like the emit thread does.

        It does not go through the pool, does not set emitted_state, and does not add to the
        symptom burden. BAD_EVENT_RATE applies, as for every raw event.
        """
        if not self.producers:
            return False
        event = self.generate_symptom_report()
        event['region'] = region
        event['timestamp'] = datetime.now(timezone.utc).isoformat()
        event['source'] = 'event-generator'
        event['schema_version'] = '1.0'
        value = event
        if BAD_EVENT_RATE and random.random() < BAD_EVENT_RATE:
            value = make_bad_event(event)
        for team_id, producer in list(self.producers.items()):
            try:
                producer.send(raw_topic(team_id), value=value)
            except Exception as e:
                logger.error(f"[FALSE_ALARM] Error sending to {team_id}: {e}")
        return True

    def false_alarm_thread_worker(self):
        while self.running:
            time.sleep(0.5)
            try:
                self.false_alarms.step(datetime.now(timezone.utc), self.emitted_state)
            except Exception as e:
                logger.error(f"Error in false-alarm loop: {e}", exc_info=True)

    def _fill_pool(self, count: int):
        """Generate `count` events and queue them, each tagged with the state it was generated under.

        The pool is FIFO and is drained at the emit pace, so the events being emitted
        can be several hours older than self.state. The tag (profile, affected tuple)
        travels with the event but is never part of the emitted JSON; the emit thread
        copies it into self.emitted_state, and labels follow that.
        """
        with self.state_lock:
            tag = (self.state.profile, tuple(self.state.affected_regions))
        for _ in range(count):
            event = self.generate_event()
            try:
                self.event_pool.put((tag, event), block=False)
            except queue.Full:
                logger.warning("[REFILL] Event pool full, dropping event")
                break

    def _ensure_label_topic(self, team_id: str) -> bool:
        """Make sure the team's label topic exists with unlimited retention before the first send.

        Runs in the label thread only. A failure holds the labels (they are retried on the next
        tick) rather than letting the broker auto-create the topic with 24 h retention.
        """
        if team_id in self._topics_ready:
            return True
        bootstrap = SINGLE_BOOTSTRAP if team_id == 'shared' else TEAM_KAFKA_MAPPING.get(team_id)
        if not bootstrap:
            self._topics_ready.add(team_id)  # nothing to configure through
            return True
        if ensure_unlimited_topics(bootstrap, [label_topic(team_id)]):
            self._topics_ready.add(team_id)
            return True
        n = self._topic_failures[team_id] = self._topic_failures.get(team_id, 0) + 1
        if n == 1 or n % 20 == 0:
            logger.warning(f"[LABELS] Labels for {team_id} are held until {label_topic(team_id)} has "
                           f"unlimited retention (attempt {n}). Set retention.ms=-1 and retention.bytes=-1 by hand if this persists.")
        return False

    def send_labels(self, team_id: str, messages: list) -> bool:
        """Send one hour of labels to one team. True only if every message was acknowledged.

        Labels are never passed through make_bad_event.
        """
        producer = self.producers.get(team_id)
        if producer is None:
            return False
        if not self._ensure_label_topic(team_id):
            return False  # labels wait; event streaming is not affected
        topic = label_topic(team_id)
        try:
            futures = [producer.send(topic, key=key.encode('utf-8'), value=value) for key, value in messages]
            producer.flush(timeout=15)
            for future in futures:
                if future is not None:
                    future.get(timeout=15)  # raises if the send failed
            return True
        except Exception as e:
            logger.error(f"[LABELS] Could not send labels to {team_id}: {e}")
            return False

    def label_thread_worker(self):
        """Every LABEL_TICK_SECONDS: record the emitted state at HH:30, release labels that are due."""
        while self.running:
            time.sleep(LABEL_TICK_SECONDS)
            emitted = self.emitted_state
            if emitted is None:  # no event has been emitted yet
                continue
            try:
                profile, affected = emitted
                self.labels.tick(datetime.now(timezone.utc), profile, list(affected))
            except Exception as e:
                logger.error(f"Error in label loop: {e}", exc_info=True)

    def connect_kafka_for_team(self, team_id: str, bootstrap_server: str):
        """Initialize Kafka producer for a specific team."""
        max_retries = 5
        retry_delay = 3

        for attempt in range(max_retries):
            try:
                producer = KafkaProducer(
                    bootstrap_servers=[bootstrap_server],
                    # Bytes pass through unchanged: make_bad_event sends broken JSON.
                    value_serializer=lambda v: v if isinstance(v, bytes) else json.dumps(v).encode('utf-8'),
                    acks='all',
                    retries=3,
                    max_in_flight_requests_per_connection=1
                )
                logger.info(f"Connected to Kafka for {team_id}: {bootstrap_server}")
                return producer
            except Exception as e:
                logger.warning(f"Kafka connection for {team_id} attempt {attempt + 1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    logger.error(f"Failed to connect to Kafka for {team_id} after all retries")
                    return None

    def connect_all_kafka(self):
        """Initialize Kafka producers for all teams."""
        if TEAM_KAFKA_MAPPING:
            success_count = 0
            for team_id, bootstrap_server in TEAM_KAFKA_MAPPING.items():
                logger.info(f"Connecting to Kafka for team {team_id}...")
                producer = self.connect_kafka_for_team(team_id, bootstrap_server)
                if producer:
                    self.producers[team_id] = producer
                    success_count += 1
                else:
                    logger.warning(f"Skipping {team_id} - connection failed")

            logger.info(f"Connected to {success_count}/{len(TEAM_KAFKA_MAPPING)} team Kafka instances")
            if self.producers:
                connected_teams = sorted(list(self.producers.keys()))
                logger.info(f"Successfully connected teams: {', '.join(connected_teams)}")
            return success_count > 0
        elif SINGLE_BOOTSTRAP:
            logger.info("Connecting to single shared Kafka cluster...")
            producer = self.connect_kafka_for_team('shared', SINGLE_BOOTSTRAP)
            if producer:
                self.producers['shared'] = producer
                logger.info("Connected to shared Kafka cluster")
                return True
            logger.error("Failed to connect to shared Kafka cluster")
            return False
        else:
            logger.error("No Kafka configuration provided.")
            return False

    def generate_symptom_report(self) -> dict:
        """Generate a synthetic symptom report event."""
        with self.state_lock:
            _profile = self.state.profile
            _affected = list(self.state.affected_regions)
            _symptom_burden = self.state.symptom_burden

        available_beds = max(0, int(BASE_BEDS - _symptom_burden * BED_PRESSURE_FACTOR))

        # Realistic age distribution: mostly adults, some elderly, few children
        age_group = random.choices(['child', 'adult', 'elderly'], weights=[10, 70, 20])[0]
        if age_group == 'child':
            _age = random.randint(5, 17)
        elif age_group == 'adult':
            _age = random.randint(18, 64)
        else:
            _age = random.randint(65, 85)

        # Severity shifts with outbreak state
        if _profile == 'outbreak':
            _severity = random.choices(['mild', 'moderate', 'severe'], weights=[20, 40, 40])[0]
        elif _profile == 'winddown':
            _severity = random.choices(['mild', 'moderate', 'severe'], weights=[40, 35, 25])[0]
        else:  # baseline
            _severity = random.choices(['mild', 'moderate', 'severe'], weights=[60, 30, 10])[0]

        # During outbreak windows, skew region toward affected areas (75%); baseline stays uniform
        if _affected:
            _region = random.choice(_affected) if random.random() < 0.75 else random.choice(REGIONS)
        else:
            _region = random.choice(REGIONS)

        _duration_ranges = {'mild': (1, 5), 'moderate': (3, 10), 'severe': (7, 14)}
        _lo, _hi = _duration_ranges.get(_severity, (1, 14))

        return {
            'event_type': 'symptom_report',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'patient_id': f"P{random.randint(10000, 99999)}",
            'age': _age,
            'region': _region,
            'symptoms': random.sample(self.SYMPTOMS, random.randint(1, 4)),
            'suspected_pathogen': PATHOGEN_NAME,
            'severity': _severity,
            'duration_days': random.randint(_lo, _hi),
            'reported_via': random.choice(['mobile_app', 'web_portal', 'phone_hotline']),
            'available_beds': available_beds,
        }

    def generate_clinic_visit(self) -> dict:
        """Generate a synthetic clinic visit event."""
        with self.state_lock:
            _profile = self.state.profile
            available_beds = max(0, int(BASE_BEDS - self.state.symptom_burden * BED_PRESSURE_FACTOR))
            _affected = list(self.state.affected_regions)

        age_group = random.choices(['child', 'adult', 'elderly'], weights=[10, 70, 20])[0]
        if age_group == 'child':
            _age = random.randint(5, 17)
        elif age_group == 'adult':
            _age = random.randint(18, 64)
        else:
            _age = random.randint(65, 85)

        if _affected:
            _region = random.choice(_affected) if random.random() < 0.75 else random.choice(REGIONS)
        else:
            _region = random.choice(REGIONS)

        # VISIT_TYPES order: routine_checkup, emergency, follow_up, preventive_care, diagnostic_test, consultation
        if _profile == 'outbreak':
            _visit_type = random.choices(self.VISIT_TYPES, weights=[5, 30, 25, 5, 20, 15])[0]
            _temp_f = round(random.triangular(97.5, 103.5, 101.0), 1)
        elif _profile == 'winddown':
            _visit_type = random.choices(self.VISIT_TYPES, weights=[10, 20, 25, 8, 20, 17])[0]
            _temp_f = round(random.triangular(97.5, 103.5, 100.0), 1)
        else:
            _visit_type = random.choice(self.VISIT_TYPES)
            _temp_f = round(random.triangular(97.5, 103.5, 98.9), 1)

        return {
            'event_type': 'clinic_visit',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'visit_id': f"V{random.randint(100000, 999999)}",
            'patient_id': f"P{random.randint(10000, 99999)}",
            'age': _age,
            'clinic_id': f"C{random.randint(1, 50)}",
            'region': _region,
            'visit_type': _visit_type,
            'primary_complaint': random.choice(self.SYMPTOMS),
            'toastiness_level': _temp_f,
            'diagnosis_code': f"ZDX{random.randint(100, 999)}",
            'prescribed_medication': random.random() < 0.35,
            'follow_up_required': random.random() < 0.25,
            'available_beds': available_beds,
        }

    def generate_hospital_admission(self) -> dict:
        """Generate a synthetic hospital admission event."""
        with self.state_lock:
            _profile = self.state.profile
            _affected = list(self.state.affected_regions)
            available_beds = max(0, int(BASE_BEDS - self.state.symptom_burden * BED_PRESSURE_FACTOR))

        age_group = random.choices(['child', 'adult', 'elderly'], weights=[5, 55, 40])[0]
        if age_group == 'child':
            _age = random.randint(1, 17)
        elif age_group == 'adult':
            _age = random.randint(18, 64)
        else:
            _age = random.randint(65, 85)

        # Severity is outbreak-aware: more critical/severe cases during outbreak peaks
        if _profile == 'outbreak':
            _severity = random.choices(['mild', 'moderate', 'severe', 'critical'], weights=[10, 25, 35, 30])[0]
        elif _profile == 'winddown':
            _severity = random.choices(['mild', 'moderate', 'severe', 'critical'], weights=[20, 30, 30, 20])[0]
        else:
            _severity = random.choices(['mild', 'moderate', 'severe', 'critical'], weights=[40, 35, 18, 7])[0]

        if _affected:
            _region = random.choice(_affected) if random.random() < 0.75 else random.choice(REGIONS)
        else:
            _region = random.choice(REGIONS)

        _low_o2_prob = {'mild': 0.05, 'moderate': 0.15, 'severe': 0.40, 'critical': 0.65}.get(_severity, 0.20)
        if random.random() < _low_o2_prob:
            _o2 = round(random.uniform(85.0, 92.0), 1)
        else:
            _o2 = round(random.uniform(92.0, 99.5), 1)

        _temp_params = {
            'mild':     (98.5, 101.5, 99.5),
            'moderate': (99.0, 103.0, 100.8),
            'severe':   (100.0, 104.5, 102.0),
            'critical': (101.0, 105.5, 103.0),
        }
        _lo, _hi, _mode = _temp_params.get(_severity, (98.5, 105.0, 101.2))

        _los_rate = {'mild': 1.0, 'moderate': 0.5, 'severe': 0.25, 'critical': 0.12}.get(_severity, 0.35)

        return {
            'event_type': 'hospital_admission',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'admission_id': f"HA{random.randint(100000, 999999)}",
            'patient_id': f"P{random.randint(10000, 99999)}",
            'age': _age,
            'hospital_id': f"H{random.randint(1, 20)}",
            'region': _region,
            'admission_reason': random.choice(self.SYMPTOMS),
            'suspected_pathogen': PATHOGEN_NAME,
            'severity': _severity,
            'toastiness_level': round(random.triangular(_lo, _hi, _mode), 1),
            'fizz_level': _o2,
            'expected_los_days': min(21, max(1, round(random.expovariate(_los_rate)))),
            'available_beds': available_beds,
        }

    def generate_vaccination(self) -> dict:
        """Generate a synthetic vaccination event."""
        with self.state_lock:
            _affected = list(self.state.affected_regions)

        age_group = random.choices(['child', 'adult', 'elderly'], weights=[20, 55, 25])[0]
        if age_group == 'child':
            _age = random.randint(1, 17)
        elif age_group == 'adult':
            _age = random.randint(18, 64)
        else:
            _age = random.randint(65, 85)

        if _affected:
            _region = random.choice(_affected) if random.random() < 0.75 else random.choice(REGIONS)
        else:
            _region = random.choice(REGIONS)

        _vaccine_type = random.choice(self.VACCINE_TYPES)
        _max_doses = {'anti_wobble_serum': 1, 'giggle_pox_booster': 2, 'zorbovian_flu_shot': 3,
                      'purple_toe_prevention': 2, 'hiccup_guard': 1, 'grumpiness_inhibitor': 2}.get(_vaccine_type, 2)
        if _max_doses == 1:
            dose_number = 1
        elif _max_doses == 2:
            dose_number = random.choices([1, 2], weights=[65, 35])[0]
        else:
            dose_number = random.choices([1, 2, 3], weights=[60, 30, 10])[0]

        adverse = random.random() < 0.05

        return {
            'event_type': 'vaccination',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'vaccination_id': f"VAC{random.randint(100000, 999999)}",
            'patient_id': f"P{random.randint(10000, 99999)}",
            'age': _age,
            'region': _region,
            'vaccine_type': _vaccine_type,
            'dose_number': dose_number,
            'administered_at': random.choice(self.ADMINISTERED_AT),
            'healthcare_provider_id': f"HP{random.randint(100, 999)}",
            'lot_number': f"LOT{random.randint(1000, 9999)}",
            'adverse_reaction': adverse,
            'adverse_reaction_type': random.choice(['mild_soreness', 'toasty_ears', 'sleepy_socks', 'sparkly_sneezes']) if adverse else None,
        }

    def generate_emergency_incident(self) -> dict:
        """Generate a synthetic emergency incident event."""
        with self.state_lock:
            _profile = self.state.profile
            _affected = list(self.state.affected_regions)

        age_group = random.choices(['child', 'adult', 'elderly'], weights=[10, 55, 35])[0]
        if age_group == 'child':
            _age = random.randint(5, 17)
        elif age_group == 'adult':
            _age = random.randint(18, 64)
        else:
            _age = random.randint(65, 85)

        # During outbreaks: more respiratory_distress and cardiac_event, slower response
        if _profile == 'outbreak':
            incident_type = random.choices(
                self.INCIDENT_TYPES,
                weights=[35, 25, 10, 10, 8, 7, 5]
            )[0]
            severity = random.choices(['moderate', 'severe', 'critical'], weights=[20, 40, 40])[0]
            response_time = round(random.triangular(5, 40, 18), 1)
        elif _profile == 'winddown':
            incident_type = random.choices(
                self.INCIDENT_TYPES,
                weights=[25, 20, 15, 11, 10, 12, 7]
            )[0]
            severity = random.choices(['moderate', 'severe', 'critical'], weights=[35, 40, 25])[0]
            response_time = round(random.triangular(4, 35, 14), 1)
        else:  # baseline
            incident_type = random.choices(
                self.INCIDENT_TYPES,
                weights=[15, 15, 20, 12, 12, 16, 10]
            )[0]
            severity = random.choices(['moderate', 'severe', 'critical'], weights=[50, 35, 15])[0]
            response_time = round(random.triangular(4, 25, 10), 1)

        # Outcome weighted by severity
        if severity == 'critical':
            _outcome = random.choices(['stable', 'admitted', 'critical', 'discharged'], weights=[15, 50, 25, 10])[0]
        elif severity == 'severe':
            _outcome = random.choices(['stable', 'admitted', 'critical', 'discharged'], weights=[30, 45, 5, 20])[0]
        else:
            _outcome = random.choices(['stable', 'admitted', 'critical', 'discharged'], weights=[35, 25, 2, 38])[0]

        # Critical/admitted outcomes always require hospital transport; others 80% probability
        if _outcome in ('critical', 'admitted'):
            transported = True
        else:
            transported = random.random() < 0.80

        if _affected:
            _region = random.choice(_affected) if random.random() < 0.75 else random.choice(REGIONS)
        else:
            _region = random.choice(REGIONS)

        return {
            'event_type': 'emergency_incident',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'incident_id': f"EI{random.randint(100000, 999999)}",
            'patient_id': f"P{random.randint(10000, 99999)}",
            'age': _age,
            'region': _region,
            'incident_type': incident_type,
            'severity': severity,
            'response_time_minutes': response_time,
            'triage_level': (
                random.randint(1, 2) if severity == 'critical' else
                random.randint(2, 3) if severity == 'severe' else
                random.randint(3, 5)
            ),
            'transported_to_hospital': transported,
            'hospital_id': f"H{random.randint(1, 20)}" if transported else None,
            'outcome': _outcome,
        }

    def generate_general_health_report(self) -> dict:
        """Generate a synthetic general health report event."""
        with self.state_lock:
            _affected = list(self.state.affected_regions)

        age_group = random.choices(['child', 'adult', 'elderly'], weights=[10, 65, 25])[0]
        if age_group == 'child':
            _age = random.randint(5, 17)
        elif age_group == 'adult':
            _age = random.randint(18, 64)
        else:
            _age = random.randint(65, 85)

        bounce_index = round(random.triangular(17.0, 45.0, 27.5), 1)

        # Age-group appropriate blood pressure ranges
        if _age < 18:
            bp_systolic = random.randint(90, 120)
            bp_diastolic = random.randint(55, 80)
        elif _age < 65:
            bp_systolic = random.randint(110, 155)
            bp_diastolic = random.randint(65, 95)
        else:
            bp_systolic = random.randint(120, 170)
            bp_diastolic = random.randint(70, 100)

        glitter_level = round(random.triangular(150, 300, 195))

        _kazoo_habit = random.choices(['never', 'former', 'current'], weights=[60, 25, 15])[0]
        _sprinkle_sensitivity = random.choices(['none', 'mild', 'high'], weights=[70, 20, 10])[0]
        _kazoo_penalty = {'never': 0.0, 'former': 0.5, 'current': 1.5}.get(_kazoo_habit, 0.0)
        _sprinkle_penalty = {'none': 0.0, 'mild': 0.5, 'high': 1.0}.get(_sprinkle_sensitivity, 0.0)
        _health_score = max(1, min(10, round(
            10 - abs(bounce_index - 22) * 0.15
            - max(0, _age - 30) * 0.03
            - _kazoo_penalty
            - _sprinkle_penalty
            + random.gauss(0, 1)
        )))

        if _affected:
            _region = random.choice(_affected) if random.random() < 0.75 else random.choice(REGIONS)
        else:
            _region = random.choice(REGIONS)

        return {
            'event_type': 'general_health_report',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'report_id': f"GHR{random.randint(100000, 999999)}",
            'patient_id': f"P{random.randint(10000, 99999)}",
            'age': _age,
            'region': _region,
            'bounce_index': bounce_index,
            'wobble_pressure_high': bp_systolic,
            'wobble_pressure_low': bp_diastolic,
            'hops_per_minute': random.randint(55, 100),
            'glitter_level': glitter_level,
            'kazoo_habit': _kazoo_habit,
            'sprinkle_sensitivity': _sprinkle_sensitivity,
            'overall_health_score': _health_score,
            'last_checkup_days_ago': random.randint(0, 730),
        }

    def generate_event(self) -> dict:
        """Generate an event based on current outbreak state."""
        with self.state_lock:
            profile = self.state.profile
            intensity = self.state.intensity

        # Choose event type based on outbreak profile
        # Note: severity distributions are handled inside each generator method
        if profile == "outbreak":
            event_weights = normalize_weights(outbreak_event_weights(intensity))
        elif profile == "winddown":
            event_weights = normalize_weights(winddown_event_weights(intensity))
        else:  # baseline
            event_weights = normalize_weights(baseline_event_weights())

        # Weighted choice of event type
        event_type = random.choices(
            list(event_weights.keys()),
            weights=list(event_weights.values()),
        )[0]

        if event_type == 'symptom_report':
            return self.generate_symptom_report()
        elif event_type == 'clinic_visit':
            return self.generate_clinic_visit()
        elif event_type == 'hospital_admission':
            return self.generate_hospital_admission()
        elif event_type == 'vaccination':
            return self.generate_vaccination()
        elif event_type == 'emergency_incident':
            return self.generate_emergency_incident()
        else:  # general_health_report
            return self.generate_general_health_report()

    def refill_thread_worker(self):
        """Refill thread: checks outbreak schedule every 6h, generates batch."""
        last_refill = datetime.now(timezone.utc) - timedelta(hours=6)  # trigger immediate refill on startup
        _was_active = False  # track previous outbreak state to detect transitions

        while self.running:
            now = datetime.now(timezone.utc)

            # Check if it's time to refill (every 6 hours)
            if (now - last_refill).total_seconds() < 21600:  # 6h in seconds
                time.sleep(60)  # Check every minute
                continue

            try:
                last_refill = now

                # Check outbreak window
                is_active, profile, intensity, _ = is_in_outbreak_window(now)

                with self.state_lock:
                    self.state.profile = profile
                    self.state.intensity = intensity
                    if is_active:
                        # Only pick new affected regions when transitioning into outbreak;
                        # keep the same regions for subsequent 6h checks within the same window
                        if not _was_active:
                            # After a restart in the middle of a window, keep the regions
                            # recorded before the restart so the labels match the events.
                            restored = self.labels.restorable_regions(now)
                            if restored:
                                self.state.affected_regions = restored
                                logger.info(f"[REFILL] Restored affected regions after restart: {restored}")
                            else:
                                if not self.labels.loaded:
                                    logger.warning(
                                        "[REFILL] The state topic could not be replayed, so affected regions are "
                                        "sampled anew. If this is a restart in the middle of an outbreak, the "
                                        "labels for the hours before the restart and the events after it can disagree.")
                                k = min(random.randint(2, 4), len(REGIONS))
                                self.state.affected_regions = random.sample(REGIONS, k)
                    else:
                        self.state.affected_regions = []
                        self.state.symptom_burden = 0.0
                _was_active = is_active

                with self.state_lock:
                    _logged_regions = list(self.state.affected_regions)
                logger.info(
                    f"[REFILL] Outbreak state updated: profile={profile}, intensity={intensity:.2f}, "
                    f"affected_regions={_logged_regions or 'all (baseline)'}"
                )

                # Generate batch of events
                batch_size = max(0, EVENT_POOL_REFILL_THRESHOLD - self.event_pool.qsize())
                if batch_size > 0:
                    logger.info(f"[REFILL] Generating {batch_size} events...")
                    self._fill_pool(batch_size)
                    logger.info(f"[REFILL] Pool now has {self.event_pool.qsize()}/{EVENT_POOL_SIZE} events")

            except Exception as e:
                logger.error(f"Error in refill loop: {e}", exc_info=True)
                time.sleep(60)

    def emit_thread_worker(self):
        """Emit thread: drains pool to Kafka at variable pace based on outbreak profile."""
        logger.info(f"Starting emission to {len(self.producers)} teams")
        event_count = 0
        bad_count = 0
        failed_sends = {}
        _burden_cap = BASE_BEDS / BED_PRESSURE_FACTOR
        _last_burden_decay_time = time.monotonic()

        while self.running:
            try:
                # Get event from pool (wait max 5s)
                try:
                    tag, event = self.event_pool.get(timeout=5)
                except queue.Empty:
                    logger.warning("[EMIT] Pool empty; waiting for RefillThread to catch up")
                    time.sleep(1)
                    continue
                # The state this event was generated under: labels follow it.
                self.emitted_state = tag

                # Determine pace based on profile
                with self.state_lock:
                    profile = self.state.profile

                _base_sleep = 1.0 / EVENT_RATE_PER_SEC
                if profile == "outbreak":
                    sleep_time = random.uniform(_base_sleep * 0.5, _base_sleep * 1.5)
                elif profile == "winddown":
                    sleep_time = random.uniform(_base_sleep * 1.5, _base_sleep * 3.0)
                else:  # baseline
                    sleep_time = random.uniform(_base_sleep * 2.0, _base_sleep * 4.0)

                # Stamp the event when it is sent. The refill thread builds a
                # 6-hour batch at once, so a build-time stamp put every event
                # in a batch at the same moment.
                event['timestamp'] = datetime.now(timezone.utc).isoformat()
                event['source'] = 'event-generator'
                event['schema_version'] = '1.0'

                # Every team gets the same value, so a bad event is the same
                # for all teams.
                value = event
                if BAD_EVENT_RATE and random.random() < BAD_EVENT_RATE:
                    value = make_bad_event(event)
                    bad_count += 1

                # Send to all teams
                for team_id, producer in self.producers.items():
                    if team_id == 'shared':
                        topic = EXPLICIT_TOPIC if EXPLICIT_TOPIC else f"{TOPIC_PREFIX}{TOPIC_SUFFIX.lstrip('.')}"
                    else:
                        topic = f"{TOPIC_PREFIX}{team_id}{TOPIC_SUFFIX}"

                    def _on_error(exc, tid=team_id):
                        failed_sends[tid] = failed_sends.get(tid, 0) + 1
                        if failed_sends[tid] % 10 == 1:
                            logger.error(f"Error sending to {tid}: {exc}")

                    def _on_success(_, tid=team_id):
                        failed_sends.pop(tid, None)

                    try:
                        producer.send(topic, value=value).add_callback(_on_success).add_errback(_on_error)
                    except Exception as e:
                        _on_error(e)

                # Update symptom burden: increment for high-acuity events, time-based decay always
                event_type = event.get('event_type', '')
                _now_mono = time.monotonic()
                _elapsed = _now_mono - _last_burden_decay_time
                _last_burden_decay_time = _now_mono
                with self.state_lock:
                    # Apply time-proportional exponential decay regardless of event type
                    self.state.symptom_burden *= SYMPTOM_BURDEN_DECAY ** _elapsed
                    if event_type in ['symptom_report', 'hospital_admission']:
                        self.state.symptom_burden = min(
                            self.state.symptom_burden + 1, _burden_cap
                        )

                event_count += 1

                # Flush every 100 events
                if event_count % 100 == 0:
                    for producer in self.producers.values():
                        try:
                            producer.flush(timeout=5)
                        except Exception as e:
                            logger.error(f"Flush error: {e}")
                    team_names = ', '.join(sorted(self.producers.keys()))
                    logger.info(f"[EMIT] Produced {event_count} events ({bad_count} bad) → teams: [{team_names}]")

                time.sleep(sleep_time)

            except Exception as e:
                logger.error(f"Error in emission loop: {e}")
                time.sleep(1)

    def reconnect_missing_teams_worker(self):
        """Every 60s, retries any team still missing from self.producers.

        connect_all_kafka() only tries once, at startup — this is what lets
        a team recover later without a pod restart.
        """
        while self.running:
            time.sleep(60)
            if not TEAM_KAFKA_MAPPING:
                continue
            missing = [t for t in TEAM_KAFKA_MAPPING if t not in self.producers]
            for team_id in missing:
                logger.info(f"Retrying previously-failed Kafka connection for {team_id}...")
                producer = self.connect_kafka_for_team(team_id, TEAM_KAFKA_MAPPING[team_id])
                if producer:
                    self.producers[team_id] = producer
                    logger.info(f"Recovered Kafka connection for {team_id} — resuming emission to it")

    def start(self):
        """Start the event generator."""
        if not self.connect_all_kafka():
            logger.error("Cannot start generator without at least one Kafka connection")
            return False

        self.running = True

        # Replay the staff state topic before the first refill, so a restart in the
        # middle of an outbreak keeps its affected regions. A failure here is
        # retried by the label thread; nothing is released before it succeeds.
        self.labels.ensure_loaded()

        # Start RefillThread (generates events periodically)
        Thread(target=self.refill_thread_worker, daemon=True).start()
        logger.info("RefillThread started (generates batch every 6h, checks outbreak schedule)")

        # Start EmitThread (drains pool to Kafka)
        Thread(target=self.emit_thread_worker, daemon=True).start()
        logger.info("EmitThread started (continuous emission at variable pace)")

        # Start LabelThread (records each hour's state, releases delayed labels)
        Thread(target=self.label_thread_worker, daemon=True).start()
        logger.info(f"LabelThread started (labels released {LABEL_DELAY_HOURS}h after each hour ends)")
        if self.labels.ai is not None:
            ai = self.labels.ai
            logger.info(f"AI labels from {format_ts(ai.start)} (delay {ai.delay_hours}h, error rate "
                        f"{ai.error_rate}, corrections after {ai.correction_delay_hours}h)")
        if self.false_alarms is not None:
            fa = self.false_alarms.cfg
            Thread(target=self.false_alarm_thread_worker, daemon=True).start()
            logger.info(f"FalseAlarmThread started (from {format_ts(fa.start)}, p={fa.probability}/hour, "
                        f"{fa.minutes} min, {fa.events_per_minute} events/min)")

        # Start ReconnectThread (retries teams skipped at startup)
        Thread(target=self.reconnect_missing_teams_worker, daemon=True).start()
        logger.info("ReconnectThread started (retries any team skipped at startup, every 60s)")

        return True

    def stop(self):
        """Stop the event generator."""
        self.running = False
        for team_id, producer in self.producers.items():
            try:
                producer.close(timeout=5)
                logger.info(f"Closed producer for {team_id}")
            except Exception as e:
                logger.error(f"Error closing producer for {team_id}: {e}")
        logger.info("Event generator stopped")


def main():
    """Main entry point."""
    global _generator

    if not TEAM_KAFKA_MAPPING and not SINGLE_BOOTSTRAP:
        global _waiting_for_config
        _waiting_for_config = True
        logger.info("No active Kafka clusters configured — event generation paused.")
        logger.info("Waiting for teams to be deployed. Pod will be restarted automatically when teams are added.")
        health_thread = Thread(target=lambda: serve(app, host='0.0.0.0', port=8000), daemon=True)
        health_thread.start()
        while True:
            time.sleep(60)
            logger.info("No active Kafka clusters configured — still waiting.")

    generator = EventGenerator()

    if not generator.start():
        logger.error("Failed to start event generator")
        return 1

    _generator = generator

    # Graceful shutdown: flush and close Kafka producers on SIGTERM/SIGINT
    def _shutdown(signum, frame):
        logger.info(f"Shutting down (signal {signum})...")
        generator.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Start health check server (blocks until process is terminated)
    logger.info("Starting health check server on port 8000")
    serve(app, host='0.0.0.0', port=8000)


if __name__ == '__main__':
    sys.exit(main() or 0)
