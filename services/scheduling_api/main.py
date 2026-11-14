"""Registration API: no patient identity or collection slot is persisted."""

import secrets
import string
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, status

from services.scheduling_api import schemas
from services.scheduling_api.database import get_connection
from services.scheduling_api.schemas import AppointmentCreateRequest, AppointmentCreateResponse

app = FastAPI(title="Synthetic Laboratory Registration API", version="1.0.0")
PREPARATION = "Preparo simulado; confirme as orientações com a unidade."


def new_protocol_id() -> str:
    """Digit-run-free on purpose: BR PII regexes (RG/PHONE/CNS) must never match a protocol."""
    suffix = "".join(secrets.choice(string.ascii_uppercase) for _ in range(10))
    return f"LAB-{datetime.now(timezone.utc):%Y-%m-%d}-{suffix}"


@app.get("/health", operation_id="health_check", summary="Check scheduling database", description="Checks connectivity to the scheduling schema.")
def health_check(connection=Depends(get_connection)):
    try:
        if schemas.local_analyzer() is None:
            raise RuntimeError("Privacy detector unavailable")
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM scheduling.appointments LIMIT 0")
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Scheduling database unavailable") from exc


@app.post(
    "/api/v1/appointments", response_model=AppointmentCreateResponse,
    status_code=status.HTTP_201_CREATED, operation_id="create_exam_appointment",
    summary="Register resolved synthetic laboratory exams",
    description="Atomically registers only complete matched orders. No collection time is reserved.",
)
def create_exam_appointment(request: AppointmentCreateRequest, connection=Depends(get_connection)):
    protocol = new_protocol_id()
    state = "CONFIRMED"
    # A single transaction commits the parent, all items and unmatched terms together.
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO scheduling.appointments
                   (protocol_id, preferred_unit, status, unmatched_terms, notes)
                   VALUES (%s, %s, %s, %s, %s) RETURNING created_at""",
                (protocol, request.preferred_unit, state, request.unmatched_terms,
                 request.clinical_observations),
            )
            created_at = cursor.fetchone()[0]
            for exam in request.exams:
                cursor.execute(
                    """INSERT INTO scheduling.appointment_items
                       (protocol_id, tuss_code, official_name, original_query,
                        category, confidence_score, fasting_hours)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (protocol, exam.tuss_code, exam.official_name, exam.original_query,
                     exam.category, exam.confidence_score, exam.fasting_hours),
                )
    return AppointmentCreateResponse(
        protocol_id=protocol, status=state, scheduled_at=created_at,
        preferred_unit=request.preferred_unit,
        max_fasting_hours_required=max(exam.fasting_hours for exam in request.exams),
        preparation_summary=PREPARATION, total_exams_scheduled=len(request.exams),
        scheduled_exams=request.exams, unmatched_terms=request.unmatched_terms,
    )


@app.get(
    "/api/v1/appointments/{protocol_id}", response_model=AppointmentCreateResponse,
    operation_id="get_exam_appointment", summary="Retrieve a registration",
    description="Returns registered items and unresolved terms; scheduled_at is registration time only.",
)
def get_exam_appointment(protocol_id: str, connection=Depends(get_connection)):
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT protocol_id, status, created_at, preferred_unit, unmatched_terms
               FROM scheduling.appointments WHERE protocol_id = %s""", (protocol_id,),
        )
        appointment = cursor.fetchone()
        if appointment is None:
            raise HTTPException(status_code=404, detail="Protocol not found")
        cursor.execute(
            """SELECT original_query, tuss_code, official_name, category,
                      fasting_hours, confidence_score
               FROM scheduling.appointment_items WHERE protocol_id = %s ORDER BY id""",
            (protocol_id,),
        )
        exams = [dict(zip(("original_query", "tuss_code", "official_name", "category",
                           "fasting_hours", "confidence_score"), row, strict=True))
                 for row in cursor.fetchall()]
    return AppointmentCreateResponse(
        protocol_id=appointment[0], status=appointment[1], scheduled_at=appointment[2],
        preferred_unit=appointment[3], unmatched_terms=appointment[4],
        scheduled_exams=exams, total_exams_scheduled=len(exams),
        max_fasting_hours_required=max((exam["fasting_hours"] for exam in exams), default=0),
        preparation_summary=PREPARATION,
    )
