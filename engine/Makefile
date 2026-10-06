.PHONY: help install lint format test coverage precommit verify-gcp clean

help:
	@echo "Available targets:"
	@echo "  install      Install dev dependencies"
	@echo "  lint         Run ruff linter"
	@echo "  format       Format code with ruff"
	@echo "  test         Run unit tests"
	@echo "  coverage     Run tests with coverage report"
	@echo "  precommit    Run all pre-commit hooks"
	@echo "  verify-gcp   Check BigQuery and GCP setup"
	@echo "  clean        Remove build artefacts and caches"

install:
	pip install -e ".[dev]"
	pre-commit install

lint:
	ruff check .

format:
	ruff format .
	ruff check --fix .

test:
	pytest tests/unit/ tests/repository/ -v

coverage:
	pytest tests/unit/ --cov=src --cov-report=html --cov-report=term

precommit:
	pre-commit run --all-files

verify-gcp:
	python scripts/verify_gcp_setup.py

clean:
	rm -rf build/ dist/ *.egg-info htmlcov/ .coverage .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
