# Production operations for the charging platform on a Linux (Ubuntu) server. Run `make help`.
#
# First install on a fresh VPS:
#   make install-docker        # once; then log out and back in
#   make setup                 # asks for the domain and the MapTiler key, generates secrets
#   make deploy                # build, start, migrate, load the PlugShare sites
# Later:
#   make update                # pull new code, rebuild, migrate, restart
#
# Every command reads its settings from $(ENV_FILE); see DEPLOY.md.

SHELL := /bin/bash
.DEFAULT_GOAL := help

ENV_FILE ?= .env.production
-include $(ENV_FILE)

COMPOSE := docker compose --env-file $(ENV_FILE)
TOOLS := $(COMPOSE) --profile tools run --rm tools
BACKUP_DIR ?= backups
STAMP := $(shell date +%Y%m%d-%H%M%S)
WAIT := --wait --wait-timeout 300

.PHONY: help install-docker setup env-check build up down restart ps status logs \
        migrate migrate-status seed seed-demo demo-up demo-down deploy update \
        register-charger chargers rotate-key operate shell mongo-shell backup restore test destroy

help: ## Show the commands
	@echo "Usage: make <command> [NAME=value ...]"
	@awk 'BEGIN {FS = ":.*## "} /^## / {printf "\n\033[1m%s\033[0m\n", substr($$0, 4)} \
	     /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-17s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

## Setup
install-docker: ## Install Docker Engine + Compose on Ubuntu (sudo)
	@bash deploy/scripts/install-docker.sh

setup: ## Check prerequisites and create .env.production (DOMAIN=, MAPTILER_KEY=)
	@ENV_FILE=$(ENV_FILE) bash deploy/scripts/setup.sh

env-check:
	@test -f $(ENV_FILE) || { echo "No $(ENV_FILE) yet: run make setup first." >&2; exit 1; }

build: env-check ## Build the backend and website images
	$(COMPOSE) build

## Run
deploy: build ## First install: start databases, migrate, seed, start everything
	$(COMPOSE) up -d $(WAIT) mongo redis
	$(MAKE) --no-print-directory migrate
	$(MAKE) --no-print-directory seed
	$(MAKE) --no-print-directory up
	$(MAKE) --no-print-directory status

up: env-check ## Start the stack (or apply changes) and wait until healthy
	$(COMPOSE) up -d --remove-orphans $(WAIT)

down: env-check ## Stop everything, the demo fleet included (data is kept)
	$(COMPOSE) --profile demo down

restart: env-check ## Restart services (SERVICE=cs to restart one)
	$(COMPOSE) restart $(SERVICE)

update: env-check ## Pull new code (both repos), rebuild, migrate, restart
	git pull --ff-only
	git -C "$(FRONTEND_DIR)" pull --ff-only
	$(COMPOSE) build
	$(COMPOSE) up -d $(WAIT) mongo redis
	$(MAKE) --no-print-directory migrate
	$(MAKE) --no-print-directory up

ps: env-check ## List the containers and their health
	$(COMPOSE) --profile demo ps

# Asks this server's own Caddy (--connect-to), so it works before DNS points here; -k because a
# certificate may still be on its way. Use a browser for the real public check.
LOCAL_CURL = curl -sS -k --max-time 15 \
  --connect-to $(DOMAIN):80:127.0.0.1:$(HTTP_PORT) --connect-to $(DOMAIN):443:127.0.0.1:$(HTTPS_PORT)

status: env-check ## Container health, plus a check of the website and API through Caddy
	@$(COMPOSE) --profile demo ps
	@echo
	@code=$$($(LOCAL_CURL) -o /dev/null -w '%{http_code}' "$(PUBLIC_URL)/" 2>/dev/null); \
	  echo "website  $(PUBLIC_URL)/  HTTP $${code:-000}"
	@body=$$($(LOCAL_CURL) "$(PUBLIC_URL)/api/v1/sites" 2>/dev/null); \
	  count=$$(printf '%s' "$$body" | python3 -c 'import json, sys; print(len(json.load(sys.stdin)))' 2>/dev/null); \
	  if [ -n "$$count" ]; then echo "api      $(PUBLIC_URL)/api/v1/sites  $$count sites"; \
	  else echo "api      $(PUBLIC_URL)/api/v1/sites  not answering (make logs SERVICE=api)"; fi

logs: env-check ## Follow logs (SERVICE=cs|api|frontend|caddy|mongo|redis|fleet)
	$(COMPOSE) --profile demo logs -f --tail=200 $(SERVICE)

## Database
migrate: env-check ## Apply database migrations (safe to re-run)
	$(TOOLS) python migrate.py

migrate-status: env-check ## List applied and pending migrations
	$(TOOLS) python migrate.py --status

seed: env-check ## Load the PlugShare reference sites (safe to re-run)
	$(TOOLS) python import_plugshare_sites.py

backup: env-check ## Dump MongoDB and the app-data volume into backups/
	@mkdir -p $(BACKUP_DIR) && chmod 700 $(BACKUP_DIR)
	$(COMPOSE) exec -T mongo sh -c 'mongodump --quiet --username "$$MONGO_INITDB_ROOT_USERNAME" \
	  --password "$$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin --archive --gzip' \
	  > $(BACKUP_DIR)/mongo-$(STAMP).archive.gz
	$(TOOLS) tar czf - -C /data . > $(BACKUP_DIR)/app-data-$(STAMP).tar.gz
	@ls -lh $(BACKUP_DIR)/*-$(STAMP).*

# The app-data archive from the same backup (charger keys, the demo fleet's manifest and state) is
# restored with it, so the fleet's memory of open sessions matches the restored database.
APP_DATA_FILE = $(subst /mongo-,/app-data-,$(subst .archive.gz,.tar.gz,$(FILE)))

restore: env-check ## Restore a backup: FILE=backups/mongo-....archive.gz CONFIRM=yes
	@test -n "$(FILE)" -a -f "$(FILE)" || { echo "FILE=<path to a mongo-*.archive.gz> is required" >&2; exit 1; }
	@test "$(CONFIRM)" = yes || { echo "This replaces the current database. Re-run with CONFIRM=yes" >&2; exit 1; }
	$(COMPOSE) --profile demo stop fleet
	$(COMPOSE) exec -T mongo sh -c 'mongorestore --quiet --drop --username "$$MONGO_INITDB_ROOT_USERNAME" \
	  --password "$$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin --archive --gzip' < "$(FILE)"
	@if [ -f "$(APP_DATA_FILE)" ]; then \
	  echo "Restoring $(APP_DATA_FILE)"; \
	  $(TOOLS) sh -c 'find /data -mindepth 1 -delete && tar xzf - -C /data' < "$(APP_DATA_FILE)"; \
	else echo "No $(APP_DATA_FILE) next to it: /data (keys, demo state) left as it is."; fi
	@echo "Restored. Start the demo fleet again with make demo-up if it was running."

mongo-shell: env-check ## Open a MongoDB shell on the application database
	$(COMPOSE) exec mongo sh -c 'mongosh --quiet -u "$$MONGO_INITDB_ROOT_USERNAME" \
	  -p "$$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin "$(MONGODB_DB)"'

## Chargers and operations
register-charger: env-check ## Register a real charger (ID=...); prints its key once
	@test -n "$(ID)" || { echo "ID=<charger identity> is required" >&2; exit 1; }
	$(TOOLS) python register_charge_point.py register "$(ID)"

chargers: env-check ## List registered chargers
	$(TOOLS) python register_charge_point.py list

rotate-key: env-check ## Issue a new key for a charger (ID=...)
	@test -n "$(ID)" || { echo "ID=<charger identity> is required" >&2; exit 1; }
	$(TOOLS) python register_charge_point.py rotate "$(ID)"

operate: env-check ## Operator command: ARGS="remote-start PS-2946795 DEMO-REMOTE"
	$(TOOLS) python operate.py $(ARGS)

shell: env-check ## A shell in the backend image, with the database and admin API reachable
	$(COMPOSE) --profile tools run --rm tools bash

## Demo fleet (simulated chargers; instructions/11-demo-fleet.md)
seed-demo: env-check ## Create the demo data: 195 simulated chargers on the PlugShare sites
	$(TOOLS) python seed_demo_fleet.py --manifest /data/demo_fleet_manifest.json

demo-up: env-check ## Start the demo fleet
	$(COMPOSE) --profile demo up -d fleet

demo-down: env-check ## Stop the demo fleet cleanly (ends sessions, switches chargers off)
	$(COMPOSE) --profile demo stop fleet

## Maintenance
test: env-check ## Run the test suite in the backend image (uses a throwaway database)
	$(TOOLS) python -m pytest -q -p no:cacheprovider

destroy: env-check ## Delete the containers AND every data volume: CONFIRM=yes
	@test "$(CONFIRM)" = yes || { echo "This deletes the database and certificates. Re-run with CONFIRM=yes" >&2; exit 1; }
	$(COMPOSE) --profile demo --profile tools down --volumes
