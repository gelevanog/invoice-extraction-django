from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings as django_settings
from rest_framework.test import APIClient

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"


@pytest.fixture(autouse=True)
def _pipeline_settings(settings: Any, tmp_path: Path) -> Iterator[None]:
    """Every test runs offline: fake LLM, static FX, eager Celery, throwaway media dir."""
    settings.DOCEXTRACT = {
        **django_settings.DOCEXTRACT,
        "LLM_PROVIDER": "fake",
        "FX_PROVIDER": "static",
        "FX_RATES_FILE": "",
        "LINE_ITEM_CATEGORIZER": "keyword",
        "REVIEW_CONFIDENCE_THRESHOLD": 0.75,
        "REVIEW_ON_WARNINGS": False,
        "REVIEW_NEW_VENDORS": False,
        "BASE_CURRENCY": "EUR",
    }
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.STORAGES = {
        **django_settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    yield


@pytest.fixture
def sample_dir() -> Path:
    return SAMPLE_DIR


@pytest.fixture
def demo_vendors(db: None) -> list[Any]:
    from documents.management.commands.seed_demo import DEMO_VENDORS
    from documents.models import Vendor

    return [Vendor.objects.create(**data) for data in DEMO_VENDORS]


@pytest.fixture
def reviewer(django_user_model: Any) -> Any:
    return django_user_model.objects.create_user(username="reviewer", password="test-pass-123")


@pytest.fixture
def api_client(reviewer: Any) -> APIClient:
    client = APIClient()
    client.force_authenticate(reviewer)
    return client
