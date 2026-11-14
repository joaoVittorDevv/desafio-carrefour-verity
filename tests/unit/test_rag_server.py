"""RAG SSE startup and health, with every external boundary mocked."""
import importlib
import sys
from types import ModuleType, SimpleNamespace

import pytest


def test_rag_server_seed_search_health_with_mocks(monkeypatch):
    class FakeMCP:
        def __init__(self, *args, **kwargs):
            assert kwargs["port"] == 8002

        def tool(self):
            return lambda function: function

        def sse_app(self):
            return SimpleNamespace(router=SimpleNamespace(routes=[]))

    mcp_module = ModuleType("mcp.server.fastmcp")
    mcp_module.FastMCP = FakeMCP
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", mcp_module)
    monkeypatch.setitem(sys.modules, "starlette.responses", SimpleNamespace(
        JSONResponse=lambda body, status_code=200: (body, status_code)))
    monkeypatch.setitem(sys.modules, "starlette.routing", SimpleNamespace(
        Route=lambda path, endpoint, methods: (path, methods)))
    sys.modules.pop("services.mcp_rag.server", None)
    server = importlib.import_module("services.mcp_rag.server")
    assert server.app.router.routes == [("/health", ["GET"])]

    class FakeConnection:
        def __init__(self, count=150):
            self.count = count
            self.closed = False

        def close(self):
            self.closed = True

        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def execute(self, sql):
            assert "rag.exams_catalog" in sql

        def fetchone(self):
            return (self.count,)

    connection = FakeConnection()
    monkeypatch.setattr(server, "connect", lambda: connection)
    with pytest.raises(RuntimeError, match="not initialized"):
        server.search_exam_catalog_hybrid(["Hemograma"])
    server._embedder = object()
    monkeypatch.setattr(server, "search", lambda queries, top_k, **kwargs: {
        "matched_exams": [], "unmatched_terms": queries,
    })
    assert server.search_exam_catalog_hybrid(["Termo fictício"]) == {
        "matched_exams": [], "unmatched_terms": ["Termo fictício"],
    }
    assert connection.closed

    import asyncio
    assert asyncio.run(server.health(None))[1] == 200
    monkeypatch.setattr(server, "connect", lambda: FakeConnection(5))
    assert asyncio.run(server.health(None))[1] == 503

    monkeypatch.setattr(server, "connect", lambda: FakeConnection(150))
    monkeypatch.setattr(server, "load_embedder", lambda: "fake-vectorizer")
    seeds = []
    monkeypatch.setattr(server, "seed_catalog", lambda connection, embedder: seeds.append(embedder))
    uvicorn = ModuleType("uvicorn")
    uvicorn.run = lambda app, **kwargs: seeds.append(kwargs["port"])
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    server.main()
    assert seeds == ["fake-vectorizer", 8002]
    sys.modules.pop("services.mcp_rag.server", None)
