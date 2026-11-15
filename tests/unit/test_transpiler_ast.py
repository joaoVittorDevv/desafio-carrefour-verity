import ast
import json
import sys
import types
from pathlib import Path

import pytest

from security import GuardrailViolation
from transpiler.compiler import SpecTranspilationError, compile_spec, load_spec, transpile
from transpiler.schema import PipelineSpec


SPEC = Path("specs/clinical_agent_spec.json")


def _llm_agent_kwargs(tree):
    """Map each generated LlmAgent name to its keyword names, as built in build_pipeline."""
    agents = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "LlmAgent":
            agents[next(kw.value.value for kw in node.keywords if kw.arg == "name")] = {
                kw.arg for kw in node.keywords if kw.arg}
    return agents


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
    # ruff format normalizes quotes, so the compiled contract uses double quotes.
    assert 'output_schema=' in code and 'include_contents="none"' in code
    # Emenda 2: the ADK validates the final LLM text against output_schema mid-stream, so the
    # booking agent (contract enforced via verified tool evidence) omits the kwarg; OCR/RAG keep it.
    agents = _llm_agent_kwargs(tree)
    assert agents.keys() == {"ocr_intake_agent", "rag_lookup_agent", "scheduling_agent"}
    assert "output_schema" not in agents["scheduling_agent"]
    assert "output_schema" in agents["ocr_intake_agent"]
    assert "output_schema" in agents["rag_lookup_agent"]
    assert 'before_agent_callback=_validate_input(' in code
    assert 'INPUT_SCHEMAS' in code and 'INPUT_KEYS' in code
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
    from security.adk_callbacks import inspect_for_pii as real_inspect_for_pii
    from security.adk_callbacks import require_complete_order

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
                after_tool_callback=lambda *args: None,
                inspect_for_pii=real_inspect_for_pii, require_complete_order=require_complete_order)
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
    ocr_output = {"sanitized_text": "Paciente: [REDACTED_PERSON]\nSolicito:\n1. Hemograma",
                  "pii_entities_masked": 1,
                  "exams": [{"raw_name": "Hemograma", "is_handwritten_hint": False}]}
    context = types.SimpleNamespace(state={"extracted_exams": ocr_output})
    with pytest.raises(namespace["AgentPipelineExecutionError"]):
        pipeline["sub_agents"][0]["after_agent_callback"](context)
    context.state["_ocr_verified"] = {"sanitized_text": ocr_output["sanitized_text"],
                                      "pii_entities_masked": 1}
    assert pipeline["sub_agents"][0]["after_agent_callback"](context) is None
    omitted = {**ocr_output,
               "sanitized_text": "Paciente: [REDACTED_PERSON]\nSolicito:\n1. Hemograma\n2. Creatinina"}
    context.state["_ocr_verified"] = {"sanitized_text": omitted["sanitized_text"],
                                      "pii_entities_masked": 1}
    context.state["extracted_exams"] = omitted
    with pytest.raises(namespace["AgentPipelineExecutionError"]):
        pipeline["sub_agents"][0]["after_agent_callback"](context)
    for reported in (("Creatinina", "Hemograma"), ("Hemograma", "Creatinina", "Ureia")):
        context.state["extracted_exams"] = {**omitted, "exams": [
            {"raw_name": name, "is_handwritten_hint": False} for name in reported]}
        with pytest.raises(namespace["AgentPipelineExecutionError"]):
            pipeline["sub_agents"][0]["after_agent_callback"](context)
    adended = {**ocr_output, "sanitized_text": ocr_output["sanitized_text"] + "\n+ Creatinina"}
    context.state["_ocr_verified"] = {"sanitized_text": adended["sanitized_text"],
                                      "pii_entities_masked": 1}
    context.state["extracted_exams"] = adended
    with pytest.raises(namespace["AgentPipelineExecutionError"]):
        pipeline["sub_agents"][0]["after_agent_callback"](context)
    context.state["_ocr_verified"] = {"sanitized_text": ocr_output["sanitized_text"],
                                      "pii_entities_masked": 1}
    context.state["extracted_exams"] = {**ocr_output, "exams": [
        {"raw_name": "Hemograma", "is_handwritten_hint": True}]}
    with pytest.raises(namespace["AgentPipelineExecutionError"]):
        pipeline["sub_agents"][0]["after_agent_callback"](context)
    context.state["extracted_exams"] = ocr_output
    fenced = "```json\n" + json.dumps(ocr_output, ensure_ascii=False) + "\n```"
    context.state["extracted_exams"] = fenced
    assert pipeline["sub_agents"][0]["after_agent_callback"](context) is None
    context.state["resolved_exams"] = {"matched_exams": [], "unmatched_terms": ["unknown"]}
    with pytest.raises(namespace["AgentPipelineExecutionError"]):
        pipeline["sub_agents"][1]["after_agent_callback"](context)

    validate_input = namespace["_validate_input"]
    rag_input = validate_input("rag_lookup_agent")
    valid_input = {"sanitized_text": "Paciente: [REDACTED_PERSON]\nHemograma",
                   "pii_entities_masked": 1,
                   "exams": [{"raw_name": "Hemograma", "is_handwritten_hint": False}]}
    input_context = types.SimpleNamespace(state={})
    with pytest.raises(namespace["AgentPipelineExecutionError"]):
        rag_input(input_context)
    input_context.state["extracted_exams"] = {**valid_input, "exams": "not a list"}
    with pytest.raises(namespace["AgentPipelineExecutionError"]):
        rag_input(input_context)
    input_context.state["extracted_exams"] = {**valid_input, "exams": []}
    with pytest.raises(namespace["AgentPipelineExecutionError"]):
        rag_input(input_context)
    input_context.state["extracted_exams"] = {**valid_input, "sanitized_text": "CPF: 123.456.789-00"}
    with pytest.raises(GuardrailViolation):
        rag_input(input_context)
    input_context.state["extracted_exams"] = valid_input
    assert rag_input(input_context) is None
    assert input_context.state["extracted_exams"] == valid_input
    assert input_context.state["temp:rag_lookup_agent_input"] == valid_input

    scheduling_input = validate_input("scheduling_agent")
    exam = {"original_query": "Hemograma", "tuss_code": "MOCK-0001",
            "official_name": "Hemograma Completo (simulado)", "category": "Hematologia",
            "fasting_hours": 0, "confidence_score": 0.9}
    resolved = {"matched_exams": [exam], "unmatched_terms": []}
    input_context.state["resolved_exams"] = {"matched_exams": [], "unmatched_terms": []}
    with pytest.raises(ValueError):
        scheduling_input(input_context)
    input_context.state["resolved_exams"] = resolved
    input_context.state["_rag_verified"] = {"matched_exams": [{**exam, "tuss_code": "MOCK-9999"}],
                                           "unmatched_terms": []}
    with pytest.raises(namespace["AgentPipelineExecutionError"]):
        scheduling_input(input_context)
    input_context.state["_rag_verified"] = resolved
    input_context.state["extracted_exams"] = valid_input
    assert scheduling_input(input_context) is None
    for bad in ({"matched_exams": [exam], "unmatched_terms": ["ilegível"]},
                {"matched_exams": [{**exam, "original_query": "Outro"}], "unmatched_terms": []}):
        input_context.state["resolved_exams"] = bad
        input_context.state["_rag_verified"] = bad
        with pytest.raises(ValueError):
            scheduling_input(input_context)
    assert input_context.state["temp:scheduling_agent_input"] == resolved


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
         before_tool_callback=lambda *a: None, after_tool_callback=lambda *a: None,
         inspect_for_pii=lambda value: None, require_complete_order=lambda *a: None)
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
