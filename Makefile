.PHONY: setup lint typecheck test test-dataset test-e2e test-vcan test-all build fetch-dataset \
	fetch-route replay serve privacy-scan secrets-scan stealth-scan copy-lint check

UV ?= uv
PNPM ?= pnpm

setup:
	$(UV) sync
	cd web && $(PNPM) install --frozen-lockfile

lint:
	$(UV) run ruff check src tests scripts
	$(UV) run ruff format --check src tests scripts
	cd web && $(PNPM) lint && $(PNPM) format

typecheck:
	$(UV) run mypy
	cd web && $(PNPM) typecheck

test:
	$(UV) run pytest

test-dataset:
	$(UV) run pytest -m dataset

test-e2e: build
	$(UV) run pytest -m e2e

test-vcan:
	$(UV) run pytest -m vcan

test-all: test test-dataset test-e2e

build:
	cd web && $(PNPM) build

fetch-dataset:
	$(UV) run python scripts/fetch_comma2k19_sample.py

fetch-route:
	$(UV) run python scripts/fetch_comma2k19_route.py

replay:
	$(UV) run photoncar replay --dataset comma2k19

serve: build
	$(UV) run photoncar serve

privacy-scan:
	$(UV) run python scripts/privacy_scan.py

secrets-scan:
	gitleaks dir . --redact --no-banner
	gitleaks git . --redact --no-banner

stealth-scan:
	$(UV) run python scripts/stealth_scan.py

copy-lint:
	$(UV) run python scripts/copy_lint.py

check: lint typecheck test copy-lint privacy-scan
