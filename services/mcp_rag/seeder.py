"""Idempotently load the explicitly fictitious laboratory catalog."""

import json
from pathlib import Path

from services.mcp_rag.hybrid_search import embed_texts

CATALOG = Path(__file__).parent / "data" / "tuss_exams_catalog.json"
INSERT_SQL = """
INSERT INTO rag.exams_catalog
    (tuss_code, official_name, synonyms, category, fasting_hours,
     preparation_instructions, turnaround_days, embedding)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector)
ON CONFLICT (tuss_code) DO NOTHING
"""


def seed_catalog(connection, embedder, catalog_path=CATALOG):
    records = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    codes = [item["tuss_code"] for item in records]
    if len(records) < 125 or len(set(codes)) != len(codes) or any(
        code != f"MOCK-{index:04d}" for index, code in enumerate(codes, 1)
    ):
        raise ValueError("Catalog must have at least 125 distinct sequential MOCK identifiers")
    with connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT tuss_code FROM rag.exams_catalog")
            existing = {row[0] for row in cursor.fetchall()}
            missing = [item for item in records if item["tuss_code"] not in existing]
            if not missing:
                return 0
            documents = [
                f"{item['official_name']}; {item['synonyms']}; {item['category']}"
                for item in missing
            ]
            vectors = embed_texts(embedder, documents, "passage")
            for item, vector in zip(missing, vectors, strict=True):
                cursor.execute(INSERT_SQL, (
                    item["tuss_code"], item["official_name"], item["synonyms"],
                    item["category"], item["fasting_hours"],
                    item["preparation_instructions"], item["turnaround_days"],
                    "[" + ",".join(str(value) for value in vector) + "]",
                ))
    return len(missing)
