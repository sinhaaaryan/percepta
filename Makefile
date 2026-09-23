# Requires: Lean 4.15 (lean + lake on PATH, or LEAN_BIN_DIR), Python 3.11+, Node 18+, PostgreSQL 14+.
LEAN_BIN_DIR ?= /opt/lean/bin
export PATH := $(LEAN_BIN_DIR):$(PATH)
PY := .venv/bin/python

.PHONY: setup lean deps ui seed test run dev-ui

setup: lean deps ui

lean:            ## build proofs + the nightingale-check binary
	cd lean && lake build

deps:
	python3 -m venv .venv && .venv/bin/pip install -q -r backend/requirements.txt

ui:
	cd frontend && npm install && npm run build

seed:            ## load demo data (~90 nurses, 3 units, 4 weeks)
	cd backend && ../$(PY) -m app.seed

test:
	cd backend && ../$(PY) -m pytest -q

run:             ## API + built UI on http://localhost:8000
	cd backend && ../.venv/bin/uvicorn app.main:app --port 8000

dev-ui:          ## Vite dev server on :5173 (proxies /api to :8000)
	cd frontend && npm run dev
