"""MCP OCR over SSE; tool responses never carry unsanitized OCR text."""

import re

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.routing import Route

from security.pii_guardrail import GuardrailViolation, assert_no_pii, local_analyzer, redact_pii
from services.mcp_ocr.ocr_engine import extract_text


mcp = FastMCP("mcp-ocr", host="0.0.0.0", port=8001)

EXAM_HEADING = re.compile(r"^\s*Solicito\s*:\s*$", re.IGNORECASE)
NUMBERED_EXAM = re.compile(r"^\s*\d+[.)]\s*[^\W\d_]{2,}", re.UNICODE)


@mcp.tool()
def extract_and_anonymize_medical_order(file_path: str) -> dict:
    try:
        raw, confidence, name = extract_text(file_path)
        if confidence < 0.8:
            raise GuardrailViolation()
        lines = raw.splitlines()
        if not any(
            EXAM_HEADING.match(line) and any(NUMBERED_EXAM.match(item) for item in lines[index + 1:])
            for index, line in enumerate(lines)
        ):
            raise GuardrailViolation()
        analyzer = local_analyzer()
        if analyzer is None:
            raise GuardrailViolation()
        result = redact_pii(raw, analyzer)
        # Filename is operator-chosen, not extracted text: check deterministic patterns only
        # (NER false-positives on ordinary Portuguese words like "pedido medico teste").
        assert_no_pii(name, None)
        return {
            "file_name": name,
            "ocr_confidence": confidence,
            "sanitized_text": result.sanitized_text,
            "pii_redaction_report": {
                "total_redacted": result.total_redacted,
                "entities_found": result.entities_found,
            },
        }
    except Exception:
        raise GuardrailViolation() from None


async def health(request):
    try:
        ready = local_analyzer() is not None
    except GuardrailViolation:
        ready = False
    return JSONResponse({"status": "ok"} if ready else {"status": "unavailable"},
                        status_code=200 if ready else 503)


app = mcp.sse_app()
app.router.routes.insert(0, Route("/health", health, methods=["GET"]))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
