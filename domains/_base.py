"""
Base domain interface: DomainSpec dataclass, universal report_findings tool, and tool builder.
Every domain package must conform to the DomainSpec contract.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class DomainSpec:
    """Contract that every domain package must satisfy."""

    name: str
    MockStore: type  # class with __init__() and dispatch(name, tool_input) -> dict
    domain_tools: list[dict[str, Any]]
    exploration_system_prompt: str
    adversarial_system_prompt: str
    judge_system_prompt: str
    default_exploration_user_prompt: str
    default_adversarial_user_prompt: str
    control_rules: list[dict[str, Any]]
    rejection_signals: Callable[[dict[str, Any]], list[str]]
    dispatch_live_http: Optional[Callable[..., dict[str, Any]]] = None
    probe_live_api: Optional[Callable[..., tuple[bool, str]]] = None


# ---------------------------------------------------------------------------
# Universal report_findings tool -- shared across all domains
# ---------------------------------------------------------------------------

REPORT_FINDINGS_TOOL: dict[str, Any] = {
    "name": "report_findings",
    "description": (
        "Log a QA step with request, response interpretation, and whether the outcome was correct."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "step_name": {"type": "string"},
            "request_summary": {"type": "string"},
            "response_summary": {"type": "string"},
            "outcome_correct": {"type": "boolean"},
            "notes": {"type": "string"},
        },
        "required": [
            "step_name",
            "request_summary",
            "response_summary",
            "outcome_correct",
            "notes",
        ],
    },
}


def report_findings(
    step_name: str,
    request_summary: str,
    response_summary: str,
    outcome_correct: bool,
    notes: str,
) -> dict[str, Any]:
    """Universal report_findings handler -- not domain-specific."""
    return {
        "recorded": True,
        "finding_id": str(uuid.uuid4()),
        "step_name": step_name,
        "request_summary": request_summary,
        "response_summary": response_summary,
        "outcome_correct": outcome_correct,
        "notes": notes,
    }


def build_full_tools(domain_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Combine domain-specific tools with the universal report_findings tool."""
    return domain_tools + [REPORT_FINDINGS_TOOL]
