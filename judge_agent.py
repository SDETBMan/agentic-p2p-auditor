"""
Judge agent: reads exploration and adversarial test outputs, verifies verdicts
against tool evidence, and emits a structured JSON report.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Literal

from agent_client import make_judge_anthropic_client

# --- Judge session (tool state) ------------------------------------------------

_JudgeState: dict[str, Any] = {
    "exploration_text": None,
    "adversarial_text": None,
    "scores": [],
    "verifications": [],
    "last_report": None,
}


def _reset_judge_state() -> None:
    _JudgeState["exploration_text"] = None
    _JudgeState["adversarial_text"] = None
    _JudgeState["scores"] = []
    _JudgeState["verifications"] = []
    _JudgeState["last_report"] = None


def _read_file_safe(path: str) -> tuple[str | None, str | None]:
    if not path or not path.strip():
        return None, "empty_path"
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(), None
    except OSError as e:
        return None, str(e)


def read_test_report(
    exploration_path: str | None = None,
    adversarial_path: str | None = None,
) -> dict[str, Any]:
    """
    Load raw text from saved exploration and/or adversarial agent transcripts or logs.
    """
    out: dict[str, Any] = {"exploration_loaded": False, "adversarial_loaded": False}
    if exploration_path:
        text, err = _read_file_safe(exploration_path)
        if err:
            out["exploration_error"] = err
        else:
            _JudgeState["exploration_text"] = text
            out["exploration_loaded"] = True
            out["exploration_chars"] = len(text or "")
            out["exploration_preview"] = (text or "")[:4000]
    if adversarial_path:
        text, err = _read_file_safe(adversarial_path)
        if err:
            out["adversarial_error"] = err
        else:
            _JudgeState["adversarial_text"] = text
            out["adversarial_loaded"] = True
            out["adversarial_chars"] = len(text or "")
            out["adversarial_preview"] = (text or "")[:4000]
    if not exploration_path and not adversarial_path:
        return {
            "error": "validation_error",
            "message": "Provide at least one of exploration_path or adversarial_path.",
        }
    return out


def _parse_tool_response(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    return {}


def _rejection_signals(obj: dict[str, Any]) -> list[str]:
    """Signals that indicate the mock API explicitly rejected an operation."""
    signals: list[str] = []
    if "error" in obj:
        signals.append("error")
        err = obj.get("error")
        if isinstance(err, str):
            signals.append(f"error:{err}")
    if obj.get("three_way_match_gate") is False:
        signals.append("three_way_match_gate:false")
    if obj.get("match_status") == "variance_hold":
        signals.append("match_status:variance_hold")
    if obj.get("inactive_vendor_gate") or (
        isinstance(obj.get("message"), str) and "inactive" in obj["message"].lower()
    ):
        signals.append("inactive_vendor")
    msg = obj.get("message")
    if isinstance(msg, str) and "duplicate" in msg.lower():
        signals.append("duplicate_invoice_message")
    if isinstance(obj.get("error"), str) and "duplicate" in obj["error"].lower():
        signals.append("duplicate_invoice_error")
    return signals


def verify_tool_evidence(
    verdict: Literal["HELD", "BREACHED"],
    tool_response_json: str,
    claim_summary: str = "",
) -> dict[str, Any]:
    """
    Deterministic check: adversarial HELD requires explicit rejection-style evidence
    in the parsed tool JSON; BREACHED requires absence of those rejection signals
    on the same object (successful bypass of the control under test).
    """
    if verdict not in ("HELD", "BREACHED"):
        rec = {
            "grounded": False,
            "verdict": verdict,
            "reason": "invalid_verdict",
            "claim_summary": claim_summary,
        }
        _JudgeState["verifications"].append(rec)
        return rec

    try:
        parsed = json.loads(tool_response_json)
    except json.JSONDecodeError as e:
        rec = {
            "grounded": False,
            "verdict": verdict,
            "reason": "invalid_json",
            "detail": str(e),
            "claim_summary": claim_summary,
        }
        _JudgeState["verifications"].append(rec)
        return rec

    root = _parse_tool_response(parsed)
    signals = _rejection_signals(root)
    has_rejection = len(signals) > 0

    if verdict == "HELD":
        grounded = has_rejection
        reason = (
            "rejection_evidence_present"
            if grounded
            else "held_requires_explicit_rejection_in_tool_response"
        )
    else:
        grounded = not has_rejection
        reason = (
            "no_rejection_evidence_breach_plausible"
            if grounded
            else "breached_but_rejection_present_in_tool_response"
        )

    rec = {
        "grounded": grounded,
        "verdict": verdict,
        "reason": reason,
        "rejection_signals": signals,
        "claim_summary": claim_summary,
    }
    _JudgeState["verifications"].append(rec)
    return rec


def score_finding(
    finding_id: str,
    score: float,
    rationale: str,
) -> dict[str, Any]:
    """Record a 0.0–1.0 grounding score for a finding or sub-report."""
    s = max(0.0, min(1.0, float(score)))
    row = {"finding_id": finding_id, "score": s, "rationale": rationale}
    _JudgeState["scores"].append(row)
    return {"recorded": True, **row}


def generate_judge_report(
    happy_path_status: Literal["PASS", "FAIL"],
    happy_path_steps: list[str],
    adversarial: list[dict[str, Any]],
    summary: str,
) -> dict[str, Any]:
    """
    Build the final judge JSON. Validates enums and shapes; stores on state.
    """
    if happy_path_status not in ("PASS", "FAIL"):
        return {"error": "happy_path_status must be PASS or FAIL"}
    if not isinstance(happy_path_steps, list):
        return {"error": "happy_path_steps must be a list"}
    if not isinstance(adversarial, list):
        return {"error": "adversarial must be a list"}
    clean_adv: list[dict[str, Any]] = []
    for i, item in enumerate(adversarial):
        if not isinstance(item, dict):
            return {"error": f"adversarial[{i}] must be an object"}
        rule = item.get("rule")
        status = item.get("status")
        evidence = item.get("evidence")
        if not isinstance(rule, str):
            return {"error": f"adversarial[{i}].rule must be a string"}
        if status not in ("HELD", "BREACHED"):
            return {"error": f"adversarial[{i}].status must be HELD or BREACHED"}
        if evidence is None:
            evidence = {}
        if not isinstance(evidence, dict):
            return {"error": f"adversarial[{i}].evidence must be an object"}
        clean_adv.append({"rule": rule, "status": status, "evidence": evidence})
    if not isinstance(summary, str):
        return {"error": "summary must be a string"}

    report = {
        "happy_path": {"status": happy_path_status, "steps": list(happy_path_steps)},
        "adversarial": clean_adv,
        "summary": summary,
    }
    _JudgeState["last_report"] = report
    return {"ok": True, "report": report}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_test_report",
        "description": (
            "Load exploration and/or adversarial agent output from UTF-8 text files "
            "(saved transcripts, logs, or captured stdout). At least one path required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "exploration_path": {"type": "string", "description": "Path to exploration agent output file."},
                "adversarial_path": {"type": "string", "description": "Path to adversarial agent output file."},
            },
            "required": [],
        },
    },
    {
        "name": "verify_tool_evidence",
        "description": (
            "Check whether an adversarial HELD/BREACHED verdict is grounded in the given "
            "tool_response_json string (must be JSON from a mock tool). HELD requires "
            "explicit rejection signals; BREACHED requires their absence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["HELD", "BREACHED"]},
                "tool_response_json": {"type": "string"},
                "claim_summary": {"type": "string"},
            },
            "required": ["verdict", "tool_response_json"],
        },
    },
    {
        "name": "score_finding",
        "description": "Attach a 0.0–1.0 score and rationale to a finding id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "string"},
                "score": {"type": "number"},
                "rationale": {"type": "string"},
            },
            "required": ["finding_id", "score", "rationale"],
        },
    },
    {
        "name": "generate_judge_report",
        "description": (
            "Emit the final judge JSON: happy_path.status PASS|FAIL, happy_path.steps, "
            "adversarial array with rule, status HELD|BREACHED, evidence object, and summary string."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "happy_path_status": {"type": "string", "enum": ["PASS", "FAIL"]},
                "happy_path_steps": {"type": "array", "items": {"type": "string"}},
                "adversarial": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "rule": {"type": "string"},
                            "status": {"type": "string", "enum": ["HELD", "BREACHED"]},
                            "evidence": {"type": "object"},
                        },
                        "required": ["rule", "status", "evidence"],
                    },
                },
                "summary": {"type": "string"},
            },
            "required": ["happy_path_status", "happy_path_steps", "adversarial", "summary"],
        },
    },
]


def dispatch_judge_tool(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    if name == "read_test_report":
        return read_test_report(
            tool_input.get("exploration_path"),
            tool_input.get("adversarial_path"),
        )
    if name == "verify_tool_evidence":
        return verify_tool_evidence(
            tool_input["verdict"],
            tool_input["tool_response_json"],
            tool_input.get("claim_summary", ""),
        )
    if name == "score_finding":
        return score_finding(
            tool_input["finding_id"],
            tool_input["score"],
            tool_input["rationale"],
        )
    if name == "generate_judge_report":
        return generate_judge_report(
            tool_input["happy_path_status"],
            tool_input["happy_path_steps"],
            tool_input["adversarial"],
            tool_input["summary"],
        )
    return {"error": "unknown_tool", "name": name}


SYSTEM_PROMPT = """You are an impartial judge for ERP QA automation. You read outputs from an exploration agent (happy-path P2P) and an adversarial agent (control attacks). Your job is to ensure every verdict is grounded in actual mock tool evidence (tool request/response JSON embedded in the transcripts).

Use read_test_report first when paths are known. Extract or infer tool payloads from the transcripts; call verify_tool_evidence with the exact JSON string of the relevant tool response when possible. Use score_finding to note weak or missing evidence. Call generate_judge_report exactly once when your conclusions are ready.

The final artifact must match this JSON shape exactly:
{"happy_path": {"status": "PASS" or "FAIL", "steps": []}, "adversarial": [{"rule": string, "status": "HELD" or "BREACHED", "evidence": {}}], "summary": string}

Populate happy_path.steps with short strings describing each validated happy-path step (or failure reasons). For each adversarial rule, set evidence to the concrete tool snippets or parsed fields you relied on; if evidence is missing from the log, say so in evidence and reflect that in summary.

When the judge JSON is finalized via generate_judge_report, end your assistant text with the line [[JUDGE_COMPLETE]]. Do not treat end_turn alone as completion."""


def _content_blocks_to_assistant_message(content: list[Any]) -> dict[str, Any]:
    return {"role": "assistant", "content": content}


def _extract_text(content: list[Any]) -> str:
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts)


def _has_exit_marker(text: str) -> bool:
    return "[[JUDGE_COMPLETE]]" in text


def run_judge(
    exploration_path: str | None = None,
    adversarial_path: str | None = None,
    user_prompt: str | None = None,
    *,
    max_iterations: int = 30,
    model: str | None = None,
) -> dict[str, Any] | None:
    _reset_judge_state()

    client = make_judge_anthropic_client()
    model_name = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

    paths_hint: list[str] = []
    if exploration_path:
        paths_hint.append(f"exploration_path: {exploration_path}")
    if adversarial_path:
        paths_hint.append(f"adversarial_path: {adversarial_path}")

    preload_note = ""
    if exploration_path or adversarial_path:
        preload = read_test_report(exploration_path, adversarial_path)
        preload_note = "\n\nPreloaded read_test_report result:\n" + json.dumps(preload, indent=2, default=str)[:12000]
        if _JudgeState.get("exploration_text"):
            et = _JudgeState["exploration_text"]
            preload_note += "\n\n--- exploration_full_text (truncated) ---\n" + et[:200000]
        if _JudgeState.get("adversarial_text"):
            at = _JudgeState["adversarial_text"]
            preload_note += "\n\n--- adversarial_full_text (truncated) ---\n" + at[:200000]

    initial = user_prompt or (
        "Judge the exploration and adversarial runs. "
        + (" ".join(paths_hint) if paths_hint else "Use read_test_report with file paths if provided in this message.")
        + " Validate verdicts against tool evidence, then generate_judge_report, then output [[JUDGE_COMPLETE]]."
    )
    if preload_note:
        initial = initial + preload_note

    messages: list[dict[str, Any]] = [{"role": "user", "content": initial}]

    for iteration in range(max_iterations):
        try:
            response = client.messages.create(
                model=model_name,
                max_tokens=8192,
                temperature=0,
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=TOOLS,
            )
        except Exception as e:
            print(
                json.dumps(
                    {
                        "event": "api_error",
                        "iteration": iteration + 1,
                        "error_type": type(e).__name__,
                        "message": str(e),
                    }
                )
            )
            break

        assistant_content = list(response.content)
        messages.append(_content_blocks_to_assistant_message(assistant_content))

        text = _extract_text(assistant_content)
        if text.strip():
            print(f"\n--- iteration {iteration + 1} assistant text ---\n{text}\n")

        tool_uses = [b for b in assistant_content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            if _has_exit_marker(text):
                print("--- Exit: [[JUDGE_COMPLETE]] ---")
                break
            print(
                f"--- iteration {iteration + 1}: no tool calls "
                f"(stop_reason={getattr(response, 'stop_reason', None)}); nudging ---"
            )
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Continue judging: read reports if needed, verify evidence, "
                                "call generate_judge_report, then output [[JUDGE_COMPLETE]]."
                            ),
                        }
                    ],
                }
            )
            continue

        tool_result_blocks: list[dict[str, Any]] = []
        for block in tool_uses:
            tid = block.id
            name = block.name
            tool_input = dict(block.input)
            print(f"\n--- iteration {iteration + 1} tool request: {name} ---")
            print(json.dumps(tool_input, indent=2, default=str))
            result = dispatch_judge_tool(name, tool_input)
            print(f"--- iteration {iteration + 1} tool response: {name} ---")
            print(json.dumps(result, indent=2, default=str))
            tool_result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": json.dumps(result, default=str),
                }
            )

        messages.append({"role": "user", "content": tool_result_blocks})

        if _has_exit_marker(text):
            print("--- Exit: [[JUDGE_COMPLETE]] (marker after tool results) ---")
            break
    else:
        print("--- Max iterations reached without [[JUDGE_COMPLETE]]. ---")

    report = _JudgeState.get("last_report")
    if isinstance(report, dict):
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return report

    print("--- Judge finished without a stored report; check generate_judge_report tool usage. ---")
    return None


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Judge agent: validate exploration/adversarial outputs.")
    p.add_argument("--exploration", help="Path to exploration agent output (UTF-8 text).")
    p.add_argument("--adversarial", help="Path to adversarial agent output (UTF-8 text).")
    p.add_argument("--model", default=None, help="Override ANTHROPIC_MODEL.")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_judge(
        exploration_path=args.exploration,
        adversarial_path=args.adversarial,
        model=args.model,
    )
