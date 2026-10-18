# Container for the Cygnus backend (API + dashboard + TwiML webhooks).
# Deployed to Google Cloud Run. Cloud Run sets $PORT; uvicorn must bind to it.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Cloud Run provides PORT (default 8080). Bind to 0.0.0.0.
ENV PORT=8080
CMD ["sh", "-c", "uvicorn backend.api:app --host 0.0.0.0 --port ${PORT}"]
