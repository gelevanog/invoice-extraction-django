.PHONY: install dev worker test lint format typecheck demo feedback samples docker-up docker-down

install:  ## Install dependencies into .venv
	uv sync

dev: install  ## Migrate, seed demo data and run the dev server (tasks run inline)
	uv run python manage.py migrate
	DEMO_USER_PASSWORD=$${DEMO_USER_PASSWORD:-demo} uv run python manage.py seed_demo --with-samples
	DJANGO_DEBUG=true uv run python manage.py runserver

worker:  ## Run a Celery worker (needs Redis and CELERY_TASK_ALWAYS_EAGER=false)
	uv run celery -A config worker --loglevel=info

test:  ## Run the test suite (offline, SQLite, eager Celery)
	uv run pytest

lint:  ## Ruff lint + format check, mypy on the pipeline core
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format:  ## Auto-fix lint issues and format
	uv run ruff check --fix .
	uv run ruff format .

demo:  ## Process sample_data/ and print the batch summary
	uv run python manage.py migrate --verbosity 0
	uv run python manage.py seed_demo
	uv run python manage.py process_folder sample_data/

feedback:  ## Reviewer-correction loop (few-shot examples), then the accuracy report
	uv run python manage.py migrate --verbosity 0
	uv run python manage.py demo_feedback_loop
	uv run python manage.py accuracy_report

samples:  ## Regenerate the fictional sample documents
	uv run python scripts/make_samples.py

docker-up:  ## Full stack: web + worker + PostgreSQL + Redis (fake LLM, no keys)
	docker compose up --build

docker-down:  ## Stop the stack and delete its volumes
	docker compose down -v
