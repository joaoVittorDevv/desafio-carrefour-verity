"""SSE-only MCP server for synthetic exam catalog search."""

import os
from contextlib import closing

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.routing import Route

from services.mcp_rag.hybrid_search import load_embedder, search_exam_catalog_hybrid as search
from services.mcp_rag.seeder import seed_catalog

mcp = FastMCP("synthetic-exam-rag", host="0.0.0.0", port=8002)
_embedder = None


def connect():
    import psycopg2

    return psycopg2.connect(os.environ["DB_DSN"])


@mcp.tool()
def search_exam_catalog_hybrid(exam_queries: list[str], top_k: int = 3) -> dict:
    """Resolve synthetic laboratory exam names using vector and trigram ranking."""
    if _embedder is None:
        raise RuntimeError("RAG catalog is not initialized")
    with closing(connect()) as connection:
        return search(exam_queries, top_k, connection=connection, embedder=_embedder)


async def health(request):
    try:
        with closing(connect()) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM rag.exams_catalog")
            count = cursor.fetchone()[0]
        return JSONResponse({"status": "ok", "catalog_count": count} if count >= 125 else
                            {"status": "unavailable", "catalog_count": count},
                            status_code=200 if count >= 125 else 503)
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)


app = mcp.sse_app()
app.router.routes.append(Route("/health", health, methods=["GET"]))


def main():
    global _embedder
    _embedder = load_embedder()
    with closing(connect()) as connection:
        seed_catalog(connection, _embedder)
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8002)


if __name__ == "__main__":
    main()
