# Dockerfile for Event Generator
# Produces synthetic health events for Kafka testing and demos

# Digest-pinned so the base can't silently change under this build —
# resolved from python:3.11-slim (the multi-arch index digest, not a
# single-platform manifest, so both amd64/arm64 builds stay possible).
FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce
LABEL org.opencontainers.image.source=https://github.com/langd0n-classes/data-eng-at-scale-infra

WORKDIR /app

# Install dependencies
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/event_generator.py .

# Health check endpoint port (also used for readiness/liveness probes)
EXPOSE 8000

# Run the generator
CMD ["python", "event_generator.py"]
