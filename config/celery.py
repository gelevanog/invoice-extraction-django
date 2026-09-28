"""Celery application. With CELERY_TASK_ALWAYS_EAGER=true tasks run inline (tests, local)."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("docextract")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
