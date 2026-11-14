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
    resolved = {"matched_exams": [exam], "unmatched_terms": ["ilegível"]}
    assert after_tool_callback(rag_tool, {}, context, {"structuredContent": resolved}) is None
    context.state["resolved_exams"] = resolved
    with pytest.raises(ValueError):
        before_tool_callback(api_tool, {"exams": [dict(exam, tuss_code="MOCK-9999")],
                                        "unmatched_terms": ["ilegível"]}, context)
    with pytest.raises(ValueError):
        before_tool_callback(api_tool, {"exams": [exam], "unmatched_terms": []}, context)
    args = {"exams": [exam], "unmatched_terms": ["ilegível"]}
    assert before_tool_callback(api_tool, args, context) is None
    with pytest.raises(ValueError):
        after_tool_callback(api_tool, args, context, {"scheduled_exams": [],
                                                     "unmatched_terms": ["ilegível"]})
    confirmation = {"status": "PARTIAL", "scheduled_exams": [exam],
                    "unmatched_terms": ["ilegível"], "protocol_id": "LAB-MOCK"}
    after_tool_callback(api_tool, args, context, confirmation)
    assert context.state["_booking_verified"] == confirmation


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
    from security.adk_callbacks import before_model_callback

    class StateLike:
        def __init__(self, value):
            self._value = value

        def to_dict(self):
            return self._value

    request = SimpleNamespace(contents=[], config=None)
    clean = StateLike({"expected_image_path": "/app/inputs/pedido.png"})
    assert before_model_callback(SimpleNamespace(state=clean), request) is None
    dirty = StateLike({"note": "CPF: 123.456.789-00"})
    with pytest.raises(GuardrailViolation):
        before_model_callback(SimpleNamespace(state=dirty), request)


def test_thought_signature_bytes_pass_but_other_bytes_still_block():
    """Regression: Gemini 2.5 parts carry opaque thought_signature bytes."""
    from security.adk_callbacks import _inspect

    content = {"parts": [{"function_call": {"name": "t", "args": {}},
                          "thought_signature": b"\x00\x01opaque"}]}
    _inspect([content])  # must not raise
    with pytest.raises(GuardrailViolation):
        _inspect({"parts": [{"text": "x", "other_payload": b"\x00"}]})


def test_same_payload_tolerates_float_rounding_but_not_value_changes():
    from security.adk_callbacks import _same_payload

    verified = [{"tuss_code": "MOCK-0001", "confidence_score": 0.9320218712091446}]
    assert _same_payload([{"tuss_code": "MOCK-0001", "confidence_score": 0.932021871}], verified)
    assert not _same_payload([{"tuss_code": "MOCK-0002", "confidence_score": 0.932}], verified)
    assert not _same_payload([{"tuss_code": "MOCK-0001"}], verified)
    assert not _same_payload([{"tuss_code": "MOCK-0001", "confidence_score": 0.5}], verified)
