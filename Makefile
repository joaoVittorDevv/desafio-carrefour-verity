.PHONY: setup setup-local build up preflight transpile transpile-local run run-image test logs stop down
setup: build up preflight
setup-local:
	@command -v uv >/dev/null || { echo 'Instale o uv: https://docs.astral.sh/uv/'; exit 2; }
	uv venv --python 3.12 --clear
	uv pip install --python .venv/bin/python -e '.[dev]'
	@.venv/bin/python -m runtime.cli --help >/dev/null && echo 'Ambiente local pronto: use make transpile-local SPEC=... OUTPUT=...'
build:
	@echo 'Copie .env.example para .env e configure credenciais locais antes de usar Docker.'
	docker compose build agent-cli mcp-ocr mcp-rag scheduling-api
up:
	docker compose up -d postgres-db mcp-ocr mcp-rag scheduling-api
preflight:
	docker compose run --rm agent-cli python -m runtime.cli preflight
SPEC ?= specs/clinical_agent_spec.json
OUTPUT ?= runtime/generated/clinical_exam_scheduler_agent.py
transpile:
	docker compose run --rm --no-deps agent-cli python -m runtime.cli transpile --spec /app/specs/clinical_agent_spec.json --output /app/runtime/generated/clinical_exam_scheduler_agent.py
transpile-local:
	@test -x .venv/bin/python || { echo 'Sem .venv: rode uv venv && uv pip install --python .venv/bin/python -e ".[dev]"'; exit 2; }
	.venv/bin/python -m runtime.cli transpile --spec "$(SPEC)" --output "$(OUTPUT)"
run:
	docker compose run --rm agent-cli python -m runtime.cli run --agent-module runtime.generated.clinical_exam_scheduler_agent --image /app/inputs/carlos__good.png
run-image:
	@test -n "$(IMAGE)" || { echo 'Uso: make run-image IMAGE=/caminho/na/sua/maquina/pedido.png'; exit 2; }
	@test -f "$(IMAGE)" || { echo "Imagem não encontrada: $(IMAGE)"; exit 2; }
	if [ "$(abspath $(IMAGE))" != "$(abspath assets/$(notdir $(IMAGE)))" ]; then cp -f "$(IMAGE)" assets/; fi
	docker compose run --rm agent-cli python -m runtime.cli run --agent-module runtime.generated.clinical_exam_scheduler_agent --image "/app/inputs/$(notdir $(IMAGE))"
test:
	uv run --extra dev pytest -q --cov=transpiler --cov=runtime --cov=security --cov=services
logs:
	docker compose logs --tail=100 -f
stop:
	docker compose stop
down:
	docker compose down
