from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings as django_settings
from rest_framework.test import APIClient

from extraction.ocr.tesseract import TesseractOcrEngine

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"
OCR_SAMPLES = ("07_lantern_print_scan.pdf", "08_copperleaf_catering_photo.jpg")
HAS_TESSERACT = TesseractOcrEngine.is_available()


def pytest_configure(config: pytest.Config) -> None:
    # CI sets REQUIRE_TESSERACT=true so OCR tests can never be skipped there by accident.
    if os.environ.get("REQUIRE_TESSERACT", "").lower() == "true" and not HAS_TESSERACT:
        raise pytest.UsageError("REQUIRE_TESSERACT=true but the tesseract binary is not on PATH")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if HAS_TESSERACT:
        return
    skip = pytest.mark.skip(reason="tesseract binary not installed (apt install tesseract-ocr)")
    for item in items:
        if "requires_tesseract" in item.keywords:
            item.add_marker(skip)


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
        "OCR_ENGINE": "tesseract",
        "OCR_LANGUAGES": "eng",
        "OCR_DPI": 300,
        "OCR_MIN_CONFIDENCE": 0.65,
        "FEWSHOT_MAX_EXAMPLES": 3,
        "FEWSHOT_MAX_CHARS": 4000,
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
def text_sample_dir(tmp_path: Path) -> Path:
    """The samples that need no OCR (API/UI tests should not depend on Tesseract)."""
    folder = tmp_path / "text_samples"
    folder.mkdir()
    for path in SAMPLE_DIR.iterdir():
        if path.is_file() and path.name not in OCR_SAMPLES:
            (folder / path.name).write_bytes(path.read_bytes())
    return folder


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
