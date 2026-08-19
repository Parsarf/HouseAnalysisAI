.PHONY: test lint typecheck eval schema types migrate up

test:
	python -m pytest

lint:
	python -m ruff check .
	lint-imports

typecheck:
	python -m mypy api analyst auth calibration changes classification common contracts db exports extraction finance flags identity ingestion jobs normalization ops pipeline report_analysis scoring strategies

web:
	cd web && npm ci && npm run build

eval:
	python -m extraction.eval

schema:
	python scripts/generate_types.py

types: schema

migrate:
	alembic upgrade head

up:
	docker compose up -d

worker:
	python -m pipeline.run_worker

backup:
	bash scripts/backup.sh

restore:
	bash scripts/restore.sh $(BACKUP_DIR) $(TARGET_DIR)
