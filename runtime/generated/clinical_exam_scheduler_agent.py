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


class ExtractedExamRaw(BaseModel):
    raw_name: str = Field(description="Nome do exame como consta no pedido médico")

    is_handwritten_hint: bool = Field(
        description="Indica se parece vir de nota/observação adicional"
    )


class ExtractedExamsOutput(BaseModel):
    sanitized_text: str = Field(description="Texto completo após anonimização de dados sensíveis")

    pii_entities_masked: int = Field(description="Quantidade de entidades sensíveis mascaradas")

    exams: list[ExtractedExamRaw] = Field(description="Lista de exames extraídos da imagem")


class ResolvedExamItem(BaseModel):
    original_query: str = Field(description="Termo original extraído no OCR")

    tuss_code: str = Field(
        description="Identificador fictício do catálogo (ex: MOCK-0001); campo legado, não é código TUSS oficial"
    )

    official_name: str = Field(description="Nome padronizado do exame no catálogo")

    category: str = Field(description="Categoria laboratorial (ex: Hematologia, Bioquímica)")

    fasting_hours: int = Field(description="Horas de jejum necessárias")

    confidence_score: float = Field(description="Score híbrido de similaridade entre 0.0 e 1.0")


class ResolvedExamsListOutput(BaseModel):
    matched_exams: list[ResolvedExamItem] = Field(
        description="Exames encontrados na base com seus códigos"
    )

    unmatched_terms: list[str] = Field(description="Termos que não atingiram o score mínimo")


class BookingConfirmationOutput(BaseModel):
    protocol_id: str = Field(description="Número de protocolo único gerado pela API")

    status: str = Field(description="CONFIRMED para novos registros")

    scheduled_at: str = Field(
        description="Data/hora ISO-8601 do registro da solicitação; não é horário reservado para coleta"
    )

    preferred_unit: str = Field(description="Unidade indicada na solicitação")

    max_fasting_hours_required: int = Field(
        description="Maior duração fictícia de jejum entre os exames registrados"
    )

    preparation_summary: str = Field(
        description="Resumo fictício de preparo/jejum; não substitui orientação clínica"
    )

    total_exams_scheduled: int = Field(description="Quantidade de exames registrados")

    scheduled_exams: list[ResolvedExamItem] = Field(
        description="Exames efetivamente registrados, com todos os metadados da reconciliação"
    )

    unmatched_terms: list[str] = Field(
        description="Exames solicitados não encontrados e não registrados"
    )


ExtractedExamRaw.model_rebuild()

ExtractedExamsOutput.model_rebuild()

ResolvedExamItem.model_rebuild()

ResolvedExamsListOutput.model_rebuild()

BookingConfirmationOutput.model_rebuild()


OUTPUT_SCHEMAS = {
    "extracted_exams": ExtractedExamsOutput,
    "resolved_exams": ResolvedExamsListOutput,
    "booking_result": BookingConfirmationOutput,
}


INPUT_SCHEMAS = {
    "rag_lookup_agent": ExtractedExamsOutput,
    "scheduling_agent": ResolvedExamsListOutput,
}

INPUT_KEYS = {
    "rag_lookup_agent": "extracted_exams",
    "scheduling_agent": "resolved_exams",
}


def _validate_output(key):
    def callback(callback_context):

        if key == "booking_result":
            # A verified POST is the registration evidence itself; the final LLM text is
            # never parsed, so transcription drift cannot fail an already committed POST.
            verified = callback_context.state.get("_booking_verified")
            if not isinstance(verified, dict):
                raise AgentPipelineExecutionError("Booking output lacks verified API response")
            try:
                result = OUTPUT_SCHEMAS[key].model_validate(verified)
            except (ValidationError, ValueError, TypeError):
                raise AgentPipelineExecutionError(
                    "Booking output lacks verified API response"
                ) from None
            # Byte-identical dump of the raw evidence keeps the CLI confirmation equality intact.
            callback_context.state[key] = result.model_dump(mode="json")
            return None

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

        if key == "extracted_exams":
            evidence = callback_context.state.get("_ocr_verified")
            if (
                not evidence
                or not output.get("exams")
                or output.get("sanitized_text") != evidence["sanitized_text"]
                or output.get("pii_entities_masked") != evidence["pii_entities_masked"]
            ):
                raise AgentPipelineExecutionError("OCR output lacks verified tool evidence")
            numbered = re.findall(r"(?m)^\s*\d+[.)]\s*([^\n]+?)\s*$", evidence["sanitized_text"])
            if (
                not numbered
                or [name.strip().casefold() for name in numbered]
                != [item["raw_name"].strip().casefold() for item in output["exams"]]
                or any(item["is_handwritten_hint"] for item in output["exams"])
                or re.search(r"(?m)^\s*\+", evidence["sanitized_text"])
            ):
                raise AgentPipelineExecutionError("OCR exam lines differ from extracted exams")

        if key == "resolved_exams":
            if output != callback_context.state.get("_rag_verified"):
                raise AgentPipelineExecutionError("RAG output lacks verified tool evidence")
            require_complete_order(callback_context.state.get("extracted_exams"), output)

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
            require_complete_order(callback_context.state.get("extracted_exams"), normalized)
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
        "mcp_ocr": SseConnectionParams(url="http://mcp-ocr:8001/sse", timeout=30),
        "mcp_rag": SseConnectionParams(url="http://mcp-rag:8002/sse", timeout=30),
    }
    service_specs = {}

    response = httpx.get("http://scheduling-api:8000/openapi.json", timeout=10)
    response.raise_for_status()
    endpoint = urlsplit("http://scheduling-api:8000/openapi.json")
    service_spec = response.json()
    # FastAPI serves a relative "servers" URL; the toolset needs an absolute origin.
    service_spec.setdefault("servers", [{"url": f"{endpoint.scheme}://{endpoint.netloc}"}])
    service_specs["scheduling_fastapi"] = service_spec

    agents = []

    agents.append(
        LlmAgent(
            name="ocr_intake_agent",
            model="gemini-2.5-flash",
            instruction="Você é um agente especialista em triagem de pedidos médicos. Invoque a ferramenta 'extract_and_anonymize_medical_order' passando o file_path recebido do usuário. A partir do texto sanitizado retornado, identifique todos os exames laboratoriais solicitados do pedido digitado; não processe manuscritos ou adendos. Responda SOMENTE com um objeto JSON válido, sem cercas de código markdown, seguindo exatamente o schema ExtractedExamsOutput: sanitized_text copiado EXATAMENTE como retornado pela ferramenta (sem resumir, sem alterar, sem omitir); pii_entities_masked é o número inteiro do campo total_redacted dentro de pii_redaction_report (apenas o inteiro, nunca o objeto); exams como lista de objetos com os campos raw_name (nome do exame como consta no texto) e is_handwritten_hint (false para todos os itens do pedido digitado).",
            output_key="extracted_exams",
            output_schema=ExtractedExamsOutput,
            include_contents="default",
            tools=[
                MCPToolset(
                    connection_params=server_params["mcp_ocr"],
                    tool_filter=["extract_and_anonymize_medical_order"],
                ),
            ],
            before_model_callback=before_model_callback,
            before_tool_callback=before_tool_callback,
            after_tool_callback=after_tool_callback,
            after_agent_callback=_validate_output("extracted_exams"),
        )
    )

    agents.append(
        LlmAgent(
            name="rag_lookup_agent",
            model="gemini-2.5-flash",
            instruction="Os exames extraídos chegam como JSON estruturado na mensagem do usuário desta etapa (schema ExtractedExamsOutput); não há interpolação no prompt do sistema. Invoque a ferramenta 'search_exam_catalog_hybrid' enviando a lista completa de nomes de exames (na mesma ordem e com os mesmos raw_name recebidos) para recuperar identificadores fictícios, instruções simuladas de jejum e score de confiança. Nunca apresente identificadores como códigos oficiais TUSS/CBHPM. Responda SOMENTE com JSON válido, sem cercas de código: matched_exams e unmatched_terms EXATAMENTE como retornados pela ferramenta, com todos os campos de cada item (original_query, tuss_code, official_name, category, fasting_hours, confidence_score) preservados sem arredondar nem resumir. Se algum termo ficar sem correspondência, o pedido inteiro será recusado sem registro.",
            output_key="resolved_exams",
            output_schema=ResolvedExamsListOutput,
            include_contents="none",
            tools=[
                MCPToolset(
                    connection_params=server_params["mcp_rag"],
                    tool_filter=["search_exam_catalog_hybrid"],
                ),
            ],
            before_model_callback=before_model_callback,
            before_tool_callback=before_tool_callback,
            after_tool_callback=after_tool_callback,
            after_agent_callback=_validate_output("resolved_exams"),
            before_agent_callback=_validate_input("rag_lookup_agent"),
        )
    )

    agents.append(
        LlmAgent(
            name="scheduling_agent",
            model="gemini-2.5-flash",
            instruction="Você coordena o registro fictício apenas de pedidos digitados completos. Receba o JSON estruturado ResolvedExamsListOutput. Se houver qualquer unmatched_terms ou se a lista matched_exams estiver vazia, não invoque a API. Caso contrário invoque create_exam_appointment com todos os matched_exams exatamente como recebidos e unmatched_terms vazio. SEMPRE invoque a ferramenta antes de responder; não simule resposta. Responda SOMENTE com JSON válido, sem cercas: reproduza a resposta completa da API EXATAMENTE como retornada, sem inventar exames, códigos nem horários. CONFIRMED é registro, não reserva de horário.",
            output_key="booking_result",
            # No output_schema kwarg: the ADK validates the final LLM text against it mid-stream and a
            # flaky model echo aborts the turn; the booking contract is the verified tool evidence instead.
            include_contents="none",
            tools=[
                OpenAPIToolset(
                    spec_dict=service_specs["scheduling_fastapi"],
                    tool_filter=["create_exam_appointment"],
                    allow_private_network_access=True,
                ),
            ],
            before_model_callback=before_model_callback,
            before_tool_callback=before_tool_callback,
            after_tool_callback=after_tool_callback,
            after_agent_callback=_validate_output("booking_result"),
            before_agent_callback=_validate_input("scheduling_agent"),
        )
    )

    return SequentialAgent(name="clinical_scheduling_pipeline", sub_agents=agents)
