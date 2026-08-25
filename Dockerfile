# Docker image for the lineabot backend (FastAPI + APScheduler).
# Portable across hosts: Cloud Run / Railway inject PORT and health-check it,
# while Hugging Face Spaces expects 7860 - so listen on $PORT with a 7860
# default. On Cloud Run, run with --min-instances 1 and --no-cpu-throttling so
# the in-process APScheduler keeps scanning while the instance is idle.
FROM python:3.11-slim

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./

EXPOSE 7860

# REFRESH_MINUTES <= 0 disables the internal scheduler (see server.py).
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-7860}"]
