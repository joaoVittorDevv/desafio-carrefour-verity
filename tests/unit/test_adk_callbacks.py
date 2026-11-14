"""Tool evidence and pre-POST guardrails: mocks only, no network or model."""
import json
from types import SimpleNamespace

import pytest

from security import GuardrailViolation
from security.adk_callbacks import after_tool_callback, before_tool_callback


def test_tool_results_required_and_registration_checked_before_post():
    context = SimpleNamespace(state={"expected_image_path": "/app/inputs/pedido.png"})
    ocr_tool = SimpleNamespace(name="extract_and_anonymize_medical_order")
    rag_tool = SimpleNamespace(name="search_exam_catalog_hybrid")
    api_tool = SimpleNamespace(name="create_exam_appointment")
    with pytest.raises(ValueError):
        before_tool_callback(ocr_tool, {"file_path": "/app/inputs/outra.png"}, context)
    assert before_tool_callback(ocr_tool, {"file_path": "/app/inputs/pedido.png"}, context) is None
    ocr = {"sanitized_text": "Paciente: [REDACTED_PERSON]\nHemograma",
           "pii_redaction_report": {"total_redacted": 1}}
    assert after_tool_callback(ocr_tool, {}, context, {
        "content": [{"type": "text", "text": json.dumps(ocr)}], "isError": False,
    }) is None
    assert context.state["_ocr_verified"]["pii_entities_masked"] == 1

    context.state["extracted_exams"] = {"exams": [{"raw_name": "Hemograma"}]}
    with pytest.raises(ValueError):
        before_tool_callback(rag_tool, {"exam_queries": ["Exame inventado"]}, context)
    assert before_tool_callback(rag_tool, {"exam_queries": ["Hemograma"]}, context) is None

    exam = {"original_query": "Hemograma", "tuss_code": "MOCK-0001",
            "official_name": "Hemograma Completo (simulado)", "category": "Hematologia",
            "fasting_hours": 0, "confidence_score": 0.9}
    resolved = {"matched_exams": [exam], "unmatched_terms": []}
    after_tool_callback(rag_tool, {}, context, {"structuredContent": resolved})
    context.state["resolved_exams"] = resolved
    args = {"exams": [exam], "unmatched_terms": []}
    assert before_tool_callback(api_tool, args, context) is None
    for bad in (
        {"matched_exams": [exam], "unmatched_terms": ["ilegível"]},
        {"matched_exams": [dict(exam, original_query="Outro")], "unmatched_terms": []},
        {"matched_exams": [exam, exam], "unmatched_terms": []},
        {"matched_exams": [], "unmatched_terms": ["Hemograma"]},
    ):
        context.state["resolved_exams"] = bad
        with pytest.raises(ValueError):
            before_tool_callback(api_tool, {"exams": bad["matched_exams"],
                                           "unmatched_terms": bad["unmatched_terms"]}, context)
    context.state["resolved_exams"] = resolved
    with pytest.raises(ValueError):
        before_tool_callback(api_tool, {"exams": [dict(exam, tuss_code="MOCK-9999")],
                                       "unmatched_terms": []}, context)
    with pytest.raises(ValueError):
        after_tool_callback(api_tool, args, context, {"scheduled_exams": [],
                                                     "unmatched_terms": []})
    confirmation = {"status": "CONFIRMED", "scheduled_exams": [exam],
                    "unmatched_terms": [], "protocol_id": "LAB-MOCK"}
    after_tool_callback(api_tool, args, context, confirmation)
    assert context.state["_booking_verified"] == confirmation


def test_complete_order_requires_exact_order_and_multiplicity():
    from security.adk_callbacks import IncompleteOrderError, require_complete_order

    extracted = {"exams": [{"raw_name": name} for name in ("A", "B", "A")]}
    for names in (("B", "A", "A"), ("A", "B"), ("A", "B", "A", "A")):
        with pytest.raises(IncompleteOrderError):
            require_complete_order(extracted, {"matched_exams": [
                {"original_query": name} for name in names], "unmatched_terms": []})
    require_complete_order(extracted, {"matched_exams": [
        {"original_query": name} for name in ("A", "B", "A")], "unmatched_terms": []})


def test_ocr_error_and_unsanitized_response_cannot_reach_next_agent():
    tool = SimpleNamespace(name="extract_and_anonymize_medical_order")
    context = SimpleNamespace(state={})
    with pytest.raises(ValueError):
        after_tool_callback(tool, {}, context, {"error": "OCR unavailable"})
    with pytest.raises(GuardrailViolation):
        after_tool_callback(tool, {}, context, {"sanitized_text": "Paciente: Ana Silva",
                                                    "pii_redaction_report": {"total_redacted": 0}})
    assert "_ocr_verified" not in context.state


def test_adk_state_wrapper_object_is_inspected_via_to_dict():
    """Regression: the real ADK State is a custom wrapper (no Mapping/model_dump)."""
    from google.adk.sessions.state import State

    from security.adk_callbacks import _inspect, before_model_callback

    request = SimpleNamespace(contents=[], config=None)
    clean = State(value={"expected_image_path": "/app/inputs/pedido.png"}, delta={})
    _inspect(clean)  # must not raise
    assert before_model_callback(SimpleNamespace(agent_name="ocr_intake_agent", state=clean), request) is None
    with pytest.raises(GuardrailViolation):
        _inspect(State(value={"note": "CPF: 123.456.789-00"}, delta={}))
    dirty = State(value={"note": "CPF: 123.456.789-00"}, delta={})
    with pytest.raises(GuardrailViolation):
        before_model_callback(SimpleNamespace(agent_name="ocr_intake_agent", state=dirty), request)


def test_real_genai_part_passes_but_other_bytes_still_block():
    """Regression: Gemini 2.5 parts carry opaque thought_signature bytes."""
    from google.genai import types

    from security.adk_callbacks import _inspect

    _inspect(types.Part(text="ok", thought_signature=b"sig"))  # must not raise
    _inspect([{"parts": [{"function_call": {"name": "t", "args": {}},
                          "thought_signature": b"\x00\x01opaque"}]}])
    with pytest.raises(GuardrailViolation):
        _inspect({"binary": b"raw"})


def test_before_model_callback_delivers_typed_payload_as_user_turn():
    from google.genai import types

    from security.adk_callbacks import before_model_callback

    payload = {"sanitized_text": "[REDACTED_PERSON]\nHemograma", "pii_entities_masked": 1,
               "exams": [{"raw_name": "Hemograma", "is_handwritten_hint": False}]}
    context = SimpleNamespace(agent_name="rag_lookup_agent",
                              state={"temp:rag_lookup_agent_input": payload})
    request = SimpleNamespace(
        contents=[types.Content(role="user", parts=[types.Part(text="Process file")])],
        config=SimpleNamespace(system_instruction="Instrução fixa"))
    assert before_model_callback(context, request) is None
    assert len(request.contents) == 1
    assert request.contents[0].role == "user"
    assert request.contents[0].parts[0].text == json.dumps(payload, ensure_ascii=False)
    assert request.config.system_instruction == "Instrução fixa"

    context.state["temp:rag_lookup_agent_input"] = {
        **payload, "sanitized_text": "CPF: 123.456.789-00"}
    with pytest.raises(GuardrailViolation):
        before_model_callback(context, request)


def test_before_model_callback_keeps_contents_with_function_response():
    from google.genai import types

    from security.adk_callbacks import before_model_callback

    payload = {"matched_exams": [], "unmatched_terms": ["ilegível"]}
    context = SimpleNamespace(agent_name="scheduling_agent",
                              state={"temp:scheduling_agent_input": payload})
    original = [types.Content(role="user", parts=[types.Part(
        function_response=types.FunctionResponse(name="search_exam_catalog_hybrid", response={}))])]
    request = SimpleNamespace(contents=original, config=None)
    assert before_model_callback(context, request) is None
    assert request.contents is original
    assert request.contents[0].parts[0].function_response.name == "search_exam_catalog_hybrid"


def test_same_payload_tolerates_float_rounding_but_not_value_changes():
    from security.adk_callbacks import _same_payload

    verified = [{"tuss_code": "MOCK-0001", "confidence_score": 0.9320218712091446}]
    assert _same_payload([{"tuss_code": "MOCK-0001", "confidence_score": 0.932021871}], verified)
    assert not _same_payload([{"tuss_code": "MOCK-0002", "confidence_score": 0.932}], verified)
    assert not _same_payload([{"tuss_code": "MOCK-0001"}], verified)
    assert not _same_payload([{"tuss_code": "MOCK-0001", "confidence_score": 0.5}], verified)
