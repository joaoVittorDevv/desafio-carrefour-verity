"""API contract and transaction tests with an in-memory PostgreSQL stand-in."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from services.scheduling_api import schemas
from services.scheduling_api.database import get_connection
from services.scheduling_api.main import app


class FakeConnection:
    def __init__(self):
        self.appointments = {}
        self.items = []
        self.fail_item = False
        self.rollbacks = 0

    def __enter__(self):
        self._snapshot = (self.appointments.copy(), self.items.copy())
        return self

    def __exit__(self, kind, *_):
        if kind:
            self.appointments, self.items = self._snapshot
            self.rollbacks += 1

    def cursor(self):
        return FakeCursor(self)


class FakeCursor:
    def __init__(self, connection):
        self.db = connection
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def execute(self, sql, args=()):
        if "INSERT INTO scheduling.appointments" in sql:
            protocol, unit, state, unmatched, notes = args
            created = datetime(2026, 11, 12, 19, 45, tzinfo=timezone.utc)
            self.db.appointments[protocol] = (protocol, state, created, unit, unmatched, notes)
            self.result = [(created,)]
        elif "INSERT INTO scheduling.appointment_items" in sql:
            if self.db.fail_item:
                raise RuntimeError("insert failed")
            self.db.items.append(args)
        elif "FROM scheduling.appointment_items" in sql:
            self.result = [
                (item[3], item[1], item[2], item[4], item[6], item[5])
                for item in self.db.items if item[0] == args[0]
            ]
        elif "FROM scheduling.appointments WHERE" in sql:
            row = self.db.appointments.get(args[0])
            self.result = [row[:5]] if row else []
        else:
            self.result = []

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return self.result


@pytest.fixture
def api(monkeypatch):
    # Simulate the installed Portuguese PERSON recognizer without loading any model.
    monkeypatch.setattr(schemas, "local_analyzer", lambda: SimpleNamespace(analyze=lambda **kw: [
        SimpleNamespace(entity_type="PERSON")
    ] if "Carlos Eduardo Albuquerque" in kw["text"] else []))
    db = FakeConnection()
    app.dependency_overrides[get_connection] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, db
    app.dependency_overrides.clear()


@pytest.fixture
def payload():
    return {
        "preferred_unit": "Unidade Central - Matriz",
        "exams": [{"original_query": "Hemo completo", "tuss_code": "MOCK-0001",
                   "official_name": "Hemograma Completo (simulado)",
                   "category": "Hematologia", "fasting_hours": 0,
                   "confidence_score": 0.98}],
        "unmatched_terms": [],
        "clinical_observations": "Pedido fictício processado com PII anonimizada",
    }


def test_partial_post_rejected_but_historical_get_supported(api, payload):
    client, db = api
    payload["unmatched_terms"] = ["Exame ilegível"]
    assert client.post("/api/v1/appointments", json=payload).status_code == 422
    assert not db.appointments and not db.items
    protocol = "LAB-2026-11-12-ABCDEFGHIJ"
    db.appointments[protocol] = (protocol, "PARTIAL", datetime(2026, 11, 12, tzinfo=timezone.utc),
                                 payload["preferred_unit"], payload["unmatched_terms"], None)
    exam = payload["exams"][0]
    db.items.append((protocol, exam["tuss_code"], exam["official_name"], exam["original_query"],
                     exam["category"], exam["confidence_score"], exam["fasting_hours"]))
    historical = client.get(f"/api/v1/appointments/{protocol}")
    assert historical.status_code == 200
    assert historical.json()["status"] == "PARTIAL"
    assert historical.json()["unmatched_terms"] == payload["unmatched_terms"]


def test_confirmed_and_not_found(api, payload):
    client, _ = api
    payload["unmatched_terms"] = []
    result = client.post("/api/v1/appointments", json=payload)
    assert result.status_code == 201
    assert result.json()["status"] == "CONFIRMED"
    assert client.get("/api/v1/appointments/LAB-NOT-FOUND").status_code == 404


@pytest.mark.parametrize("field,value", [
    ("preferred_unit", "Paciente: Ana Silva"),
    ("clinical_observations", "CPF: 123.456.789-00"),
    ("unmatched_terms", ["Dra.: Ana Silva"]),
    ("unmatched_terms", ["CNS: 123456789012345"]),
    ("unmatched_terms", ["Carlos Eduardo Albuquerque"]),
    ("original_query", "Dra. Ana Silva"),
    ("original_query", "ana@example.com"),
    ("official_name", "CRM-SP 145892"),
    ("category", "(11) 98765-4321"),
    ("tuss_code", "123.456.789-00"),
])
def test_reject_pii_in_every_text_field(api, payload, field, value):
    client, db = api
    if field in payload:
        payload[field] = value
    else:
        payload["exams"][0][field] = value
    assert client.post("/api/v1/appointments", json=payload).status_code == 422
    assert not db.appointments


def test_privacy_detector_missing_rejects_request_and_readiness(api, payload, monkeypatch):
    client, db = api
    monkeypatch.setattr(schemas, "local_analyzer", lambda: None)
    assert client.get("/health").status_code == 503
    assert client.post("/api/v1/appointments", json=payload).status_code == 422
    assert not db.appointments


def test_empty_exams_rejected(api, payload):
    client, db = api
    payload["exams"] = []
    assert client.post("/api/v1/appointments", json=payload).status_code == 422
    assert not db.appointments


def test_insert_failure_rolls_back_protocol_and_items(api, payload):
    client, db = api
    db.fail_item = True
    assert client.post("/api/v1/appointments", json=payload).status_code == 500
    assert db.rollbacks == 1
    assert not db.appointments and not db.items


def test_openapi_contract(api):
    client, _ = api
    spec = client.get("/openapi.json").json()
    post = spec["paths"]["/api/v1/appointments"]["post"]
    assert post["operationId"] == "create_exam_appointment"
    assert post["responses"]["201"]["content"]["application/json"]["schema"]
    fields = spec["components"]["schemas"]["AppointmentCreateResponse"]["properties"]
    assert {"scheduled_exams", "unmatched_terms", "preparation_summary", "scheduled_at"} <= fields.keys()
    assert client.get("/docs").status_code == 200


def test_no_pii_allows_catalog_exam_names_but_rejects_person(monkeypatch):
    """The API validator shares the OCR guardrail's exam-lexicon defense."""
    from services.scheduling_api import schemas

    class FakeSpan:
        def __init__(self, start, end):
            self.start, self.end = start, end

    class FlaggingAnalyzer:
        def analyze(self, text, language, entities):
            return [FakeSpan(0, len(text))]

    monkeypatch.setattr(schemas, "local_analyzer", lambda: FlaggingAnalyzer())
    assert schemas.no_pii("Hemograma Completo (simulado)") == "Hemograma Completo (simulado)"
    assert schemas.no_pii("Endocrinologia") == "Endocrinologia"
    import pytest as _pytest
    with _pytest.raises(ValueError):
        schemas.no_pii("Carlos Eduardo Albuquerque")


def test_generated_protocol_never_triggers_brazilian_pii_regexes():
    from security.pii_guardrail import assert_no_pii
    from services.scheduling_api.main import new_protocol_id

    for _ in range(100):
        assert_no_pii(new_protocol_id())
