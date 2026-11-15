"""Generated clinical pipeline. Catalog identifiers and preparation are fictional."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import httpx

from pydantic import BaseModel, Field, ValidationError
from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, SseConnectionParams
from google.adk.tools.openapi_tool.openapi_spec_parser.openapi_toolset import OpenAPIToolset
from security.adk_callbacks import (
    after_tool_callback,
    before_model_callback,
    before_tool_callback,
    inspect_for_pii,
    require_complete_order,
)


class AgentPipelineExecutionError(Exception):
    """An agent failed to produce a valid state or no exams were resolved."""


class DocumentoColetado(BaseModel):
    titulo: str = Field(description="Título do documento recuperado")

    trecho_relevante: str = Field(description="Trecho mais relevante do conteúdo")


class TextoNormalizado(BaseModel):
    texto_limpo: str = Field(description="Documento reescrito em linguagem objetiva")

    palavras_chave: list[str] = Field(description="Até cinco palavras-chave do texto")


class MetricasTexto(BaseModel):
    sentimento: str = Field(description="positivo, neutro ou negativo")

    complexidade: str = Field(description="simples, media ou alta")


class RegistroAuditoria(BaseModel):
    registro_id: str = Field(description="Identificador do registro criado pela API")

    status: str = Field(description="Status retornado pela API")


DocumentoColetado.model_rebuild()

TextoNormalizado.model_rebuild()

MetricasTexto.model_rebuild()

RegistroAuditoria.model_rebuild()


OUTPUT_SCHEMAS = {
    "documento": DocumentoColetado,
    "texto_normalizado": TextoNormalizado,
    "metricas": MetricasTexto,
    "registro": RegistroAuditoria,
}


INPUT_SCHEMAS = {
    "normalizador": DocumentoColetado,
    "enriquecedor": TextoNormalizado,
    "registrador": MetricasTexto,
}

INPUT_KEYS = {
    "normalizador": "documento",
    "enriquecedor": "texto_normalizado",
    "registrador": "metricas",
}


def _validate_output(key):
    def callback(callback_context):

        value = callback_context.state.get(key)
        try:
            schema = OUTPUT_SCHEMAS[key]
            if isinstance(value, str):
                # LLMs routinely wrap JSON in markdown fences; strip that noise only.
                value = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", value.strip())
                value = json.loads(value)
            if (
                isinstance(value, dict)
                and len(value) == 1
                and next(iter(value)).endswith("_response")
                and isinstance(value[next(iter(value))], dict)
            ):
                # OpenAPIToolset advertises the payload under an "<operation>_response" wrapper.
                value = value[next(iter(value))]
            result = (
                schema.model_validate_json(value)
                if isinstance(value, str)
                else schema.model_validate(value)
            )
        except (ValidationError, ValueError, TypeError):
            raise AgentPipelineExecutionError(
                f"Invalid output at {key}; no registration confirmed"
            ) from None
        output = result.model_dump(mode="json")

        callback_context.state[key] = output
        return None

    return callback


def _validate_input(agent_name):
    """Typed input contract: validate the upstream payload before the LLM runs."""

    def callback(callback_context):
        raw = callback_context.state.get(INPUT_KEYS[agent_name])
        try:
            schema = INPUT_SCHEMAS[agent_name]
            if isinstance(raw, str):
                # Same markdown-fence stripping as _validate_output for LLM-wrapped JSON.
                raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw.strip())
                result = schema.model_validate_json(raw)
            elif isinstance(raw, dict):
                result = schema.model_validate(raw)
            else:
                raise TypeError("missing upstream input")
        except (ValidationError, ValueError, TypeError):
            raise AgentPipelineExecutionError(
                f"Invalid input for {agent_name}; no registration confirmed"
            ) from None
        inspect_for_pii(result)
        normalized = result.model_dump(mode="json")
        if "exams" in normalized and not normalized["exams"]:
            raise AgentPipelineExecutionError(
                f"Invalid input for {agent_name}; no registration confirmed"
            )
        if "matched_exams" in normalized:
            require_complete_order(callback_context.state.get("documento"), normalized)
            verified = callback_context.state.get("_rag_verified")
            if verified is None or normalized != verified:
                raise AgentPipelineExecutionError(
                    f"Invalid input for {agent_name}; differs from verified RAG evidence"
                )
        callback_context.state[INPUT_KEYS[agent_name]] = normalized
        callback_context.state[f"temp:{agent_name}_input"] = normalized
        return None

    return callback


def build_pipeline():
    """Create toolsets at runtime, after preflight (never during import/transpilation)."""
    server_params = {
        "busca_docs": SseConnectionParams(url="http://mcp-busca:9001/sse", timeout=30),
        "metricas_texto": SseConnectionParams(url="http://mcp-metricas:9002/sse", timeout=30),
    }
    service_specs = {}

    response = httpx.get("http://auditoria-api:9100/openapi.json", timeout=10)
    response.raise_for_status()
    endpoint = urlsplit("http://auditoria-api:9100/openapi.json")
    service_spec = response.json()
    # FastAPI serves a relative "servers" URL; the toolset needs an absolute origin.
    service_spec.setdefault("servers", [{"url": f"{endpoint.scheme}://{endpoint.netloc}"}])
    service_specs["auditoria_api"] = service_spec

    agents = []

    agents.append(
        LlmAgent(
            name="coletor",
            model="gemini-2.5-flash",
            instruction="Você recupera o documento fictício mais relevante para o tema informado pelo usuário. Invoque a ferramenta 'buscar_documento' com o tema recebido. Responda SOMENTE com JSON válido, sem cercas de código, seguindo o schema DocumentoColetado com os campos retornados pela ferramenta.",
            output_key="documento",
            output_schema=DocumentoColetado,
            include_contents="default",
            tools=[
                MCPToolset(
                    connection_params=server_params["busca_docs"], tool_filter=["buscar_documento"]
                ),
            ],
            before_model_callback=before_model_callback,
            before_tool_callback=before_tool_callback,
            after_tool_callback=after_tool_callback,
            after_agent_callback=_validate_output("documento"),
        )
    )

    agents.append(
        LlmAgent(
            name="normalizador",
            model="gemini-2.5-flash",
            instruction="O documento coletado chega como JSON estruturado na mensagem do usuário desta etapa (schema DocumentoColetado); não há interpolação no prompt do sistema. Reescreva o conteúdo em linguagem objetiva. Responda SOMENTE com JSON válido, sem cercas de código, seguindo o schema TextoNormalizado.",
            output_key="texto_normalizado",
            output_schema=TextoNormalizado,
            include_contents="none",
            tools=[],
            before_model_callback=before_model_callback,
            before_tool_callback=before_tool_callback,
            after_tool_callback=after_tool_callback,
            after_agent_callback=_validate_output("texto_normalizado"),
            before_agent_callback=_validate_input("normalizador"),
        )
    )

    agents.append(
        LlmAgent(
            name="enriquecedor",
            model="gemini-2.5-flash",
            instruction="O texto normalizado chega como JSON estruturado na mensagem do usuário desta etapa (schema TextoNormalizado); não há interpolação no prompt do sistema. Invoque a ferramenta 'analisar_texto' com o texto_limpo recebido. Responda SOMENTE com JSON válido, sem cercas de código, seguindo o schema MetricasTexto com os campos retornados pela ferramenta.",
            output_key="metricas",
            output_schema=MetricasTexto,
            include_contents="none",
            tools=[
                MCPToolset(
                    connection_params=server_params["metricas_texto"],
                    tool_filter=["analisar_texto"],
                ),
            ],
            before_model_callback=before_model_callback,
            before_tool_callback=before_tool_callback,
            after_tool_callback=after_tool_callback,
            after_agent_callback=_validate_output("metricas"),
            before_agent_callback=_validate_input("enriquecedor"),
        )
    )

    agents.append(
        LlmAgent(
            name="registrador",
            model="gemini-2.5-flash",
            instruction="As métricas do texto chegam como JSON estruturado na mensagem do usuário desta etapa (schema MetricasTexto); não há interpolação no prompt do sistema. SEMPRE invoque a operação 'criar_registro' antes de responder; não simule resposta. Responda SOMENTE com JSON válido, sem cercas de código, reproduzindo a resposta da API conforme o schema RegistroAuditoria.",
            output_key="registro",
            output_schema=RegistroAuditoria,
            include_contents="none",
            tools=[
                OpenAPIToolset(
                    spec_dict=service_specs["auditoria_api"],
                    tool_filter=["criar_registro"],
                    allow_private_network_access=True,
                ),
            ],
            before_model_callback=before_model_callback,
            before_tool_callback=before_tool_callback,
            after_tool_callback=after_tool_callback,
            after_agent_callback=_validate_output("registro"),
            before_agent_callback=_validate_input("registrador"),
        )
    )

    return SequentialAgent(name="pipeline_auditoria_documental", sub_agents=agents)
