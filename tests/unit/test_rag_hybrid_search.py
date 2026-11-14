"""Mock-only ranking, E5 prefix, and synthetic seed checks."""

import json
import sys
from types import SimpleNamespace

import pytest

from services.mcp_rag.hybrid_search import (
    SEARCH_SQL, EmbeddingUnavailable, embed_texts, load_embedder,
    search_exam_catalog_hybrid,
)
from services.mcp_rag.seeder import CATALOG, seed_catalog


class FakeEmbedder:
    def __init__(self):
        self.inputs = []

    def embed(self, texts):
        self.inputs.extend(texts)
        return [[2.0] + [0.0] * 383 for _ in texts]


class FakeConnection:
    def __init__(self, results=None):
        self.results = results or []
        self.executed = []
        self.codes = set()
        self.committed = 0

    def __enter__(self):
        return self

    def __exit__(self, kind, *_):
        if not kind:
            self.committed += 1

    def cursor(self):
        return FakeCursor(self)

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "INSERT INTO rag.exams_catalog" in sql:
            self.codes.add(params[0])

    def fetchall(self):
        if self.executed[-1][0].startswith("SELECT tuss_code FROM"):
            return [(code,) for code in self.codes]
        return self.results.pop(0)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_):
        pass


def test_search_hybrid_threshold_and_parameterization():
    connection = FakeConnection([
        [("MOCK-0001", "Hemograma Completo (simulado)", "Hematologia", 0, 0.91)],
        [("MOCK-0002", "Glicemia (simulado)", "Bioquímica", 0, 0.44)],
    ])
    embedder = FakeEmbedder()
    result = search_exam_catalog_hybrid(["Hemo completo", "desconhecido"],
                                        connection=connection, embedder=embedder)
    assert [item["tuss_code"] for item in result["matched_exams"]] == ["MOCK-0001"]
    assert result["unmatched_terms"] == ["desconhecido"]
    assert embedder.inputs == ["query: Hemo completo", "query: desconhecido"]
    assert "<=>" in SEARCH_SQL and "word_similarity" in SEARCH_SQL and "similarity(" in SEARCH_SQL
    assert connection.executed[0][1][1:] == ("Hemo completo", "Hemo completo", 3)
    assert connection.executed[0][1][0].startswith("[1.0,0.0")


def test_catalog_has_150_distinct_mock_exams_and_idempotent_seed():
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    assert len(catalog) >= 125
    assert len({row["official_name"] for row in catalog}) == len(catalog)
    assert [row["tuss_code"] for row in catalog] == [f"MOCK-{n:04d}" for n in range(1, len(catalog) + 1)]
    connection, embedder = FakeConnection(), FakeEmbedder()
    assert seed_catalog(connection, embedder) == len(catalog)
    assert embedder.inputs[0].startswith("passage: Hemograma")
    assert seed_catalog(connection, embedder) == 0
    assert len(embedder.inputs) == len(catalog)
    assert connection.committed == 2


def test_required_model_failure_is_actionable_and_does_not_fallback(monkeypatch):
    def unavailable(*, model_name):
        assert model_name == "intfloat/multilingual-e5-small"
        raise ValueError("unknown model")

    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=unavailable))
    with pytest.raises(EmbeddingUnavailable, match="384-dimensional ONNX model"):
        load_embedder()


def test_reject_more_than_50_query_terms():
    with pytest.raises(ValueError, match="exam_queries must contain at most 50 items"):
        search_exam_catalog_hybrid(["x"] * 51, connection=None, embedder=None)


def test_reject_query_term_over_200_characters():
    with pytest.raises(ValueError, match="exam_queries terms must be at most 200 characters"):
        search_exam_catalog_hybrid(["x" * 201], connection=None, embedder=None)


def test_reject_bad_embedding_and_queries():
    with pytest.raises(ValueError, match="384"):
        embed_texts(type("WrongEmbedder", (), {"embed": lambda self, _: [[1.0]]})(), ["x"], "query")
    with pytest.raises(ValueError, match="nonempty"):
        search_exam_catalog_hybrid([""], connection=FakeConnection(), embedder=FakeEmbedder())
    with pytest.raises(ValueError, match="top_k"):
        search_exam_catalog_hybrid(["x"], top_k=0, connection=FakeConnection(), embedder=FakeEmbedder())
