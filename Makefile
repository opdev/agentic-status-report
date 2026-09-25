SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help
.NOTPARALLEL:

DOCKER ?= docker
OC ?= oc
PYTHON ?= python3

IMAGE ?= quay.io/opdev/weekly-status
TAG ?= $(shell git rev-parse --short HEAD)
IMAGE_REF := $(IMAGE):$(TAG)
PLATFORM ?= linux/amd64

APP_CONTAINER ?= weekly-status
DB_CONTAINER ?= weekly-status-postgres
DOCKER_NETWORK ?= weekly-status
POSTGRES_IMAGE ?= postgres:16
POSTGRES_DB ?= weekly_status
POSTGRES_USER ?= postgres
POSTGRES_PASSWORD ?= postgres
DB_PORT ?= 5432
LOCAL_DATABASE_URL ?= postgresql+psycopg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(DB_CONTAINER):5432/$(POSTGRES_DB)
ENV_FILE ?= .env

NAMESPACE ?= weekly-status
SECRET_FILE ?= deploy/secrets.yaml
PG_CLUSTER ?= weekly-status-db
PG_USER_SECRET ?= weekly-status-db-pguser-weeklystatus
APP_SECRET ?= weekly-status-secrets
APP_DEPLOYMENT ?= weekly-status-slack-bot

APP_MANIFESTS := \
	deploy/deployment.yaml \
	deploy/cronjob-collect-and-draft.yaml \
	deploy/cronjob-send-drafts.yaml \
	deploy/cronjob-nudge.yaml \
	deploy/cronjob-lock-and-report.yaml

.PHONY: help install test validate check build push scan db db-wait db-logs db-shell \
	migrate e2e run stop logs clean namespace secrets postgres postgres-wait \
	migrate-openshift application deploy rollout logs-openshift status-openshift

help: ## Show available targets and configurable variables
	@awk 'BEGIN {FS = ":.*## "; printf "Usage: make <target> [VARIABLE=value]\n\nTargets:\n"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@printf '\nCommon variables:\n  IMAGE=%s\n  TAG=%s\n  PLATFORM=%s\n  NAMESPACE=%s\n  ENV_FILE=%s\n  SECRET_FILE=%s\n' "$(IMAGE)" "$(TAG)" "$(PLATFORM)" "$(NAMESPACE)" "$(ENV_FILE)" "$(SECRET_FILE)"

install: ## Install local development dependencies
	$(PYTHON) -m pip install -e '.[dev,slack]'

test: ## Run the unit test suite
	PYTHONPATH=src $(PYTHON) -m pytest

validate: ## Validate deployment YAML and whitespace
	$(PYTHON) -c 'import pathlib, yaml; [list(yaml.safe_load_all(path.read_text())) for path in pathlib.Path("deploy").rglob("*.yaml")]'
	git diff --check

check: validate test ## Run deployment validation and unit tests

build: ## Build the application image for linux/amd64
	$(DOCKER) buildx build --pull --platform "$(PLATFORM)" --load \
		-f deploy/Dockerfile -t "$(IMAGE_REF)" .

push: build ## Push the built image to its registry
	$(DOCKER) push "$(IMAGE_REF)"

scan: build ## Scan the image for vulnerabilities at every severity
	$(DOCKER) run --rm -v /var/run/docker.sock:/var/run/docker.sock \
		aquasec/trivy:0.69.3 image --scanners vuln \
		--severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL --exit-code 1 \
		--skip-version-check "$(IMAGE_REF)"

db: ## Start the local PostgreSQL container
	@$(DOCKER) network inspect "$(DOCKER_NETWORK)" >/dev/null 2>&1 || $(DOCKER) network create "$(DOCKER_NETWORK)" >/dev/null
	@if $(DOCKER) container inspect "$(DB_CONTAINER)" >/dev/null 2>&1; then \
		$(DOCKER) start "$(DB_CONTAINER)" >/dev/null; \
	else \
		$(DOCKER) run -d --name "$(DB_CONTAINER)" --network "$(DOCKER_NETWORK)" \
			-e POSTGRES_DB="$(POSTGRES_DB)" \
			-e POSTGRES_USER="$(POSTGRES_USER)" \
			-e POSTGRES_PASSWORD="$(POSTGRES_PASSWORD)" \
			-p "$(DB_PORT):5432" \
			--health-cmd='pg_isready -U $(POSTGRES_USER) -d $(POSTGRES_DB)' \
			--health-interval=2s --health-timeout=3s --health-retries=30 \
			"$(POSTGRES_IMAGE)" >/dev/null; \
	fi
	@$(MAKE) --no-print-directory db-wait

db-wait: ## Wait for local PostgreSQL to become healthy
	@for attempt in $$(seq 1 60); do \
		status="$$( $(DOCKER) inspect -f '{{.State.Health.Status}}' "$(DB_CONTAINER)" 2>/dev/null || true )"; \
		if [[ "$$status" == healthy ]]; then echo "PostgreSQL is healthy"; exit 0; fi; \
		sleep 2; \
	done; \
	echo "PostgreSQL did not become healthy" >&2; \
	$(DOCKER) logs "$(DB_CONTAINER)" >&2; \
	exit 1

db-logs: ## Follow local PostgreSQL logs
	$(DOCKER) logs -f "$(DB_CONTAINER)"

db-shell: ## Open psql in the local PostgreSQL container
	$(DOCKER) exec -it "$(DB_CONTAINER)" psql -U "$(POSTGRES_USER)" -d "$(POSTGRES_DB)"

migrate: build db ## Apply Alembic migrations to local PostgreSQL
	$(DOCKER) run --rm --platform "$(PLATFORM)" --network "$(DOCKER_NETWORK)" \
		-e DATABASE_URL="$(LOCAL_DATABASE_URL)" --entrypoint alembic \
		"$(IMAGE_REF)" upgrade head

e2e: migrate ## Build, migrate a real DB, and verify the container and schema
	$(DOCKER) run --rm --platform "$(PLATFORM)" --entrypoint status "$(IMAGE_REF)" --help >/dev/null
	@test "$$($(DOCKER) exec "$(DB_CONTAINER)" psql -U "$(POSTGRES_USER)" -d "$(POSTGRES_DB)" -Atc "select to_regclass('public.person')")" = person
	@echo "End-to-end container and database checks passed"

run: build db ## Run the application locally in the background
	@test -f "$(ENV_FILE)" || { echo "Missing $(ENV_FILE); copy .env.example and fill required values" >&2; exit 1; }
	@$(DOCKER) rm -f "$(APP_CONTAINER)" >/dev/null 2>&1 || true
	$(DOCKER) run -d --name "$(APP_CONTAINER)" --platform "$(PLATFORM)" \
		--network "$(DOCKER_NETWORK)" --env-file "$(ENV_FILE)" \
		-e DATABASE_URL="$(LOCAL_DATABASE_URL)" "$(IMAGE_REF)"

stop: ## Stop and remove local application and database containers
	@$(DOCKER) rm -f "$(APP_CONTAINER)" >/dev/null 2>&1 || true
	@$(DOCKER) rm -f "$(DB_CONTAINER)" >/dev/null 2>&1 || true

logs: ## Follow local application logs
	$(DOCKER) logs -f "$(APP_CONTAINER)"

clean: stop ## Remove the local Docker network
	@$(DOCKER) network rm "$(DOCKER_NETWORK)" >/dev/null 2>&1 || true

namespace: ## Create or select the OpenShift namespace
	@$(OC) get namespace "$(NAMESPACE)" >/dev/null 2>&1 || $(OC) create namespace "$(NAMESPACE)"
	$(OC) project "$(NAMESPACE)" >/dev/null

secrets: namespace ## Apply all application secrets from the ignored secret manifest
	@test -f "$(SECRET_FILE)" || { echo "Missing $(SECRET_FILE); copy deploy/secrets.example.yaml and fill it securely" >&2; exit 1; }
	$(OC) apply -n "$(NAMESPACE)" -f "$(SECRET_FILE)"

postgres: namespace ## Deploy the Crunchy PGO PostgreSQL cluster
	$(OC) apply -n "$(NAMESPACE)" -f deploy/postgres/postgrescluster.yaml

postgres-wait: ## Wait for the OpenShift PostgreSQL primary pod
	@for attempt in $$(seq 1 60); do \
		if $(OC) get pods -n "$(NAMESPACE)" \
			-l postgres-operator.crunchydata.com/cluster="$(PG_CLUSTER)" \
			-o name | grep -q .; then break; fi; \
		sleep 5; \
	done
	$(OC) wait -n "$(NAMESPACE)" --for=condition=Ready pod \
		-l postgres-operator.crunchydata.com/cluster="$(PG_CLUSTER)" \
		--timeout=600s
	$(OC) get secret "$(PG_USER_SECRET)" -n "$(NAMESPACE)" >/dev/null

migrate-openshift: ## Run Alembic migrations as an OpenShift Job
	@$(OC) delete job weekly-status-db-migrate -n "$(NAMESPACE)" --ignore-not-found >/dev/null
	@sed 's|quay.io/opdev/weekly-status:latest|$(IMAGE_REF)|g' deploy/postgres/migrate-job.yaml | $(OC) apply -n "$(NAMESPACE)" -f -
	@$(OC) wait -n "$(NAMESPACE)" --for=condition=Complete job/weekly-status-db-migrate --timeout=600s || { \
		$(OC) logs -n "$(NAMESPACE)" job/weekly-status-db-migrate; exit 1; }

application: namespace ## Deploy the Slack bot and scheduled automation
	@for manifest in $(APP_MANIFESTS); do \
		printf '%s\n' '---'; \
		sed 's|quay.io/opdev/weekly-status:latest|$(IMAGE_REF)|g' "$$manifest"; \
	done | $(OC) apply -n "$(NAMESPACE)" -f -

deploy: ## Build/push and deploy database, secrets, migrations, and application
	@$(MAKE) --no-print-directory push
	@$(MAKE) --no-print-directory postgres
	@$(MAKE) --no-print-directory postgres-wait
	@$(MAKE) --no-print-directory secrets
	@$(MAKE) --no-print-directory migrate-openshift
	@$(MAKE) --no-print-directory application
	@$(MAKE) --no-print-directory rollout

rollout: ## Wait for the OpenShift application rollout
	$(OC) rollout status -n "$(NAMESPACE)" deployment/"$(APP_DEPLOYMENT)" --timeout=300s

logs-openshift: ## Follow OpenShift application logs
	$(OC) logs -n "$(NAMESPACE)" -f deployment/"$(APP_DEPLOYMENT)"

status-openshift: ## Show deployed OpenShift application, schedules, jobs, and database
	$(OC) get -n "$(NAMESPACE)" deployment,cronjob,job,postgrescluster
