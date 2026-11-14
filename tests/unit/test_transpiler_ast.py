import ast
import json
import sys
import types
from pathlib import Path

import pytest

from transpiler.compiler import SpecTranspilationError, compile_spec, load_spec, transpile
from transpiler.schema import PipelineSpec


SPEC = Path("specs/clinical_agent_spec.json")


def test_compile_spec_ast_imports_and_escaped_literals(tmp_path):
    data = json.loads(SPEC.read_text())
    data["orchestration"]["agents"][0]["instruction"] = "quoted ' \" \\ \\n and new line\nnot_code = 1"
    data["schemas"]["ExtractedExamRaw"]["fields"]["raw_name"]["description"] = "'; raise RuntimeError('escaped')"
    code = compile_spec(PipelineSpec.model_validate(data))
    tree = ast.parse(code)
    assert any(isinstance(n, ast.ImportFrom) and n.module == "google.adk.agents" for n in ast.walk(tree))
    assert any(isinstance(n, ast.ImportFrom) and n.module == "google.adk.tools.mcp_tool.mcp_toolset"
               and "SseConnectionParams" in [alias.name for alias in n.names] for n in ast.walk(tree))
    assert 'not_code = 1\n' not in code
    assert 'tool_filter=[' in code and 'before_tool_callback=before_tool_callback' in code
    assert 'after_agent_callback=_validate_output' in code
    assert 'after_tool_callback=after_tool_callback' in code
    output = tmp_path / "agent.py"
    transpile(SPEC, output)
    assert output.read_text() == compile_spec(load_spec(SPEC))


def test_generic_one_agent_manifest_does_not_require_clinical_tool_evidence():
    data = json.loads(SPEC.read_text())
    data["schemas"] = {"Answer": {"description": "generic", "fields": {
        "text": {"type": "str", "description": "answer"}}}}
    data["mcp_servers"] = []
    data["openapi_services"] = []
    data["orchestration"]["agents"] = [{"name": "answer_agent", "model": "unused",
        "instruction": "Return text", "output_key": "answer", "output_schema_ref": "Answer"}]
    code = compile_spec(PipelineSpec.model_validate(data))
    assert "_ocr_verified" not in code and "_booking_verified" not in code
    ast.parse(code)


def test_invalid_json_does_not_overwrite_output(tmp_path):
    bad = tmp_path / "bad.json"
    output = tmp_path / "agent.py"
    bad.write_text("{")
    output.write_text("unchanged")
    with pytest.raises(SpecTranspilationError):
        transpile(bad, output)
    assert output.read_text() == "unchanged"


def test_generated_pipeline_with_mock_adk_and_no_network(monkeypatch):
    """Generated module imports and toolsets are mocked; no ADK SDK/model/network used."""
    created = []
    def fake_module(name, **items):
        module = types.ModuleType(name)
        module.__dict__.update(items)
        monkeypatch.setitem(sys.modules, name, module)
    for name in ("google", "google.adk", "google.adk.tools", "google.adk.tools.mcp_tool",
                 "google.adk.tools.openapi_tool", "google.adk.tools.openapi_tool.openapi_spec_parser",
                 "security"):
        fake_module(name)
    def toolset(**kwargs):
        created.append(kwargs)
        return kwargs
    fake_module("google.adk.agents", LlmAgent=lambda **kwargs: kwargs,
                SequentialAgent=lambda **kwargs: kwargs)
    fake_module("google.adk.tools.mcp_tool.mcp_toolset", MCPToolset=toolset,
                SseConnectionParams=lambda **kwargs: kwargs)
    fake_module("google.adk.tools.openapi_tool.openapi_spec_parser.openapi_toolset", OpenAPIToolset=toolset)
    fake_module("security.adk_callbacks", before_model_callback=lambda *args: None,
                before_tool_callback=lambda *args: None,
                after_tool_callback=lambda *args: None)
    namespace = {"__name__": "mock_generated"}
    exec(compile_spec(load_spec(SPEC)), namespace)
    monkeypatch.setattr(namespace["httpx"], "get", lambda *a, **kw: type("Response", (), {
        "raise_for_status": lambda self: None, "json": lambda self: {},
    })())
    pipeline = namespace["build_pipeline"]()
    assert len(pipeline["sub_agents"]) == 3
    assert [tool["tool_filter"] for tool in created] == [
        ["extract_and_anonymize_medical_order"], ["search_exam_catalog_hybrid"], ["create_exam_appointment"]]
    assert created[0]["connection_params"]["url"].endswith("/sse")
    assert created[2]["allow_private_network_access"] is True
    ocr_output = {"sanitized_text": "Paciente: [REDACTED_PERSON]\nHemograma",
                  "pii_entities_masked": 1,
                  "exams": [{"raw_name": "Hemograma", "is_handwritten_hint": False}]}
    context = types.SimpleNamespace(state={"extracted_exams": ocr_output})
    with pytest.raises(namespace["AgentPipelineExecutionError"]):
        pipeline["sub_agents"][0]["after_agent_callback"](context)
    context.state["_ocr_verified"] = {"sanitized_text": ocr_output["sanitized_text"],
                                      "pii_entities_masked": 1}
    assert pipeline["sub_agents"][0]["after_agent_callback"](context) is None
    fenced = "```json\n" + json.dumps(ocr_output, ensure_ascii=False) + "\n```"
    context.state["extracted_exams"] = fenced
    assert pipeline["sub_agents"][0]["after_agent_callback"](context) is None
    context.state["resolved_exams"] = {"matched_exams": [], "unmatched_terms": ["unknown"]}
    with pytest.raises(namespace["AgentPipelineExecutionError"]):
        pipeline["sub_agents"][1]["after_agent_callback"](context)


def test_booking_output_accepts_operation_response_wrapper(monkeypatch):
    """The OpenAPIToolset advertises the payload under <operation>_response."""
    import json as _json
    data = _json.loads(SPEC.read_text())
    code = compile_spec(PipelineSpec.model_validate(data))
    namespace = {"__name__": "mock_generated_wrap"}
    def stub(name, **items):
        module = types.ModuleType(name)
        module.__dict__.update(items)
        monkeypatch.setitem(sys.modules, name, module)
    for name in ("google", "google.adk", "google.adk.tools", "google.adk.tools.mcp_tool",
                 "google.adk.tools.openapi_tool", "google.adk.tools.openapi_tool.openapi_spec_parser",
                 "security", "httpx", "re"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    stub("google.adk.agents", LlmAgent=lambda **kwargs: kwargs,
         SequentialAgent=lambda **kwargs: kwargs)
    stub("google.adk.tools.mcp_tool.mcp_toolset", MCPToolset=lambda **kw: kw,
         SseConnectionParams=lambda **kw: kw)
    stub("google.adk.tools.openapi_tool.openapi_spec_parser.openapi_toolset",
         OpenAPIToolset=lambda **kw: kw)
    stub("security.adk_callbacks", before_model_callback=lambda *a: None,
         before_tool_callback=lambda *a: None, after_tool_callback=lambda *a: None)
    exec(code, namespace)
    monkeypatch.setattr(namespace["httpx"], "get", lambda *a, **kw: type("Response", (), {
        "raise_for_status": lambda self: None, "json": lambda self: {},
    })())
    pipeline = namespace["build_pipeline"]()
    booking = {"protocol_id": "LAB-2026-11-13-ABCDEFGHIJ", "status": "CONFIRMED",
               "scheduled_at": "2026-11-13T12:40:57Z", "preferred_unit": "Unidade Central - Matriz",
               "max_fasting_hours_required": 0, "preparation_summary": "Preparo simulado",
               "total_exams_scheduled": 0, "scheduled_exams": [], "unmatched_terms": []}
    context = types.SimpleNamespace(state={"booking_result": json.dumps(
        {"create_exam_appointment_response": booking}), "_booking_verified": booking})
    assert pipeline["sub_agents"][2]["after_agent_callback"](context) is None
    assert context.state["booking_result"] == booking
