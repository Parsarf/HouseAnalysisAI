.PHONY: test lint typecheck eval schema types migrate up

test:
	python -m pytest

lint:
	python -m ruff check .

typecheck:
	python -m mypy contracts common jobs

eval:
	python -m extraction.eval

schema:
	python scripts/generate_types.py

types: schema

migrate:
	alembic upgrade head

up:
	docker compose up -d
