# Cross-platform-ish task runner. On Windows, run these via PowerShell's `make`
# (Chocolatey `make` package) or invoke the underlying commands directly - each
# target is a single command so copy-pasting into PowerShell works too.

.PHONY: dev up down build test lint format migrate worker seed logs

dev: up

up:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build

down:
	docker compose down

build:
	docker compose build

logs:
	docker compose logs -f

test:
	cd backend && .venv/Scripts/python -m pytest --cov=app --cov-report=term-missing

lint:
	cd backend && .venv/Scripts/python -m ruff check app tests
	cd frontend && npm run lint

format:
	cd backend && .venv/Scripts/python -m ruff format app tests

migrate:
	cd backend && .venv/Scripts/python -m alembic upgrade head

migration:
	cd backend && .venv/Scripts/python -m alembic revision --autogenerate -m "$(name)"

worker:
	cd backend && .venv/Scripts/python -m celery -A app.workers.celery_app worker --loglevel=info

seed:
	cd backend && .venv/Scripts/python -m app.seed
