FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the sonar-tools (echelon-demo.py + sonar_client.py)
COPY sonar-tools/ /app/sonar-tools/

# Copy the API server
COPY api.py .

# The sonar_client module is imported by echelon-demo.py
ENV PYTHONPATH="/app/sonar-tools:${PYTHONPATH}"

# Expose port (Render assigns $PORT, default 8000)
EXPOSE 8000

# Start the server — Render sets $PORT
CMD uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}
