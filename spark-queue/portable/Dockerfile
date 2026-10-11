# Dockerfile for the Spark queue controller
# Enforces the cluster-wide 4-slot cap by flipping Job.spec.suspend

FROM python:3.11-slim
LABEL org.opencontainers.image.source=https://github.com/langd0n-classes/data-eng-at-scale-infra

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY scripts/queue-controller.py .

# Run the controller
CMD ["python", "queue-controller.py"]
