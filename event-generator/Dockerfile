# Dockerfile for Event Generator
# Produces synthetic health events for Kafka testing and demos

FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/event_generator.py .

# Health check endpoint port
EXPOSE 8000

# Run the generator
CMD ["python", "event_generator.py"]
