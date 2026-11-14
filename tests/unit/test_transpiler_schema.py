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


def test_clinical_manifest_declares_typed_inputs(manifest):
    spec = PipelineSpec.model_validate(manifest)
    rag, scheduling = spec.orchestration.agents[1], spec.orchestration.agents[2]
    assert (rag.input_key, rag.input_schema_ref, rag.include_contents) == (
        "extracted_exams", "ExtractedExamsOutput", "none")
    assert (scheduling.input_key, scheduling.input_schema_ref, scheduling.include_contents) == (
        "resolved_exams", "ResolvedExamsListOutput", "none")
    assert spec.orchestration.agents[0].input_key is None


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
    (("orchestration", "agents", 1, "input_key"), None),
    (("orchestration", "agents", 1, "input_schema_ref"), None),
    (("orchestration", "agents", 1, "input_key"), "booking_result"),
    (("orchestration", "agents", 1, "input_key"), "resolved_exams"),
    (("orchestration", "agents", 1, "input_key"), "not a key"),
    (("orchestration", "agents", 1, "input_schema_ref"), "MissingModel"),
    (("orchestration", "agents", 1, "instruction"), "Leia os exames em {extracted_exams}"),
])
def test_reject_invalid_cross_references_and_types(manifest, path, value):
    node = manifest
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value
    with pytest.raises(ValidationError):
        PipelineSpec.model_validate(manifest)


def test_instruction_rejects_any_state_placeholder_not_only_own_input_key(manifest):
    from transpiler.schema import PipelineSpec
    data = manifest
    agent = data["orchestration"]["agents"][2]  # scheduling_agent (input_key=resolved_exams)
    agent["instruction"] = "Use {extracted_exams} diretamente."
    with pytest.raises(ValueError, match="must not interpolate"):
        PipelineSpec.model_validate(data)
