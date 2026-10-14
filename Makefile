PYTHON ?= .venv/bin/python
TARGET ?= $(CURDIR)/.factory/demo-target
DEMO_ARGS ?=

.PHONY: install test lint demo demo-offline demo-mock demo-pause

.venv/bin/python:
	python3 -m venv .venv
	$(PYTHON) -m pip install -U pip
	$(PYTHON) -m pip install -e .

install: .venv/bin/python

test: .venv/bin/python
	$(PYTHON) -m pytest -q

lint: .venv/bin/python
	$(PYTHON) -m ruff check factory tests
	$(PYTHON) -m mypy

demo: .venv/bin/python demo-fixture
	$(PYTHON) -m factory demo --repo $(TARGET) $(DEMO_ARGS)

demo-offline: .venv/bin/python demo-fixture
	$(PYTHON) -m factory demo --repo $(TARGET) $(DEMO_ARGS) --ld offline

demo-mock: .venv/bin/python demo-fixture
	$(PYTHON) -m factory demo --repo $(TARGET) $(DEMO_ARGS) --agents mock --ld offline

demo-pause: .venv/bin/python demo-fixture
	$(PYTHON) -m factory run samples/ambiguous.json --repo $(TARGET) $(DEMO_ARGS)

.PHONY: demo-fixture
demo-fixture: .venv/bin/python
	$(PYTHON) -m factory.demo_fixture

.PHONY: temporal-server temporal-worker temporal-demo temporal-pause test-temporal
# Dedicated ports avoid sharing another application's dev server.
temporal-server:
	mkdir -p .factory/temporal-server
	temporal server start-dev --port 7235 --ui-port 8235 --db-filename $(CURDIR)/.factory/temporal-server/history.db

temporal-worker: .venv/bin/python demo-fixture
	$(PYTHON) -m factory temporal worker --repo $(TARGET) $(DEMO_ARGS)

temporal-demo: .venv/bin/python
	$(PYTHON) -m factory temporal start samples/decor-room-mood.json

temporal-pause: .venv/bin/python
	$(PYTHON) -m factory temporal start samples/ambiguous.json

test-temporal: .venv/bin/python
	$(PYTHON) -m pytest -q tests/test_temporal_integration.py
