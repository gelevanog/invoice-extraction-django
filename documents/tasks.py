"""Celery tasks. In eager mode (CELERY_TASK_ALWAYS_EAGER) ``delay`` runs inline."""

from __future__ import annotations

from celery import shared_task
from django.db import transaction

from documents import services
from documents.models import Document


@shared_task(name="documents.process_document", acks_late=True)
def process_document_task(document_id: int) -> str:
    return services.process_document(document_id).status


def enqueue_processing(document: Document) -> None:
    """Queue processing once the surrounding transaction has committed."""
    document_id = document.pk
    transaction.on_commit(lambda: process_document_task.delay(document_id))
