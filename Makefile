PYTHON ?= .venv/bin/python
TARGET ?= /Users/alexisroberson/Documents/Claude_Code_Projects/langgraph-projects/decor-agent

.PHONY: install test lint demo demo-pause

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

demo: .venv/bin/python
	$(PYTHON) -m factory demo --repo $(TARGET)

demo-pause: .venv/bin/python
	$(PYTHON) -m factory run samples/ambiguous.json --repo $(TARGET)
