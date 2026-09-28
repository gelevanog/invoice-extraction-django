from typing import Any

from django.conf import settings
from django.http import HttpRequest


def pipeline_settings(request: HttpRequest) -> dict[str, Any]:
    cfg = settings.DOCEXTRACT
    return {
        "llm_provider": cfg["LLM_PROVIDER"],
        "confidence_threshold": cfg["REVIEW_CONFIDENCE_THRESHOLD"],
        "base_currency": cfg["BASE_CURRENCY"],
    }
