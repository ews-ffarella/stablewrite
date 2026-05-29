OS := $(shell uname -s)
NPROCS ?= $(shell nproc)
SHELL := /bin/bash

.DEFAULT_GOAL := all

# Export CMAKE_BUILD_PARALLEL_LEVEL so both `cmake --build` and tools that observe
# this environment variable (like uv) will pick up the parallelism setting.
export CMAKE_BUILD_PARALLEL_LEVEL := $(NPROCS)

# Use the system cmake (do not mangle with a custom CMAKE wrapper)
CMAKE := cmake


PROJECT_ROOT := $(abspath .)
DOCS_DIR := $(PROJECT_ROOT)/docs
TEST_DIR := $(PROJECT_ROOT)/tests
DIST_DIR := $(PROJECT_ROOT)/dist
VENV := $(PROJECT_ROOT)/.venv/
PYTHON:=$(VENV)/bin/python
PYTEST:=$(VENV)/bin/pytest
UV:=$(CMAKE_OPTS) uv
SRC:=src/ examples/ tests/ notebooks/

EXTRA_ARGS ?=

# PRETTIER_CMD detection: prefer any prettier on PATH, otherwise fall back to `npx prettier` so we don't require a global install.
PRETTIER_CMD ?= $(shell \
if command -v prettier >/dev/null 2>&1; then \
	command -v prettier; \
else \
	echo "npx prettier"; \
fi)
PRECOMMIT_CMD ?= $(shell \
if command -v prek >/dev/null 2>&1; then \
	command -v prek; \
else \
	echo "uvx prek"; \
fi)
PRETTIER_ARGS ?= --ignore-path .prettierignore --print-width 80 --prose-wrap preserve


all: build format

.PHONY: help
help:
	@echo "Available targets:"
	@echo ""
	@echo "all						     Run all build and formatting targets"
	@echo ""
	@echo "Formatting:"
	@echo "  ruff-format                 Format and lint code with ruff"
	@echo "  md-format                   Format and fix markdown files"
	@echo "  nb-strip-notebooks          Strip notebooks (excluding certain directories)"
	@echo "  pre-commit-all-files        Run pre-commit hooks on all files"
	@echo "  format                      Run all formatting targets"
	@echo ""
	@echo "Build & Release:"
	@echo "  build                       Build the project (sync dependencies and build extensions if any)"
	@echo "  wheel                       Build the project wheel (for distribution)"
	@echo ""
	@echo "Utilities:"
	@echo "  setup                       Set up development environment (git filters, pre-commit hooks)"
	@echo "  clean                       Clean up build artifacts and caches"

# ============================================================================
# Formatting targets
# ============================================================================

# Format and lint code with ruff
.PHONY: ruff-format
ruff-format:
	@echo "Formatting code with ruff..."
	$(UV) run ruff format $(EXTRA_ARGS)
	@echo "Linting code with ruff..."
	$(UV) run ruff check --fix --show-fixes --unsafe-fixes $(EXTRA_ARGS) || true

# Format and fix markdown files
.PHONY: md-format
md-format:
	@echo "Formatting markdown files..."
	@# Format docs if any markdown files exist (portable find)
	@if [ -d "$(DOCS_DIR)" ] && find "$(DOCS_DIR)" -type f -name '*.md' -print -quit | grep -q .; then \
		find "$(DOCS_DIR)" -type f -name '*.md' -print0 | xargs -0 $(PRETTIER_CMD) $(PRETTIER_ARGS) --write $(EXTRA_ARGS) || true; \
	else \
		echo "No docs markdown files found in $(DOCS_DIR)"; \
	fi
	# Format .github markdown files if any exist (config, instructions, etc.)
	@if [ -d ".github" ] && find ".github" -type f -name '*.md' -print -quit | grep -q .; then \
		find ".github" -type f -name '*.md' -print0 | xargs -0 $(PRETTIER_CMD) $(PRETTIER_ARGS) --write $(EXTRA_ARGS) || true; \
	else \
		echo "No .github markdown files found"; \
	fi
	@# Format README.md if present
	@if [ -f README.md ]; then \
		$(PRETTIER_CMD) $(PRETTIER_ARGS) --write README.md $(EXTRA_ARGS) || true; \
	else \
		echo "No README.md found"; \
	fi

# Strip notebooks (excluding certain directories)
.PHONY: nb-strip-notebooks
nb-strip-notebooks:
	@if ! command -v nbstripout-fast >/dev/null 2>&1; then \
		echo "⚠️  nbstripout-fast not found. Skipping notebook stripping."; \
		echo "   Install it with: uv tool install nbstripout-fast"; \
	else \
		echo "Stripping notebooks (excluding any .venv/ , docs/ , .ipynb_checkpoints/ , and .virtual_documents/ directories)..."; \
		find . \( -type d \( -name '.venv' -o -name 'docs' -o -name '.ipynb_checkpoints' -o -name '.virtual_documents' \) -prune \) -o -type f -name '*.ipynb' -print0 \
		| while IFS= read -r -d '' f; do \
			if nbstripout-fast "$$f" >/dev/null 2>&1; then \
				echo "STRIPPED: $$f"; \
			else \
				echo "FAILED nbstripout: $$f"; \
			fi; \
		done; \
	fi

.PHONY: pre-commit-all-files
pre-commit-all-files:
	@echo "Running pre-commit hooks..."
	$(PRECOMMIT_CMD) run --all-files

.PHONY: setup
setup:
	@echo "Setting up development environment..."
	$(UV) sync --all-extras
	@if ! command -v nbstripout-fast >/dev/null 2>&1; then \
		echo "❌ nbstripout-fast not found. Install it with: uv tool install nbstripout-fast"; \
		exit 1; \
	else \
		git config filter.jupyter.clean nbstripout-fast; \
		git config filter.jupyter.smudge cat; \
		echo "✅ nbstripout-fast found."; \
	fi
	$(PRECOMMIT_CMD) install -f

.PHONY: format
format: ruff-format md-format nb-strip-notebooks pre-commit-all-files


EXCLUDES := -path "./.venv" -prune -o -path "./.submodules" -prune -o

CLEAN_DIRS := \
	"__pycache__" \
	".ipynb_checkpoints" \
	".pytest_cache" \
	".ruff_cache" \
	".mypy_cache"

CLEAN_FILES := \
	"*.pyc" \
	"*.pyo"

.PHONY: clean
clean:
	@echo "Cleaning up..."
	rm -rf $(DIST_DIR)
	# Remove directories
	@for d in $(CLEAN_DIRS); do \
		find . $(EXCLUDES) -type d -name $$d -exec rm -rf {} + ; \
	done
	# Remove files
	@for f in $(CLEAN_FILES); do \
		find . $(EXCLUDES) -type f -name $$f -exec rm -f {} \; ; \
	done
	# Remove top-level caches
	rm -rf .pytest_cache .mypy_cache .ruff_cache .nox

# ============================================================================
# Build targets
# ============================================================================

# Build the project wheel (for distribution)
.PHONY:  wheel
wheel:
	@echo "Building the project..."
	rm -rf $(DIST_DIR)
	$(UV) build --wheel --out-dir  $(DIST_DIR) 2>&1 | tee uv_build.log
	unzip -l $(DIST_DIR)/*.whl

.PHONY:  build
build:
	$(UV) sync --all-extras
