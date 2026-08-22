# Docker image for the lineabot backend (FastAPI + APScheduler).
# Portable across hosts: Railway injects PORT and health-checks it, while
# Hugging Face Spaces expects 7860 - so listen on $PORT with a 7860 default.
FROM python:3.11-slim

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./

EXPOSE 7860

# REFRESH_MINUTES <= 0 disables the internal scheduler (see server.py).
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-7860}"]
