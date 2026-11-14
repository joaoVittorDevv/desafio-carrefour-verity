"""Typer CLI: transpile (10), input (20), preflight (30), pipeline (40), PII (50)."""

import asyncio
import importlib
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

from PIL import Image, UnidentifiedImageError
import typer
from rich.console import Console

from transpiler.compiler import SpecTranspilationError, load_spec, transpile as compile_to_file

app = typer.Typer()
console = Console()
DEFAULT_SPEC = Path("specs/clinical_agent_spec.json")


class InputImageError(Exception):
    pass


class InfrastructureUnavailableError(Exception):
    pass


class AgentPipelineExecutionError(Exception):
    pass


def _fail(code: int, message: str):
    console.print(f"[red]{message}[/red]")
    raise typer.Exit(code)


def _health_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))


def check_preflight(spec):
    failed = []
    if not os.environ.get("GEMINI_API_KEY"):
        failed.append("GEMINI_API_KEY missing")
    for label, url in [(server.id, server.url) for server in spec.mcp_servers] + [
        (service.id, service.spec_url) for service in spec.openapi_services
    ]:
        try:
            with urlopen(_health_url(url), timeout=3) as response:
                if response.status != 200:
                    raise OSError("unhealthy")
        except (OSError, ValueError):
            failed.append(f"{label} unavailable")
    if failed:
        raise InfrastructureUnavailableError("; ".join(failed) + "; try docker compose up -d")


def check_image(image: Path, volume: Path):
    expected = volume.resolve()
    actual = image.resolve()
    if actual == expected or expected not in actual.parents or image.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise InputImageError(f"Image must be within {volume} (.png, .jpg, .jpeg, .webp)")
    try:
        with image.open("rb") as stream:
            header = stream.read(16)
        valid = (header.startswith(b"\x89PNG\r\n\x1a\n") or
                 header.startswith(b"\xff\xd8\xff") or
                 header.startswith(b"RIFF") and header[8:12] == b"WEBP")
        if not valid or image.stat().st_size <= 16:
            raise InputImageError("Image empty or invalid header; no request registered")
        with Image.open(image) as parsed:
            parsed.verify()
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise InputImageError(f"Image invalid or unavailable in {volume}; no request registered") from exc


def validate_confirmation(state):
    """Fail closed if the response differs from the accepted RAG items or terms."""
    from pydantic import ValidationError

    try:
        resolved = state["resolved_exams"]
        booking = state["booking_result"]
        if isinstance(resolved, str):
            import json
            resolved = json.loads(resolved)
        if isinstance(booking, str):
            import json
            booking = json.loads(booking)
        matched, unmatched = resolved["matched_exams"], resolved["unmatched_terms"]
        if (not matched or booking["scheduled_exams"] != matched or
                booking["unmatched_terms"] != unmatched or
                booking["total_exams_scheduled"] != len(matched) or
                booking["status"] != ("PARTIAL" if unmatched else "CONFIRMED") or
                any(not item["tuss_code"].startswith("MOCK-") for item in matched)):
            raise ValueError("mismatch")
        return booking
    except (KeyError, TypeError, ValueError, ValidationError):
        raise AgentPipelineExecutionError("Registration response inconsistent; confirmation withheld") from None


async def execute_pipeline(module_name: str, image: Path):
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    module = importlib.import_module(module_name)
    agent = module.build_pipeline()
    sessions = InMemorySessionService()
    await sessions.create_session(app_name="clinical_exam_scheduler", user_id="cli", session_id="run",
                                  state={"expected_image_path": str(image)})
    runner = Runner(agent=agent, app_name="clinical_exam_scheduler", session_service=sessions)
    message = types.Content(role="user", parts=[types.Part(text=f"Process file_path: {image}")])
    async for _ in runner.run_async(user_id="cli", session_id="run", new_message=message):
        pass
    session = await sessions.get_session(app_name="clinical_exam_scheduler", user_id="cli", session_id="run")
    return validate_confirmation(session.state)


@app.command()
def transpile(spec: Path = typer.Option(DEFAULT_SPEC), output: Path = typer.Option(Path("runtime/generated/clinical_exam_scheduler_agent.py"))):
    try:
        compile_to_file(spec, output)
    except SpecTranspilationError as exc:
        _fail(10, f"Invalid specification: {exc}")
    console.print(f"Generated {output}")


@app.command()
def preflight(spec: Path = typer.Option(DEFAULT_SPEC)):
    try:
        check_preflight(load_spec(spec))
    except SpecTranspilationError as exc:
        _fail(10, f"Invalid specification: {exc}")
    except InfrastructureUnavailableError as exc:
        _fail(30, str(exc))
    console.print("Preflight OK")


@app.command()
def run(
    agent_module: str = typer.Option("runtime.generated.clinical_exam_scheduler_agent"),
    image: Path = typer.Option(...),
    spec: Path = typer.Option(DEFAULT_SPEC),
):
    try:
        pipeline_spec = load_spec(spec)
        check_image(image, Path(pipeline_spec.orchestration.input_volume_path))
        check_preflight(pipeline_spec)
        booking = asyncio.run(execute_pipeline(agent_module, image))
    except SpecTranspilationError as exc:
        _fail(10, f"Invalid specification: {exc}")
    except InputImageError as exc:
        _fail(20, str(exc))
    except InfrastructureUnavailableError as exc:
        _fail(30, str(exc))
    except AgentPipelineExecutionError as exc:
        _fail(40, f"Agent pipeline failed; registration not confirmed. Check OCR/RAG and SSE services. Detail: {exc}")
    except Exception as exc:
        try:
            from security import GuardrailViolation
        except ImportError:
            GuardrailViolation = ()
        if isinstance(exc, GuardrailViolation):
            _fail(50, "Privacy could not be guaranteed; nothing registered. Retry with a clearer image.")
        if type(exc).__name__ == "AgentPipelineExecutionError":
            # The transpiled module defines its own class; message carries stage identifiers only.
            _fail(40, f"Agent pipeline failed; registration not confirmed. Check OCR/RAG and SSE services. Detail: {exc}")
        _fail(40, "Agent pipeline failed; registration not confirmed. Check OCR/RAG and SSE services.")
    console.print(f"Protocol: {booking['protocol_id']} ({booking['status']}); registered request at {booking['scheduled_at']} (not a collection reservation).")
    for item in booking["scheduled_exams"]:
        console.print(f"Registered: {item['official_name']} ({item['tuss_code']} fictitious)")
    for term in booking["unmatched_terms"]:
        console.print(f"[yellow]Not registered: {term}[/yellow]")
    console.print(f"Simulated preparation: {booking['preparation_summary']}")


if __name__ == "__main__":
    app()
