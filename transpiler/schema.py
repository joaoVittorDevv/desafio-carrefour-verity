"""Validated JSON DSL for the sequential ADK pipeline."""

import keyword
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator


_TYPE = re.compile(r"^(str|int|float|bool|list\[(?:str|int|float|bool|[A-Za-z_][A-Za-z_0-9]*)\]|[A-Za-z_][A-Za-z_0-9]*)$")

_RESERVED = {"str", "int", "float", "bool", "list", "BaseModel", "Field", "ValidationError",
             "LlmAgent", "SequentialAgent", "MCPToolset", "SseConnectionParams", "OpenAPIToolset",
             "AgentPipelineExecutionError", "OUTPUT_SCHEMAS", "build_pipeline", "httpx",
             "before_model_callback", "before_tool_callback", "_validate_output"}


def _identifier(value: str) -> bool:
    return value.isidentifier() and not keyword.iskeyword(value) and not value.startswith("__")


def _http_url(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc) and not parts.username and not parts.password


class FieldSpec(BaseModel):
    type: str
    description: str


class SchemaSpec(BaseModel):
    description: str
    fields: dict[str, FieldSpec]


class MCPServer(BaseModel):
    id: str
    transport: str
    url: str
    timeout: int = Field(default=30, gt=0)

    @model_validator(mode="after")
    def sse_only(self):
        if self.transport != "sse" or not _http_url(self.url):
            raise ValueError("MCP requires transport 'sse' and an http(s) URL")
        return self


class OpenAPIService(BaseModel):
    id: str
    spec_url: str

    @model_validator(mode="after")
    def valid_url(self):
        if not _http_url(self.spec_url):
            raise ValueError("OpenAPI spec_url must be an http(s) URL")
        return self


class MCPBinding(BaseModel):
    server_id: str
    allowed_tools: list[str] = Field(min_length=1)


class OpenAPIBinding(BaseModel):
    service_id: str
    allowed_operations: list[str] = Field(min_length=1)


class AgentSpec(BaseModel):
    name: str
    model: str
    instruction: str
    mcp_bindings: list[MCPBinding] = Field(default_factory=list)
    openapi_bindings: list[OpenAPIBinding] = Field(default_factory=list)
    output_key: str
    output_schema_ref: str


class Orchestration(BaseModel):
    type: Literal["sequential"]
    name: str
    input_volume_path: str
    agents: list[AgentSpec] = Field(min_length=1)


class Guardrails(BaseModel):
    enable_pii_redaction: bool
    entities: list[str]
    mask_token: str


class Metadata(BaseModel):
    name: str
    description: str


class PipelineSpec(BaseModel):
    version: str
    metadata: Metadata
    guardrails: Guardrails
    schemas: dict[str, SchemaSpec]
    mcp_servers: list[MCPServer]
    openapi_services: list[OpenAPIService]
    orchestration: Orchestration

    @model_validator(mode="after")
    def cross_references(self):
        if not self.guardrails.enable_pii_redaction:
            raise ValueError("guardrails.enable_pii_redaction must be true")
        server_ids = [server.id for server in self.mcp_servers]
        service_ids = [service.id for service in self.openapi_services]
        if len(set(server_ids)) != len(server_ids) or len(set(service_ids)) != len(service_ids):
            raise ValueError("mcp_servers and openapi_services IDs must be unique")
        if not _identifier(self.orchestration.name):
            raise ValueError("orchestration.name must be a Python identifier")
        if not self.schemas:
            raise ValueError("schemas must not be empty")
        for name, schema in self.schemas.items():
            if not _identifier(name) or name in _RESERVED:
                raise ValueError(f"schemas.{name}: invalid Python class name")
            if not schema.fields:
                raise ValueError(f"schemas.{name}.fields must not be empty")
            for field_name, field in schema.fields.items():
                if not _identifier(field_name) or field_name.startswith("model_") or field_name in {"model_config", "model_fields"}:
                    raise ValueError(f"schemas.{name}.fields.{field_name}: invalid field name")
                match = _TYPE.fullmatch(field.type)
                if not match:
                    raise ValueError(f"schemas.{name}.fields.{field_name}.type: unsupported type")
                inner = field.type.removeprefix("list[").removesuffix("]") if field.type.startswith("list[") else field.type
                if inner not in {"str", "int", "float", "bool"} and inner not in self.schemas:
                    raise ValueError(f"schemas.{name}.fields.{field_name}.type: unknown schema {inner}")
        names = [agent.name for agent in self.orchestration.agents]
        keys = [agent.output_key for agent in self.orchestration.agents]
        if len(set(names)) != len(names) or any(not _identifier(name) for name in names):
            raise ValueError("orchestration.agents.name must be unique Python identifiers")
        if len(set(keys)) != len(keys) or any(not _identifier(key) for key in keys):
            raise ValueError("orchestration.agents.output_key must be unique Python identifiers")
        for index, agent in enumerate(self.orchestration.agents):
            path = f"orchestration.agents.{index}"
            if agent.output_schema_ref not in self.schemas:
                raise ValueError(f"{path}.output_schema_ref: unknown schema {agent.output_schema_ref}")
            for binding in agent.mcp_bindings:
                if binding.server_id not in server_ids:
                    raise ValueError(f"{path}.mcp_bindings.server_id: unknown server {binding.server_id}")
                if any(not tool.strip() for tool in binding.allowed_tools):
                    raise ValueError(f"{path}.mcp_bindings.allowed_tools: empty tool identifier")
            for binding in agent.openapi_bindings:
                if binding.service_id not in service_ids:
                    raise ValueError(f"{path}.openapi_bindings.service_id: unknown service {binding.service_id}")
                if any(not operation.strip() for operation in binding.allowed_operations):
                    raise ValueError(f"{path}.openapi_bindings.allowed_operations: empty operation identifier")
        return self
