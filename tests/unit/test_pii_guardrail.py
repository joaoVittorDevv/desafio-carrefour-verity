from types import SimpleNamespace

import pytest

from security import GuardrailViolation
from security.pii_guardrail import BLOCK_MESSAGE, assert_no_pii, local_analyzer, redact_pii
from security.adk_callbacks import before_model_callback, before_tool_callback


ORDER = ("Paciente: Carlos Eduardo Albuquerque\nCPF: 123.456.789-00 | RG: 12.345.678-9 "
         "| Tel: (11) 98765-4321\nE-mail: carlos@exemplo.com.br\n"
         "Solicito: Hemograma Completo\nDra. Mariana Costa Silveira - CRM-SP 145892\nCNS: 123456789012345")


def test_redact_pii_masks_brazilian_identifiers_and_preserves_exam():
    result = redact_pii(ORDER)
    for token in ("PERSON", "CPF", "RG", "PHONE", "EMAIL", "CRM", "CNS"):
        assert f"[REDACTED_{token}]" in result.sanitized_text
    assert "Hemograma Completo" in result.sanitized_text
    assert "Carlos" not in result.sanitized_text
    assert result.total_redacted == 8
    assert_no_pii(result.sanitized_text)


def test_redact_pii_fails_closed_for_empty_input_and_analyzer_failure():
    class BrokenAnalyzer:
        def analyze(self, **kwargs):
            raise RuntimeError(ORDER)

    for text, analyzer in (("", None), (ORDER, BrokenAnalyzer())):
        with pytest.raises(GuardrailViolation) as exc:
            redact_pii(text, analyzer)
        assert ORDER not in str(exc.value)


def test_redact_pii_optional_local_person_analyzer():
    class LocalAnalyzer:
        def analyze(self, *, text, language, entities):
            assert language == "pt" and entities == ["PERSON"]
            return [SimpleNamespace(start=0, end=len("Mariana Costa"))] if text.startswith("Mariana") else []

    assert redact_pii("Mariana Costa\nHemograma", LocalAnalyzer()).sanitized_text == "[REDACTED_PERSON]\nHemograma"


def test_exam_names_flagged_as_person_by_ner_are_not_masked(monkeypatch, tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text('[{"official_name": "Hemograma Completo (simulado)", "synonyms": "Hemo completo"},'
                      ' {"official_name": "Glicemia de Jejum (simulado)", "synonyms": "Glicemia jejum"}]', encoding="utf-8")
    monkeypatch.setattr("security.pii_guardrail._EXAMS_CATALOG", catalog)
    from security import pii_guardrail
    pii_guardrail._exam_term_tokens.cache_clear()

    class OverzealousNer:
        def analyze(self, *, text, language, entities):
            spans = []
            for term in ("Hemograma Completo", "Glicemia de Jejum", "Mariana Costa Silveira"):
                start = text.find(term)
                if start >= 0:
                    spans.append(SimpleNamespace(start=start, end=start + len(term)))
            return spans

    result = redact_pii("1. Hemograma Completo\n2. Glicemia de Jejum\nDra. Mariana Costa Silveira", OverzealousNer())
    assert "Hemograma Completo" in result.sanitized_text
    assert "Glicemia de Jejum" in result.sanitized_text
    assert "Mariana" not in result.sanitized_text
    assert result.sanitized_text.count("[REDACTED_PERSON]") >= 1


def test_local_analyzer_does_not_download_missing_model(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "spacy", SimpleNamespace(util=SimpleNamespace(is_package=lambda name: False)))
    assert local_analyzer() is None


def test_callbacks_block_unsafe_model_and_tool_payloads(monkeypatch):
    import security.adk_callbacks as callbacks
    monkeypatch.setattr(callbacks, "local_analyzer", lambda: None)
    context = SimpleNamespace(state={"safe": "Hemograma"})
    tool = SimpleNamespace(name="unrelated")
    request = SimpleNamespace(contents=[{"parts": [{"text": ORDER}]}], config=None)
    with pytest.raises(GuardrailViolation) as exc:
        before_model_callback(context, request)
    assert ORDER not in str(exc.value)
    with pytest.raises(GuardrailViolation):
        before_tool_callback(tool, {"text": ORDER}, context)
    request.contents = [{"parts": [{"text": "Hemograma"}]}]
    assert before_model_callback(context, request) is None
    assert before_tool_callback(tool, {"text": "Hemograma"}, context) is None
    request.contents = [{"parts": [{"text": f"Tool error: {BLOCK_MESSAGE}"}]}]
    with pytest.raises(GuardrailViolation):
        before_model_callback(context, request)
    request.contents = [{"parts": [{"text": "Hemograma"}]}]
    with pytest.raises(GuardrailViolation):
        before_tool_callback(tool, {"binary": b"unknown"}, context)
    context.state["extracted_exams"] = ORDER
    with pytest.raises(GuardrailViolation):
        before_model_callback(context, request)
    with pytest.raises(GuardrailViolation):
        before_tool_callback(tool, {"text": "Hemograma"}, context)


def test_cns_pattern_ignores_long_decimal_fractions():
    """Regression: high-precision floats in tool JSON are not CNS numbers."""
    text = 'response: {"confidence_score": 0.823797008395195, "tuss_code": "MOCK-0001"}'
    assert_no_pii(text)
    result = redact_pii("CNS: 823797008395195")
    assert "[REDACTED_CNS]" in result.sanitized_text


def test_exam_vocabulary_includes_categories():
    from security import pii_guardrail
    from security.pii_guardrail import _looks_like_exam

    pii_guardrail._exam_term_tokens.cache_clear()  # earlier tests may have cached a tmp catalog
    assert _looks_like_exam("Endocrinologia")
    assert _looks_like_exam("Hemograma Completo (simulado)")
    assert not _looks_like_exam("Carlos Eduardo Albuquerque")


def test_person_pattern_tolerates_json_escaped_sanitized_text():
    """Regression: typed payloads travel JSON-escaped; \n becomes literal backslash-n."""
    import json

    payload = json.dumps({"sanitized_text": "Paciente: [REDACTED_PERSON]\nCPF: [REDACTED_CPF]"},
                         ensure_ascii=False)
    assert_no_pii(payload)
    leaked = json.dumps({"a": "Paciente: Carlos Eduardo\nCPF: 123.456.789-00"}, ensure_ascii=False)
    with pytest.raises(GuardrailViolation):
        assert_no_pii(leaked)


def test_person_pattern_placeholder_at_json_value_end():
    """Closing JSON quote also delimits a placeholder that ends the value."""
    import json

    payload = json.dumps({"sanitized_text": "Pedido\nPaciente: [REDACTED_PERSON]"}, ensure_ascii=False)
    assert_no_pii(payload)
    leaked = json.dumps({"a": "Paciente: Carlos Eduardo"}, ensure_ascii=False)
    with pytest.raises(GuardrailViolation):
        assert_no_pii(leaked)
