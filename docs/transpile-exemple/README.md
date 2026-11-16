# Exemplos de especificações para o transpilador

Casos de teste manuais para `make transpile-local`: especificações válidas (que geram código ADK) e inválidas (que devem falhar com **exit 10** e mensagem específica). Execução:

```bash
# válido (substitua SPEC/OUTPUT conforme o arquivo)
make transpile-local SPEC=docs/transpile-exemple/valido_01_agente_simples.json OUTPUT=docs/transpile-exemple/gerados/valido_01_agente_simples.py

# inválido (deve falhar; nenhum arquivo é gravado)
make transpile-local SPEC=docs/transpile-exemple/invalido_04_pii_desativado.json OUTPUT=docs/transpile-exemple/gerados/NAO_DEVE_EXISTIR.py
```

Observações: os caminhos válidos usam `OUTPUT` próprio em `gerados/` para não sobrescrever o artefato clínico versionado em `runtime/generated/` (protegido pelo drift test). Ao rodar via `make`, o código de saída real da CLI é **10** (exibido pelo Make como `Erro 10`, enquanto o processo `make` retorna status `2` ao shell). Todos os dados são fictícios.

## Especificações válidas

| Arquivo | Cenário | Verificado |
|---|---|---|
| `valido_01_agente_simples.json` | Agente único, sem ferramentas, pipeline mínimo | 1 `LlmAgent`, `tools=[]` |
| `valido_02_cadeia_linear.json` | Cadeia linear de 2 agentes com contrato tipado (`input_key`/`input_schema_ref`) e schema aninhado `list[ChamadoItem]` | chaining + tipos compostos |
| `valido_03_orquestrador_misto.json` | Orquestrador sequential de 4 agentes: 2 servidores MCP, agente de passagem sem tools no meio, registro via OpenAPI | 4 `LlmAgent`, 3 toolsets |
| `valido_04_agente_multi_ferramentas.json` | Agente único com 2 bindings MCP + 1 binding OpenAPI simultâneos | 1 `LlmAgent`, 3 toolsets |

## Especificações inválidas (uma violação por arquivo)

| Arquivo | Violação | Mensagem esperada (trecho) |
|---|---|---|
| `invalido_01_json_malformado.json` | Sintaxe JSON quebrada | `Invalid JSON: EOF while parsing an object` |
| `invalido_02_campo_ausente.json` | `output_key` obrigatório removido do agente | `orchestration.agents.0.output_key` (field required) |
| `invalido_03_transport_nao_sse.json` | `transport: "stdio"` em servidor MCP | `MCP requires transport 'sse' and an http(s) URL` |
| `invalido_04_pii_desativado.json` | `enable_pii_redaction: false` | `guardrails.enable_pii_redaction must be true` |
| `invalido_05_input_key_sem_origem.json` | `input_key` sem agente anterior correspondente | `input_key: must match the output_key of an earlier agent` |
| `invalido_06_nome_reservado.json` | Schema com nome reservado (`LlmAgent`) | `schemas.LlmAgent: invalid Python class name` |
| `invalido_07_tipo_nao_suportado.json` | Campo com tipo fora da whitelist (`dict[str, int]`) | `fields.tabela.type: unsupported type` |
| `invalido_08_schema_ref_inexistente.json` | `output_schema_ref` aponta para schema inexistente | `output_schema_ref: unknown schema SaidaFantasma` |
| `invalido_09_id_duplicado.json` | Dois servidores MCP com o mesmo `id` | `mcp_servers and openapi_services IDs must be unique` |
| `invalido_10_placeholder_instrucao.json` | Placeholder `{saida_um}` interpolado na instruction de agente com input tipado | `instruction: must not interpolate state placeholders` |
| `invalido_11_ferramenta_vazia.json` | `allowed_tools` com string em branco | `mcp_bindings.allowed_tools: empty tool identifier` |
| `invalido_12_identificador_invalido.json` | `orchestration.name` com hífen (não é identificador Python) | `orchestration.name must be a Python identifier` |
