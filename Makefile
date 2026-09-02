.DEFAULT_GOAL := help

.PHONY: .uv
.uv:
	@uv --version || echo 'Please install uv: https://docs.astral.sh/uv/getting-started/installation/'

.PHONY: .pnpm
.pnpm:
	@pnpm --version || echo 'Please install pnpm: https://pnpm.io/installation'

.PHONY: install
install: .uv .pnpm ## Install JS and Python dependencies and prek hooks
	pnpm install
	uv sync --directory server
	uvx prek install --install-hooks

.PHONY: format
format: ## Format TypeScript (biome) and Python (ruff)
	pnpm exec biome format --write .
	pnpm exec biome check --write --formatter-enabled=false .
	uv run --directory server ruff format
	uv run --directory server ruff check --fix --fix-only

.PHONY: lint
lint: ## Lint TypeScript (biome) and Python (ruff, basedpyright strict)
	pnpm exec biome check .
	uv run --directory server ruff format --check
	uv run --directory server ruff check
	uv run --directory server basedpyright

.PHONY: typecheck
typecheck: ## Type-check TypeScript with tsc
	pnpm exec tsc --noEmit

.PHONY: test
test: ## Run the Python tests
	uv run --directory server pytest

.PHONY: build
build: typecheck ## Build the static site into dist/
	pnpm exec vite build

.PHONY: dev
dev: ## Run the Vite dev server (the game) on http://localhost:5173
	pnpm exec vite

.PHONY: server
server: ## Run the FastAPI autopilot server on ws://localhost:8000/ws
	uv run --directory server uvicorn thrust_server.main:app --reload --port 8000

.PHONY: main
main: format lint typecheck test ## Run formatting, linting, type-checking and tests

# (must stay last!)
.PHONY: help
help: ## Show this help (usage: make help)
	@echo "Usage: make [recipe]"
	@echo "Recipes:"
	@awk '/^[a-zA-Z0-9_-]+:.*?##/ { \
	    helpMessage = match($$0, /## (.*)/); \
	        if (helpMessage) { \
	            recipe = $$1; \
	            sub(/:/, "", recipe); \
	            printf "  \033[36mmake %-20s\033[0m %s\n", recipe, substr($$0, RSTART + 3, RLENGTH); \
	    } \
	}' $(MAKEFILE_LIST)
