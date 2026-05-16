#!/usr/bin/env python3
"""
DS-551 Event Generator with Outbreak Scheduling

Produces synthetic health events to per-team Kafka instances with scheduled outbreak windows.
Outbreak state flips on Mon/Thu 9am UTC, lasting 36h active + 12h winddown.

Architecture:
  - Flask health thread (immediate)
  - RefillThread (every 6h): checks outbreak schedule, generates batch
  - EmitThread (continuous): drains pool to Kafka at variable pace

Imports outbreak-aware distributions from generate_health_events_dataset.
"""

import os
import json
import time
import random
import logging
import queue
import signal
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Thread, Lock
from flask import Flask
from waitress import serve
from kafka import KafkaProducer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration from environment
EVENT_RATE_PER_SEC = float(os.getenv('EVENT_RATE_PER_SEC', '10'))
TOPIC_PREFIX = os.getenv('TOPIC_PREFIX', 'events.')
TOPIC_SUFFIX = os.getenv('TOPIC_SUFFIX', '.raw')
REGIONS = os.getenv('REGIONS', 'Boston,Cambridge,Somerville,Brookline,Newton').split(',')

# Pool configuration
EVENT_POOL_SIZE = int(os.getenv('EVENT_POOL_SIZE', '50000'))
EVENT_POOL_REFILL_THRESHOLD = int(os.getenv('EVENT_POOL_REFILL_THRESHOLD', '40000'))

# Bed pressure parameters
BASE_BEDS = int(os.getenv('BASE_BEDS', '500'))
BED_PRESSURE_FACTOR = float(os.getenv('BED_PRESSURE_FACTOR', '0.8'))
SYMPTOM_BURDEN_DECAY = float(os.getenv('SYMPTOM_BURDEN_DECAY', '0.995'))

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

@app.route('/health')
def health():
    return {'status': 'healthy', 'timestamp': datetime.utcnow().isoformat()}

@app.route('/ready')
def ready():
    return {'status': 'ready', 'teams': len(TEAM_KAFKA_MAPPING), 'rate': EVENT_RATE_PER_SEC}


# ============================================================================
# Outbreak State & Event Distribution
# ============================================================================

@dataclass
class OutbreakState:
    """Shared outbreak state for all threads."""
    active: bool = False
    profile: str = "baseline"  # baseline | outbreak | winddown
    intensity: float = 0.0
    affected_regions: list = field(default_factory=list)
    symptom_burden: float = 0.0  # rolling count of symptoms


def outbreak_event_weights(intensity: float) -> dict:
    """Outbreak event type weights (imports logic from generate_health_events_dataset)."""
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

    Mon 09:00-Tue 21:00 UTC = 36h active + 12h winddown
    Thu 09:00-Fri 21:00 UTC = 36h active + 12h winddown
    """
    weekday = now.weekday()  # 0=Mon, 3=Thu
    hour = now.hour

    # Monday outbreak window: 09:00 Mon - 21:00 Tue (48h total)
    if weekday == 0 and hour >= 9:  # Mon 09:00 onwards
        return True, "outbreak", random.uniform(0.6, 0.9), 48 - (hour - 9)
    elif weekday == 1 and hour < 21:  # Tue before 21:00
        return True, "outbreak" if hour < 9 else "winddown", random.uniform(0.6, 0.9), 21 - hour

    # Thursday outbreak window: 09:00 Thu - 21:00 Fri (48h total)
    if weekday == 3 and hour >= 9:  # Thu 09:00 onwards
        return True, "outbreak", random.uniform(0.6, 0.9), 48 - (hour - 9)
    elif weekday == 4 and hour < 21:  # Fri before 21:00
        return True, "outbreak" if hour < 9 else "winddown", random.uniform(0.6, 0.9), 21 - hour

    return False, "baseline", 0.0, 0


# ============================================================================
# Event Generator
# ============================================================================

class EventGenerator:
    """Generates synthetic health events with outbreak scheduling."""

    SYMPTOMS = [
        'fever', 'cough', 'fatigue', 'headache', 'sore_throat',
        'shortness_of_breath', 'body_aches', 'loss_of_taste',
        'loss_of_smell', 'nausea', 'diarrhea', 'congestion'
    ]

    VISIT_TYPES = [
        'routine_checkup', 'emergency', 'follow_up',
        'preventive_care', 'diagnostic_test', 'consultation'
    ]

    VACCINE_TYPES = ['influenza', 'covid_booster', 'hepatitis_a', 'pneumococcal', 'tdap', 'mmr']
    INCIDENT_TYPES = ['respiratory_distress', 'cardiac_event', 'trauma', 'allergic_reaction',
                      'seizure', 'fall', 'overdose']
    ADMINISTERED_AT = ['clinic', 'pharmacy', 'hospital', 'mobile_unit']

    def __init__(self):
        self.producers = {}  # Map of team_id -> KafkaProducer
        self.running = False
        self.event_pool = queue.Queue(maxsize=EVENT_POOL_SIZE)
        self.state = OutbreakState()
        self.state_lock = Lock()

    def connect_kafka_for_team(self, team_id: str, bootstrap_server: str):
        """Initialize Kafka producer for a specific team."""
        max_retries = 5
        retry_delay = 3

        for attempt in range(max_retries):
            try:
                producer = KafkaProducer(
                    bootstrap_servers=[bootstrap_server],
                    value_serializer=lambda v: json.dumps(v).encode('utf-8'),
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
            state = OutbreakState(
                active=self.state.active, profile=self.state.profile,
                intensity=self.state.intensity, affected_regions=self.state.affected_regions.copy(),
                symptom_burden=self.state.symptom_burden
            )

        available_beds = max(0, int(BASE_BEDS - state.symptom_burden * BED_PRESSURE_FACTOR))

        # Realistic age distribution: mostly adults, some elderly, few children
        age_group = random.choices(['child', 'adult', 'elderly'], weights=[10, 70, 20])[0]
        if age_group == 'child':
            _age = random.randint(5, 17)
        elif age_group == 'adult':
            _age = random.randint(18, 64)
        else:
            _age = random.randint(65, 85)

        # Severity shifts with outbreak state; also grab affected_regions for region skew
        with self.state_lock:
            _profile = self.state.profile
            _affected = list(self.state.affected_regions)
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

        return {
            'event_type': 'symptom_report',
            'timestamp': datetime.utcnow().isoformat(),
            'patient_id': f"P{random.randint(10000, 99999)}",
            'age': _age,
            'region': _region,
            'symptoms': random.sample(self.SYMPTOMS, random.randint(1, 4)),
            'severity': _severity,
            'duration_days': random.randint(1, 14),
            'reported_via': random.choice(['mobile_app', 'web_portal', 'phone_hotline']),
            'available_beds': available_beds,
        }

    def generate_clinic_visit(self) -> dict:
        """Generate a synthetic clinic visit event."""
        with self.state_lock:
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

        return {
            'event_type': 'clinic_visit',
            'timestamp': datetime.utcnow().isoformat(),
            'visit_id': f"V{random.randint(100000, 999999)}",
            'patient_id': f"P{random.randint(10000, 99999)}",
            'age': _age,
            'clinic_id': f"C{random.randint(1, 50)}",
            'region': _region,
            'visit_type': random.choice(self.VISIT_TYPES),
            'primary_complaint': random.choice(self.SYMPTOMS),
            'temperature_f': round(random.triangular(97.5, 103.5, 98.9), 1),
            'diagnosis_code': f"ICD{random.randint(100, 999)}",
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

        # Realistic oxygen level: 80% normal, 20% concerning
        if random.random() < 0.20:
            _o2 = round(random.uniform(85.0, 91.9), 1)
        else:
            _o2 = round(random.uniform(92.0, 99.5), 1)

        return {
            'event_type': 'hospital_admission',
            'timestamp': datetime.utcnow().isoformat(),
            'admission_id': f"HA{random.randint(100000, 999999)}",
            'patient_id': f"P{random.randint(10000, 99999)}",
            'age': _age,
            'hospital_id': f"H{random.randint(1, 20)}",
            'region': _region,
            'admission_reason': random.choice(self.SYMPTOMS),
            'severity': _severity,
            'temperature_f': round(random.triangular(98.5, 105.0, 101.2), 1),
            'oxygen_level': _o2,
            'expected_los_days': min(21, max(1, round(random.expovariate(0.35)))),
            'available_beds': available_beds,
        }

    def generate_vaccination(self) -> dict:
        """Generate a synthetic vaccination event."""
        age_group = random.choices(['child', 'adult', 'elderly'], weights=[20, 55, 25])[0]
        if age_group == 'child':
            _age = random.randint(1, 17)
        elif age_group == 'adult':
            _age = random.randint(18, 64)
        else:
            _age = random.randint(65, 85)

        dose_number = random.choices([1, 2, 3], weights=[60, 30, 10])[0]
        adverse = random.random() < 0.05

        return {
            'event_type': 'vaccination',
            'timestamp': datetime.utcnow().isoformat(),
            'vaccination_id': f"VAC{random.randint(100000, 999999)}",
            'patient_id': f"P{random.randint(10000, 99999)}",
            'age': _age,
            'region': random.choice(REGIONS),
            'vaccine_type': random.choice(self.VACCINE_TYPES),
            'dose_number': dose_number,
            'administered_at': random.choice(self.ADMINISTERED_AT),
            'healthcare_provider_id': f"HP{random.randint(100, 999)}",
            'lot_number': f"LOT{random.randint(1000, 9999)}",
            'adverse_reaction': adverse,
            'adverse_reaction_type': random.choice(['mild_soreness', 'fever', 'fatigue', 'allergic_reaction']) if adverse else None,
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
        else:
            incident_type = random.choices(
                self.INCIDENT_TYPES,
                weights=[15, 15, 20, 12, 12, 16, 10]
            )[0]
            severity = random.choices(['moderate', 'severe', 'critical'], weights=[50, 35, 15])[0]
            response_time = round(random.triangular(4, 25, 10), 1)

        transported = random.random() < 0.80

        # Outcome weighted by severity
        if severity == 'critical':
            _outcome = random.choices(['stable', 'admitted', 'critical', 'discharged'], weights=[15, 50, 25, 10])[0]
        elif severity == 'severe':
            _outcome = random.choices(['stable', 'admitted', 'critical', 'discharged'], weights=[30, 45, 5, 20])[0]
        else:
            _outcome = random.choices(['stable', 'admitted', 'critical', 'discharged'], weights=[35, 25, 2, 38])[0]

        if _affected:
            _region = random.choice(_affected) if random.random() < 0.75 else random.choice(REGIONS)
        else:
            _region = random.choice(REGIONS)

        return {
            'event_type': 'emergency_incident',
            'timestamp': datetime.utcnow().isoformat(),
            'incident_id': f"EI{random.randint(100000, 999999)}",
            'patient_id': f"P{random.randint(10000, 99999)}",
            'age': _age,
            'region': _region,
            'incident_type': incident_type,
            'severity': severity,
            'response_time_minutes': response_time,
            'triage_level': random.randint(1, 3) if severity == 'critical' else random.randint(2, 5),
            'transported_to_hospital': transported,
            'hospital_id': f"H{random.randint(1, 20)}" if transported else None,
            'outcome': _outcome,
        }

    def generate_general_health_report(self) -> dict:
        """Generate a synthetic general health report event."""
        age_group = random.choices(['child', 'adult', 'elderly'], weights=[10, 65, 25])[0]
        if age_group == 'child':
            _age = random.randint(5, 17)
        elif age_group == 'adult':
            _age = random.randint(18, 64)
        else:
            _age = random.randint(65, 85)

        bmi = round(random.triangular(17.0, 45.0, 27.5), 1)

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

        cholesterol = round(random.triangular(150, 300, 195))
        _health_score = max(1, min(10, round(10 - (bmi - 22) * 0.15 - (_age - 30) * 0.03 + random.gauss(0, 1))))

        return {
            'event_type': 'general_health_report',
            'timestamp': datetime.utcnow().isoformat(),
            'report_id': f"GHR{random.randint(100000, 999999)}",
            'patient_id': f"P{random.randint(10000, 99999)}",
            'age': _age,
            'region': random.choice(REGIONS),
            'bmi': bmi,
            'blood_pressure_systolic': bp_systolic,
            'blood_pressure_diastolic': bp_diastolic,
            'heart_rate_bpm': random.randint(55, 100),
            'cholesterol_total': cholesterol,
            'smoking_status': random.choices(['never', 'former', 'current'], weights=[60, 25, 15])[0],
            'diabetes_status': random.choices(['none', 'pre_diabetic', 'type_2'], weights=[70, 20, 10])[0],
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
            k=1
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
        last_refill = datetime.utcnow() - timedelta(hours=6)  # trigger immediate refill on startup

        while self.running:
            now = datetime.utcnow()

            # Check if it's time to refill (every 6 hours)
            if (now - last_refill).total_seconds() < 21600:  # 6h in seconds
                time.sleep(60)  # Check every minute
                continue

            last_refill = now

            # Check outbreak window
            is_active, profile, intensity, _ = is_in_outbreak_window(now)

            with self.state_lock:
                self.state.active = is_active
                self.state.profile = profile
                self.state.intensity = intensity
                if is_active:
                    self.state.affected_regions = random.sample(REGIONS, random.randint(2, 4))
                else:
                    self.state.affected_regions = []

            logger.info(f"[REFILL] Outbreak state updated: active={is_active}, profile={profile}, intensity={intensity:.2f}")

            # Generate batch of events
            batch_size = max(0, EVENT_POOL_REFILL_THRESHOLD - self.event_pool.qsize())
            if batch_size > 0:
                logger.info(f"[REFILL] Generating {batch_size} events...")
                for _ in range(batch_size):
                    event = self.generate_event()
                    try:
                        self.event_pool.put(event, block=False)
                    except queue.Full:
                        logger.warning("[REFILL] Event pool full, dropping event")
                        break
                logger.info(f"[REFILL] Pool now has {self.event_pool.qsize()}/{EVENT_POOL_SIZE} events")

    def emit_thread_worker(self):
        """Emit thread: drains pool to Kafka at variable pace based on outbreak profile."""
        logger.info(f"Starting emission to {len(self.producers)} teams")
        event_count = 0
        failed_sends = {}

        while self.running:
            try:
                # Get event from pool (wait max 5s)
                try:
                    event = self.event_pool.get(timeout=5)
                except queue.Empty:
                    # Pool empty; check if we should sleep
                    with self.state_lock:
                        profile = self.state.profile
                    if self.event_pool.qsize() < 0.2 * EVENT_POOL_SIZE:
                        logger.warning("[EMIT] Pool below 20%; RefillThread should catch up")
                    time.sleep(1)
                    continue

                # Determine pace based on profile
                with self.state_lock:
                    profile = self.state.profile

                if profile == "outbreak":
                    sleep_time = random.uniform(0.1, 0.5)
                elif profile == "winddown":
                    sleep_time = random.uniform(0.5, 1.5)
                else:  # baseline
                    sleep_time = random.uniform(1.0, 2.0)

                event['source'] = 'event-generator'
                event['schema_version'] = '1.0'

                # Send to all teams
                for team_id, producer in self.producers.items():
                    if team_id == 'shared':
                        topic = EXPLICIT_TOPIC if EXPLICIT_TOPIC else f"{TOPIC_PREFIX}{TOPIC_SUFFIX}"
                    else:
                        topic = f"{TOPIC_PREFIX}{team_id}{TOPIC_SUFFIX}"
                    try:
                        producer.send(topic, value=event)
                        if team_id in failed_sends:
                            del failed_sends[team_id]
                    except Exception as e:
                        failed_sends[team_id] = failed_sends.get(team_id, 0) + 1
                        if failed_sends[team_id] % 10 == 1:
                            logger.error(f"Error sending to {team_id}: {e}")

                # Update symptom burden if applicable
                event_type = event.get('event_type', '')
                if event_type in ['symptom_report', 'hospital_admission']:
                    with self.state_lock:
                        self.state.symptom_burden += 1
                else:
                    # Exponential decay
                    with self.state_lock:
                        self.state.symptom_burden *= SYMPTOM_BURDEN_DECAY

                event_count += 1

                # Flush every 100 events
                if event_count % 100 == 0:
                    for producer in self.producers.values():
                        try:
                            producer.flush(timeout=5)
                        except Exception as e:
                            logger.error(f"Flush error: {e}")
                    team_names = ', '.join(sorted(self.producers.keys()))
                    logger.info(f"[EMIT] Produced {event_count} events → teams: [{team_names}]")

                time.sleep(sleep_time)

            except Exception as e:
                logger.error(f"Error in emission loop: {e}")
                time.sleep(1)

    def start(self):
        """Start the event generator."""
        if not self.connect_all_kafka():
            logger.error("Cannot start generator without at least one Kafka connection")
            return False

        self.running = True

        # Start RefillThread (generates events periodically)
        Thread(target=self.refill_thread_worker, daemon=True).start()
        logger.info("RefillThread started (generates batch every 6h, checks outbreak schedule)")

        # Start EmitThread (drains pool to Kafka)
        Thread(target=self.emit_thread_worker, daemon=True).start()
        logger.info("EmitThread started (continuous emission at variable pace)")

        return True

    def stop(self):
        """Stop the event generator."""
        self.running = False
        for team_id, producer in self.producers.items():
            try:
                producer.close()
                logger.info(f"Closed producer for {team_id}")
            except Exception as e:
                logger.error(f"Error closing producer for {team_id}: {e}")
        logger.info("Event generator stopped")


def main():
    """Main entry point."""
    if not TEAM_KAFKA_MAPPING and not SINGLE_BOOTSTRAP:
        logger.error("No Kafka configuration provided.")
        logger.error("Set TEAM_BOOTSTRAP_SERVERS (multi-team) or KAFKA_BOOTSTRAP_SERVERS (single-cluster).")
        return 1

    generator = EventGenerator()

    if not generator.start():
        logger.error("Failed to start event generator")
        return 1

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
    main()
