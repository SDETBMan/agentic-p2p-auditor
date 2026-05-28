"""
Medical lien domain control rules and rejection signal detection.

These controls map to the compliance requirements of pre-settlement funding
and medical lien management systems (e.g., MoveDocs).
"""

from __future__ import annotations

from typing import Any

CONTROL_RULES: list[dict[str, Any]] = [
    {
        "name": "lien_priority_enforcement",
        "description": "Federal liens (Medicare/Medicaid) subordinated in payment order",
    },
    {
        "name": "balance_cap",
        "description": "Lien balance inflated beyond original billed amount",
    },
    {
        "name": "duplicate_lien_detection",
        "description": "Same provider + case + service date paying twice",
    },
    {
        "name": "provider_status_gate",
        "description": "Liens filed by suspended/excluded providers accepted",
    },
    {
        "name": "settlement_waterfall_order",
        "description": "Disbursement out of legal priority order",
    },
    {
        "name": "reduction_negotiation_cap",
        "description": "Negotiated reduction exceeding policy max percentage",
    },
]


def rejection_signals(obj: dict[str, Any]) -> list[str]:
    """Signals that indicate the mock lien API explicitly rejected an operation."""
    signals: list[str] = []

    if "error" in obj:
        signals.append("error")
        err = obj.get("error")
        if isinstance(err, str):
            signals.append(f"error:{err}")

    # Balance cap violation
    if obj.get("balance_cap_exceeded") is True:
        signals.append("balance_cap_exceeded")

    # Duplicate lien detection
    msg = obj.get("message", "")
    if isinstance(msg, str) and "duplicate" in msg.lower():
        signals.append("duplicate_lien_message")
    if isinstance(obj.get("error"), str) and "duplicate" in obj["error"].lower():
        signals.append("duplicate_lien_error")

    # Provider status gate
    if isinstance(msg, str) and ("suspended" in msg.lower() or "excluded" in msg.lower()):
        signals.append("provider_status_rejected")
    if obj.get("provider_status_gate") is False:
        signals.append("provider_status_gate:false")

    # Lien priority enforcement
    if obj.get("priority_violation") is True:
        signals.append("priority_violation")
    if isinstance(msg, str) and "priority" in msg.lower():
        signals.append("priority_order_message")

    # Settlement waterfall order
    if obj.get("waterfall_violation") is True:
        signals.append("waterfall_violation")

    # Reduction negotiation cap
    if obj.get("reduction_cap_exceeded") is True:
        signals.append("reduction_cap_exceeded")

    # Compliance check failures
    compliance = obj.get("compliance_status")
    if compliance == "non_compliant":
        signals.append("compliance_status:non_compliant")

    return signals
