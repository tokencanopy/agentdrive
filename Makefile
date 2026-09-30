# AgentDrive — development and maintenance targets.
#
# Everything here runs against your own checkout or install: the local stack,
# the generated stylesheet, the model checker, the OpenAPI policy, the image,
# and the maintenance jobs a deployment must schedule. compose.selfhost.yml
# runs them on their cadence inside the API container
# (SCHEDULER_ENABLED=true starts `python -m agentdrive.jobs.scheduler`
# there); anything else runs the commands
# `python -m agentdrive.jobs.scheduler --list` prints, on that cadence,
# against the same settings as the API. The targets below run one job now.

SHELL := /usr/bin/env bash

.PHONY: help dev css tla tla-full openapi-compat-check openapi-compat-test \
        build run-local gc-now-dry gc-now usage-snapshot-now reconcile-generations-dry

help: ## Show this help.
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[1;34m%-26s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

dev: ## Run the full local stack: db + GCS emulator + schema + app. Ctrl-C stops it.
	@./scripts/dev.sh

css: ## Regenerate src/agentdrive/static/agentdrive.css from design-system/src/styles/agentdrive.css.
	@node design-system/scripts/sync-css.mjs

tla: ## Model-check the TLA+ specs at CI bounds (~5 min).
	@./specs/tla/run.sh

tla-full: ## make tla plus the exhaustive fix configs (adds ~10-30 min).
	@./specs/tla/run.sh --full

OPENAPI_BASE ?= origin/main:tests/openapi.golden.json
OPENAPI_REVISION ?= tests/openapi.golden.json

openapi-compat-check: ## Reject breaking stable OpenAPI changes against OPENAPI_BASE.
	@./scripts/check-openapi-compat.sh "$(OPENAPI_BASE)" "$(OPENAPI_REVISION)"

openapi-compat-test: ## Exercise the compatibility policy's fixtures.
	@./scripts/test-openapi-compat.sh

build: ## docker build the runtime image locally as agentdrive:dev.
	docker build -t agentdrive:dev .

run-local: build ## Boot the image against the developer compose's Postgres + GCS emulator.
	docker run --rm --name agentdrive-local -p 8765:8080 \
	  -e DATABASE_URL='postgresql://agentdrive:dev@host.docker.internal:5432/agentdrive' \
	  -e GCS_BUCKET=agentdrive-dev \
	  -e GCS_EMULATOR_HOST='http://host.docker.internal:4443' \
	  -e PUBLIC_BASE_URL='http://localhost:8765' \
	  -e SESSION_SECRET='dev-secret-must-be-at-least-32-characters-long-yes' \
	  agentdrive:dev

gc-now-dry: ## Garbage-collection sweep in --dry-run mode (no writes; safe).
	uv run python -m agentdrive.jobs.gc --dry-run

gc-now: ## Garbage-collection sweep (purges expired soft-deletes and unreferenced blobs).
	uv run python -m agentdrive.jobs.gc

usage-snapshot-now: ## Usage maintenance pass (finalize reservations, prune history).
	uv run python -m agentdrive.jobs.usage_snapshot

reconcile-generations-dry: ## Preview which legacy version rows would resolve (no writes).
	uv run python -m agentdrive.jobs.reconcile_generations --dry-run
