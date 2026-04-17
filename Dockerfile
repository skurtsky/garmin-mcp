# Dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY garmin_client.py .
COPY server.py .
COPY tools/ ./tools/

# Create token directory
RUN mkdir -p /root/.garminconnect

EXPOSE 8000

CMD ["python", "server.py"]