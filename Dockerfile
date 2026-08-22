# Docker image for the lineabot backend (FastAPI + APScheduler).
# Used on Hugging Face Spaces (Docker SDK): the container must listen on
# 0.0.0.0:7860, which HF proxies to the public https://<space>.hf.space URL.
FROM python:3.11-slim

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./

EXPOSE 7860

# REFRESH_MINUTES <= 0 disables the internal scheduler (see server.py).
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7860"]
