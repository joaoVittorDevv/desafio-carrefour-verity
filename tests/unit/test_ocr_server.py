import importlib
import sys
from types import ModuleType, SimpleNamespace

import pytest

from security import GuardrailViolation


@pytest.fixture
def server(monkeypatch):
    class FakeMCP:
        def __init__(self, *args, **kwargs):
            assert kwargs["port"] == 8001

        def tool(self):
            return lambda function: function

        def sse_app(self):
            return SimpleNamespace(router=SimpleNamespace(routes=["/sse", "/messages"]))

    fake_mcp = ModuleType("mcp.server.fastmcp")
    fake_mcp.FastMCP = FakeMCP
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fake_mcp)
    fake_responses = ModuleType("starlette.responses")
    fake_responses.JSONResponse = lambda body, status_code=200: {**body, "status_code": status_code}
    monkeypatch.setitem(sys.modules, "starlette.responses", fake_responses)
    fake_routing = ModuleType("starlette.routing")
    fake_routing.Route = lambda path, endpoint, methods: SimpleNamespace(path=path, endpoint=endpoint, methods=methods)
    monkeypatch.setitem(sys.modules, "starlette.routing", fake_routing)
    sys.modules.pop("services.mcp_ocr.server", None)
    server = importlib.import_module("services.mcp_ocr.server")
    yield server
    sys.modules.pop("services.mcp_ocr.server", None)


def test_server_sse_health_and_sanitized_tool(server, monkeypatch):
    monkeypatch.setattr(server, "extract_text", lambda path: (
        "Paciente: Ana Lima\nSolicito:\n1. Hemograma Completo", 0.95, "pedido.png"))
    monkeypatch.setattr(server, "local_analyzer", lambda: SimpleNamespace(analyze=lambda **kw: []))
    result = server.extract_and_anonymize_medical_order("/app/inputs/pedido.png")
    assert result["sanitized_text"] == "Paciente: [REDACTED_PERSON]\nSolicito:\n1. Hemograma Completo"
    assert result["pii_redaction_report"]["total_redacted"] == 1
    assert server.app.router.routes[0].path == "/health"
    assert server.app.router.routes[1:] == ["/sse", "/messages"]
    import asyncio
    assert asyncio.run(server.health(None)) == {"status": "ok", "status_code": 200}
    monkeypatch.setattr(server, "extract_text", lambda path: ("Paciente: Ana Lima", 0.5, "pedido.png"))
    with pytest.raises(GuardrailViolation) as exc:
        server.extract_and_anonymize_medical_order("/app/inputs/pedido.png")
    assert "Ana Lima" not in str(exc.value)
    monkeypatch.setattr(server, "extract_text", lambda path: ("Paciente: Ana Lima", 0.95, "pedido.png"))
    monkeypatch.setattr(server, "local_analyzer", lambda: None)
    with pytest.raises(GuardrailViolation):
        server.extract_and_anonymize_medical_order("/app/inputs/pedido.png")
    assert asyncio.run(server.health(None)) == {"status": "unavailable", "status_code": 503}


@pytest.mark.parametrize("raw", ["X", "Paciente: Ana Lima\n1. Hemograma Completo",
                                 "Paciente: Ana Lima\nSolicito:\n1.  "])
def test_rejects_inadequate_high_confidence_ocr(server, monkeypatch, raw):
    monkeypatch.setattr(server, "extract_text", lambda path: (raw, 0.99, "marina__good.png"))
    monkeypatch.setattr(server, "local_analyzer", lambda: SimpleNamespace(analyze=lambda **kw: []))
    with pytest.raises(GuardrailViolation) as exc:
        server.extract_and_anonymize_medical_order("/app/inputs/marina__good.png")
    assert raw not in str(exc.value)


def test_accepts_clear_typed_order_with_masked_pii(server, monkeypatch):
    raw = "Paciente: Ana Lima\nCPF: 123.456.789-00\nSolicito:\n1. Hemograma Completo"
    monkeypatch.setattr(server, "extract_text", lambda path: (raw, 0.95, "pedido.png"))
    monkeypatch.setattr(server, "local_analyzer", lambda: SimpleNamespace(analyze=lambda **kw: []))
    result = server.extract_and_anonymize_medical_order("/app/inputs/pedido.png")
    assert result["sanitized_text"] == (
        "Paciente: [REDACTED_PERSON]\nCPF: [REDACTED_CPF]\nSolicito:\n1. Hemograma Completo"
    )
    assert result["pii_redaction_report"]["total_redacted"] == 2
