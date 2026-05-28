"""
Domain-agnostic adversarial red-team loop. All domain-specific logic
(store, tools, prompts) is injected via DomainSpec.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from agent_client import agent_wall_clock_seconds, make_anthropic_client
from domains._base import DomainSpec, build_full_tools, report_findings


# ---------------------------------------------------------------------------
# Agent loop helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Main adversarial loop
# ---------------------------------------------------------------------------

def run_adversarial(
    domain: DomainSpec,
    user_prompt: str | None = None,
    *,
    max_iterations: int = 30,
    model: str | None = None,
    live: bool = False,
) -> None:
    """Run the adversarial agent loop for the given domain.

    Each run creates a fresh store -- no cross-contamination from exploration.
    """
    store = domain.MockStore()

    client = make_anthropic_client()
    model_name = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

    wall_start = time.monotonic()
    wall_limit = agent_wall_clock_seconds()
    wall_deadline = wall_start + wall_limit

    initial = user_prompt or domain.default_adversarial_user_prompt
    tools = build_full_tools(domain.domain_tools)

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
                system=domain.adversarial_system_prompt,
                messages=messages,
                tools=tools,
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
                    "Continue adversarial testing with the tools until all control rules "
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

            if name == "report_findings":
                result = report_findings(
                    step_name=tool_input["step_name"],
                    request_summary=tool_input["request_summary"],
                    response_summary=tool_input["response_summary"],
                    outcome_correct=tool_input["outcome_correct"],
                    notes=tool_input["notes"],
                )
            elif live and domain.dispatch_live_http is not None:
                result = domain.dispatch_live_http(name, tool_input)
            else:
                result = store.dispatch(name, tool_input)

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
    from domains import load_domain

    domain = load_domain("p2p")
    run_adversarial(domain)
