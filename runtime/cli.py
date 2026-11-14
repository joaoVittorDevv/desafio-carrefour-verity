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
        failed.append("GEMINI_API_KEY ausente")
    for label, url in [(server.id, server.url) for server in spec.mcp_servers] + [
        (service.id, service.spec_url) for service in spec.openapi_services
    ]:
        try:
            with urlopen(_health_url(url), timeout=3) as response:
                if response.status != 200:
                    raise OSError("unhealthy")
        except (OSError, ValueError):
            failed.append(f"{label} indisponível")
    if failed:
        raise InfrastructureUnavailableError("; ".join(failed) + "; tente docker compose up -d")


def check_image(image: Path, volume: Path):
    expected = volume.resolve()
    actual = image.resolve()
    if actual == expected or expected not in actual.parents or image.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise InputImageError(f"A imagem deve estar em {volume} (.png, .jpg, .jpeg, .webp)")
    try:
        with image.open("rb") as stream:
            header = stream.read(16)
        valid = (header.startswith(b"\x89PNG\r\n\x1a\n") or
                 header.startswith(b"\xff\xd8\xff") or
                 header.startswith(b"RIFF") and header[8:12] == b"WEBP")
        if not valid or image.stat().st_size <= 16:
            raise InputImageError("Imagem vazia ou com cabeçalho inválido; solicitação não registrada")
        with Image.open(image) as parsed:
            parsed.verify()
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise InputImageError(f"Imagem inválida ou indisponível em {volume}; solicitação não registrada") from exc


def validate_confirmation(state):
    """Fail closed if the response differs from the accepted RAG items or terms."""
    from pydantic import ValidationError
    from security.adk_callbacks import require_complete_order

    try:
        resolved = state["resolved_exams"]
        booking = state["booking_result"]
        if isinstance(resolved, str):
            import json
            resolved = json.loads(resolved)
        if isinstance(booking, str):
            import json
            booking = json.loads(booking)
        require_complete_order(state["extracted_exams"], resolved)
        if resolved != state["_rag_verified"] or booking != state["_booking_verified"]:
            raise ValueError("mismatch")
        matched, unmatched = resolved["matched_exams"], resolved["unmatched_terms"]
        if (not matched or booking["scheduled_exams"] != matched or
                booking["unmatched_terms"] != unmatched or
                booking["total_exams_scheduled"] != len(matched) or
                booking["status"] != "CONFIRMED" or
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
    stages = {
        "ocr_intake_agent": "Extraindo exames e protegendo dados pessoais",
        "rag_lookup_agent": "Conferindo exames no catálogo fictício",
        "scheduling_agent": "Registrando a solicitação fictícia",
    }
    current = "ocr_intake_agent"
    console.print(f"Iniciando: {stages[current]}...")
    with console.status(stages[current], spinner="dots") as status:
        async for event in runner.run_async(user_id="cli", session_id="run", new_message=message):
            if event.author in stages and event.author != current:
                console.print(f"[green]✓[/green] {stages[current]}")
                current = event.author
                console.print(f"Iniciando: {stages[current]}...")
                status.update(stages[current])
        console.print("Conferindo o resultado do registro...")
        status.update("Conferindo o resultado do registro")
        session = await sessions.get_session(app_name="clinical_exam_scheduler", user_id="cli", session_id="run")
        booking = validate_confirmation(session.state)
    console.print(f"[green]✓[/green] {stages[current]}")
    return booking


@app.command()
def transpile(spec: Path = typer.Option(DEFAULT_SPEC), output: Path = typer.Option(Path("runtime/generated/clinical_exam_scheduler_agent.py"))):
    try:
        compile_to_file(spec, output)
    except SpecTranspilationError:
        _fail(10, "Especificação inválida; confira o arquivo JSON e seus campos.")
    console.print(f"Código do agente gerado em {output}")


@app.command()
def preflight(spec: Path = typer.Option(DEFAULT_SPEC)):
    try:
        check_preflight(load_spec(spec))
    except SpecTranspilationError:
        _fail(10, "Especificação inválida; confira o arquivo JSON e seus campos.")
    except InfrastructureUnavailableError as exc:
        _fail(30, str(exc))
    console.print("Verificação dos serviços concluída.")


@app.command()
def run(
    agent_module: str = typer.Option("runtime.generated.clinical_exam_scheduler_agent"),
    image: Path = typer.Option(...),
    spec: Path = typer.Option(DEFAULT_SPEC),
):
    try:
        pipeline_spec = load_spec(spec)
        console.print("Validando imagem e serviços...")
        with console.status("Validando imagem e serviços", spinner="dots"):
            check_image(image, Path(pipeline_spec.orchestration.input_volume_path))
            check_preflight(pipeline_spec)
        console.print("[green]✓[/green] Imagem e serviços disponíveis")
        booking = asyncio.run(execute_pipeline(agent_module, image))
    except SpecTranspilationError:
        _fail(10, "Especificação inválida; confira o arquivo JSON e seus campos.")
    except InputImageError as exc:
        _fail(20, str(exc))
    except InfrastructureUnavailableError as exc:
        _fail(30, str(exc))
    except AgentPipelineExecutionError:
        _fail(40, "Não foi possível confirmar a solicitação. Se a API chegou a ser chamada, consulte-a antes de repetir.")
    except Exception as exc:
        from security.adk_callbacks import IncompleteOrderError

        if isinstance(exc, IncompleteOrderError):
            _fail(40, "Pedido incompleto ou não reconciliado; solicitação não registrada.")
        try:
            from security import GuardrailViolation
        except ImportError:
            GuardrailViolation = ()
        if isinstance(exc, GuardrailViolation):
            _fail(50, "Não foi possível garantir a privacidade. Consulte a API antes de repetir a operação.")
        _fail(40, "Não foi possível confirmar a solicitação. Se a API chegou a ser chamada, consulte-a antes de repetir.")
    situation = "confirmada"
    console.print(f"Protocolo: {booking['protocol_id']} (solicitação {situation}); registrada em {booking['scheduled_at']} (não é uma reserva de horário).")
    for item in booking["scheduled_exams"]:
        console.print(f"Registrado: {item['official_name']} ({item['tuss_code']} fictício)")
    console.print(f"Preparo simulado: {booking['preparation_summary']}")


if __name__ == "__main__":
    app()
