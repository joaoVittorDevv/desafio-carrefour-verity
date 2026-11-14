.PHONY: setup up preflight transpile run test logs
setup:
	@echo 'Copie .env.example para .env e configure credenciais locais antes de usar Docker.'
up:
	docker compose up -d --build postgres-db mcp-ocr mcp-rag scheduling-api
preflight:
	docker compose run --rm agent-cli python -m runtime.cli preflight
transpile:
	docker compose run --rm --no-deps agent-cli python -m runtime.cli transpile --spec /app/specs/clinical_agent_spec.json --output /app/runtime/generated/clinical_exam_scheduler_agent.py
run:
	docker compose run --rm agent-cli python -m runtime.cli run --agent-module runtime.generated.clinical_exam_scheduler_agent --image /app/inputs/pedido_medico_teste.png
test:
	uv run --extra dev pytest -q --cov=transpiler --cov=runtime --cov=security --cov=services
logs:
	docker compose logs --tail=100 -f
