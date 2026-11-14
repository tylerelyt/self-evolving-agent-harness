PYTHON := .deps/hermes-agent/.venv/bin/python
CHECK_PYTHON := $(if $(wildcard $(PYTHON)),$(PYTHON),python3.12)
PORT ?= 8765

.PHONY: setup demo ui lab check

setup:
	bash scripts/setup_deps.sh
	bash scripts/setup_skillclaw.sh

demo:
	@ test -x "$(PYTHON)" || { echo 'Run make setup first.' >&2; exit 1; }
	"$(PYTHON)" -B scripts/demo.py

ui:
	"$(PYTHON)" -B -m apps.oncall workbench --offline --state workshop/capstone/runtime/workbench --new-state --port $(PORT)

lab:
	"$(PYTHON)" -B workshop/capstone/app.py --offline --state workshop/capstone/runtime/lab --new-state --port $(PORT)

check:
	"$(CHECK_PYTHON)" -B scripts/check_offline.py
