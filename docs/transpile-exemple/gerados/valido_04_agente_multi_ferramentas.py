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


class ConsultaOutput(BaseModel):
    resposta: str = Field(description="Resposta direta à consulta do usuário")

    fontes: list[str] = Field(description="Ferramentas utilizadas para compor a resposta")

    registro_id: str = Field(description="Identificador do registro criado na API, se aplicável")


ConsultaOutput.model_rebuild()


OUTPUT_SCHEMAS = {
    "consulta_final": ConsultaOutput,
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
            require_complete_order(callback_context.state.get("consulta_final"), normalized)
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
        "kb_docs": SseConnectionParams(url="http://mcp-kb:9201/sse", timeout=30),
        "clima": SseConnectionParams(url="http://mcp-clima:9202/sse", timeout=30),
    }
    service_specs = {}

    response = httpx.get("http://registro-api:9300/openapi.json", timeout=10)
    response.raise_for_status()
    endpoint = urlsplit("http://registro-api:9300/openapi.json")
    service_spec = response.json()
    # FastAPI serves a relative "servers" URL; the toolset needs an absolute origin.
    service_spec.setdefault("servers", [{"url": f"{endpoint.scheme}://{endpoint.netloc}"}])
    service_specs["registro_api"] = service_spec

    agents = []

    agents.append(
        LlmAgent(
            name="assistente",
            model="gemini-2.5-flash",
            instruction="Você responde consultas fictícias combinando três fontes: a base de conhecimento (ferramenta 'consultar_kb'), o clima (ferramenta 'consultar_clima') e o registro da interação (operação 'salvar_consulta'). Invoque as ferramentas de conhecimento e clima conforme a pergunta e sempre registre a resposta na API antes de finalizar. Responda SOMENTE com JSON válido, sem cercas de código, seguindo o schema ConsultaOutput: resposta com o conteúdo, fontes com os nomes das ferramentas usadas e registro_id com o identificador retornado pela API.",
            output_key="consulta_final",
            output_schema=ConsultaOutput,
            include_contents="default",
            tools=[
                MCPToolset(
                    connection_params=server_params["kb_docs"], tool_filter=["consultar_kb"]
                ),
                MCPToolset(
                    connection_params=server_params["clima"], tool_filter=["consultar_clima"]
                ),
                OpenAPIToolset(
                    spec_dict=service_specs["registro_api"],
                    tool_filter=["salvar_consulta"],
                    allow_private_network_access=True,
                ),
            ],
            before_model_callback=before_model_callback,
            before_tool_callback=before_tool_callback,
            after_tool_callback=after_tool_callback,
            after_agent_callback=_validate_output("consulta_final"),
        )
    )

    return SequentialAgent(name="pipeline_consulta_unica", sub_agents=agents)
