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
