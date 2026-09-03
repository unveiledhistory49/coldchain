PYTHON ?= python3
DB ?= sqlite:///./coldchain.db
PORT ?= 8000
HOST ?= 127.0.0.1

# Placeholder IDs for the simulate example (override on the command line).
ORG_ID ?=
DEVICES ?= SN-1

.PHONY: install test lint fmt typecheck serve simulate e2e lock audit clean

install:
	$(PYTHON) -m pip install -e ".[dev,test]"

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check src tests scripts
	$(PYTHON) -m ruff format --check src tests scripts

fmt:
	$(PYTHON) -m ruff check --fix src tests scripts
	$(PYTHON) -m ruff format src tests scripts

typecheck:
	$(PYTHON) -m mypy src/coldchain

serve:
	coldchain serve --db "$(DB)" --host "$(HOST)" --port "$(PORT)"

# Flags verified against src/coldchain/cli.py build_parser():
# --db/--org-id/--devices/--minutes/--interval-s/--seed/--base-c/
# --excursion/--dup-rate/--late-rate/--start-ms. No `seed` subcommand exists.
simulate:
	@if [ -z "$(ORG_ID)" ]; then echo "ORG_ID is required (make simulate ORG_ID=<id> DEVICES=SN-1)" >&2; exit 2; fi
	coldchain simulate --db "$(DB)" --org-id "$(ORG_ID)" --devices "$(DEVICES)" \
		--minutes 30 --interval-s 60 --seed 7 --base-c 5.0 \
		--excursion 10,20,8.0 --dup-rate 0.05 --late-rate 0.05

e2e:
	bash scripts/e2e.sh

lock:
	$(PYTHON) -m piptools compile --output-file=requirements.lock pyproject.toml
	$(PYTHON) -m piptools compile --extra=dev --extra=test --output-file=requirements-dev.lock pyproject.toml

audit:
	pip-audit -r requirements.lock

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find src tests -name '__pycache__' -type d -prune -exec rm -rf {} +
