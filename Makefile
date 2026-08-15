.PHONY: test lint typecheck eval

test:
	python -m pytest

lint:
	python -m ruff check .

typecheck:
	python -m mypy contracts common jobs

eval:
	python -m extraction.eval
