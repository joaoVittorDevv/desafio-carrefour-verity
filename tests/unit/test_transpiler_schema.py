import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from transpiler.schema import PipelineSpec


@pytest.fixture
def manifest():
    return json.loads(Path("specs/clinical_agent_spec.json").read_text())


def test_clinical_manifest(manifest):
    spec = PipelineSpec.model_validate(manifest)
    assert spec.schemas["ResolvedExamItem"].fields["tuss_code"].description.startswith("Identificador fictício")
    assert "unmatched_terms" in spec.schemas["BookingConfirmationOutput"].fields


def test_reject_reserved_schema_name(manifest):
    manifest["schemas"]["str"] = manifest["schemas"].pop("ExtractedExamRaw")
    with pytest.raises(ValidationError):
        PipelineSpec.model_validate(manifest)


@pytest.mark.parametrize("path,value", [
    (("mcp_servers", 0, "transport"), "stdio"),
    (("mcp_servers", 0, "url"), "file:///tmp/socket"),
    (("orchestration", "agents", 0, "mcp_bindings", 0, "server_id"), "missing"),
    (("orchestration", "agents", 1, "output_schema_ref"), "MissingModel"),
    (("orchestration", "agents", 2, "openapi_bindings", 0, "service_id"), "missing"),
    (("schemas", "ExtractedExamRaw", "fields", "raw_name", "type"), "list[MissingModel]"),
    (("schemas", "ExtractedExamRaw", "fields", "raw_name", "type"), "__import__('os').system('echo unsafe')"),
    (("orchestration", "agents", 1, "output_key"), "extracted_exams"),
    (("orchestration", "agents", 1, "name"), "ocr_intake_agent"),
    (("orchestration", "agents", 1, "output_key"), "not a key"),
    (("orchestration", "agents", 1, "mcp_bindings", 0, "allowed_tools"), [""]),
    (("orchestration", "agents", 2, "openapi_bindings", 0, "allowed_operations"), []),
])
def test_reject_invalid_cross_references_and_types(manifest, path, value):
    node = manifest
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value
    with pytest.raises(ValidationError):
        PipelineSpec.model_validate(manifest)
