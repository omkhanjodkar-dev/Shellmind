# Shellmind - one command sets up whatever is missing, then starts the app.
#
#   make           install/check Ollama, pull the model, install deps, run
#   make setup     same, but do not start the app
#   make doctor    show what is installed and what is missing
#   make clean     remove the virtual environment
#
# Options:  MODEL=llama3.1:8b   YES=1 (never ask)   PYTHON=py

ifeq ($(OS),Windows_NT)
PYTHON ?= python
else
PYTHON ?= python3
endif
MODEL ?= qwen2.5-coder:7b
BOOT = $(PYTHON) bootstrap.py --model "$(MODEL)" $(if $(YES),--yes,)

.DEFAULT_GOAL := run
.PHONY: run setup ollama model deps doctor clean help

run:
	@$(BOOT) run

setup:
	@$(BOOT) setup

ollama:
	@$(BOOT) ollama

model:
	@$(BOOT) model

deps:
	@$(BOOT) deps

doctor:
	@$(BOOT) doctor

clean:
	@$(BOOT) clean

help:
	@echo make          - set up whatever is missing, then run Shellmind
	@echo make setup    - set up only
	@echo make doctor   - show what is installed
	@echo make clean    - remove .venv
	@echo Options: MODEL=name YES=1 PYTHON=py
