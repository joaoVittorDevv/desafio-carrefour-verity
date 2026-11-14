# Transpilador Google ADK — solicitação clínica fictícia

Implementação do [desafio original](docs/DESAFIO_SENIOR_IA.pdf). A [especificação técnica](docs/especificacao-tecnica-e-arquitetura.md) documenta os contratos e decisões.

> **Escopo atual:** demonstração com pedido **digitado, nítido e bem enquadrado**. Só há novo registro quando todos os termos extraídos são encontrados; termos sem correspondência bloqueiam o POST. As capturas em `evidence/` precedem esta política e demonstram a versão anterior, que aceitava registros parciais. O catálogo continua 100% fictício (150 exames `MOCK-*`). Uma suíte mock-only e o drift-guard validam o código atual; o novo fluxo ainda não foi reexecutado com Gemini.

## Fluxo e contratos

O transpilador (`transpiler/`) valida [`specs/clinical_agent_spec.json`](specs/clinical_agent_spec.json) com Pydantic e gera um `SequentialAgent` Python do Google ADK com três etapas: OCR → RAG → registro. O artefato gerado é [`runtime/generated/clinical_exam_scheduler_agent.py`](runtime/generated/clinical_exam_scheduler_agent.py), versionável e coberto por teste contra drift. A CLI (`runtime/cli.py`) oferece `transpile`, `preflight` e `run`; códigos 10 (spec), 20 (imagem), 30 (infra), 40 (pipeline) e 50 (privacidade). O `run` mostra em português o andamento da validação da imagem/serviços, OCR, busca no catálogo e registro, com indicador animado em terminal interativo. Não exibe texto extraído ou argumentos das ferramentas; só confirma o registro após conferir o resultado.

- **`mcp-ocr` (:8001 SSE):** recebe a imagem por volume somente-leitura, faz OCR local com RapidOCR e mascara PII antes de passar texto ao LLM. Se a confiança for baixa, faltar o analisador português local ou restar PII detectável, bloqueia. O guardrail usa regex BR, Presidio/spaCy e léxico do catálogo para reduzir falsos positivos de nomes de exames. Os callbacks do ADK exigem evidências reais de cada ferramenta antes do próximo passo.
- **`mcp-rag` (:8002 SSE):** reconcilia nomes com catálogo **sintético** de 150 exames em PostgreSQL/pgvector + pg_trgm; FastEmbed usa o export ONNX quantizado do E5-small (`Xenova/multilingual-e5-small`, 384 dimensões, MEAN + L2). Busca híbrida: α=0,65 e limiar τ=0,80. `MOCK-*` **não é TUSS oficial**; `tuss_code` é nome legado de campo.
- **`scheduling-api` (:8000):** aceita apenas POST com lista de exames não vazia e `unmatched_terms` vazio; novos registros são sempre `CONFIRMED` (**solicitação registrada, não reserva de horário**). Resultados parciais do RAG causam erro sem POST. GET preserva leitura de registros `PARTIAL` criados pela versão anterior. Swagger: <http://localhost:8000/docs>.

**Não usar com dados reais ou para orientação clínica.** A imagem principal [`assets/pedido_medico_teste.png`](assets/pedido_medico_teste.png) contém somente exames digitados e enumerados. O catálogo e o preparo/jejum são fictícios. A validação do OCR exige seção `Solicito:` e ao menos uma linha numerada legível, mas **não garante** detectar uma foto cortada que ainda contenha esses sinais; a qualidade e o enquadramento são pré-condições da demonstração.

## Setup e testes locais (somente mocks)

Python 3.12 e [`uv`](https://docs.astral.sh/uv/). Não exige chave, banco, rede, OCR ou pesos para a suíte automatizada:

```bash
uv venv --python 3.12
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest -q --cov=transpiler --cov=runtime --cov=security --cov=services
.venv/bin/ruff check transpiler runtime security services tests assets
```

Os testes substituem OCR, embeddings, MCP, runner/SDK, HTTP e PostgreSQL por fakes/mocks; o teste em `tests/e2e/` é **simulado**, não prova o fluxo real. O teste de drift compila a spec localmente (`ruff format` deve estar no PATH) e compara byte a byte com o artefato gerado. Uma verificação em imagem também pode rodar `docker compose build agent-cli` e `docker run --rm desafio-carrefour-verity-agent-cli python -m pytest -q`.

## Docker e execução

Copie `.env.example` para `.env` e preencha localmente `POSTGRES_PASSWORD` e `GEMINI_API_KEY`. A chave é usada **somente no `run` real**; não a imprima, não a coloque em código ou commits. `.env` é ignorado pelo Git e excluído do contexto Docker. Se uma chave já foi exposta fora de `.env`, rotacione-a. Modelos locais (spaCy `pt_core_news_sm` + Presidio no OCR/API; E5-small ONNX quantizado no RAG) são baixados durante o build das respectivas imagens.

```bash
docker compose config --quiet
docker compose up -d --build postgres-db mcp-ocr mcp-rag scheduling-api
docker compose ps
docker compose run --rm agent-cli python -m runtime.cli preflight
# Codegen puro também funciona com --no-deps (make transpile).
docker compose run --rm --no-deps agent-cli python -m runtime.cli transpile \
  --spec /app/specs/clinical_agent_spec.json \
  --output /app/runtime/generated/clinical_exam_scheduler_agent.py
# Chama o Gemini e pode ter custo; execute somente com sua própria autorização.
docker compose run --rm agent-cli python -m runtime.cli run \
  --agent-module runtime.generated.clinical_exam_scheduler_agent \
  --image /app/inputs/pedido_medico_teste.png
```

## Evidências da entrega

[`evidence/`](evidence/) contém artefatos da execução de **13/11/2026**:

| Arquivo | Demonstração |
|---|---|
| [`01-compose-ps.txt`](evidence/01-compose-ps.txt), [`01-compose-ps.json`](evidence/01-compose-ps.json), [`01-images-digests.txt`](evidence/01-images-digests.txt) | Serviços healthy e IDs/tamanhos de imagens locais (digest remoto `<none>`, não prova de imutabilidade). |
| [`02-smoke-ocr.txt`](evidence/02-smoke-ocr.txt), [`02-smoke-rag.json`](evidence/02-smoke-rag.json), [`02-rag-seed-log.txt`](evidence/02-rag-seed-log.txt) | Exames preservados com PII mascarada; `Hemograma Completo` → `MOCK-0001` (score ~0,93), typo → mesmo código, lixo sem match; 150 registros. |
| [`03-preflight.txt`](evidence/03-preflight.txt) | Preflight exit 0. |
| [`04-swagger.png`](evidence/04-swagger.png), [`04-openapi.json`](evidence/04-openapi.json) | Interface e contrato OpenAPI da API. |
| [`05-run-full.txt`](evidence/05-run-full.txt), [`05-protocol-get.json`](evidence/05-protocol-get.json) | Execução com Gemini exit 0: `CONFIRMED`, 6 exames `MOCK-*`, sem reserva; GET confirma o registro. |
| [`06-tests.txt`](evidence/06-tests.txt) | 68 testes, cobertura e Ruff. |
| [`07-code-review.md`](evidence/07-code-review.md) | Revisão de código, correções e limitações residuais. |

**Evidência histórica:** `05-run-full.txt` é uma execução bem-sucedida anterior à redução de escopo; `evidence/fixture-audit/` documenta 31 execuções da versão que aceitava `PARTIAL`. Não interpretá-las como validação do fluxo atual. O protocolo é criado quando o POST chega à API; um exit 40 posterior pode significar registro efetuado sem confirmação pela CLI — consulte a API antes de repetir para evitar duplicação.

## Limitações e transparência

IA auxiliou arquitetura, documentação e implementação; testes mock-only e execução sintética foram revisados. Não há avaliação de acurácia clínica, OCR manuscrito real, performance ou proteção contra PII arbitrária. O léxico anti-falso-positivo protege termos do catálogo, não qualquer exame; o NER local do `agent-cli` é opcional, enquanto OCR/API exigem detector local; o validador fail-closed pode rejeitar outros textos legítimos (ex.: sequências numéricas semelhantes a telefones). A suíte usa banco mockado e `FakeRunner`, portanto não valida migrações/SQL real nem callbacks encadeados; os smokes/execução real complementam, mas não substituem testes de produção. `infra/init.sql` roda apenas na primeira criação do volume PostgreSQL; alterar DDL requer migração ou recriação deliberada do volume. O gerador de imagem usa fontes DejaVu em caminhos Linux, e a imagem de runtime inclui dependências dev para permitir teste dentro do container. Os modelos e versões não são reproduzíveis bit a bit sem pin de artefatos/imagens.
