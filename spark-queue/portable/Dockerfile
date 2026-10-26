# Dockerfile for the Spark queue controller
# Enforces the cluster-wide 4-slot cap by flipping Job.spec.suspend

# Digest-pinned so the base can't silently change under this build —
# resolved from python:3.11-slim (the multi-arch index digest, not a
# single-platform manifest, so both amd64/arm64 builds stay possible).
FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce
LABEL org.opencontainers.image.source=https://github.com/langd0n-classes/data-eng-at-scale-infra

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY scripts/queue-controller.py .

# Run as a non-root UID — the controller only needs Kubernetes API access
# (via its own ServiceAccount token), never OS-level root.
RUN useradd --no-create-home --uid 1000 controller && chown -R controller:controller /app
USER controller

# Run the controller
CMD ["python", "queue-controller.py"]
