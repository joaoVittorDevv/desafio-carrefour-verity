"""Local PII redaction. Never include input text in a security exception."""

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


BLOCK_MESSAGE = "Privacy check failed; nothing was registered. Try a clearer image."


class SecurityGuardrailViolationError(Exception):
    def __init__(self):
        super().__init__(BLOCK_MESSAGE)


GuardrailViolation = SecurityGuardrailViolationError


_PATTERNS = (
    ("EMAIL_ADDRESS", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
    ("CRM", re.compile(r"\bCRM(?:-[A-Z]{2})?[\s:]*\d{4,7}\b", re.I)),
    ("CPF", re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")),
    ("CNS", re.compile(r"(?<![\d.])(?:\d[ .-]?){14}\d\b")),
    ("RG", re.compile(r"\b\d{1,2}\.?\d{3}\.?\d{3}-?[0-9Xx]\b")),
    ("PHONE_NUMBER", re.compile(r"(?<!\w)(?:\+55[\s-]?)?(?:\(\d{2}\)|\d{2})?[\s-]?(?:9\d{4}|\d{4})-?\d{4}\b")),
    # Capture stops at backslash too: JSON-escaped payloads use literal \n where raw
    # text has line breaks, and the placeholder check must still see clean boundaries.
    ("PERSON", re.compile(r'\b(?:Paciente|Nome|Solicitante|Dr\.?|Dra\.?|Médico\(?a\)?)\s*:\s*([^\n|;\\"]+?)(?=\s+(?:CPF|RG|Tel|CRM|E-mail)\s*:|\s+-\s+CRM|[\n|;"]|\\n|$)', re.I)),
    ("PERSON", re.compile(r"\b(?:Dr\.?|Dra\.?)\s+([A-ZÀ-Ý][\wÀ-ÿ]+(?:\s+[A-ZÀ-Ý][\wÀ-ÿ]+)+)", re.I)),
)
_MASK = {"EMAIL_ADDRESS": "EMAIL", "PHONE_NUMBER": "PHONE"}

# Exam catalog guards the NER: pt_core_news_sm flags exam names as PERSON.
_EXAMS_CATALOG = Path(__file__).resolve().parents[1] / "services" / "mcp_rag" / "data" / "tuss_exams_catalog.json"


def _normalize(text: str) -> str:
    stripped = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in stripped if not unicodedata.combining(ch)).lower()


@lru_cache(maxsize=1)
def _exam_term_tokens() -> frozenset[str]:
    import json

    tokens: set[str] = set()
    try:
        records = json.loads(_EXAMS_CATALOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset(tokens)
    for record in records:
        names = [record["official_name"].replace("(simulado)", "")]
        names.append(record.get("category", ""))
        names.extend(record.get("synonyms", "").split(";"))
        for name in names:
            tokens.update(word for word in _normalize(name).split() if len(word) > 3)
    return frozenset(tokens)


def _looks_like_exam(text: str) -> bool:
    """True when most alphabetic tokens belong to catalog exam terms."""
    words = [word for word in _normalize(text).split() if word.isalpha()]
    if not words:
        return False
    vocabulary = _exam_term_tokens()
    hits = sum(1 for word in words if word in vocabulary)
    return hits / len(words) >= 0.5


@dataclass(frozen=True)
class RedactionResult:
    sanitized_text: str
    total_redacted: int
    entities_found: list[str]


def _spans(text: str, analyzer=None):
    spans = [(match.start(1) if entity == "PERSON" else match.start(),
              match.end(1) if entity == "PERSON" else match.end(), entity)
             for entity, pattern in _PATTERNS for match in pattern.finditer(text)
             if not (entity == "PERSON" and match.group(1).strip() == "[REDACTED_PERSON]")]
    if analyzer is not None:
        # AnalyzerEngine must be configured with a *local* pt_core_news_sm model by the caller.
        for item in analyzer.analyze(text=text, language="pt", entities=["PERSON"]):
            if _looks_like_exam(text[item.start:item.end]):
                continue
            spans.append((item.start, item.end, "PERSON"))
    return sorted(spans, key=lambda span: (span[0], -(span[1] - span[0])))


def redact_pii(text: str, analyzer=None) -> RedactionResult:
    if not isinstance(text, str) or not text.strip():
        raise SecurityGuardrailViolationError()
    try:
        output, found, end = [], [], 0
        for start, stop, entity in _spans(text, analyzer):
            if start < end or stop <= start:
                continue
            output.extend((text[end:start], f"[REDACTED_{_MASK.get(entity, entity)}]"))
            found.append(entity)
            end = stop
        output.append(text[end:])
        sanitized = "".join(output)
        # Overlapping recognizers must not leave a document or contact fragment behind.
        if _spans(sanitized, analyzer):
            raise SecurityGuardrailViolationError()
        return RedactionResult(sanitized, len(found), list(dict.fromkeys(found)))
    except Exception:
        raise SecurityGuardrailViolationError() from None


def assert_no_pii(text: str, analyzer=None) -> None:
    if not isinstance(text, str):
        raise SecurityGuardrailViolationError()
    try:
        if _spans(text, analyzer):
            raise SecurityGuardrailViolationError()
    except Exception:
        raise SecurityGuardrailViolationError() from None


@lru_cache(maxsize=1)
def local_analyzer():
    """Use Presidio only when its Portuguese spaCy model is installed locally."""
    try:
        import spacy
        from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        if not spacy.util.is_package("pt_core_news_sm"):
            return None
        engine = NlpEngineProvider(nlp_configuration={"nlp_engine_name": "spacy", "models": [{"lang_code": "pt", "model_name": "pt_core_news_sm"}]}).create_engine()
        registry = RecognizerRegistry(supported_languages=["pt"])
        registry.load_predefined_recognizers(languages=["pt"])
        return AnalyzerEngine(nlp_engine=engine, registry=registry, supported_languages=["pt"])
    except ImportError:
        return None
    except Exception:
        raise SecurityGuardrailViolationError() from None
