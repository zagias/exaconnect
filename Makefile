# ExaConnect. Lab targets run on the Linux lab VM (Ubuntu 24.04 arm64 in Parallels), never on macOS.
SHELL := bash
NODE_IMAGE := exaconnect/node:dev
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
COMMIT ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
LDFLAGS := -s -w -X github.com/zagias/exaconnect/agent/internal/version.Version=$(VERSION) \
           -X github.com/zagias/exaconnect/agent/internal/version.Commit=$(COMMIT)
PY ?= python3

.PHONY: help build build-agent build-portal test test-agent test-controller test-portal lint \
        lab-image lab-up lab-down lab-smoke controller-up controller-down demo-seed demo

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

# ---- build and test (any OS) ----
build: build-agent build-portal ## Build the agent (linux arm64 + amd64) and the portal

build-agent: ## Build exa-agent into bin/linux-{arm64,amd64}/
	cd agent && for arch in arm64 amd64; do \
	  CGO_ENABLED=0 GOOS=linux GOARCH=$$arch go build -trimpath -ldflags "$(LDFLAGS)" \
	    -o ../bin/linux-$$arch/exa-agent ./cmd/exa-agent || exit 1; done

build-portal: ## Build the portal into portal/dist/
	cd portal && npm ci && npm run build

test: test-agent test-controller test-portal ## Run all unit tests and checks

test-agent: ## Go vet and unit tests
	cd agent && go vet ./... && go test ./...

test-controller: ## Controller unit tests (pip install -e 'controller[dev]' first)
	cd controller && $(PY) -m pytest -q

test-portal: ## Portal type check
	cd portal && npm run typecheck

lint: ## Lint Go, Python and shell
	cd agent && test -z "$$(gofmt -l .)" || (gofmt -l . && exit 1)
	cd controller && ruff check . && ruff format --check .
	shellcheck -x lab/netem/*.sh lab/faults/*.sh lab/scripts/*.sh lab/host/*.sh

# ---- lab (Linux lab VM only) ----
lab-image: ## Build the lab node image (FRR 10 + WireGuard + tools)
	docker build -t $(NODE_IMAGE) lab/images/node

lab-up: lab-image ## Deploy the containerlab topology, apply underlay profiles, smoke test
	cd lab && sudo containerlab deploy -t exaconnect.clab.yml --reconfigure
	lab/netem/apply-profiles.sh
	lab/scripts/smoke.sh

lab-down: ## Destroy the lab
	cd lab && sudo containerlab destroy -t exaconnect.clab.yml --cleanup
	rm -rf lab/.state

lab-smoke: ## Ping the PoP from every site over every underlay
	lab/scripts/smoke.sh

controller-up: ## Start controller + database (needs .env; run after lab-up)
	docker compose -f deploy/docker-compose.yml --env-file .env up -d --build

controller-down: ## Stop controller + database
	docker compose -f deploy/docker-compose.yml --env-file .env down

demo-seed: ## Seed two sites, one PoP, three paths, three classes (arrives in M2)
	@echo "demo-seed arrives in M2 (CLAUDE.md section 7)"; exit 1

demo: ## Run the full acceptance demo (arrives in M7)
	@echo "demo arrives in M7 (CLAUDE.md section 5)"; exit 1
