# Especificação Técnica e Arquitetura: Transpilador Google ADK & Assistente Clínico de Agendamento (End-to-End)

**Versão do Documento:** 1.1.0

**Status:** Aprovado para Implementação; contratos atualizados com decisões de produto

**Público-Alvo:** Avaliadores Técnicos e Engenharia de IA Sênior

---

## 1. Visão Geral Executiva e Escopo

Este documento estabelece a especificação técnica, as decisões arquiteturais (ADRs), os contratos de integração, a modelagem de dados e os critérios de validação (KPIs) para a construção de:

1. **Um Transpilador Declarativo ($\text{JSON} \rightarrow \text{Python Google ADK}$):** Motor de compilação responsável por validar um manifesto declarativo em JSON (via Pydantic v2), verificar referências cruzadas de ferramentas e schemas, e gerar código Python executável, formatado e validado sintaticamente via Árvore Sintática Abstrata (`ast`), instanciando agentes exclusivamente com o **Google Agent Development Kit (`google-adk`)**.
2. **Um Ecossistema Distribuído de Execução (Caso de Uso Clínico):** Assistente de linha de comando (CLI) conteinerizado via Docker Compose para processamento de pedidos médicos fictícios digitados, nítidos e bem enquadrados, contemplando:
   * **Extração Óptica e Guardrail de Privacidade (`mcp-ocr`):** Servidor MCP comunicando-se exclusivamente via *Server-Sent Events* (SSE), executando pré-processamento de imagem, OCR local e anonimização determinística/NLP de dados sensíveis (PII) **antes** do envio ao LLM.
   * **Recuperação Aumentada Híbrida (`mcp-rag`):** Servidor MCP (SSE) conectado ao PostgreSQL (`pgvector` + `pg_trgm`) com catálogo sintético superior a $100$ exames laboratoriais com códigos fictícios (convenção `MOCK-0001`, `MOCK-0002`, etc.; não são códigos TUSS/CBHPM oficiais) e geração de embeddings locais em CPU via `FastEmbed`.
   * **Serviço de Agendamento (`scheduling-api`):** Microsserviço RESTful em FastAPI com persistência relacional no PostgreSQL e documentação OpenAPI 3.1 (`/docs`) consumida nativamente pelo agente via `OpenAPIToolset`.

---

## 2. Registros de Decisões Arquiteturais (ADRs)

### ADR-001: Orquestração Determinística com `SequentialAgent`; instruções estáticas, contratos tipados de entrada/saída e o conflito `tools` vs. `output_schema`
* **Contexto:** O caso de uso exige uma cadeia estritamente ordenada de 5 etapas (Entrada $\rightarrow$ OCR $\rightarrow$ RAG $\rightarrow$ Agendamento $\rightarrow$ Saída CLI). Além disso, na API do Gemini/ADK, configurar simultaneamente `tools` (*Function Calling*) e `output_schema` (*Controlled Generation*) no mesmo `LlmAgent` pode ocasionar conflitos na geração de chamadas de ferramentas. Por fim, interpolar dados da etapa anterior dentro do `instruction` (system prompt) mistura dado dinâmico com regra de sistema e amplia a superfície de *prompt injection*.
* **Decisão:**
  1. Adotar o padrão `SequentialAgent` do Google ADK coordenando subagentes especializados (`LlmAgent`), compartilhando dados através do dicionário de sessão (`session.state`) via `output_key`.
  2. **Instruções estritamente estáticas e dados tipados fora do system prompt.** A DSL permite que cada agente declare `input_key` + `input_schema_ref` + `include_contents`. O transpilador gera um `before_agent_callback` (`_validate_input`) que valida a entrada com Pydantic, aplica a inspeção anti-PII e checa invariantes determinísticos (lista de exames não vazia; `matched_exams` não vazio e idêntico ao `_rag_verified`) **antes** de acionar o LLM. A entrada validada é entregue ao modelo como mensagem de turno `role="user"` isolada (`include_contents="none"` remove o histórico bruto dos agentes anteriores; o `before_model_callback` injeta o JSON sanitizado na primeira chamada do turno), nunca no `instruction`. O validador da DSL rejeita `instruction` que contenha `{<input_key>}` interpolado.
  3. **Saída estruturada em duas camadas.** No `google-adk==2.14.1`, `output_schema=` convive com `tools` (o ADK injeta a tool `set_model_response`, que estrutura apenas a resposta final, mantendo as tools disponíveis no loop de raciocínio); a saída final continua auditada pelo `after_agent_callback` contra as evidências reais das tools (`_ocr_verified`/`_rag_verified`/`_booking_verified`), e o callback de tool ignora `set_model_response` na gravação de evidências.
* **Motivação:** Previsibilidade de execução, rastreabilidade por etapa, facilidade de testes isolados, garantia de tipagem forte sem quebrar o loop de *tool calling*, e separação entre instrução (estática) e dado (dinâmico, tipado e sanitizado) para reduzir injeção de prompt e acoplamento de regra de negócio a texto livre.

### ADR-002: Múltiplos Servidores MCP via SSE com Princípio do Menor Privilégio (`tool_filter`)
* **Contexto:** Os requisitos determinam que as ferramentas de OCR e RAG sejam servidores MCP comunicando-se exclusivamente via SSE e avaliam a separação clara de responsabilidades.
* **Decisão:** Isolar `mcp-ocr` (porta `8001`) e `mcp-rag` (porta `8002`) em containers independentes utilizando `FastMCP` sobre Starlette/Uvicorn com transporte SSE. No JSON de especificação, cada agente declara explicitamente a lista `allowed_tools`, que o transpilador converte para o parâmetro nativo `tool_filter` do `MCPToolset` no Google ADK.
* **Motivação:** Isolamento de dependências de sistema operacional (bibliotecas gráficas do OpenCV/OCR separadas das bibliotecas de busca vetorial), escalabilidade independente e segurança em nível de *runtime* (o agente de OCR jamais pode invocar ferramentas de banco de dados e vice-versa).

### ADR-003: PostgreSQL 16 Unificado (`pgvector` + `pg_trgm`) com Isolamento por Schemas e Busca Híbrida
* **Contexto:** O sistema precisa armazenar um catálogo sintético com mais de $100$ exames médicos para consulta RAG e persistir as solicitações registradas pela API FastAPI. Textos extraídos por OCR de pedidos digitados podem apresentar ruídos ortográficos ou abreviações clínicas (ex.: `Hemo completo`, `Glicemia jejum`, `TSH ultra`).
* **Decisão:** Utilizar uma única instância PostgreSQL (`pgvector/pgvector:pg16`) dividida em dois schemas lógicos (`rag` e `scheduling`). No schema `rag`, habilitar as extensões `vector` (índice HNSW) e `pg_trgm` (índice GIN de trigramas) para executar **Busca Híbrida** combinando similaridade semântica de cosseno e similaridade léxica.
* **Motivação:** Reduz o overhead operacional do Docker Compose a um único SGBD robusto, enquanto a busca híbrida contorna erros de caracteres produzidos pelo OCR onde a busca puramente semântica ou puramente exata falharia.

### ADR-004: Inferência Local em CPU via ONNX Runtime (`FastEmbed` e `RapidOCR`); modelos provisionados no build
* **Contexto:** O uso de PyTorch tradicional (`sentence-transformers` ou `EasyOCR`) gera imagens Docker superiores a $2\text{ GB}$ e introduz lentidão no build e no boot dos containers. Além disso, o container de RAG deve operar de forma independente de APIs externas de embedding.
* **Decisão:**
  1. Utilizar **`FastEmbed`** (`intfloat/multilingual-e5-small`, dimensão $d = 384$) sobre `onnxruntime` no `mcp-rag`.
  2. Utilizar **`RapidOCR`** (`rapidocr-onnxruntime`) combinado com **`OpenCV`** (`opencv-python-headless`) no `mcp-ocr`.
  3. **Implementado:** o FastEmbed registra `intfloat/multilingual-e5-small` como modelo custom usando o export ONNX quantizado `Xenova/multilingual-e5-small` (`onnx/model_quantized.onnx`, pooling MEAN + L2, 384 dimensões) e baixa os pesos no build de `mcp-rag`. `mcp-ocr` e `scheduling-api` instalam Presidio e `pt_core_news_sm` nos respectivos builds; o OCR força OpenCV headless. Os testes automatizados permanecem mock-only; houve validação real com a imagem sintética e uma execução autorizada do Gemini.
* **Motivação:** Inferência local sem GPU e sem API externa de embeddings. Tamanhos locais medidos após o build: OCR 830 MB, RAG 538 MB, API 527 MB e CLI 368 MB, todos abaixo de 2 GB; cold start e latência ainda não foram medidos.

### ADR-005: Tráfego de Imagem via Volume Compartilhado Read-Only (`/app/inputs`)
* **Contexto:** Trafegar imagens codificadas em Base64 dentro do prompt do LLM para repassá-las via argumentos JSON de uma chamada MCP SSE degrada drasticamente a latência, consome milhares de tokens desnecessariamente e está sujeito a truncamento pelo modelo.
* **Decisão:** Montar um volume compartilhado somente-leitura (`./assets:/app/inputs:ro`) entre o container/ambiente do `runtime` (CLI) e o container `mcp-ocr`. O agente recebe e manipula exclusivamente o caminho lógico do arquivo (`file_path: "/app/inputs/carlos__good.png"`).
* **Motivação:** Otimização de custo e latência, eliminação de alucinações em strings Base64 e aderência às boas práticas de engenharia de dados com agentes.

### ADR-006: Defesa em Profundidade para Proteção de Dados Sensíveis (PII Guardrail em Dupla Camada)
* **Contexto:** O desafio exige detectar e anonimizar/mascarar dados sensíveis (nomes, documentos, contatos) extraídos da imagem **antes** que sejam enviados para um LLM ou persistidos.
* **Decisão:** Implementar anonimização em duas barreiras:
  1. **Barreira Primária (Dentro do `mcp-ocr`, Pré-LLM):** Imediatamente após o `RapidOCR` extrair o texto da imagem, o texto passa por um pipeline de sanitização combinando **Microsoft Presidio** (`pt_core_news_sm`) e **Reconhecedores Regex Brasileiros** (CPF, RG, CRM, CNS, Telefone/Celular, E-mail e heurística de cabeçalhos `Paciente:` / `Dr(a).:`). O retorno da tool SSE já entrega o texto mascarado com `[REDACTED_<ENTITY>]`.
  2. **Barreira Secundária (No Runtime ADK via Callbacks):** O código Python gerado registra `before_model_callback`, `before_tool_callback` e `after_tool_callback`. Eles bloqueiam PII residual, falhas de OCR comunicadas pelo MCP, respostas de tool inválidas e qualquer POST cujos itens ou termos difiram do resultado RAG verificado. A própria FastAPI também exige detector local de nomes; se ele não estiver disponível, rejeita a escrita e indica indisponibilidade no `/health`.
* **Motivação:** Garante cumprimento estrito do requisito de privacidade ("Zero-PII Exposure to LLM"), pois os dados brutos da imagem nunca saem da memória volátil do container `mcp-ocr`.

### ADR-007: Consumo Nativo da FastAPI via `OpenAPIToolset` do Google ADK
* **Contexto:** A API de agendamentos deve ser construída em FastAPI com documentação clara no Swagger (`/docs`), refletindo exatamente o contrato consumido pelo agente.
* **Decisão:** Gerar no transpilador a integração usando `OpenAPIToolset` do `google-adk`; o runtime obtém o schema em `http://scheduling-api:8000/openapi.json` e o passa como `spec_dict`, com `allow_private_network_access=True` apenas para o serviço interno e `tool_filter` restringindo a operação `create_exam_appointment`. A lista `allowed_operations` no manifesto é traduzida para os nomes de ferramentas gerados pelo ADK.
* **Motivação:** Elimina código *boilerplate* de clientes HTTP manuais e garante acoplamento contratual único (*Single Source of Truth*) entre o Swagger (`/docs`) e o agente.

### ADR-008: Registro integral obrigatório e compatibilidade de leitura histórica
* **Decisão atual:** aceitar apenas pedidos fictícios digitados com pelo menos um exame extraído e **todos** os termos extraídos reconciliados com o catálogo. O RAG continua retornando `matched_exams` e `unmatched_terms` como diagnóstico; qualquer termo sem correspondência, lista vazia ou divergência de ordem/multiplicidade entre extração e RAG bloqueia antes do POST. A API rejeita POST parcial diretamente, sem persistência. Novos registros são `CONFIRMED` (**solicitação registrada**, não reserva de horário); `scheduled_at` é o instante do registro. GET e o schema de resposta ainda conseguem ler registros `PARTIAL` históricos; o banco não é migrado nem apagado. Após o POST, uma falha de confirmação não desfaz o registro: consultar a API antes de repetir.
* **Motivação:** Evitar registros silenciosamente incompletos; preservar rastreabilidade e compatibilidade de leitura do histórico sem alterar a stack.

### ADR-009: Falha segura de privacidade e catálogo explicitamente fictício
* **Decisão:** Se o OCR/anonimizador não conseguir liberar o texto com segurança, faltar detector local de nomes no OCR/API ou qualquer barreira detectar PII residual, bloquear o fluxo **antes do LLM ou da persistência**, informar que não houve registro e sugerir nova tentativa com uma imagem mais legível; nunca exibir nem registrar o valor sensível no erro. Todos os exames, códigos e instruções do catálogo são simulados; usar identificadores distinguíveis (`MOCK-0001` etc.), sem atribuir-lhes validade TUSS/CBHPM real nem orientação clínica real.
* **Motivação:** Não transformar exemplos fictícios em dados clínicos oficiais e não fazer chamadas externas quando a segurança da entrada não estiver estabelecida.

---

## 3. Arquitetura do Sistema e Topologia

### 3.1. Diagrama do Pipeline de Compilação (Build-Time: Transpilador)

```text
+-----------------------------------------------------------------------------------+
|                         PIPELINE DO TRANSPILADOR (CLI)                            |
|                                                                                   |
|  [specs/clinical_agent_spec.json]                                                 |
|                 |                                                                 |
|                 v                                                                 |
|  +------------------------------+     Erros Estruturais/Semânticos                |
|  | 1. Parser & Pydantic v2      |----------------------------------> [Exit 10]    |
|  |    (@model_validator)        |     (Painel Rich com caminho JSON)              |
|  +--------------+---------------+                                                 |
|                 | AST Abstrata da Spec Validada                                   |
|                 v                                                                 |
|  +------------------------------+                                                 |
|  | 2. Motor de Templates Jinja2 |                                                 |
|  | (adk_sequential_agent.py.j2) |                                                 |
|  +--------------+---------------+                                                 |
|                 | Código Python em Memória                                        |
|                 v                                                                 |
|  +------------------------------+     SyntaxError / Static Check                  |
|  | 3. Validador Sintático (ast) |----------------------------------> [Exit 10]    |
|  |    (ast.parse + Ruff Format) |                                                 |
|  +--------------+---------------+                                                 |
|                 |                                                                 |
|                 v                                                                 |
|  [runtime/generated/clinical_exam_scheduler_agent.py]                             |
+-----------------------------------------------------------------------------------+
```

### 3.2. Diagrama de Execução End-to-End (Run-Time: Containers & Agentes)

```text
+-----------------------------------------------------------------------------------+
|                           AMBIENTE DOCKER COMPOSE                                 |
|                                                                                   |
|  [Usuário / Avaliador]                                                            |
|           |  cli run --agent-module ... --image /app/inputs/carlos__good.png      |
|           v                                                                       |
|  +-----------------------------------------------------------------------------+  |
|  | Container / Host: ADK Runtime (Código Python Gerado pelo Transpilador)      |  |
|  |                                                                             |  |
|  |  +-----------------------------------------------------------------------+  |  |
|  |  | SequentialAgent: clinical_scheduling_pipeline                         |  |  |
|  |  |                                                                       |  |  |
|  |  |  [1. ocr_intake_agent] --> [2. rag_lookup_agent] --> [3. sched_agent] |  |  |
|  |  |   output_key:               output_key:               output_key:     |  |  |
|  |  |   "extracted_exams"         "resolved_exams"          "booking_result"|  |  |
|  |  +-----------+-------------------------+------------------------+--------+  |  |
|  +--------------|-------------------------|------------------------|-----------+  |
|                 |                         |                        |              |
|        MCP over SSE              MCP over SSE             HTTP REST (OpenAPI)     |
|     http://mcp-ocr:8001/sse   http://mcp-rag:8002/sse   http://scheduling-api:8000|
|                 |                         |                        |              |
|                 v                         v                        v              |
|  +------------------------+  +------------------------+  +---------------------+  |
|  | Container: mcp-ocr     |  | Container: mcp-rag     |  | Container:          |  |
|  | - Lê /app/inputs (RO)  |  | - FastEmbed (ONNX CPU) |  | scheduling-api      |  |
|  | - OpenCV Deskew/Thresh |  | - Query Híbrida        |  | - FastAPI + Swagger |  |
|  | - RapidOCR (ONNX CPU)  |  |   (pgvector + pg_trgm) |  | - Validação Exames  |  |
|  | - Guardrail PII        |  +------------+-----------+  +----------+----------+  |
|  |   (Presidio + Regex BR)|               |                         |             |
|  +------------------------+               +------------+------------+             |
|                                                        |                          |
|                                                        v                          |
|                                        +-------------------------------+          |
|                                        | Container: postgres-db        |          |
|                                        | Image: pgvector/pgvector:pg16 |          |
|                                        | - Schema `rag` (>100 exames)  |          |
|                                        | - Schema `scheduling`         |          |
|                                        +-------------------------------+          |
+-----------------------------------------------------------------------------------+
```

### 3.3. Matriz de Serviços e Rede (`docker-compose.yml`)

| Serviço | Imagem / Base | Porta Interna / Externa | Volumes Mapeados | Dependências (`depends_on`) | Healthcheck Endpoint |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `postgres-db` | `pgvector/pgvector:pg16` | `5432:5432` | `pgdata:/var/lib/postgresql/data`<br>`./infra/init.sql:/docker-entrypoint-initdb.d/01-init.sql:ro` | Nenhuma | `pg_isready -U $POSTGRES_USER -d $POSTGRES_DB` |
| `mcp-ocr` | `python:3.12-slim` (Custom) | `8001:8001` | `./assets:/app/inputs:ro` | Nenhuma | `GET http://localhost:8001/health` |
| `mcp-rag` | `python:3.12-slim` (Custom) | `8002:8002` | `./services/mcp_rag/data:/app/data:ro` | `postgres-db (healthy)` | `GET http://localhost:8002/health` |
| `scheduling-api` | `python:3.12-slim` (Custom) | `8000:8000` | Nenhum (Persistência no Postgres) | `postgres-db (healthy)` | `GET http://localhost:8000/health` |
| `agent-cli` | `python:3.12-slim` (Custom) | N/A (Interativo / CLI) | `./assets:/app/inputs:ro`<br>`./specs:/app/specs:ro`<br>`./runtime/generated:/app/runtime/generated:rw` | `mcp-ocr`, `mcp-rag`, `scheduling-api` (all `healthy`) | One-shot: sem healthcheck próprio; `preflight` é comando explícito |

---

## 4. Especificação do Transpilador (DSL JSON $\rightarrow$ Python ADK)

### 4.1. Regras de Validação Estática e Semântica do Transpilador
O módulo `transpiler/schema.py` deve implementar modelos Pydantic v2 com `@model_validator(mode="after")` para assegurar as seguintes invariantes **antes** de iniciar a geração de código:

1. **Restrição de Transporte MCP:** Todo item em `mcp_servers` deve obrigatoriamente possuir `transport == "sse"` e uma URL válida iniciada por `http://` ou `https://`. Caso contrário, lançar erro semântico explicando o requisito de transporte exclusivo via SSE.
2. **Integridade Referencial de Servidores MCP (`mcp_bindings`):** Para cada agente em `orchestration.agents`, todo `server_id` referenciado em `mcp_bindings` deve existir no bloco raiz `mcp_servers`, e `allowed_tools` deve conter ao menos $1$ identificador de tool não vazio.
3. **Integridade Referencial de Serviços OpenAPI (`openapi_bindings`):** Todo `service_id` referenciado em `openapi_bindings` deve existir em `openapi_services`, com `allowed_operations` não vazio.
4. **Integridade Referencial de Schemas (`output_schema_ref`):** Todo `output_schema_ref` declarado em um agente (e tipos aninhados como `list[ResolvedExamItem]`) deve existir no dicionário raiz `schemas`.
5. **Unicidade de Nomes e `output_key`:** Nomes de agentes (`agent.name`) e chaves de estado (`agent.output_key`) devem ser únicos e identificadores Python válidos (`str.isidentifier()`), evitando sobrescrita acidental no `session.state`.
6. **Validação da Árvore Sintática (`ast.parse`):** Após a renderização do template Jinja2, o código Python gerado deve passar por `ast.parse(generated_code)` e verificação de imports obrigatórios (`from google.adk.agents import LlmAgent, SequentialAgent`, `from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, SseConnectionParams`).
7. **Contratos de entrada tipados (`input_key`/`input_schema_ref`/`include_contents`):** Quando um agente declara entrada, `input_key` e `input_schema_ref` devem vir juntos; `input_key` precisa ser identificador Python válido e igual ao `output_key` de um agente **anterior** na sequência; `input_schema_ref` deve existir em `schemas`; e o `instruction` não pode conter a interpolação `{<input_key>}` (instrução estática, dado entregue em turno separado). `include_contents` aceita apenas `default` ou `none`.

### 4.2. Contrato Completo do JSON de Especificação (`specs/clinical_agent_spec.json`)

```json
{
  "version": "1.0.0",
  "metadata": {
    "name": "clinical_exam_scheduler",
    "description": "Assistente CLI para extração de exames médicos via OCR, reconciliação RAG e agendamento via FastAPI"
  },
  "guardrails": {
    "enable_pii_redaction": true,
    "entities": [
      "PERSON",
      "CPF",
      "RG",
      "PHONE_NUMBER",
      "EMAIL_ADDRESS",
      "CRM"
    ],
    "mask_token": "[REDACTED]"
  },
  "schemas": {
    "ExtractedExamRaw": {
      "description": "Item bruto extraído do pedido médico",
      "fields": {
        "raw_name": {
          "type": "str",
          "description": "Nome do exame como consta no pedido médico"
        },
        "is_handwritten_hint": {
          "type": "bool",
          "description": "Indica se parece vir de nota/observação adicional"
        }
      }
    },
    "ExtractedExamsOutput": {
      "description": "Resultado estruturado da etapa de OCR e sanitização de PII",
      "fields": {
        "sanitized_text": {
          "type": "str",
          "description": "Texto completo após anonimização de dados sensíveis"
        },
        "pii_entities_masked": {
          "type": "int",
          "description": "Quantidade de entidades sensíveis mascaradas"
        },
        "exams": {
          "type": "list[ExtractedExamRaw]",
          "description": "Lista de exames extraídos da imagem"
        }
      }
    },
    "ResolvedExamItem": {
      "description": "Exame reconciliado com o catálogo sintético no banco vetorial",
      "fields": {
        "original_query": {
          "type": "str",
          "description": "Termo original extraído no OCR"
        },
        "tuss_code": {
          "type": "str",
          "description": "Identificador fictício do catálogo (ex: MOCK-0001); campo legado, não é código TUSS oficial"
        },
        "official_name": {
          "type": "str",
          "description": "Nome padronizado do exame no catálogo"
        },
        "category": {
          "type": "str",
          "description": "Categoria laboratorial (ex: Hematologia, Bioquímica)"
        },
        "fasting_hours": {
          "type": "int",
          "description": "Horas de jejum necessárias"
        },
        "confidence_score": {
          "type": "float",
          "description": "Score híbrido de similaridade entre 0.0 e 1.0"
        }
      }
    },
    "ResolvedExamsListOutput": {
      "description": "Lista consolidada de exames reconciliados pelo RAG",
      "fields": {
        "matched_exams": {
          "type": "list[ResolvedExamItem]",
          "description": "Exames encontrados na base com seus códigos"
        },
        "unmatched_terms": {
          "type": "list[str]",
          "description": "Termos que não atingiram o score mínimo"
        }
      }
    },
    "BookingConfirmationOutput": {
      "description": "Retorno completo do registro confirmado pela FastAPI",
      "fields": {
        "protocol_id": {
          "type": "str",
          "description": "Número de protocolo único gerado pela API"
        },
        "status": {
          "type": "str",
          "description": "CONFIRMED para novos registros"
        },
        "scheduled_at": {
          "type": "str",
          "description": "Data/hora ISO-8601 do registro da solicitação; não é horário reservado para coleta"
        },
        "preferred_unit": {
          "type": "str",
          "description": "Unidade indicada na solicitação"
        },
        "max_fasting_hours_required": {
          "type": "int",
          "description": "Maior duração fictícia de jejum entre os exames registrados"
        },
        "preparation_summary": {
          "type": "str",
          "description": "Resumo fictício de preparo/jejum; não substitui orientação clínica"
        },
        "total_exams_scheduled": {
          "type": "int",
          "description": "Quantidade de exames registrados"
        },
        "scheduled_exams": {
          "type": "list[ResolvedExamItem]",
          "description": "Exames efetivamente registrados, com todos os metadados da reconciliação"
        },
        "unmatched_terms": {
          "type": "list[str]",
          "description": "Exames solicitados não encontrados e não registrados"
        }
      }
    }
  },
  "mcp_servers": [
    {
      "id": "mcp_ocr",
      "transport": "sse",
      "url": "http://mcp-ocr:8001/sse",
      "timeout": 30
    },
    {
      "id": "mcp_rag",
      "transport": "sse",
      "url": "http://mcp-rag:8002/sse",
      "timeout": 30
    }
  ],
  "openapi_services": [
    {
      "id": "scheduling_fastapi",
      "spec_url": "http://scheduling-api:8000/openapi.json"
    }
  ],
  "orchestration": {
    "type": "sequential",
    "name": "clinical_scheduling_pipeline",
    "input_volume_path": "/app/inputs",
    "agents": [
      {
        "name": "ocr_intake_agent",
        "model": "gemini-2.5-flash",
        "instruction": "Você é um agente especialista em triagem de pedidos médicos. Invoque a ferramenta 'extract_and_anonymize_medical_order' passando o file_path recebido do usuário. A partir do texto sanitizado retornado, identifique todos os exames laboratoriais solicitados do pedido digitado; não processe manuscritos ou adendos. Responda SOMENTE com um objeto JSON válido, sem cercas de código markdown, seguindo exatamente o schema ExtractedExamsOutput: sanitized_text copiado EXATAMENTE como retornado pela ferramenta (sem resumir, sem alterar, sem omitir); pii_entities_masked é o número inteiro do campo total_redacted dentro de pii_redaction_report (apenas o inteiro, nunca o objeto); exams como lista de objetos com os campos raw_name (nome do exame como consta no texto) e is_handwritten_hint (false para todos os itens do pedido digitado).",
        "mcp_bindings": [
          {
            "server_id": "mcp_ocr",
            "allowed_tools": [
              "extract_and_anonymize_medical_order"
            ]
          }
        ],
        "output_key": "extracted_exams",
        "output_schema_ref": "ExtractedExamsOutput"
      },
      {
        "name": "rag_lookup_agent",
        "model": "gemini-2.5-flash",
        "instruction": "Os exames extraídos chegam como JSON estruturado na mensagem do usuário desta etapa (schema ExtractedExamsOutput); não há interpolação no prompt do sistema. Invoque a ferramenta 'search_exam_catalog_hybrid' enviando a lista completa de nomes de exames (na mesma ordem e com os mesmos raw_name recebidos) para recuperar identificadores fictícios, instruções simuladas de jejum e score de confiança. Nunca apresente identificadores como códigos oficiais TUSS/CBHPM. Responda SOMENTE com JSON válido, sem cercas de código: matched_exams e unmatched_terms EXATAMENTE como retornados pela ferramenta, com todos os campos de cada item (original_query, tuss_code, official_name, category, fasting_hours, confidence_score) preservados sem arredondar nem resumir. Se algum termo ficar sem correspondência, o pedido inteiro será recusado sem registro.",
        "mcp_bindings": [
          {
            "server_id": "mcp_rag",
            "allowed_tools": [
              "search_exam_catalog_hybrid"
            ]
          }
        ],
        "output_key": "resolved_exams",
        "output_schema_ref": "ResolvedExamsListOutput",
        "input_key": "extracted_exams",
        "input_schema_ref": "ExtractedExamsOutput",
        "include_contents": "none"
      },
      {
        "name": "scheduling_agent",
        "model": "gemini-2.5-flash",
        "instruction": "Você coordena o registro fictício apenas de pedidos digitados completos. Receba o JSON estruturado ResolvedExamsListOutput. Se houver qualquer unmatched_terms ou se a lista matched_exams estiver vazia, não invoque a API. Caso contrário invoque create_exam_appointment com todos os matched_exams exatamente como recebidos e unmatched_terms vazio. SEMPRE invoque a ferramenta antes de responder; não simule resposta. Responda SOMENTE com JSON válido, sem cercas: reproduza a resposta completa da API EXATAMENTE como retornada, sem inventar exames, códigos nem horários. CONFIRMED é registro, não reserva de horário.",
        "openapi_bindings": [
          {
            "service_id": "scheduling_fastapi",
            "allowed_operations": [
              "create_exam_appointment"
            ]
          }
        ],
        "output_key": "booking_result",
        "output_schema_ref": "BookingConfirmationOutput",
        "input_key": "resolved_exams",
        "input_schema_ref": "ResolvedExamsListOutput",
        "include_contents": "none"
      }
    ]
  }
}
```

---

## 5. Especificação dos Componentes de Runtime e Contratos de Interface

### 5.1. Servidor MCP 1: Extração Óptica e Guardrail PII (`services/mcp_ocr`)

* **Protocolo:** MCP sobre SSE (`GET /sse` e `POST /messages`).
* **Pipeline Interno da Tool `extract_and_anonymize_medical_order(file_path: str)`:**
  1. **Validação de Path Traversal:** Garante que `os.path.realpath(file_path)` reside estritamente dentro de `/app/inputs` e que a extensão pertence a `{.png, .jpg, .jpeg, .webp}`.
  2. **Pré-processamento OpenCV:** Converte para escala de cinza, aplica remoção leve de ruído (`cv2.fastNlMeansDenoising`) e ajuste de contraste (CLAHE) para pedidos digitados e legíveis.
  3. **Extração com RapidOCR:** Extrai caixas delimitadoras, linhas de texto e confidência média $\bar{c} \in [0, 1]$.
  4. **Sanitização Determinística + NLP (Camada de Segurança PII):** A tool libera apenas texto sanitizado; se não for possível garantir a anonimização (falha de OCR/sanitização ou suspeita de PII residual), falha fechada sem retornar texto bruto. O runtime informa o bloqueio e sugere reenviar imagem mais legível, sem encaminhar conteúdo ao LLM ou persistir dados.
     * Aplica reconhecedores customizados Regex/Presidio para padrões brasileiros:
       * `CPF`: `\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b` $\rightarrow$ `[REDACTED_CPF]`
       * `RG`: `\b\d{1,2}\.?\d{3}\.?\d{3}-?[0-9Xx]\b` $\rightarrow$ `[REDACTED_RG]`
       * `CRM`: `\bCRM(?:-[A-Z]{2})?[\s:]*\d{4,7}\b` $\rightarrow$ `[REDACTED_CRM]`
       * `PHONE_NUMBER`: `\b(?:\+55\s?)?(?:\(?\d{2}\)?\s?)?(?:9\d{4}|\d{4})-?\d{4}\b` $\rightarrow$ `[REDACTED_PHONE]`
       * `EMAIL_ADDRESS`: Regex padrão RFC 5322 $\rightarrow$ `[REDACTED_EMAIL]`
       * `PERSON`: Entidades `PER` do spaCy `pt_core_news_sm` + captura contextual pós-rótulos (`Paciente:`, `Nome:`, `Solicitante:`, `Dr.:`, `Dra.:`, `Médico(a):`) $\rightarrow$ `[REDACTED_PERSON]`
  5. **Contrato de Saída da Tool MCP:**
     ```json
     {
       "file_name": "carlos__good.png",
       "ocr_confidence": 0.94,
       "sanitized_text": "CLÍNICA VIDA\nPaciente: [REDACTED_PERSON]\nCPF: [REDACTED_CPF] RG: [REDACTED_RG] Tel: [REDACTED_PHONE]\nE-mail: [REDACTED_EMAIL]\nSolicito:\n1. Hemograma Completo\n2. Glicemia de Jejum\n3. TSH Ultra Sensível\n4. Vitamina D (25-OH)\nMédico: [REDACTED_PERSON] - [REDACTED_CRM]",
       "pii_redaction_report": {
         "total_redacted": 7,
         "entities_found": ["PERSON", "CPF", "RG", "PHONE_NUMBER", "EMAIL_ADDRESS", "CRM"]
       }
     }
     ```

### 5.2. Servidor MCP 2: Busca Híbrida Vetorial + Trigramas (`services/mcp_rag`)

* **Protocolo:** MCP sobre SSE (`GET /sse` e `POST /messages`).
* **Modelo de Embedding Local:** `FastEmbed` com `intfloat/multilingual-e5-small` ($d = 384$), registrado como custom model com export ONNX quantizado de `Xenova/multilingual-e5-small` (MEAN + L2). Seguindo a especificação da família E5, os documentos do catálogo são indexados com o prefixo `"passage: "` e as queries de busca com `"query: "`.
* **Fórmula de Ranking Híbrido no PostgreSQL:**
  Para cada termo extraído $q$, o servidor calcula o vetor $\mathbf{v}_q \in \mathbb{R}^{384}$ normalizado ($\|\mathbf{v}_q\|_2 = 1$) e executa uma consulta única no PostgreSQL combinando a similaridade de cosseno $S_{\text{vec}}$ (`pgvector`) e a similaridade máxima de trigramas $S_{\text{trgm}}$ (`pg_trgm`) sobre o nome de referência do catálogo fictício e sinônimos do exame:

  $$S_{\text{vec}}(q, d) = 1 - (\mathbf{v}_q \Leftrightarrow \mathbf{v}_d)$$

  $$S_{\text{trgm}}(q, d) = \max\big(\text{similarity}(q, d_{\text{name}}),\; \text{word\_similarity}(q, d_{\text{synonyms}})\big)$$

  $$S_{\text{hybrid}}(q, d) = \alpha \cdot S_{\text{vec}}(q, d) + (1 - \alpha) \cdot S_{\text{trgm}}(q, d), \quad \text{com } \alpha = 0.65$$

  Um exame é considerado reconciliado (*matched*) se $\max_d S_{\text{hybrid}}(q, d) \ge \tau$. O valor original $\tau = 0.45$ foi **calibrado para $\tau = 0.80$** após medição com o export ONNX quantizado do E5: termos legítimos (inclusive com ruído de OCR, ex. `Hemograna Completo`) pontuam $\ge 0{,}814$, enquanto termos sem sentido pontuam $\le 0{,}791$; com $\tau = 0{,}45$ ocorriam falsos positivos (ex. `Exame Inexistente XYZ` casando com 0{,}587). Recalibrar $\tau$ se o modelo de embeddings for trocado.
* **Assinatura da Tool MCP:** `search_exam_catalog_hybrid(exam_queries: list[str], top_k: int = 3)`
  * Recebe uma lista de nomes de exames em lote (evitando $N$ viagens de rede SSE separadas) e retorna para cada item os candidatos ordenados por $S_{\text{hybrid}}$. A seleção final separa explicitamente `matched_exams` de `unmatched_terms`; não atribuir código ao termo abaixo do limiar.

### 5.3. Modelagem do Banco de Dados PostgreSQL (`infra/init.sql`)

```sql
-- Extensões necessárias
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================================
-- SCHEMA 1: RAG (Catálogo sintético - mínimo 120 exames; tuss_code é identificador MOCK, não TUSS real)
-- ============================================================================
CREATE SCHEMA IF NOT EXISTS rag;

CREATE TABLE IF NOT EXISTS rag.exams_catalog (
    id SERIAL PRIMARY KEY,
    tuss_code VARCHAR(16) NOT NULL UNIQUE,
    official_name VARCHAR(255) NOT NULL,
    synonyms TEXT NOT NULL,
    category VARCHAR(100) NOT NULL,
    fasting_hours INT NOT NULL DEFAULT 0,
    preparation_instructions TEXT NOT NULL,
    turnaround_days INT NOT NULL DEFAULT 1,
    embedding vector(384) NOT NULL
);

-- Índice HNSW para busca vetorial de cosseno ultrarrápida
CREATE INDEX IF NOT EXISTS idx_exams_embedding_hnsw
    ON rag.exams_catalog USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Índices GIN de trigramas para tolerância a ruídos de OCR e siglas
CREATE INDEX IF NOT EXISTS idx_exams_name_trgm
    ON rag.exams_catalog USING gin (official_name gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_exams_synonyms_trgm
    ON rag.exams_catalog USING gin (synonyms gin_trgm_ops);

-- ============================================================================
-- SCHEMA 2: SCHEDULING (Persistência da API FastAPI de Agendamentos)
-- ============================================================================
CREATE SCHEMA IF NOT EXISTS scheduling;

CREATE TABLE IF NOT EXISTS scheduling.appointments (
    protocol_id VARCHAR(32) PRIMARY KEY,
    patient_reference_hash VARCHAR(64) NOT NULL DEFAULT 'ANONYMIZED_PATIENT',
    preferred_unit VARCHAR(120) NOT NULL DEFAULT 'Unidade Central - Matriz',
    status VARCHAR(32) NOT NULL DEFAULT 'CONFIRMED' CHECK (status IN ('CONFIRMED', 'PARTIAL')),
    unmatched_terms TEXT[] NOT NULL DEFAULT '{}',
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scheduling.appointment_items (
    id SERIAL PRIMARY KEY,
    protocol_id VARCHAR(32) NOT NULL REFERENCES scheduling.appointments(protocol_id) ON DELETE CASCADE,
    tuss_code VARCHAR(16) NOT NULL,
    official_name VARCHAR(255) NOT NULL,
    original_query TEXT NOT NULL,
    category VARCHAR(100) NOT NULL,
    confidence_score DOUBLE PRECISION NOT NULL,
    fasting_hours INT NOT NULL DEFAULT 0
);
```

### 5.4. API de Agendamento em FastAPI (`services/scheduling_api`)

Para que o `OpenAPIToolset` do Google ADK interprete perfeitamente a especificação `/openapi.json`, cada endpoint no FastAPI deve definir explicitamente `operation_id`, `summary`, `description` e schemas Pydantic documentados com `Field(..., description=..., examples=[...])`.

* **Endpoints Obrigatórios:**
  1. `GET /health` (`operation_id="health_check"`): Verifica conectividade com o schema `scheduling` do PostgreSQL.
  2. `POST /api/v1/appointments` (`operation_id="create_exam_appointment"`):
     * **Request Body (`AppointmentCreateRequest`):**
       ```json
       {
         "preferred_unit": "Unidade Central - Matriz",
         "exams": [
           {
             "original_query": "Hemograma Completo",
             "tuss_code": "MOCK-0001",
             "official_name": "Hemograma Completo (simulado)",
             "category": "Hematologia",
             "fasting_hours": 0,
             "confidence_score": 0.98
           }
         ],
         "unmatched_terms": [],
         "clinical_observations": "Pedido fictício processado com PII anonimizada"
       }
       ```
     * **Validação de Segurança no Endpoint:** Um validador Pydantic na FastAPI rejeita (`HTTP 422`) `exams` vazia e PII não mascarada em qualquer campo textual, inclusive `clinical_observations`, `exams` e `unmatched_terms`. Sem o modelo local de reconhecimento de nomes no container da API, `/health` retorna 503 e o POST falha fechado. A persistência é transacional: protocolo e itens são gravados juntos. POST com `unmatched_terms` não vazio retorna 422 antes da transação; novos registros são sempre `CONFIRMED`. GET mantém leitura de `PARTIAL` histórico. O runtime bloqueia a chamada se não houver itens encontrados ou houver qualquer termo não encontrado.
     * **Response Body (`HTTP 201 Created` - `AppointmentCreateResponse`):**
       ```json
       {
         "protocol_id": "LAB-2026-11-12-ABCDEFGHIJ",
         "status": "CONFIRMED",
         "scheduled_at": "2026-11-12T19:45:00Z",
         "preferred_unit": "Unidade Central - Matriz",
         "max_fasting_hours_required": 0,
         "preparation_summary": "Preparo simulado; confirme as orientações com a unidade.",
         "total_exams_scheduled": 1,
         "scheduled_exams": [
           {
             "original_query": "Hemograma Completo",
             "tuss_code": "MOCK-0001",
             "official_name": "Hemograma Completo (simulado)",
             "category": "Hematologia",
             "fasting_hours": 0,
             "confidence_score": 0.98
           }
         ],
         "unmatched_terms": []
       }
       ```
  3. `GET /api/v1/appointments/{protocol_id}` (`operation_id="get_exam_appointment"`): Retorna o mesmo contrato completo do POST, permitindo consultar o que foi e o que não foi registrado. `scheduled_at` é a hora do registro da solicitação, não uma reserva de horário de coleta.

---

## 6. Taxonomia de Erros, Resiliência e Experiência na CLI

A ferramenta de linha de comando (`runtime/cli.py`), construída com **Typer** e **Rich**, expõe três comandos principais (`transpile`, `preflight`, `run`) e implementa uma matriz determinística de códigos de saída (*Exit Codes*):

| Exit Code | Classe de Exceção | Fase | Causa Raiz | Ação / Feedback Visual na CLI (Rich) |
| :---: | :--- | :--- | :--- | :--- |
| `0` | N/A (Sucesso) | Todas | Transpilação ou registro integral concluído. | Exibe protocolo e exames registrados apenas com `CONFIRMED`; códigos `MOCK-*` são fictícios e não há horário de coleta reservado. |
| `10` | `SpecTranspilationError` ou `OSError` | Build-Time | JSON malformado, transporte MCP $\neq$ `"sse"`, referência cruzada inválida (`server_id`, `output_schema_ref`), falha no `ast.parse` ou impossibilidade de gravar o arquivo de saída. | Exibe o detalhe da validação (Pydantic/JSON do próprio usuário) ou do erro de gravação — dado autorado em build-time, sem conteúdo da imagem; mensagens de falhas de run-time permanecem estáticas por privacidade. |
| `20` | `InputImageError` | Run-Time (Pré-ADK) | Imagem inexistente em `/app/inputs`, extensão não suportada, arquivo vazio ou cabeçalho de imagem corrompido. | Exibe alerta indicando o caminho esperado no volume compartilhado e formatos aceitos. |
| `30` | `InfrastructureUnavailableError` | Run-Time (Preflight) | Falha ao conectar em `mcp-ocr:8001/health`, `mcp-rag:8002/health`, `scheduling-api:8000/health` ou `GEMINI_API_KEY` ausente. | Lista os serviços indisponíveis e sugere `docker compose up -d`. |
| `40` | `AgentPipelineExecutionError` ou erro de integração | Run-Time (ADK) | Imagem sem exames identificáveis, timeout SSE, zero exames reconciliados no RAG ou falha após o POST. | Retém a confirmação na CLI; mostra detalhe da etapa apenas quando a exceção estruturada o contém. **Exit 40 após o POST não desfaz o registro**: antes de repetir, consulte por protocolo ou verifique a API. |
| `50` | `SecurityGuardrailViolationError` | Run-Time (Guardrail) | Falha em garantir anonimização no OCR ou detecção de PII residual em uma das barreiras. | Não exibe conteúdo sensível e retém a confirmação. Se a falha ocorrer **após** o POST, não há rollback automático; verifique a API antes de repetir. |

---

## 7. KPIs, SLAs Técnicos e Critérios de Aceite

Para validar que a ferramenta atingiu o nível de maturidade esperado na revisão técnica, o projeto será medido contra os seguintes indicadores quantitativos:

1. **Segurança e Privacidade (PII Redaction Recall):**
   $$\text{Recall}_{\text{PII}} = \frac{\text{Entidades Sensíveis Mascaradas}}{\text{Total de Entidades Sensíveis Presentes na Imagem de Teste}} = 100\%$$
   *Nenhuma informação de Nome de Paciente, CPF, RG, Telefone, E-mail ou CRM pode chegar ao prompt do Gemini ou ao banco de dados `scheduling`. Se não for possível garantir isso, bloquear e orientar nova tentativa sem registrar solicitação.*
2. **Qualidade da Recuperação no Catálogo RAG (+100 Exames):**
   * **Volume do Catálogo:** $N_{\text{exames}} \ge 120$ exames laboratoriais distintos indexados no PostgreSQL; o dataset sintético desta entrega contém 150 registros.
   * **Top-1 Accuracy (Exames Impressos):** $\ge 95\%$ na reconciliação do nome extraído para o identificador correto do catálogo sintético.
   * **Top-3 Recall (Exames com Ruído de OCR / Manuscritos):** $\ge 90\%$ utilizando a combinação híbrida ($\alpha = 0.65$).
   * **Fluxo incompleto:** com pelo menos um item encontrado e outro não encontrado, ou com zero itens encontrados, nenhum POST é enviado; a CLI retorna erro e mantém `unmatched_terms` apenas como diagnóstico interno. A API também rejeita POST parcial direto (422, zero inserções).
3. **Fidelidade e Qualidade do Transpilador:**
   * **$100\%$ de Validação Sintática:** Todo código gerado deve passar em `ast.parse()` e `ruff check` com $0$ erros ou warnings.
   * **$100\%$ de Cobertura das Regras de Negócio da Spec:** Testes unitários cobrindo JSONs inválidos (transporte `stdio` rejeitado, `server_id` inexistente rejeitado, colisão de `output_key` rejeitada).
4. **Performance e Engenharia de Containers:**
   * **Tempo de Seed Inicial (+120 exames com `FastEmbed` em CPU):** $\le 8\text{ segundos}$ (executado de forma idempotente apenas para identificadores ausentes no catálogo).
   * **Latência da Busca Híbrida no `mcp-rag`:** $P_{95} \le 80\text{ ms}$ para lotes de até $10$ exames.
   * **Latência End-to-End do Fluxo Completo (OCR + 3 Subagentes ADK + FastAPI):** $\le 25\text{ segundos}$ (observado $\approx 22\text{--}23\text{ s}$ com 3 chamadas `gemini-2.5-flash`).
   * **Cobertura de Testes Automatizados (`pytest --cov`):** $\ge 85\%$ nos módulos core (`transpiler`, `security`, `mcp_ocr`, `mcp_rag`, `scheduling_api`).

---

## 8. Roteiro de Implementação: Do Setup à Validação Final

### 8.1. Estrutura Completa do Repositório

```text
desafio-carrefour-verity/              # Nome do diretório; pacote Python: clinical-adk-transpiler
├── .gitignore / .dockerignore           # Exclusões de Git e contexto Docker
├── docker-compose.yml                  # Orquestração completa (Postgres, 2 MCPs SSE, FastAPI, CLI)
├── Dockerfile.runtime                  # Imagem da CLI, Transpilador e Runtime Google ADK
├── Makefile                            # Atalhos de automação (setup, transpile, run, test, logs)
├── pyproject.toml                      # Dependências gerenciadas (uv / pip) e config do Ruff/Pytest
├── .env.example                        # Template (GEMINI_API_KEY, POSTGRES_DB/USER/PASSWORD)
├── README.md                           # Índice do projeto, estado atual e transparência de IA
├── docs/
│   ├── especificacao-tecnica-e-arquitetura.md # Este documento, fonte de verdade
│   ├── DESAFIO_SENIOR_IA.pdf           # Enunciado original do desafio
│   ├── transpile-exemple/              # Specs de exemplo (válidas e inválidas) + código gerado
│   └── evidences/                      # Evidências de funcionamento versionadas (logs, Swagger, CLI)
├── infra/
│   └── init.sql                        # Criação das extensões vector/pg_trgm e schemas rag/scheduling
├── specs/
│   └── clinical_agent_spec.json        # Manifesto JSON de exemplo do agente (Entregável 2)
├── assets/
│   ├── carlos__good.png                # Entrada principal, digitada e fictícia (Entregável 3)
│   ├── ana__good.png / juliana__good.png / marina__good.png / rafael__good.png   # Variantes legíveis
│   ├── carlos__shadow.jpg / marina__cropped.jpg   # Variantes de estresse (sombra, corte)
│   └── pedro__bad.jpg                  # Caso ilegível (bloqueio fail-closed)
├── security/
│   ├── __init__.py
│   ├── pii_guardrail.py                # Motor Presidio + Regex BR (CPF, RG, CRM, Nome, Tel)
│   └── adk_callbacks.py                # before/after-tool e before-model com evidências ADK
├── transpiler/
│   ├── __init__.py
│   ├── schema.py                       # Metamodelo Pydantic v2 da DSL JSON e validadores cruzados
│   ├── compiler.py                     # Renderizador Jinja2 + validação ast.parse + formatação Ruff
│   └── templates/
│       └── adk_sequential_agent.py.j2  # Template Python Google ADK (SequentialAgent, MCPToolset, OpenAPI)
├── services/
│   ├── __init__.py
│   ├── mcp_ocr/
│   │   ├── __init__.py
│   │   ├── Dockerfile                  # RapidOCR, OpenCV headless, Presidio e pt_core_news_sm no build
│   │   ├── ocr_engine.py               # Pipeline OpenCV + RapidOCR
│   │   └── server.py                   # Servidor FastMCP via SSE (:8001/sse) + endpoint /health
│   ├── mcp_rag/
│   │   ├── __init__.py
│   │   ├── Dockerfile                  # FastEmbed + E5-small ONNX quantizado no build
│   │   ├── data/
│   │   │   └── tuss_exams_catalog.json # Catálogo com 150 exames e códigos MOCK-*, preparo fictício
│   │   ├── seeder.py                   # Seed idempotente no Postgres com vetores de 384 dimensões
│   │   ├── hybrid_search.py            # Query SQL combinando pgvector (<=>) e pg_trgm (similarity)
│   │   └── server.py                   # Servidor FastMCP via SSE (:8002/sse) + endpoint /health
│   └── scheduling_api/
│       ├── __init__.py
│       ├── Dockerfile                  # FastAPI + Presidio + pt_core_news_sm no build
│       ├── database.py                 # Conexão síncrona PostgreSQL com schema `scheduling`
│       ├── schemas.py                  # Contratos Pydantic (Request/Response) refletidos no /docs
│       └── main.py                     # Aplicação FastAPI (:8000) com Swagger e validação anti-PII
├── runtime/
│   ├── __init__.py
│   ├── cli.py                          # CLI em Typer + Rich (transpile, preflight, run)
│   └── generated/                      # Diretório onde o código Python transpilado é gravado
│       ├── __init__.py
│       └── clinical_exam_scheduler_agent.py # Artefato transpilado, versionado e testado contra drift
└── tests/
    ├── unit/
    │   ├── test_transpiler_schema.py   # Validação da DSL
    │   ├── test_transpiler_ast.py      # Geração Python com ADK mockado
    │   ├── test_generated_drift.py     # Compara artefato gerado à compilação da spec
    │   ├── test_transpiler_runtime.py  # CLI, caminhos e estados com mocks
    │   ├── test_pii_guardrail.py       # Privacidade com mocks
    │   ├── test_adk_callbacks.py       # Proveniência das tools e bloqueio pré-POST
    │   ├── test_ocr_engine.py          # OCR com motor simulado
    │   ├── test_ocr_server.py          # MCP OCR simulado
    │   ├── test_rag_hybrid_search.py   # Busca vetorial e seed com mocks
    │   └── test_rag_server.py          # MCP RAG simulado
    ├── integration/
    │   └── test_scheduling_api.py      # FastAPI POST integral, GET histórico e anti-PII com banco mockado
    └── e2e/
        └── test_full_clinical_flow.py  # Fluxo E2E simulado, sem rede/modelos
```

### 8.2. Imagens de Teste Fictícias (`assets/`)
Para assegurar reprodutibilidade usando apenas dados fictícios e texto digitado, a demonstração usa um conjunto de imagens geradas programaticamente (sem manuscrito):
* **Entrada principal (`assets/carlos__good.png`, Entregável 3):** cabeçalho com PII fictícia alvo do guardrail (identidade, CPF, RG, telefone e e-mail inventados) e corpo digitado `Solicito:` com exames enumerados: Hemograma Completo, Glicemia de Jejum, Hormônio Tireoestimulante (TSH), Colesterol Total e Frações (Lipidograma) e Vitamina D (25-Hidroxi); rodapé com assinatura médica e aviso fictício, ambos digitados.
* **Variantes:** legíveis (`ana__good.png`, `juliana__good.png`, `marina__good.png`, `rafael__good.png`), estresse de captura (`carlos__shadow.jpg` com sombra, `marina__cropped.jpg` cortada) e caso ilegível (`pedro__bad.jpg`), que exercem o bloqueio fail-closed da CLI.

### 8.3. Procedimento Passo a Passo de Execução e Validação (Checklist de Entrega)

Os modelos locais são provisionados nos builds Docker. Os testes automatizados continuam mock-only (sem chamadas a OCR, embeddings, MCP, banco, HTTP ou LLM reais); smokes e execuções com Gemini anteriores demonstraram a arquitetura com imagens **sintéticas**, e o contrato de aceitação integral foi **revalidado em execução real com Gemini em 15/11/2026** — pedidos completos registrados como `CONFIRMED` ([`docs/evidences/06-run-cli-valida.log`](evidences/06-run-cli-valida.log) e captura de tela da CLI). A chave fica exclusivamente em `.env`. Veja as evidências em [`docs/evidences/`](evidences/) e as limitações no README.

#### Etapa 1: Configuração Inicial e Build da Infraestrutura (equivalente: `make setup`)
```bash
# 1. Copiar arquivo de variáveis de ambiente e preencher POSTGRES_PASSWORD e GEMINI_API_KEY
cp .env.example .env

# 2. Subir todos os serviços conteinerizados (Postgres, MCP OCR, MCP RAG e FastAPI)
docker compose up -d --build postgres-db mcp-ocr mcp-rag scheduling-api

# 3. Verificar se todos os containers estão com status "healthy"
docker compose ps
```

#### Etapa 2: Validação do Seed e dos Contratos de Infraestrutura (equivalente: `make preflight`)
```bash
# Executa o diagnóstico de infraestrutura pela CLI
docker compose run --rm agent-cli python -m runtime.cli preflight
```
* **Resultado esperado:**
  * `Verificação dos serviços concluída.` e exit 0 quando a chave está presente e os três serviços respondem `/health` (o healthcheck do RAG exige pelo menos 125 exames). A contagem de 150 pode ser conferida no Postgres (`SELECT count(*) FROM rag.exams_catalog;`) — o seed é idempotente e roda no boot do container.

#### Etapa 3: Execução do Transpilador ($\text{JSON} \rightarrow \text{Python ADK}$, equivalente: `make transpile`)
```bash
# Transpila o manifesto JSON para código Python válido do Google ADK
docker compose run --rm --no-deps agent-cli python -m runtime.cli transpile \
  --spec /app/specs/clinical_agent_spec.json \
  --output /app/runtime/generated/clinical_exam_scheduler_agent.py
```
* **Validação:** Inspecionar o arquivo gerado em `runtime/generated/clinical_exam_scheduler_agent.py` e confirmar a presença dos modelos Pydantic gerados dinamicamente, dos callbacks de guardrail, do `MCPToolset` com `SseConnectionParams` e `tool_filter`, do `OpenAPIToolset` e do `SequentialAgent`.

#### Etapa 4: Execução do Agente Transpilado no Fluxo End-to-End (equivalente: `make run`)
```bash
# Executa o agente gerado processando a imagem de pedido médico fictício
docker compose run --rm agent-cli python -m runtime.cli run \
  --agent-module runtime.generated.clinical_exam_scheduler_agent \
  --image /app/inputs/carlos__good.png
```
* **Saída Visual Esperada no Terminal (Rich):** o `run` mostra feedback em português durante a validação de imagem/serviços, extração e proteção de dados, busca RAG, registro e conferência do resultado. Usa indicador animado em TTY e não mostra texto extraído, argumentos nem respostas de ferramentas. Só informa conclusão do registro após validar o estado final. A saída atual está registrada em [`docs/evidences/06-run-cli-valida.log`](evidences/06-run-cli-valida.log) e na captura de tela [`06-cli-screenshot.png`](evidences/06-cli-screenshot.png).
  1. **Smoke OCR separado:** texto sanitizado com `[REDACTED_*]` e nomes de exames preservados; a CLI não imprime esse texto.
  2. **Smoke RAG separado:** `MOCK-*`, scores e termos sem match; a CLI imprime somente nomes/códigos finais, não os scores.
  3. **CLI e GET:** protocolo (`LAB-AAAA-MM-DD-LETRAS`), `CONFIRMED`, exames registrados e preparo fictício; sem reserva de horário. A validação atual está em [`docs/evidences/06-run-cli-valida.log`](evidences/06-run-cli-valida.log) e [`06-protocolo-get.json`](evidences/06-protocolo-get.json).

#### Etapa 5: Execução da Suíte Completa de Testes Automatizados (equivalente: `make test`)
```bash
# Executa testes unitários, de segurança PII, de busca híbrida e de integração FastAPI
docker compose run --rm --no-deps agent-cli python -m pytest -q --cov=transpiler --cov=runtime --cov=security --cov=services
```

---

## 9. Diretrizes para a Seção de Transparência e Uso de IA (README.md)

Em conformidade com a seção **"Transparência e Uso de IA"** do edital, o `README.md` principal do repositório deve incluir um capítulo dedicado documentando:

1. **Abordagem no Desenvolvimento com IA:**
   * Uso de LLMs como par de arquitetura (*architectural sparring partner*) para refinar contratos JSON, desenhar a query de busca híbrida (`pgvector` + `pg_trgm`) e gerar a base sintética de $+120$ exames laboratoriais com identificadores `MOCK-*` (sem validade TUSS oficial).
   * Validação humana rigorosa e determinística: todo código gerado passa por validação de árvore sintática (`ast.parse`), linter estático (`Ruff`), tipagem (`Pydantic v2`) e testes automatizados (`Pytest`) antes de ser integrado.
2. **Principais Referências Técnicas Consultadas:**
   * Documentação Oficial do **Google Agent Development Kit (`google-adk`)**: `SequentialAgent`, `LlmAgent`, gerenciamento de estado via `session.state` / `output_key`, `MCPToolset` (`SseConnectionParams` e `tool_filter`), `OpenAPIToolset` e `before_model_callback`.
   * Especificação Oficial do **Model Context Protocol (MCP)**: Transporte *Server-Sent Events* (SSE) com `FastMCP` e Starlette.
   * Documentação do **PostgreSQL `pgvector` e `pg_trgm`**: Indexação HNSW para vetores densos e índices GIN de trigramas para tolerância a ruídos de OCR.
   * Documentação do **Microsoft Presidio** e **FastEmbed (ONNX Runtime)**.
3. **Justificativa da Estratégia de Orquestração:**
   * Explicação do porquê um pipeline `SequentialAgent` com subagentes de escopo restrito (`tool_filter`) supera um agente monolítico (`ReAct` único) em cenários clínicos críticos: menor superfície de ataque de prompt injection, isolamento de credenciais/ferramentas por etapa, previsibilidade de chamadas de API e auditoria determinística do estado intermediário entre OCR, RAG e Agendamento.