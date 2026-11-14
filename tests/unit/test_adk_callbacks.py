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
    projected = after_tool_callback(ocr_tool, {}, context, {
        "content": [{"type": "text", "text": json.dumps(ocr)}], "isError": False,
    })
    assert projected == {"sanitized_text": ocr["sanitized_text"],
                         "pii_redaction_report": {"total_redacted": 1}}
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


def test_ocr_callbacks_accept_masked_patient_label_without_weakening_evidence(monkeypatch):
    from security import adk_callbacks

    class PatientLabelAnalyzer:
        def analyze(self, *, text, language, entities):
            start = text.find("Paciente")
            return [SimpleNamespace(start=start, end=start + len("Paciente"))] if start >= 0 else []

    monkeypatch.setattr(adk_callbacks, "local_analyzer", PatientLabelAnalyzer)
    tool = SimpleNamespace(name="extract_and_anonymize_medical_order")
    state = {"expected_image_path": "/app/inputs/pedido.png"}
    context = SimpleNamespace(state=state)
    assert before_tool_callback(tool, {"file_path": state["expected_image_path"]}, context) is None
    response = {"sanitized_text": "Paciente: [REDACTED_PERSON]\\nHemograma",
                "pii_redaction_report": {"total_redacted": 1}}
    projected = after_tool_callback(tool, {}, context, {"structuredContent": response})
    assert projected == {"sanitized_text": response["sanitized_text"],
                         "pii_redaction_report": {"total_redacted": 1}}
    assert state["_ocr_verified"] == {"sanitized_text": response["sanitized_text"],
                                      "pii_entities_masked": 1}
    assert before_tool_callback(tool, {"file_path": state["expected_image_path"]}, context) is None
    with pytest.raises(ValueError):
        before_tool_callback(tool, {"file_path": "/app/inputs/outra.png"}, context)


@pytest.mark.parametrize("unsafe", [
    "Paciente: Ada Silva",
    "Paciente: [REDACTED_PERSON] Ada Silva",
    "Paciente: [REDACTED_PERSON",
    "Paciente: [REDACTED_PERSON]\\nCPF: 123.456.789-00",
    "Paciente: [REDACTED_PERSON]| Ada Silva",
    "Paciente: [REDACTED_PERSON]; Ada Silva",
    "Paciente: ",
], ids=["unmasked-person", "name-after-mask", "incomplete-mask", "cpf-after-mask", "pipe-after-mask", "semicolon-after-mask", "bare-label"])
def test_ocr_callbacks_reject_residual_pii_without_saving_evidence(monkeypatch, unsafe):
    from security import adk_callbacks

    class PatientLabelAnalyzer:
        def analyze(self, *, text, language, entities):
            start = text.find("Paciente")
            return [SimpleNamespace(start=start, end=start + len("Paciente"))] if start >= 0 else []

    monkeypatch.setattr(adk_callbacks, "local_analyzer", PatientLabelAnalyzer)
    tool = SimpleNamespace(name="extract_and_anonymize_medical_order")
    context = SimpleNamespace(state={"expected_image_path": "/app/inputs/pedido.png"})
    with pytest.raises(GuardrailViolation):
        before_tool_callback(tool, {"file_path": context.state["expected_image_path"],
                                    "sanitized_text": unsafe}, context)
    with pytest.raises(GuardrailViolation):
        after_tool_callback(tool, {}, context, {"structuredContent": {
            "sanitized_text": unsafe, "pii_redaction_report": {"total_redacted": 1}}})
    assert "_ocr_verified" not in context.state


def test_ocr_tool_response_to_model_contains_only_verified_sanitized_fields(monkeypatch):
    from security import adk_callbacks

    class FilenameNer:
        def analyze(self, *, text, language, entities):
            start = text.find("pedido_clinico")
            return [SimpleNamespace(start=start, end=start + len("pedido_clinico"))] if start >= 0 else []

    monkeypatch.setattr(adk_callbacks, "local_analyzer", FilenameNer)
    tool = SimpleNamespace(name="extract_and_anonymize_medical_order")
    args = {"file_path": "/app/inputs/pedido_clinico.png"}
    response = {"file_name": "pedido_clinico.png", "sanitized_text": "Paciente: [REDACTED_PERSON]\\nSolicito:\\n1. Hemograma",
                "pii_redaction_report": {"total_redacted": 1, "entities_found": ["PERSON"]},
                "ocr_confidence": 0.99}
    ctx = SimpleNamespace(state={"expected_image_path": args["file_path"]})
    model_response = after_tool_callback(tool, args, ctx, {"content": [{"type": "text", "text": json.dumps(response)}]})
    assert model_response["sanitized_text"] == response["sanitized_text"]
    assert model_response["pii_redaction_report"]["total_redacted"] == 1
    assert "file_name" not in model_response and "content" not in model_response
    adk_callbacks._inspect(model_response)
    assert ctx.state["_ocr_verified"] == {"sanitized_text": response["sanitized_text"], "pii_entities_masked": 1}


def test_ocr_filename_uses_regex_and_exact_requested_basename(monkeypatch):
    from security import adk_callbacks

    class FilenameNer:
        def analyze(self, *, text, language, entities):
            if text == "pedido_clinico.png":
                return [SimpleNamespace(start=0, end=len(text))]
            return []

    monkeypatch.setattr(adk_callbacks, "local_analyzer", FilenameNer)
    tool = SimpleNamespace(name="extract_and_anonymize_medical_order")
    args = {"file_path": "/app/inputs/pedido_clinico.png"}
    response = {"file_name": "pedido_clinico.png", "sanitized_text": "Solicito:\n1. Hemograma",
                "pii_redaction_report": {"total_redacted": 0}}
    ctx = SimpleNamespace(state={"expected_image_path": args["file_path"]})
    assert after_tool_callback(tool, args, ctx, response)["sanitized_text"] == response["sanitized_text"]
    assert ctx.state["_ocr_verified"]["sanitized_text"] == response["sanitized_text"]
    for name in ("outro.png", "CPF-123.456.789-00.png", "../../pedido_clinico.png"):
        blocked = SimpleNamespace(state={"expected_image_path": args["file_path"]})
        with pytest.raises(GuardrailViolation):
            after_tool_callback(tool, args, blocked, {**response, "file_name": name})
        assert "_ocr_verified" not in blocked.state
    with pytest.raises(GuardrailViolation):
        after_tool_callback(tool, args, ctx, {**response, "sanitized_text": "CPF: 123.456.789-00"})


def test_registration_tool_never_repeats_after_attempt_even_without_response():
    from security.adk_callbacks import RegistrationAlreadyAttemptedError

    exam = {"original_query": "Hemograma", "tuss_code": "MOCK-0001", "official_name": "Hemograma (simulado)",
            "category": "Hematologia", "fasting_hours": 0, "confidence_score": 0.9}
    resolved = {"matched_exams": [exam], "unmatched_terms": []}
    state = {"extracted_exams": {"exams": [{"raw_name": "Hemograma"}]},
             "resolved_exams": resolved, "_rag_verified": resolved}
    ctx = SimpleNamespace(state=state)
    tool = SimpleNamespace(name="create_exam_appointment")
    args = {"exams": [exam], "unmatched_terms": []}
    assert before_tool_callback(tool, args, ctx) is None
    assert state["_booking_attempted"] is True
    with pytest.raises(RegistrationAlreadyAttemptedError):
        before_tool_callback(tool, args, ctx)
    assert state["_booking_attempted"] is True


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
