"""Django settings. Every deployment-specific value comes from the environment."""

from __future__ import annotations

from pathlib import Path

import environ
import structlog

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env", overwrite=False)

SECRET_KEY = env("DJANGO_SECRET_KEY", default="insecure-dev-key-change-me")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "rest_framework",
    "rest_framework.authtoken",
    "drf_spectacular",
    "documents",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "documents.context_processors.pipeline_settings",
            ],
        },
    },
]

DATABASES = {"default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}")}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", default="UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = env.path("MEDIA_ROOT", default=str(BASE_DIR / "media"))
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
DATA_UPLOAD_MAX_MEMORY_SIZE = env.int("MAX_UPLOAD_MB", default=20) * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = DATA_UPLOAD_MAX_MEMORY_SIZE

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "documents:queue"
LOGOUT_REDIRECT_URL = "login"

# --- REST API -----------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": env.int("API_PAGE_SIZE", default=25),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}
SPECTACULAR_SETTINGS = {
    "TITLE": "DocExtract API",
    "DESCRIPTION": "Upload business documents and retrieve validated, structured invoice data.",
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# --- Celery -------------------------------------------------------------------------
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=True)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_WORKER_HIJACK_ROOT_LOGGER = False  # keep the structlog formatting configured below
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]

# --- Pipeline -----------------------------------------------------------------------
DOCEXTRACT = {
    "LLM_PROVIDER": env("LLM_PROVIDER", default="fake"),
    "ANTHROPIC_MODEL": env("ANTHROPIC_MODEL", default="claude-sonnet-5"),
    "OPENAI_MODEL": env("OPENAI_MODEL", default="gpt-5.4-mini"),
    "LLM_TIMEOUT_SECONDS": env.float("LLM_TIMEOUT_SECONDS", default=120.0),
    "LLM_MAX_ATTEMPTS": env.int("LLM_MAX_ATTEMPTS", default=3),
    "REVIEW_CONFIDENCE_THRESHOLD": env.float("REVIEW_CONFIDENCE_THRESHOLD", default=0.75),
    "REVIEW_ON_WARNINGS": env.bool("REVIEW_ON_WARNINGS", default=False),
    "REVIEW_NEW_VENDORS": env.bool("REVIEW_NEW_VENDORS", default=False),
    "AMOUNT_TOLERANCE": env("AMOUNT_TOLERANCE", default="0.02"),
    "VENDOR_MATCH_THRESHOLD": env.float("VENDOR_MATCH_THRESHOLD", default=85.0),
    "BASE_CURRENCY": env("BASE_CURRENCY", default="EUR"),
    "FX_PROVIDER": env("FX_PROVIDER", default="static"),
    "FX_RATES_FILE": env("FX_RATES_FILE", default=""),
    "FX_API_URL": env("FX_API_URL", default="https://api.frankfurter.dev/v1"),
    "LINE_ITEM_CATEGORIZER": env("LINE_ITEM_CATEGORIZER", default="keyword"),
}

# --- Logging (structlog on top of stdlib logging) -----------------------------------
LOG_LEVEL = env("LOG_LEVEL", default="INFO")
LOG_FORMAT = env("LOG_FORMAT", default="console")  # console | json

_shared_processors: list[structlog.typing.Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso"),
]
structlog.configure(
    processors=[*_shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {
            "()": structlog.stdlib.ProcessorFormatter,
            "processors": [
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer()
                if LOG_FORMAT == "json"
                else structlog.dev.ConsoleRenderer(colors=False),
            ],
            "foreign_pre_chain": _shared_processors,
        }
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "structured"}},
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.db.backends": {"level": "WARNING"},
        "httpx": {"level": "WARNING"},
    },
}
