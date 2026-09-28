#!/bin/sh
# Container entrypoint: `web` (migrate, seed, gunicorn) or `worker` (Celery).
set -e

case "$1" in
  web)
    python manage.py migrate --noinput
    if [ "${SEED_DEMO:-true}" = "true" ]; then
      # Idempotent: vendors are get_or_create'd and already-imported files are skipped.
      python manage.py seed_demo --with-samples
    fi
    exec gunicorn config.wsgi:application \
      --bind 0.0.0.0:8000 \
      --workers "${GUNICORN_WORKERS:-3}" \
      --access-logfile -
    ;;
  worker)
    exec celery -A config worker --loglevel="${CELERY_LOG_LEVEL:-info}" --concurrency="${CELERY_CONCURRENCY:-2}"
    ;;
  *)
    exec "$@"
    ;;
esac
