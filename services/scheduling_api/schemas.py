"""Public OpenAPI request and response contracts for synthetic registrations."""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from security.pii_guardrail import _looks_like_exam, local_analyzer

PII_PATTERNS = (
    re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"),
    re.compile(r"\b\d{1,2}\.\d{3}\.\d{3}-?[0-9Xx]\b"),
    re.compile(r"\bCRM(?:-[A-Z]{2})?[\s:]*\d{4,7}\b", re.I),
    re.compile(r"\b(?:\+55\s?)?(?:\(?\d{2}\)?[\s-]?)?(?:9\d{4}|\d{4})-?\d{4}\b"),
    re.compile(r"\b(?:\d[ .-]?){14}\d\b"),
    re.compile(r"\b(?:Dr\.?|Dra\.?)\s+[A-ZÀ-Ý][\wÀ-ÿ]+(?:\s+[A-ZÀ-Ý][\wÀ-ÿ]+)+", re.I),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    re.compile(r"\b(?:paciente|nome|solicitante|dr\.?|dra\.?|m[eé]dic[oa])\s*:\s*(?!\[REDACTED_[A-Z]+\])\S+", re.I),
    re.compile(r"\b(?:CPF|RG|CNS|telefone|celular|tel)\s*:\s*(?!\[REDACTED_[A-Z]+\])\S+", re.I),
)


def no_pii(value: str) -> str:
    if any(pattern.search(value) for pattern in PII_PATTERNS):
        raise ValueError("Sensitive personal data is not accepted")
    try:
        analyzer = local_analyzer()
        if analyzer is None:
            raise ValueError("Sensitive personal data is not accepted or privacy check is unavailable")
        # pt_core_news_sm flags exam names and lab categories as PERSON; the shared
        # catalog lexicon guard (same defense as the OCR guardrail) keeps them flowing.
        for item in analyzer.analyze(text=value, language="pt", entities=["PERSON"]):
            if _looks_like_exam(value[item.start:item.end]):
                continue
            raise ValueError("Sensitive personal data is not accepted or privacy check is unavailable")
    except ValueError:
        raise
    except Exception:
        raise ValueError("Sensitive personal data is not accepted or privacy check is unavailable") from None
    return value


class ResolvedExamItem(BaseModel):
    original_query: str = Field(min_length=1, description="OCR term, with PII removed", examples=["Hemograma Completo"])
    tuss_code: str = Field(pattern=r"^MOCK-\d{4}$", description="Fictitious identifier, not an official TUSS code", examples=["MOCK-0001"])
    official_name: str = Field(min_length=1, description="Synthetic catalog name", examples=["Hemograma Completo (simulado)"])
    category: str = Field(min_length=1, description="Laboratory category", examples=["Hematologia"])
    fasting_hours: int = Field(ge=0, description="Simulated fasting hours", examples=[0])
    confidence_score: float = Field(ge=0, le=1, description="Hybrid retrieval score", examples=[0.98])

    @field_validator("original_query", "tuss_code", "official_name", "category")
    @classmethod
    def reject_pii(cls, value: str) -> str:
        return no_pii(value)


class AppointmentCreateRequest(BaseModel):
    preferred_unit: str = Field(default="Unidade Central - Matriz", min_length=1, description="Requested unit; no time slot is reserved", examples=["Unidade Central - Matriz"])
    exams: list[ResolvedExamItem] = Field(min_length=1, description="Only reconciled synthetic exams", examples=[[{"original_query": "Hemograma Completo", "tuss_code": "MOCK-0001", "official_name": "Hemograma Completo (simulado)", "category": "Hematologia", "fasting_hours": 0, "confidence_score": 0.98}]])
    unmatched_terms: list[str] = Field(default_factory=list, max_length=0, description="Only complete orders may be registered", examples=[[]])
    clinical_observations: str | None = Field(default=None, description="Anonymized notes only", examples=["Pedido fictício processado com PII anonimizada"])

    @field_validator("preferred_unit", "clinical_observations")
    @classmethod
    def reject_pii(cls, value: str | None) -> str | None:
        return no_pii(value) if value is not None else None

    @field_validator("unmatched_terms")
    @classmethod
    def reject_unmatched_pii(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("Unmatched terms must not be empty")
        return [no_pii(value) for value in values]


class AppointmentCreateResponse(BaseModel):
    protocol_id: str = Field(description="Unique registration protocol", examples=["LAB-2026-11-12-ABCDEFGHIJ"])
    status: Literal["CONFIRMED", "PARTIAL"] = Field(description="PARTIAL when unmatched terms exist", examples=["PARTIAL"])
    scheduled_at: datetime = Field(description="Registration time, not a reserved collection time", examples=["2026-11-12T19:45:00Z"])
    preferred_unit: str = Field(description="Requested unit", examples=["Unidade Central - Matriz"])
    max_fasting_hours_required: int = Field(description="Largest simulated fasting duration", examples=[0])
    preparation_summary: str = Field(description="Fictitious preparation; confirm with unit", examples=["Preparo simulado; confirme as orientações com a unidade."])
    total_exams_scheduled: int = Field(description="Number of registered items", examples=[1])
    scheduled_exams: list[ResolvedExamItem] = Field(description="Full metadata for registered items")
    unmatched_terms: list[str] = Field(description="Unregistered terms", examples=[["Exame ilegível"]])
