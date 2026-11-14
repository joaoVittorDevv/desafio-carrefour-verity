import copy
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image
from typer.testing import CliRunner

from runtime.cli import (
    AgentPipelineExecutionError, InputImageError, app, check_image,
    check_preflight, validate_confirmation,
)
from transpiler.compiler import load_spec


SPEC = Path("specs/clinical_agent_spec.json")


def test_confirmation_requires_complete_exact_evidence():
    exam = dict(original_query="simulado", tuss_code="MOCK-0001", official_name="Fictício",
                category="Simulada", fasting_hours=0, confidence_score=0.9)
    state = {"extracted_exams": {"exams": [{"raw_name": "simulado"}]},
             "resolved_exams": {"matched_exams": [exam], "unmatched_terms": []},
             "booking_result": {"scheduled_exams": [exam.copy()], "unmatched_terms": [],
                                "total_exams_scheduled": 1, "status": "CONFIRMED", "protocol_id": "LAB-TEST"}}
    state["_rag_verified"] = copy.deepcopy(state["resolved_exams"])
    state["_booking_verified"] = copy.deepcopy(state["booking_result"])
    assert validate_confirmation(state)["status"] == "CONFIRMED"
    for change in (lambda v: v["booking_result"].update(status="PARTIAL"),
                   lambda v: v["resolved_exams"]["unmatched_terms"].append("outro"),
                   lambda v: v["extracted_exams"]["exams"][0].update(raw_name="alterado"),
                   lambda v: v["booking_result"]["scheduled_exams"][0].update(tuss_code="MOCK-9999")):
        invalid = copy.deepcopy(state)
        change(invalid)
        with pytest.raises(AgentPipelineExecutionError):
            validate_confirmation(invalid)


def test_check_image_rejects_traversal_and_bad_header(tmp_path):
    volume = tmp_path / "inputs"
    volume.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 24)
    with pytest.raises(InputImageError):
        check_image(outside, volume)
    inside = volume / "bad.png"
    inside.write_bytes(b"invalid image payload")
    with pytest.raises(InputImageError):
        check_image(inside, volume)
    good = volume / "good.png"
    Image.new("RGB", (3, 3), "white").save(good)
    check_image(good, volume)


def test_preflight_offline(monkeypatch):
    spec = load_spec(SPEC)
    monkeypatch.setenv("GEMINI_API_KEY", "test-only")
    called = []
    def urlopen(url, timeout):
        called.append(url)
        return Mock(__enter__=lambda _: Mock(status=200), __exit__=lambda *_: None)
    monkeypatch.setattr("runtime.cli.urlopen", urlopen)
    check_preflight(spec)
    assert len(called) == 3
    assert all(url.endswith("/health") for url in called)


def test_cli_exit_codes_mock_only(monkeypatch):
    import runtime.cli as cli

    result = CliRunner().invoke(app, ["run", "--image", "/app/inputs/missing.png"])
    assert result.exit_code == 20
    monkeypatch.setattr(cli, "check_image", lambda *a: None)
    monkeypatch.setattr(cli, "check_preflight", lambda *a: (_ for _ in ()).throw(cli.InfrastructureUnavailableError("offline")))
    result = CliRunner().invoke(app, ["run", "--image", "/app/inputs/test.png"])
    assert result.exit_code == 30
    monkeypatch.setattr(cli, "check_preflight", lambda *a: None)
    security = types.ModuleType("security")
    security.GuardrailViolation = type("GuardrailViolation", (Exception,), {})
    monkeypatch.setitem(sys.modules, "security", security)

    async def blocked(*args):
        raise security.GuardrailViolation("sensitive value must never print")

    monkeypatch.setattr(cli, "execute_pipeline", blocked)
    result = CliRunner().invoke(app, ["run", "--image", "/app/inputs/test.png"])
    assert result.exit_code == 50
    assert "sensitive value" not in result.stdout

    async def failed(*args):
        raise AgentPipelineExecutionError("nothing matched")

    monkeypatch.setattr(cli, "execute_pipeline", failed)
    result = CliRunner().invoke(app, ["run", "--image", "/app/inputs/test.png"])
    assert result.exit_code == 40
    assert "Não foi possível confirmar" in result.stdout
    assert "solicitação registrada" not in result.stdout
    assert "sensitive value" not in result.stdout


def test_cli_duplicate_post_is_reported_as_ambiguous(monkeypatch):
    import runtime.cli as cli
    from security.adk_callbacks import RegistrationAlreadyAttemptedError

    monkeypatch.setattr(cli, "check_image", lambda *a: None)
    monkeypatch.setattr(cli, "check_preflight", lambda *a: None)

    async def duplicate(*_args):
        raise RegistrationAlreadyAttemptedError("MARCADOR-INTERNO")

    monkeypatch.setattr(cli, "execute_pipeline", duplicate)
    result = CliRunner().invoke(app, ["run", "--image", "/app/inputs/test.png"])
    assert result.exit_code == 40
    assert "pode já ter sido efetuado" in result.stdout
    assert "consulte a API antes de repetir" in result.stdout
    assert "não registrada" not in result.stdout
    assert "MARCADOR-INTERNO" not in result.stdout


def test_cli_success_feedback_is_in_portuguese(monkeypatch):
    import runtime.cli as cli

    monkeypatch.setattr(cli, "check_image", lambda *a: None)
    monkeypatch.setattr(cli, "check_preflight", lambda *a: None)

    async def completed(*_args):
        return {"protocol_id": "LAB-TEST", "status": "CONFIRMED",
                "scheduled_at": "2026-11-13T14:02:41Z",
                "scheduled_exams": [{"official_name": "Hemograma (simulado)",
                                     "tuss_code": "MOCK-0001"}],
                "unmatched_terms": [],
                "preparation_summary": "Preparo simulado"}

    monkeypatch.setattr(cli, "execute_pipeline", completed)
    result = CliRunner().invoke(app, ["run", "--image", "/app/inputs/test.png"])
    assert result.exit_code == 0
    assert "Validando imagem e serviços" in result.stdout
    assert "Protocolo: LAB-TEST (solicitação confirmada)" in result.stdout
    assert "Não registrado:" not in result.stdout
    assert "Registered:" not in result.stdout and "Protocol:" not in result.stdout


def test_cli_stage_failure_messages_are_static_and_safe(monkeypatch):
    import runtime.cli as cli

    monkeypatch.setattr(cli, "check_image", lambda *a: None)
    monkeypatch.setattr(cli, "check_preflight", lambda *a: None)
    cases = [
        ("preparação dos serviços do agente", False, "Falha na preparação dos serviços do agente"),
        ("extração dos exames", False, "Falha na extração dos exames"),
        ("consulta ao catálogo", False, "Falha na consulta ao catálogo"),
        ("registro da solicitação", True, "Falha no registro da solicitação"),
    ]
    for stage, after_api, expected in cases:
        async def failing(*_args, _stage=stage, _after=after_api):
            raise AgentPipelineExecutionError("MARCADOR-INTERNO", stage=_stage, after_api=_after)

        monkeypatch.setattr(cli, "execute_pipeline", failing)
        result = CliRunner().invoke(app, ["run", "--image", "/app/inputs/test.png"])
        assert result.exit_code == 40
        assert expected in result.stdout
        assert "MARCADOR-INTERNO" not in result.stdout
        if after_api:
            assert "consulte a API antes de repetir" in result.stdout
            assert "não foi registrada" not in result.stdout
        else:
            assert "solicitação não foi registrada" in result.stdout


def test_cli_invalid_spec_exit_10(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{")
    result = CliRunner().invoke(app, ["transpile", "--spec", str(path), "--output", str(tmp_path / "agent.py")])
    assert result.exit_code == 10
    assert "Especificação inválida" in result.stdout
    assert "line 1" in result.stdout  # detalhe do erro do próprio JSON do usuário, requisito de mensagens claras
    assert not (tmp_path / "agent.py").exists()

    import json

    spec = json.loads(Path("specs/clinical_agent_spec.json").read_text(encoding="utf-8"))
    spec["mcp_servers"][0]["transport"] = "stdio"
    semantic = tmp_path / "stdio.json"
    semantic.write_text(json.dumps(spec, ensure_ascii=False))
    result = CliRunner().invoke(app, ["transpile", "--spec", str(semantic), "--output", str(tmp_path / "agent.py")])
    assert result.exit_code == 10
    assert "Especificação inválida" in result.stdout
    assert "'sse'" in result.stdout  # motivo semântico visível, não escondido
    assert not (tmp_path / "agent.py").exists()


def test_transpile_output_write_failure_is_clear_not_a_traceback(monkeypatch, tmp_path):
    import runtime.cli as cli

    def denied(*_args):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cli, "compile_to_file", denied)
    result = CliRunner().invoke(app, ["transpile", "--spec", "specs/clinical_agent_spec.json",
                                      "--output", "/transpile.py"])
    assert result.exit_code == 10
    shown = " ".join(result.stdout.split())  # rich quebra linhas; normaliza antes de asserir
    assert "Não foi possível gravar a saída em /transpile.py" in shown
    assert "verifique o caminho e as permissões" in shown
    assert "Traceback" not in shown



def test_failure_markers_pin_generated_raise_sites():
    """Stage labels must not silently desync from spec/artifact raise-site texts."""
    from runtime.cli import _FAILURE_MARKERS

    spec = load_spec(SPEC)
    markers = {marker for marker, _stage, _after_api in _FAILURE_MARKERS}
    for agent in spec.orchestration.agents:
        assert f"Invalid output at {agent.output_key}" in markers, agent.output_key
        if agent.input_key:
            assert f"Invalid input for {agent.name}" in markers, agent.name
    artifact = Path("runtime/generated/clinical_exam_scheduler_agent.py").read_text(encoding="utf-8")
    cli_source = Path("runtime/cli.py").read_text(encoding="utf-8")
    for marker, _stage, _after_api in _FAILURE_MARKERS:
        if marker == "Registration response inconsistent":
            # Raised by validate_confirmation in the CLI; pin the raise site, not the tuple entry.
            assert cli_source.count(marker) >= 2, marker
        elif marker.startswith(("Invalid output at ", "Invalid input for ")):
            # f-string sites: pin the rendered prefix and the key/agent target separately.
            prefix, target = marker.rsplit(" ", 1)
            assert f"{prefix} " in artifact and target in artifact, marker
        else:
            assert marker in artifact, marker
