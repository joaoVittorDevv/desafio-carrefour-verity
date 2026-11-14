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


def test_confirmation_checks_exact_partial_items_and_terms():
    exam = dict(original_query="simulado", tuss_code="MOCK-0001", official_name="Fictício",
                category="Simulada", fasting_hours=0, confidence_score=0.9)
    state = {"resolved_exams": {"matched_exams": [exam], "unmatched_terms": ["não encontrado"]},
             "booking_result": {"scheduled_exams": [exam.copy()], "unmatched_terms": ["não encontrado"],
                                "total_exams_scheduled": 1, "status": "PARTIAL", "protocol_id": "LAB-TEST"}}
    assert validate_confirmation(state)["status"] == "PARTIAL"
    invalid = copy.deepcopy(state)
    invalid["booking_result"]["unmatched_terms"] = []
    with pytest.raises(AgentPipelineExecutionError):
        validate_confirmation(invalid)
    invalid = copy.deepcopy(state)
    invalid["resolved_exams"]["matched_exams"] = []
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


def test_cli_invalid_spec_exit_10(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{")
    result = CliRunner().invoke(app, ["transpile", "--spec", str(path), "--output", str(tmp_path / "agent.py")])
    assert result.exit_code == 10
    assert not (tmp_path / "agent.py").exists()
