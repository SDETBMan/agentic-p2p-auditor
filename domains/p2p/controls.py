"""
P2P domain control rules and rejection signal detection.
"""

from __future__ import annotations

from typing import Any

CONTROL_RULES: list[dict[str, Any]] = [
    {"name": "overpayment_protection", "description": "Cumulative payments exceeding PO/invoice authorization"},
    {"name": "three_way_match_gate", "description": "Invoice approval without matching PO + receipt within tolerance"},
    {"name": "partial_receipt_flag", "description": "Full invoice matching against partially received goods"},
    {"name": "inactive_vendor_gate", "description": "PO submission or invoice posting against blocked/inactive vendors"},
    {"name": "gl_balance", "description": "Unbalanced debit/credit postings corrupting the general ledger"},
    {"name": "duplicate_invoice_detection", "description": "Same vendor + invoice number paying twice"},
]


def rejection_signals(obj: dict[str, Any]) -> list[str]:
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
