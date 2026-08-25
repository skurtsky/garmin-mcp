# Dockerfile
FROM python:3.12-slim

WORKDIR /app

# WeasyPrint renders the training-plan PDF; it needs pango/harfbuzz at runtime.
# Liberation Sans is metric-compatible with Arial, which the chart CSS targets.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
        fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY garmin_client.py .
COPY server.py .
COPY db.py .
COPY sync_garmin.py .
COPY tools/ ./tools/

# Create token directory
RUN mkdir -p /root/.garminconnect

EXPOSE 8000

CMD ["python", "server.py"]