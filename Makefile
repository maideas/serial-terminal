# Development helpers. Runtime dependency: pyserial (see requirements.txt).
# Tools (ruff, pytest, pip-audit) are expected on PATH; on this box run
#   source /mnt/shared/.pylibs/env.sh
PYTHON ?= python3

.PHONY: check lint format test audit clean

check: lint test        ## lint + tests

lint:                   ## ruff check + format check
	ruff check .
	ruff format --check .

format:                 ## reformat sources with ruff
	ruff format .
	ruff check --fix .

test:                   ## unit + pty integration tests
	$(PYTHON) -m pytest -q tests

audit:                  ## check pinned dependencies for known vulnerabilities
	pip-audit -r requirements.txt

clean:
	rm -rf build __pycache__ tests/__pycache__ .pytest_cache .ruff_cache
