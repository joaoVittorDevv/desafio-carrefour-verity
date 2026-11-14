"""ADK callbacks: fail closed before model/tool calls and verify real tool evidence."""

import json
import math
from pathlib import Path
from collections.abc import Mapping

from security.pii_guardrail import BLOCK_MESSAGE, GuardrailViolation, assert_no_pii, local_analyzer


def _check(value, analyzer, seen=None):
    if seen is None:
        seen = set()
    if isinstance(value, str):
        if BLOCK_MESSAGE in value:
            raise GuardrailViolation()
        assert_no_pii(value, analyzer)
    elif value is None or isinstance(value, (bool, int, float)):
        return
    elif isinstance(value, (bytes, bytearray)):
        raise GuardrailViolation()
    else:
        if id(value) in seen:
            raise GuardrailViolation()
        seen.add(id(value))
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key == "thought_signature":
                    # Opaque Gemini protocol artifact (encrypted), not user payload.
                    continue
                _check(key, analyzer, seen)
                _check(item, analyzer, seen)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _check(item, analyzer, seen)
        elif hasattr(value, "model_dump"):
            _check(value.model_dump(mode="python"), analyzer, seen)
        elif hasattr(value, "to_dict"):
            # ADK session State is a custom wrapper, not a Mapping; to_dict() unwraps it.
            _check(value.to_dict(), analyzer, seen)
        else:
            raise GuardrailViolation()
        seen.remove(id(value))


def _inspect(value):
    try:
        _check(value, local_analyzer())
    except Exception:
        raise GuardrailViolation() from None


inspect_for_pii = _inspect


def _as_dict(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError("Invalid pipeline state") from None
    if not isinstance(value, dict):
        raise ValueError("Invalid pipeline state")
    return value


def _tool_result(response):
    """Unwrap ADK MCP 1.x structuredContent/content or a FastAPI JSON response."""
    value = _as_dict(response)
    if value.get("isError") or value.get("error") or value.get("pending"):
        raise ValueError("Tool execution failed")
    if "structuredContent" in value:
        return _as_dict(value["structuredContent"])
    if "content" in value:
        blocks = value["content"]
        if not isinstance(blocks, list) or len(blocks) != 1 or not isinstance(blocks[0], dict):
            raise ValueError("Invalid tool response")
        return _as_dict(blocks[0].get("text"))
    return value


def before_model_callback(callback_context, llm_request):
    """Deliver the typed upstream payload as the user turn, then block residual PII."""
    agent_name = getattr(callback_context, "agent_name", "")
    agent_input = callback_context.state.get(f"temp:{agent_name}_input")
    if agent_input is not None:
        has_tool_traffic = any(
            getattr(part, "function_call", None) or getattr(part, "function_response", None)
            for content in llm_request.contents or []
            for part in getattr(content, "parts", None) or []
        )
        if not has_tool_traffic:
            # Typed input contract: the payload IS the user turn, no prompt interpolation.
            from google.genai import types

            llm_request.contents = [types.Content(
                role="user", parts=[types.Part(text=json.dumps(agent_input, ensure_ascii=False))])]
    _inspect(callback_context.state)
    _inspect(llm_request.contents)
    _inspect(llm_request.config.system_instruction if llm_request.config else None)
    return None


def _same_payload(candidate, verified):
    """Exact structure/strings; floats tolerate transcription rounding (never values)."""
    if isinstance(candidate, (int, float)) and isinstance(verified, (int, float)) and \
            (isinstance(candidate, float) or isinstance(verified, float)):
        return math.isclose(candidate, verified, rel_tol=1e-9, abs_tol=1e-12)
    if isinstance(candidate, Mapping) and isinstance(verified, Mapping):
        return candidate.keys() == verified.keys() and all(
            _same_payload(candidate[key], verified[key]) for key in candidate)
    if isinstance(candidate, list) and isinstance(verified, list):
        return len(candidate) == len(verified) and all(
            _same_payload(item, other) for item, other in zip(candidate, verified))
    return candidate == verified


class IncompleteOrderError(ValueError):
    """A typed order is incomplete; no registration tool may be called."""


class RegistrationAlreadyAttemptedError(ValueError):
    """A prior POST may have committed; a second call cannot be attempted."""


def require_complete_order(extracted, resolved):
    """Require ordered, repeated OCR terms to match all catalog evidence."""
    try:
        raw = [item["raw_name"] for item in _as_dict(extracted)["exams"]]
        resolved = _as_dict(resolved)
        found = [item["original_query"] for item in resolved["matched_exams"]]
        unmatched = resolved["unmatched_terms"]
        if (not raw or not found or unmatched or
                any(not isinstance(term, str) for term in raw + found) or
                found != raw):
            raise IncompleteOrderError("Order incomplete or catalog evidence differs")
    except (KeyError, TypeError):
        raise IncompleteOrderError("Order and catalog evidence differ") from None


def before_tool_callback(tool, args, tool_context):
    """Require exact previously verified inputs before RAG lookup or API POST."""
    state = tool_context.state
    _inspect(state)
    _inspect(args)
    if tool.name == "extract_and_anonymize_medical_order":
        if args.get("file_path") != state.get("expected_image_path"):
            raise ValueError("OCR path differs from the requested image")
    elif tool.name == "search_exam_catalog_hybrid":
        extracted = _as_dict(state.get("extracted_exams"))
        if "_ocr_verified" not in state or args.get("exam_queries") != [
            item["raw_name"] for item in extracted["exams"]
        ]:
            raise ValueError("RAG queries do not match verified OCR exams")
    elif tool.name == "create_exam_appointment":
        resolved = _as_dict(state.get("resolved_exams"))
        if ("_rag_verified" not in state or not _same_payload(resolved, state["_rag_verified"]) or
                not _same_payload(args.get("exams"), resolved["matched_exams"]) or
                args.get("unmatched_terms", []) != resolved["unmatched_terms"]):
            raise ValueError("Registration differs from verified RAG results")
        require_complete_order(state.get("extracted_exams"), resolved)
        if state.get("_booking_attempted"):
            raise RegistrationAlreadyAttemptedError("Registration already attempted; consult API before retry")
        # A response may be lost after the POST commits; never allow a second
        # invocation in the same session, even if _booking_verified is absent.
        state["_booking_attempted"] = True
    return None


def after_tool_callback(tool, args, tool_context, tool_response):
    """Save only successful, sanitized tool evidence for each agent output check."""
    keys = {
        "extract_and_anonymize_medical_order": "_ocr_verified",
        "search_exam_catalog_hybrid": "_rag_verified",
        "create_exam_appointment": "_booking_verified",
    }
    if tool.name not in keys:
        return None
    if BLOCK_MESSAGE in json.dumps(tool_response, ensure_ascii=False, default=str):
        raise GuardrailViolation()
    result = _tool_result(tool_response)
    if tool.name == "extract_and_anonymize_medical_order":
        # Filename is an operator-chosen path, not OCR text: validate provenance and
        # deterministic PII, while NER still inspects every field sent to the LLM.
        name = result.get("file_name")
        if name is not None:
            if not isinstance(name, str) or name != Path(args.get("file_path", "")).name:
                raise GuardrailViolation()
            try:
                assert_no_pii(name, None)
            except Exception:
                raise GuardrailViolation() from None
        _inspect({key: value for key, value in result.items() if key != "file_name"})
        report = _as_dict(result.get("pii_redaction_report"))
        if not isinstance(result.get("sanitized_text"), str) or not isinstance(report.get("total_redacted"), int):
            raise ValueError("OCR did not return validated sanitized text")
        result = {"sanitized_text": result["sanitized_text"],
                  "pii_entities_masked": report["total_redacted"]}
        tool_context.state[keys[tool.name]] = result
        # MCP also sends a JSON text block to the next model call. Replace it with
        # the same validated projection; the original block still carries metadata
        # and a basename the generic NER may wrongly classify as a person.
        return {"sanitized_text": result["sanitized_text"],
                "pii_redaction_report": {"total_redacted": result["pii_entities_masked"]}}
    else:
        _inspect(result)
    if tool.name == "search_exam_catalog_hybrid":
        if not isinstance(result.get("matched_exams"), list) or not isinstance(result.get("unmatched_terms"), list):
            raise ValueError("RAG tool result invalid")
        result = {"matched_exams": result["matched_exams"], "unmatched_terms": result["unmatched_terms"]}
    elif tool.name == "create_exam_appointment" and (result.get("scheduled_exams") != args.get("exams") or
          result.get("unmatched_terms") != args.get("unmatched_terms", [])):
        raise ValueError("API result differs from submitted exams")
    tool_context.state[keys[tool.name]] = result
    return None
