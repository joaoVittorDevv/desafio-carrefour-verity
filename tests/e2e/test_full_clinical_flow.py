"""Mock-only pipeline contract: no HTTP, database, OCR engine, embeddings or LLM calls."""

import asyncio
import sys
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from runtime.cli import AgentPipelineExecutionError, execute_pipeline
from security import GuardrailViolation
from security.adk_callbacks import require_complete_order


def test_complete_flow_and_blocked_cases_are_mocked(monkeypatch, tmp_path):
    import runtime.cli as cli

    calls = {"ocr": 0, "rag": 0, "api": 0}
    input_path = tmp_path / "pedido-ficticio.png"
    input_path.write_bytes(b"mock-only")
    accepted = {
        "original_query": "Hemograma",
        "tuss_code": "MOCK-0001",
        "official_name": "Hemograma Completo (simulado)",
        "category": "Hematologia",
        "fasting_hours": 0,
        "confidence_score": 0.92,
    }
    mode = {"value": "complete"}

    def ocr(_image):
        calls["ocr"] += 1
        if mode["value"] == "unsafe":
            raise GuardrailViolation()
        return {"exams": [{"raw_name": "Hemograma"}], "sanitized_text": "[REDACTED_PERSON]"}

    def rag(_exams):
        calls["rag"] += 1
        if mode["value"] == "unmatched":
            return {"matched_exams": [], "unmatched_terms": ["ilegível"]}
        return {"matched_exams": [accepted], "unmatched_terms": ["ilegível"] if mode["value"] == "partial" else []}

    def api(result):
        calls["api"] += 1
        return {
            "protocol_id": "LAB-MOCK", "status": "CONFIRMED",
            "scheduled_at": "2026-11-12T19:45:00Z", "preferred_unit": "Unidade Central",
            "max_fasting_hours_required": 0,
            "preparation_summary": "Preparo simulado",
            "total_exams_scheduled": 1,
            "scheduled_exams": result["matched_exams"],
            "unmatched_terms": result["unmatched_terms"],
        }

    state = {}
    output = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output, force_terminal=False))

    class FakeSessions:
        async def create_session(self, **_kwargs):
            pass

        async def get_session(self, **_kwargs):
            return SimpleNamespace(state=state)

    class FakeRunner:
        def __init__(self, **_kwargs):
            pass

        async def run_async(self, **_kwargs):
            extracted = ocr(input_path)
            yield SimpleNamespace(author="ocr_intake_agent")
            state["extracted_exams"] = extracted
            resolved = rag(extracted["exams"])
            if not resolved["matched_exams"] or resolved["unmatched_terms"]:
                raise AgentPipelineExecutionError("No exams resolved")
            state["resolved_exams"] = resolved
            state["_rag_verified"] = resolved
            if mode["value"] == "tampered":
                state["resolved_exams"] = {**resolved, "matched_exams": [
                    {**accepted, "original_query": "Outro"}]}
            require_complete_order(state["extracted_exams"], state["resolved_exams"])
            yield SimpleNamespace(author="rag_lookup_agent")
            state["booking_result"] = api(resolved)
            state["_booking_verified"] = state["booking_result"].copy()
            if mode["value"] == "invalid_confirmation":
                state["booking_result"]["status"] = "PARTIAL"
            yield SimpleNamespace(author="scheduling_agent")

    monkeypatch.setattr(cli.importlib, "import_module", lambda _name: SimpleNamespace(build_pipeline=lambda: object()))
    monkeypatch.setitem(sys.modules, "google", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "google.adk", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "google.adk.runners", SimpleNamespace(Runner=FakeRunner))
    monkeypatch.setitem(sys.modules, "google.adk.sessions", SimpleNamespace(InMemorySessionService=FakeSessions))
    monkeypatch.setitem(sys.modules, "google.genai", SimpleNamespace(types=SimpleNamespace(
        Content=lambda **kwargs: kwargs, Part=lambda **kwargs: kwargs,
    )))

    result = asyncio.run(execute_pipeline("mocked.agent", input_path))
    assert result["status"] == "CONFIRMED"
    assert result["scheduled_exams"] == [accepted]
    assert result["unmatched_terms"] == []
    assert calls == {"ocr": 1, "rag": 1, "api": 1}
    text = output.getvalue()
    for step in ("Extraindo exames e protegendo dados pessoais", "Conferindo exames no catálogo fictício",
                 "Registrando a solicitação fictícia"):
        assert text.count(f"✓ {step}") == 1
    assert text.index("✓ Extraindo") < text.index("✓ Conferindo") < text.index("✓ Registrando")
    assert text.index("Iniciando: Conferindo") < text.index("✓ Conferindo")
    assert "Conferindo o resultado do registro" in text
    assert "Hemograma" not in text and "[REDACTED_PERSON]" not in text

    mode["value"] = "partial"
    with pytest.raises(AgentPipelineExecutionError):
        asyncio.run(execute_pipeline("mocked.agent", input_path))
    assert calls["api"] == 1

    mode["value"] = "tampered"
    with pytest.raises(ValueError):
        asyncio.run(execute_pipeline("mocked.agent", input_path))
    assert calls["api"] == 1

    mode["value"] = "unmatched"
    output.seek(0)
    output.truncate()
    with pytest.raises(AgentPipelineExecutionError):
        asyncio.run(execute_pipeline("mocked.agent", input_path))
    assert "✓ Registrando" not in output.getvalue()
    assert calls["api"] == 1

    mode["value"] = "invalid_confirmation"
    output.seek(0)
    output.truncate()
    with pytest.raises(AgentPipelineExecutionError):
        asyncio.run(execute_pipeline("mocked.agent", input_path))
    assert "✓ Registrando" not in output.getvalue()

    mode["value"] = "unsafe"
    with pytest.raises(GuardrailViolation):
        asyncio.run(execute_pipeline("mocked.agent", input_path))
    assert calls["rag"] == 5 and calls["api"] == 2
