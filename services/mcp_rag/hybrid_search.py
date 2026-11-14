"""Hybrid cosine/trigram reconciliation against the synthetic exam catalog."""

import math

MODEL_NAME = "intfloat/multilingual-e5-small"
DIMENSIONS = 384
THRESHOLD = 0.80  # calibrated against the quantized E5 ONNX export: legit terms >=0.814, garbage <=0.791


class EmbeddingUnavailable(RuntimeError):
    """The required E5 model is not available in the local FastEmbed installation."""


def _register_custom_e5():
    """fastembed does not ship e5-small in its registry: register the community ONNX export."""
    from fastembed import TextEmbedding
    from fastembed.common.model_description import ModelSource, PoolingType

    TextEmbedding.add_custom_model(
        model=MODEL_NAME,
        pooling=PoolingType.MEAN,
        normalization=True,
        sources=ModelSource(hf="Xenova/multilingual-e5-small"),
        dim=DIMENSIONS,
        model_file="onnx/model_quantized.onnx",
        description="multilingual-e5-small ONNX quantized export (MEAN pooling + L2)",
        license="apache-2.0",
        size_in_gb=0.13,
    )


def load_embedder():
    from fastembed import TextEmbedding

    try:
        return TextEmbedding(model_name=MODEL_NAME)
    except (ValueError, RuntimeError):
        pass
    try:
        _register_custom_e5()
        return TextEmbedding(model_name=MODEL_NAME)
    except (ValueError, RuntimeError, ImportError) as exc:
        raise EmbeddingUnavailable(
            f"FastEmbed cannot load {MODEL_NAME}; install/register a compatible local "
            "384-dimensional ONNX model before starting mcp-rag"
        ) from exc


def embed_texts(embedder, texts, prefix):
    vectors = list(embedder.embed([f"{prefix}: {text}" for text in texts]))
    if len(vectors) != len(texts):
        raise ValueError("Embedding count does not match input count")
    normalized = []
    for vector in vectors:
        if len(vector) != DIMENSIONS:
            raise ValueError(f"Expected {DIMENSIONS}-dimensional E5 embeddings")
        norm = math.sqrt(sum(float(value) ** 2 for value in vector))
        if not math.isfinite(norm) or norm == 0:
            raise ValueError("Invalid embedding norm")
        normalized.append([float(value) / norm for value in vector])
    return normalized


SEARCH_SQL = """
SELECT tuss_code, official_name, category, fasting_hours,
       GREATEST(0.0, LEAST(1.0,
         0.65 * (1 - (embedding <=> %s::vector)) +
         0.35 * GREATEST(similarity(%s, official_name),
                          word_similarity(%s, synonyms)))) AS score
FROM rag.exams_catalog
ORDER BY score DESC, tuss_code
LIMIT %s
"""


def search_exam_catalog_hybrid(exam_queries, top_k=3, *, connection, embedder):
    """Return best matches and explicitly unresolved terms; never guess a code."""
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1 or top_k > 20:
        raise ValueError("top_k must be between 1 and 20")
    if not isinstance(exam_queries, list) or any(
        not isinstance(term, str) or not term.strip() for term in exam_queries
    ):
        raise ValueError("exam_queries must be a list of nonempty strings")
    if len(exam_queries) > 50:
        raise ValueError("exam_queries must contain at most 50 items")
    if any(len(term) > 200 for term in exam_queries):
        raise ValueError("exam_queries terms must be at most 200 characters")
    if not exam_queries:
        return {"matched_exams": [], "unmatched_terms": []}
    vectors = embed_texts(embedder, exam_queries, "query")
    matched, unmatched = [], []
    with connection.cursor() as cursor:
        for term, vector in zip(exam_queries, vectors, strict=True):
            # pgvector accepts a textual vector literal; parameters remain bound.
            literal = "[" + ",".join(str(value) for value in vector) + "]"
            cursor.execute(SEARCH_SQL, (literal, term, term, top_k))
            rows = cursor.fetchall()
            if not rows or rows[0][4] < THRESHOLD:
                unmatched.append(term)
                continue
            code, name, category, fasting, score = rows[0]
            matched.append({
                "original_query": term,
                "tuss_code": code,
                "official_name": name,
                "category": category,
                "fasting_hours": fasting,
                "confidence_score": float(score),
            })
    return {"matched_exams": matched, "unmatched_terms": unmatched}
