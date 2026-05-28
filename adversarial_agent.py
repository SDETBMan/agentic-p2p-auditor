"""
Adversarial red-team loop against the P2P mock API. Reuses tool definitions and
handlers from exploration_agent — do not duplicate mock implementations here.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import exploration_agent as ex

from agent_client import agent_wall_clock_seconds, make_anthropic_client

# Reuse the same five mock tools (schemas + handlers) from exploration_agent; do not duplicate mocks.
def _reset_store() -> None:
    """Fresh in-memory store per run so attacks do not inherit exploration_agent state."""
    ex.STORE = ex.MockP2PStore()


SYSTEM_PROMPT = """You are an expert red team QA engineer specializing in financial systems. Your job is to deliberately attempt to violate each of the six financial control rules: overpayment protection, 3-way match gate, partial receipt flag, inactive vendor gate, GL balance, and duplicate invoice detection. For each attempt: name the rule you are attacking, describe exactly what you tried, record what the API returned, and verdict HELD if the API rejected it or BREACHED if it did not. Only mark HELD if you have explicit rejection evidence from the actual API response. After testing all six specified rules, probe for additional edge cases a human tester would miss — including zero-amount invoices, negative quantities, floating point rounding errors on currency calculations, and race conditions from rapid sequential submissions. Label these clearly as unspecified edge cases in your report.

Use only the provided tools. After each attack or probe, call report_findings so the request, the observed API response, and your HELD/BREACHED verdict (with justification tied to explicit response fields) are recorded.

When the report is complete, include the exact marker [[ADVERSARIAL_COMPLETE]] on its own line. Do not treat end_turn alone as completion."""


def _content_blocks_to_assistant_message(content: list[Any]) -> dict[str, Any]:
    return {"role": "assistant", "content": content}


def _extract_text(content: list[Any]) -> str:
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts)


def _has_exit_marker(text: str) -> bool:
    return "[[ADVERSARIAL_COMPLETE]]" in text


def run_adversarial(
    user_prompt: str | None = None,
    *,
    max_iterations: int = 30,
    model: str | None = None,
) -> None:
    _reset_store()

    client = make_anthropic_client()
    model_name = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

    wall_start = time.monotonic()
    wall_limit = agent_wall_clock_seconds()
    wall_deadline = wall_start + wall_limit

    initial = user_prompt or (
        "Execute red-team attacks against all six financial controls using the tools. "
        "For each rule: attempt a violation, capture the real API response in report_findings, "
        "and assign HELD or BREACHED only from explicit response evidence. "
        "Then run unspecified edge-case probes (zero-amount invoice, negative qty, rounding stress, "
        "rapid duplicate submissions) and label them as unspecified edge cases. "
        "End with [[ADVERSARIAL_COMPLETE]]."
    )

    messages: list[dict[str, Any]] = [{"role": "user", "content": initial}]

    for iteration in range(max_iterations):
        if time.monotonic() >= wall_deadline:
            print(
                json.dumps(
                    {
                        "event": "time_budget_exceeded",
                        "iteration": iteration + 1,
                        "elapsed_seconds": round(time.monotonic() - wall_start, 3),
                        "limit_seconds": wall_limit,
                    }
                )
            )
            break

        if iteration >= 19:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "URGENT: You are at iteration 20 or later. Do not call any more tools. "
                                "Output only plain assistant text that includes the exact completion marker "
                                "[[ADVERSARIAL_COMPLETE]] on its own line."
                            ),
                        }
                    ],
                }
            )

        try:
            response = client.messages.create(
                model=model_name,
                max_tokens=8192,
                temperature=0,
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=ex.TOOLS,
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
                print("--- Exit: [[ADVERSARIAL_COMPLETE]] ---")
                return
            print(
                f"--- iteration {iteration + 1}: no tool calls "
                f"(stop_reason={getattr(response, 'stop_reason', None)}); nudging ---"
            )
            if iteration >= 19:
                nudge_body = (
                    "URGENT: Stop calling tools. Output only assistant text containing the marker "
                    "[[ADVERSARIAL_COMPLETE]] on its own line. Do not invoke tools again."
                )
            else:
                nudge_body = (
                    "Continue adversarial testing with the tools until all six rules "
                    "and edge-case probes are done and logged via report_findings. "
                    "When finished, output [[ADVERSARIAL_COMPLETE]]. "
                    "Do not stop on end_turn alone."
                )
            messages.append(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": nudge_body}],
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
            result = ex.dispatch_tool(name, tool_input)
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
            print("--- Exit: [[ADVERSARIAL_COMPLETE]] (marker after tool results) ---")
            return
    else:
        print("--- Max iterations reached without [[ADVERSARIAL_COMPLETE]] ---")
        print(json.dumps(messages[-3:], indent=2, default=str))


if __name__ == "__main__":
    run_adversarial()
