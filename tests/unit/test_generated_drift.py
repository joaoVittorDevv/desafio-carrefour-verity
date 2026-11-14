from pathlib import Path

from transpiler.compiler import compile_spec, load_spec


def test_generated_agent_matches_compiled_spec():
    artifact = Path("runtime/generated/clinical_exam_scheduler_agent.py")
    assert artifact.read_text(encoding="utf-8") == compile_spec(
        load_spec(Path("specs/clinical_agent_spec.json"))
    )
