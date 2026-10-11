.PHONY: help install build dev backend test check-deps test-backend test-cli test-mcp test-web clean docker

help:
	@echo "Livewall — common tasks"
	@echo
	@echo "  make install       build .venv (backend + mcp + test deps) + web/node_modules"
	@echo "  make build         build the client into web/dist"
	@echo "  make dev           run backend + client dev server together"
	@echo "  make backend       run the backend alone (loopback:8470)"
	@echo "  make test          run every test suite"
	@echo "  make docker        build + run via docker compose"
	@echo "  make clean         remove build output and caches"

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r backend/requirements.txt -r mcp/requirements.txt playwright==1.63.0
	.venv/bin/playwright install chromium
	cd web && npm install

build:
	cd web && npm run build

backend:
	.venv/bin/python backend/app.py

dev:
	@echo "backend on :8470, client on :5183 — open http://127.0.0.1:5183"
	@( .venv/bin/python backend/app.py ) & \
	 ( cd web && npm run dev ); \
	 kill %1 2>/dev/null || true

test: check-deps
	python3 scripts/run_tests.py

# Reproducibility guard: every install source must pin exactly. Runs before
# the suites so an unpinned dependency fails the build, not a stranger's month.
check-deps:
	python3 scripts/check_deps.py

test-backend:
	python3 scripts/run_tests.py backend

test-cli:
	python3 scripts/run_tests.py cli

test-mcp:
	python3 scripts/run_tests.py mcp

test-web:
	python3 scripts/run_tests.py web

docker:
	docker compose up --build -d
	@echo "up on http://127.0.0.1:$${LIVEWALL_PORT:-8470}"

clean:
	rm -rf web/dist web/node_modules web/shots .venv
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
