#!/usr/bin/env python3
"""
Event Generator
Produces synthetic health events to Kafka for testing and demos.

Supports:
- Multi-tenant (per-team) Kafka clusters via TEAM_BOOTSTRAP_SERVERS
- Single shared Kafka cluster via KAFKA_BOOTSTRAP_SERVERS
- Configurable topic naming and event streams
"""

import os
import json
import time
import random
import logging
from datetime import datetime, timedelta
from threading import Thread
from flask import Flask
from kafka import KafkaProducer
from faker import Faker

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

"""Environment configuration"""
EVENT_RATE_PER_SEC = float(os.getenv('EVENT_RATE_PER_SEC', '10'))
RATE_PER_TEAM = os.getenv('RATE_PER_TEAM', 'false').lower() == 'true'  # when true, rate applies per team

# Topic naming
TOPIC_PREFIX = os.getenv('TOPIC_PREFIX', 'events.team')
TOPIC_SUFFIX = os.getenv('TOPIC_SUFFIX', '.raw')
EXPLICIT_TOPIC = os.getenv('TOPIC')  # optional explicit topic name (used for single-cluster mode)

# Streams: use consistent names
DEFAULT_STREAMS = 'symptom_report,clinic_visit,environmental_conditions'
EVENT_STREAMS = [s.strip() for s in os.getenv('EVENT_STREAMS', DEFAULT_STREAMS).split(',') if s.strip()]

# Regions
REGIONS = [r.strip() for r in os.getenv('REGIONS', 'Boston,Cambridge,Somerville,Brookline,Newton').split(',') if r.strip()]

# Optional deterministic seed
RANDOM_SEED = os.getenv('RANDOM_SEED')
if RANDOM_SEED is not None:
    try:
        seed_val = int(RANDOM_SEED)
    except ValueError:
        seed_val = sum(ord(c) for c in RANDOM_SEED)
    random.seed(seed_val)


"""Kafka bootstrap configuration"""
# Multi-team mapping: teamId=bootstrap,teamId=bootstrap
TEAM_KAFKA_MAPPING = {}
team_bootstrap_env = os.getenv('TEAM_BOOTSTRAP_SERVERS', '')
if team_bootstrap_env:
    for entry in team_bootstrap_env.split(','):
        if '=' in entry:
            team_id, bootstrap = entry.split('=', 1)
            TEAM_KAFKA_MAPPING[team_id.strip()] = bootstrap.strip()

# Single shared cluster
SINGLE_BOOTSTRAP = os.getenv('KAFKA_BOOTSTRAP_SERVERS')

logger.info(f"Loaded {len(TEAM_KAFKA_MAPPING)} team Kafka mappings")

# Initialize Faker
fake = Faker()

# Health check Flask app
app = Flask(__name__)

@app.route('/health')
def health():
    return {'status': 'healthy', 'timestamp': datetime.utcnow().isoformat()}

@app.route('/ready')
def ready():
    return {'status': 'ready', 'teams': len(TEAM_KAFKA_MAPPING), 'rate': EVENT_RATE_PER_SEC}


class EventGenerator:
    """Generates synthetic health events"""

    SYMPTOMS = [
        'fever', 'cough', 'fatigue', 'headache', 'sore_throat',
        'shortness_of_breath', 'body_aches', 'loss_of_taste',
        'loss_of_smell', 'nausea', 'diarrhea', 'congestion'
    ]

    VISIT_TYPES = [
        'routine_checkup', 'emergency', 'follow_up',
        'vaccination', 'diagnostic_test', 'consultation'
    ]

    CONDITIONS = [
        'temperature', 'humidity', 'air_quality_index',
        'pollen_count', 'uv_index'
    ]

    def __init__(self):
        self.producers = {}  # Map of team_id (or 'shared') -> KafkaProducer
        self.running = False
        self.total_sent = 0
        self.last_log_time = time.time()

    def connect_kafka_for_team(self, team_id, bootstrap_server):
        """Initialize Kafka producer for a specific team or shared cluster"""
        max_retries = 5
        retry_delay = 3

        for attempt in range(max_retries):
            try:
                producer = KafkaProducer(
                    bootstrap_servers=[bootstrap_server],
                    value_serializer=lambda v: json.dumps(v).encode('utf-8'),
                    acks='all',
                    retries=3,
                    max_in_flight_requests_per_connection=1,
                    linger_ms=int(os.getenv('PRODUCER_LINGER_MS', '5')),
                    batch_size=int(os.getenv('PRODUCER_BATCH_SIZE', '16384'))
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
        """Initialize Kafka producers for either multi-team or single-cluster mode"""
        success_count = 0

        if TEAM_KAFKA_MAPPING:
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
            logger.error("No Kafka configuration provided. Set TEAM_BOOTSTRAP_SERVERS or KAFKA_BOOTSTRAP_SERVERS.")
            return False

    def generate_symptom_report(self):
        """Generate a synthetic symptom report event"""
        return {
            'event_type': 'symptom_report',
            'timestamp': datetime.utcnow().isoformat(),
            'patient_id': f"P{random.randint(10000, 99999)}",
            'age': random.randint(1, 90),
            'region': random.choice(REGIONS),
            'symptoms': random.sample(self.SYMPTOMS, random.randint(1, 4)),
            'severity': random.choice(['mild', 'moderate', 'severe']),
            'duration_days': random.randint(1, 14),
            'reported_via': random.choice(['mobile_app', 'web_portal', 'phone_hotline'])
        }

    def generate_clinic_visit(self):
        """Generate a synthetic clinic visit event"""
        return {
            'event_type': 'clinic_visit',
            'timestamp': datetime.utcnow().isoformat(),
            'visit_id': f"V{random.randint(100000, 999999)}",
            'patient_id': f"P{random.randint(10000, 99999)}",
            'clinic_id': f"C{random.randint(1, 50)}",
            'region': random.choice(REGIONS),
            'visit_type': random.choice(self.VISIT_TYPES),
            'primary_complaint': random.choice(self.SYMPTOMS),
            'temperature_f': round(random.uniform(97.0, 104.0), 1),
            'diagnosis_code': f"ICD{random.randint(100, 999)}",
            'prescribed_medication': random.choice([True, False]),
            'follow_up_required': random.choice([True, False])
        }

    def generate_environmental_condition(self):
        """Generate a synthetic environmental conditions event"""
        return {
            'event_type': 'environmental_conditions',
            'timestamp': datetime.utcnow().isoformat(),
            'region': random.choice(REGIONS),
            'station_id': f"S{random.randint(1, 20)}",
            'temperature_f': round(random.uniform(20.0, 95.0), 1),
            'humidity_percent': random.randint(30, 95),
            'air_quality_index': random.randint(0, 200),
            'pollen_count': random.randint(0, 500),
            'uv_index': random.randint(0, 11),
            'wind_speed_mph': round(random.uniform(0, 25), 1)
        }

    def generate_event(self, stream_type):
        """Generate an event based on stream type"""
        if stream_type == 'symptom_report':
            return self.generate_symptom_report()
        elif stream_type == 'clinic_visit':
            return self.generate_clinic_visit()
        elif stream_type == 'environmental_conditions':
            return self.generate_environmental_condition()
        else:
            logger.warning(f"Unknown stream type: {stream_type}")
            return None

    def _topic_for_team(self, team_id: str) -> str:
        if EXPLICIT_TOPIC:
            return EXPLICIT_TOPIC
        if team_id == 'shared':
            return f"{TOPIC_PREFIX}{TOPIC_SUFFIX}"
        return f"{TOPIC_PREFIX}{team_id}{TOPIC_SUFFIX}"

    def produce_events(self):
        """Main event production loop - sends events to all configured producers"""
        logger.info(f"Starting event production for {len(self.producers)} producer(s)")
        rate_desc = f"{EVENT_RATE_PER_SEC} events/sec"
        if RATE_PER_TEAM and len(self.producers) > 1:
            rate_desc += " per team"
        logger.info(f"Event rate: {rate_desc}")
        logger.info(f"Event streams: {EVENT_STREAMS}")

        # Calculate sleep interval per loop iteration
        effective_rate = EVENT_RATE_PER_SEC
        if RATE_PER_TEAM and len(self.producers) > 0:
            effective_rate = EVENT_RATE_PER_SEC
        sleep_interval = 1.0 / max(effective_rate, 0.001)
        event_count = 0
        failed_sends = {}  # Track failures per team

        while self.running:
            try:
                # Generate one event
                stream_type = random.choice(EVENT_STREAMS)
                event = self.generate_event(stream_type)

                if event:
                    # Common metadata
                    event['source'] = 'event-generator'
                    event['schema_version'] = '1.0'

                    # Send same event to all teams
                    for team_id, producer in self.producers.items():
                        topic = self._topic_for_team(team_id)

                        try:
                            # Use a key for partitioning when patient_id or visit_id exists
                            key_field = event.get('patient_id') or event.get('visit_id') or event.get('station_id')
                            key_bytes = str(key_field).encode('utf-8') if key_field else None
                            producer.send(topic, value=event, key=key_bytes)

                            # Reset failure count on success
                            if team_id in failed_sends:
                                del failed_sends[team_id]

                        except Exception as e:
                            failed_sends[team_id] = failed_sends.get(team_id, 0) + 1

                            # Log every 10th failure to avoid spam
                            if failed_sends[team_id] % 10 == 1:
                                logger.error(f"Error sending to {team_id} (failure #{failed_sends[team_id]}): {e}")

                    event_count += 1

                    # Flush producers every 100 events to ensure delivery
                    if event_count % 100 == 0:
                        for team_id, producer in self.producers.items():
                            try:
                                producer.flush(timeout=5)
                            except Exception as e:
                                logger.error(f"Error flushing producer for {team_id}: {e}")
                                failed_sends[team_id] = failed_sends.get(team_id, 0) + 1

                        # Log basic stats every 100 events
                        logger.info(f"Produced {event_count} events to {len(self.producers)} destination(s)")

                        # Log team names every 500 events for verification
                        if event_count % 500 == 0:
                            team_list = sorted(list(self.producers.keys()))
                            logger.info(f"Active destinations: {', '.join(team_list)}")

                        if failed_sends:
                            logger.warning(f"Failed sends: {failed_sends}")

                time.sleep(sleep_interval)

            except Exception as e:
                logger.error(f"Error in production loop: {e}")
                time.sleep(1)

    def start(self):
        """Start the event generator"""
        if not self.connect_all_kafka():
            logger.error("Cannot start generator without at least one Kafka connection")
            return False

        self.running = True
        Thread(target=self.produce_events, daemon=True).start()
        logger.info("Event generator started")
        return True

    def stop(self):
        """Stop the event generator"""
        self.running = False
        for team_id, producer in self.producers.items():
            try:
                producer.close()
                logger.info(f"Closed producer for {team_id}")
            except Exception as e:
                logger.error(f"Error closing producer for {team_id}: {e}")
        logger.info("Event generator stopped")


def main():
    """Main entry point"""
    if not TEAM_KAFKA_MAPPING and not SINGLE_BOOTSTRAP:
        logger.error("No Kafka configuration provided.")
        logger.error("Set TEAM_BOOTSTRAP_SERVERS for multi-team or KAFKA_BOOTSTRAP_SERVERS for single-cluster mode.")
        return 1

    generator = EventGenerator()

    if not generator.start():
        logger.error("Failed to start event generator")
        return 1

    # Start health check server
    logger.info("Starting health check server on port 8000")
    app.run(host='0.0.0.0', port=8000)


if __name__ == '__main__':
    main()
