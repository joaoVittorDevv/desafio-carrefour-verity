"""Mock-only pipeline contract: no HTTP, database, OCR engine, embeddings or LLM calls."""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from runtime.cli import AgentPipelineExecutionError, execute_pipeline
from security import GuardrailViolation


def test_partial_flow_and_blocked_cases_are_mocked(monkeypatch, tmp_path):
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
    mode = {"value": "partial"}

    def ocr(_image):
        calls["ocr"] += 1
        if mode["value"] == "unsafe":
            raise GuardrailViolation()
        return {"exams": ["Hemograma", "ilegível"], "sanitized_text": "[REDACTED_PERSON]"}

    def rag(_exams):
        calls["rag"] += 1
        if mode["value"] == "unmatched":
            return {"matched_exams": [], "unmatched_terms": ["ilegível"]}
        return {"matched_exams": [accepted], "unmatched_terms": ["ilegível"]}

    def api(result):
        calls["api"] += 1
        return {
            "protocol_id": "LAB-MOCK", "status": "PARTIAL",
            "scheduled_at": "2026-11-12T19:45:00Z", "preferred_unit": "Unidade Central",
            "max_fasting_hours_required": 0,
            "preparation_summary": "Preparo simulado",
            "total_exams_scheduled": 1,
            "scheduled_exams": result["matched_exams"],
            "unmatched_terms": result["unmatched_terms"],
        }

    state = {}

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
            resolved = rag(extracted["exams"])
            if not resolved["matched_exams"]:
                raise AgentPipelineExecutionError("No exams resolved")
            state["resolved_exams"] = resolved
            state["booking_result"] = api(resolved)
            yield {"mock": True}

    monkeypatch.setattr(cli.importlib, "import_module", lambda _name: SimpleNamespace(build_pipeline=lambda: object()))
    monkeypatch.setitem(sys.modules, "google", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "google.adk", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "google.adk.runners", SimpleNamespace(Runner=FakeRunner))
    monkeypatch.setitem(sys.modules, "google.adk.sessions", SimpleNamespace(InMemorySessionService=FakeSessions))
    monkeypatch.setitem(sys.modules, "google.genai", SimpleNamespace(types=SimpleNamespace(
        Content=lambda **kwargs: kwargs, Part=lambda **kwargs: kwargs,
    )))

    result = asyncio.run(execute_pipeline("mocked.agent", input_path))
    assert result["status"] == "PARTIAL"
    assert result["scheduled_exams"] == [accepted]
    assert result["unmatched_terms"] == ["ilegível"]
    assert calls == {"ocr": 1, "rag": 1, "api": 1}

    mode["value"] = "unmatched"
    with pytest.raises(AgentPipelineExecutionError):
        asyncio.run(execute_pipeline("mocked.agent", input_path))
    assert calls["api"] == 1

    mode["value"] = "unsafe"
    with pytest.raises(GuardrailViolation):
        asyncio.run(execute_pipeline("mocked.agent", input_path))
    assert calls["rag"] == 2 and calls["api"] == 1
