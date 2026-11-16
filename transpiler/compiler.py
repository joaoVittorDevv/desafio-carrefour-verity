"""Render a validated pipeline and reject invalid generated Python before writing it."""

import ast
import subprocess
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import ValidationError

from transpiler.schema import PipelineSpec


class SpecTranspilationError(Exception):
    """Invalid JSON, DSL, or generated Python."""


def load_spec(path: Path) -> PipelineSpec:
    try:
        return PipelineSpec.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, ValueError) as exc:
        raise SpecTranspilationError(str(exc)) from exc


def compile_spec(spec: PipelineSpec) -> str:
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )
    env.filters["pyrepr"] = repr
    try:
        code = env.get_template("adk_sequential_agent.py.j2").render(spec=spec)
        ast.parse(code)
        formatted = subprocess.run(
            [sys.executable, "-m", "ruff", "format", "--stdin-filename", "generated.py", "-"],
            input=code, capture_output=True, text=True, check=True,
        )
        code = formatted.stdout
        tree = ast.parse(code)
    except (SyntaxError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise SpecTranspilationError(f"generated Python invalid or formatter unavailable: {exc}") from exc
    required = {
        "google.adk.agents": {"LlmAgent", "SequentialAgent"},
        "google.adk.tools.mcp_tool.mcp_toolset": {"MCPToolset", "SseConnectionParams"},
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in required:
            required[node.module] -= {alias.name for alias in node.names}
    if any(required.values()):
        raise SpecTranspilationError(f"generated Python missing imports: {required}")
    return code


def transpile(spec_path: Path, output_path: Path) -> Path:
    code = compile_spec(load_spec(spec_path))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(code, encoding="utf-8")
    return output_path
