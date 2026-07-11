FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

RUN pip install pipenv
COPY Pipfile Pipfile.lock* ./
RUN pipenv install --system --deploy --ignore-pipfile || pipenv install --system --skip-lock

COPY . .

# Run as non-root user (security best practice / HIPAA)
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
RUN chown -R appuser:appgroup /app
USER appuser

EXPOSE 8000

# e2-medium has 2 vCPUs — use 5 workers (2*CPUs + 1), 2 threads each
# Cloud Run passes PORT env var, default to 8000
CMD gunicorn config.wsgi:application \
    --bind 0.0.0.0:${PORT} \
    --workers 5 \
    --threads 2 \
    --timeout 120 \
    --keep-alive 5 \
    --log-level info \
    --access-logfile - \
    --error-logfile -
