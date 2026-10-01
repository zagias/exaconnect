# ExaConnect. Lab targets run on the Linux lab host (Ubuntu 24.04, arm64 first), never on macOS.
SHELL := bash
NODE_IMAGE := exaconnect/node:dev
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
COMMIT ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
LDFLAGS := -s -w -X github.com/zagias/exaconnect/agent/internal/version.Version=$(VERSION) \
           -X github.com/zagias/exaconnect/agent/internal/version.Commit=$(COMMIT)
PY ?= python3

.PHONY: help build build-agent build-portal test test-agent test-controller test-portal lint \
        lab-image lab-agent lab-up lab-down lab-smoke lab-routing controller-up controller-down \
        controller-logs demo-seed agents-start agents-stop agents-status demo

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

# ---- lab (Linux lab host only) ----
lab-image: ## Build the lab node image (FRR 10 + WireGuard + tools)
	docker build -t $(NODE_IMAGE) lab/images/node

lab-agent: ## Build exa-agent for this host into bin/lab/ (uses Docker if Go is not installed)
	mkdir -p bin/lab
	if command -v go >/dev/null; then \
	  cd agent && CGO_ENABLED=0 go build -trimpath -ldflags "$(LDFLAGS)" -o ../bin/lab/exa-agent ./cmd/exa-agent; \
	else \
	  docker run --rm -v "$(CURDIR)":/src -w /src/agent -e CGO_ENABLED=0 golang:1.24 \
	    go build -buildvcs=false -trimpath -ldflags "$(LDFLAGS)" -o ../bin/lab/exa-agent ./cmd/exa-agent; \
	fi

lab-up: lab-image lab-agent ## Deploy the containerlab topology, apply underlay profiles, smoke test
	cd lab && sudo containerlab deploy -t exaconnect.clab.yml --reconfigure
	lab/netem/apply-profiles.sh
	lab/scripts/smoke.sh

lab-down: ## Destroy the lab
	cd lab && sudo containerlab destroy -t exaconnect.clab.yml --cleanup
	rm -rf lab/.state

lab-smoke: ## Ping the PoP from every site over every underlay
	lab/scripts/smoke.sh

lab-routing: ## M1 check: WireGuard, BGP, BFD and site-to-site ping via the PoP
	lab/scripts/check-routing.sh

controller-up: ## Start database, controller and agent TLS proxy (run after lab-up)
	lab/scripts/init-env.sh
	docker compose -f deploy/docker-compose.yml --env-file .env up -d --build --wait

controller-down: ## Stop controller, proxy and database (data is kept)
	docker compose -f deploy/docker-compose.yml --env-file .env down

controller-logs: ## Follow controller and proxy logs
	docker compose -f deploy/docker-compose.yml --env-file .env logs -f controller proxy

demo-seed: ## Seed two sites, one PoP, three paths, three classes; enrol and start the agents
	mkdir -p lab/.state
	umask 077 && docker compose -f deploy/docker-compose.yml --env-file .env exec -T controller \
	  python -m exaconnect_controller.seed --lab > lab/.state/seed.json
	lab/scripts/agents.sh enrol lab/.state/seed.json

agents-start: ## Start the agents (after demo-seed)
	lab/scripts/agents.sh start

agents-stop: ## Stop the agents (forwarding keeps running on the last state)
	lab/scripts/agents.sh stop

agents-status: ## Show whether each agent is running
	lab/scripts/agents.sh status

demo: ## Run the full acceptance demo (arrives in M7)
	@echo "demo arrives in M7 (CLAUDE.md section 5)"; exit 1
