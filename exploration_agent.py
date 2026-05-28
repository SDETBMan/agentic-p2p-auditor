"""
Agentic exploration loop for end-to-end P2P QA using the Anthropic Messages API.
Monetary values use Decimal internally; floats appear only at JSON serialization.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

import p2p_live

from agent_client import agent_wall_clock_seconds, make_anthropic_client

# -----------------------------------------------------------------------------
# Money: Decimal only for calculations; float only at JSON boundary
# -----------------------------------------------------------------------------


def money(value: Any) -> Decimal:
    """Convert arbitrary numeric input to a 2dp Decimal via str() to avoid float noise."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def money_to_json(d: Decimal) -> float:
    """Serialize Decimal to JSON-compatible float (boundary only)."""
    return float(d)


def _json_safe(obj: Any) -> Any:
    """Recursively convert Decimals to float for JSON/tool outputs."""
    if isinstance(obj, Decimal):
        return money_to_json(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


# -----------------------------------------------------------------------------
# Deterministic mock P2P store
# -----------------------------------------------------------------------------


@dataclass
class POLine:
    sku: str
    quantity_ordered: Decimal
    unit_price: Decimal
    quantity_received: Decimal = field(default_factory=lambda: money(0))


@dataclass
class PurchaseOrder:
    po_id: str
    vendor_id: str
    status: Literal["draft", "submitted", "closed"]
    lines: list[POLine]


@dataclass
class InvoiceLine:
    line_index: int
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal


@dataclass
class Invoice:
    invoice_id: str
    po_id: str
    invoice_number: str
    status: Literal["received", "matched", "approved", "paid"]
    lines: list[InvoiceLine]
    match_status: str | None = None
    three_way_match_passed: bool = False


class MockP2PStore:
    """In-memory deterministic mock for P2P tool handlers."""

    def __init__(self) -> None:
        self._vendors: dict[str, dict[str, Any]] = {}
        self._pos: dict[str, PurchaseOrder] = {}
        self._invoices: dict[str, Invoice] = {}
        self._payments: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter:04d}"

    def create_purchase_order(
        self,
        vendor_name: str | None,
        lines: list[dict[str, Any]] | None,
        purchase_order_id: str | None = None,
        submit_purchase_order: bool = False,
        receive_lines: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        receive_lines = receive_lines or []
        lines = lines or []

        if purchase_order_id is None:
            if not vendor_name or not lines:
                return {
                    "error": "validation_error",
                    "message": "vendor_name and non-empty lines are required when creating a new PO.",
                }
            vendor_id = self._next_id("ven")
            self._vendors[vendor_id] = {
                "vendor_id": vendor_id,
                "name": vendor_name,
                "status": "active",
            }
            po_id = self._next_id("po")
            po_lines: list[POLine] = []
            for row in lines:
                qty = money(row["quantity"])
                unit = money(row["unit_price"])
                po_lines.append(
                    POLine(
                        sku=str(row.get("sku", "ITEM")),
                        quantity_ordered=qty,
                        unit_price=unit,
                    )
                )
            po = PurchaseOrder(
                po_id=po_id,
                vendor_id=vendor_id,
                status="draft",
                lines=po_lines,
            )
            self._pos[po_id] = po
        else:
            po = self._pos.get(purchase_order_id)
            if not po:
                return {"error": f"purchase_order_id not found: {purchase_order_id}"}
            po_id = po.po_id

        po = self._pos[po_id]

        if submit_purchase_order and po.status == "draft":
            vid = po.vendor_id
            if self._vendors.get(vid, {}).get("status") != "active":
                return {
                    "error": "inactive_vendor_gate",
                    "message": "Cannot submit PO: vendor is not active.",
                }
            po.status = "submitted"

        for rec in receive_lines:
            idx = int(rec["line_index"])
            qty_in = money(rec["quantity_received"])
            if idx < 0 or idx >= len(po.lines):
                return {"error": f"invalid line_index: {idx}"}
            line = po.lines[idx]
            new_recv = (line.quantity_received + qty_in).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if new_recv > line.quantity_ordered:
                return {
                    "error": "over_receipt",
                    "message": "Received quantity cannot exceed ordered quantity.",
                }
            line.quantity_received = new_recv

        po_total = money(0)
        for ln in po.lines:
            line_ext = (ln.quantity_ordered * ln.unit_price).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            po_total += line_ext
        po_total = po_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        partial_flags = [
            {
                "line_index": i,
                "partial_receipt": bool(
                    ln.quantity_received > 0 and ln.quantity_received < ln.quantity_ordered
                ),
            }
            for i, ln in enumerate(po.lines)
        ]

        return _json_safe(
            {
                "vendor_id": po.vendor_id,
                "purchase_order_id": po.po_id,
                "status": po.status,
                "lines": [
                    {
                        "line_index": i,
                        "sku": ln.sku,
                        "quantity_ordered": ln.quantity_ordered,
                        "quantity_received": ln.quantity_received,
                        "unit_price": ln.unit_price,
                        "line_total": (ln.quantity_ordered * ln.unit_price).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        ),
                    }
                    for i, ln in enumerate(po.lines)
                ],
                "purchase_order_total": po_total,
                "partial_receipt_flags": partial_flags,
            }
        )

    def submit_invoice(
        self,
        purchase_order_id: str,
        invoice_number: str,
        lines: list[dict[str, Any]],
    ) -> dict[str, Any]:
        po = self._pos.get(purchase_order_id)
        if not po:
            return {"error": f"purchase_order_id not found: {purchase_order_id}"}
        if po.status != "submitted":
            return {"error": "po_not_submitted", "message": "PO must be submitted before invoicing."}

        inv_lines: list[InvoiceLine] = []
        for row in lines:
            idx = int(row["line_index"])
            qty = money(row["quantity"])
            unit = money(row["unit_price"])
            lt = (qty * unit).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            inv_lines.append(
                InvoiceLine(line_index=idx, quantity=qty, unit_price=unit, line_total=lt)
            )

        for inv in self._invoices.values():
            if inv.invoice_number == invoice_number and self._pos[inv.po_id].vendor_id == po.vendor_id:
                return {
                    "error": "duplicate_invoice",
                    "message": "Duplicate invoice for this vendor and invoice number.",
                    "duplicate_check_key": _json_safe({"vendor_id": po.vendor_id, "invoice_number": invoice_number}),
                }

        three_way_ok = True
        variance_lines: list[dict[str, Any]] = []
        for il in inv_lines:
            if il.line_index < 0 or il.line_index >= len(po.lines):
                three_way_ok = False
                variance_lines.append({"line_index": il.line_index, "reason": "no_matching_po_line"})
                continue
            pl = po.lines[il.line_index]
            qty_ok = il.quantity <= pl.quantity_received
            price_ok = il.unit_price == pl.unit_price
            if not qty_ok or not price_ok:
                three_way_ok = False
                variance_lines.append(
                    {
                        "line_index": il.line_index,
                        "reason": "quantity_or_price_mismatch",
                        "ordered": pl.quantity_ordered,
                        "received": pl.quantity_received,
                        "invoiced_qty": il.quantity,
                        "po_unit_price": pl.unit_price,
                        "invoice_unit_price": il.unit_price,
                    }
                )

        invoice_id = self._next_id("inv")
        inv = Invoice(
            invoice_id=invoice_id,
            po_id=purchase_order_id,
            invoice_number=invoice_number,
            status="matched" if three_way_ok else "received",
            lines=inv_lines,
            match_status="matched" if three_way_ok else "variance_hold",
            three_way_match_passed=three_way_ok,
        )
        self._invoices[invoice_id] = inv

        inv_total = money(0)
        for x in inv_lines:
            inv_total += x.line_total
        inv_total = inv_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        return _json_safe(
            {
                "invoice_id": invoice_id,
                "purchase_order_id": purchase_order_id,
                "invoice_number": invoice_number,
                "three_way_match_gate": three_way_ok,
                "match_status": inv.match_status,
                "variance_lines": variance_lines,
                "invoice_total": inv_total,
            }
        )

    def process_payment(self, invoice_id: str) -> dict[str, Any]:
        inv = self._invoices.get(invoice_id)
        if not inv:
            return {"error": f"invoice_id not found: {invoice_id}"}
        if not inv.three_way_match_passed:
            return {
                "error": "three_way_match_required",
                "message": "Cannot pay: 3-way match did not pass.",
            }
        inv_total = money(0)
        for ln in inv.lines:
            inv_total += ln.line_total
        inv_total = inv_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        po = self._pos[inv.po_id]
        po_total = money(0)
        for ln in po.lines:
            line_ext = (ln.quantity_ordered * ln.unit_price).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            po_total += line_ext
        po_total = po_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if inv_total > po_total:
            return {
                "error": "overpayment_protection",
                "message": "Invoice total exceeds PO authorized amount.",
                "invoice_total": inv_total,
                "po_total": po_total,
            }
        inv.status = "approved"
        pay_id = self._next_id("pay")
        self._payments[pay_id] = {
            "payment_id": pay_id,
            "invoice_id": invoice_id,
            "amount": inv_total,
            "status": "scheduled",
        }
        inv.status = "paid"
        return _json_safe(
            {
                "payment_id": pay_id,
                "invoice_id": invoice_id,
                "amount": inv_total,
                "status": "completed",
                "gl_balance_check": "balanced",
            }
        )

    def get_transaction_status(self, entity_type: str, entity_id: str) -> dict[str, Any]:
        if entity_type == "vendor":
            v = self._vendors.get(entity_id)
            if not v:
                return {"error": "not_found"}
            return _json_safe(dict(v))
        if entity_type == "purchase_order":
            po = self._pos.get(entity_id)
            if not po:
                return {"error": "not_found"}
            return _json_safe(
                {
                    "purchase_order_id": po.po_id,
                    "vendor_id": po.vendor_id,
                    "status": po.status,
                    "lines": [
                        {
                            "line_index": i,
                            "sku": ln.sku,
                            "quantity_ordered": ln.quantity_ordered,
                            "quantity_received": ln.quantity_received,
                            "unit_price": ln.unit_price,
                        }
                        for i, ln in enumerate(po.lines)
                    ],
                }
            )
        if entity_type == "invoice":
            inv = self._invoices.get(entity_id)
            if not inv:
                return {"error": "not_found"}
            return _json_safe(
                {
                    "invoice_id": inv.invoice_id,
                    "purchase_order_id": inv.po_id,
                    "invoice_number": inv.invoice_number,
                    "status": inv.status,
                    "three_way_match_passed": inv.three_way_match_passed,
                    "match_status": inv.match_status,
                }
            )
        if entity_type == "payment":
            p = self._payments.get(entity_id)
            if not p:
                return {"error": "not_found"}
            return _json_safe(dict(p))
        return {"error": "unknown_entity_type", "entity_type": entity_type}

    def report_findings(
        self,
        step_name: str,
        request_summary: str,
        response_summary: str,
        outcome_correct: bool,
        notes: str,
    ) -> dict[str, Any]:
        return {
            "recorded": True,
            "finding_id": str(uuid.uuid4()),
            "step_name": step_name,
            "request_summary": request_summary,
            "response_summary": response_summary,
            "outcome_correct": outcome_correct,
            "notes": notes,
        }


# -----------------------------------------------------------------------------
# Tool definitions for Anthropic API
# -----------------------------------------------------------------------------

STORE = MockP2PStore()

TOOLS: list[dict[str, Any]] = [
    {
        "name": "create_purchase_order",
        "description": (
            "Create a vendor and draft PO, or update an existing PO by id: submit the PO "
            "and/or record goods received. Use for vendor+PO creation, PO submit, and receipts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "vendor_name": {
                    "type": "string",
                    "description": "Required for new PO. Omit when only updating via purchase_order_id.",
                },
                "lines": {
                    "type": "array",
                    "description": "PO lines when creating a new PO; omit or [] when updating via purchase_order_id.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sku": {"type": "string"},
                            "quantity": {"type": "string", "description": "Decimal-safe quantity as string."},
                            "unit_price": {"type": "string", "description": "Unit price as string for Decimal parsing."},
                        },
                        "required": ["quantity", "unit_price"],
                    },
                },
                "purchase_order_id": {
                    "type": "string",
                    "description": "Existing PO id for submit/receive updates; omit to create new vendor+PO.",
                },
                "submit_purchase_order": {
                    "type": "boolean",
                    "description": "If true, transition draft PO to submitted (inactive vendor gate applies).",
                },
                "receive_lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "line_index": {"type": "integer"},
                            "quantity_received": {"type": "string"},
                        },
                        "required": ["line_index", "quantity_received"],
                    },
                },
            },
            "required": [],
        },
    },
    {
        "name": "submit_invoice",
        "description": (
            "Submit a supplier invoice against a submitted PO; runs 3-way match vs PO and receipts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "purchase_order_id": {"type": "string"},
                "invoice_number": {"type": "string"},
                "lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "line_index": {"type": "integer"},
                            "quantity": {"type": "string"},
                            "unit_price": {"type": "string"},
                        },
                        "required": ["line_index", "quantity", "unit_price"],
                    },
                },
            },
            "required": ["purchase_order_id", "invoice_number", "lines"],
        },
    },
    {
        "name": "process_payment",
        "description": (
            "Approve-for-pay and process payment for a matched invoice (overpayment and GL checks)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"invoice_id": {"type": "string"}},
            "required": ["invoice_id"],
        },
    },
    {
        "name": "get_transaction_status",
        "description": "Fetch status for vendor, purchase_order, invoice, or payment by id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_type": {
                    "type": "string",
                    "enum": ["vendor", "purchase_order", "invoice", "payment"],
                },
                "entity_id": {"type": "string"},
            },
            "required": ["entity_type", "entity_id"],
        },
    },
    {
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
    },
]


def use_live_p2p() -> bool:
    return os.environ.get("P2P_LIVE", "").strip().lower() in ("1", "true", "yes")


def dispatch_tool(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    if name == "report_findings":
        return STORE.report_findings(
            step_name=tool_input["step_name"],
            request_summary=tool_input["request_summary"],
            response_summary=tool_input["response_summary"],
            outcome_correct=tool_input["outcome_correct"],
            notes=tool_input["notes"],
        )
    if use_live_p2p():
        return p2p_live.dispatch_live_http(name, tool_input)
    if name == "create_purchase_order":
        return STORE.create_purchase_order(
            vendor_name=tool_input.get("vendor_name"),
            lines=tool_input.get("lines"),
            purchase_order_id=tool_input.get("purchase_order_id"),
            submit_purchase_order=bool(tool_input.get("submit_purchase_order")),
            receive_lines=tool_input.get("receive_lines"),
        )
    if name == "submit_invoice":
        return STORE.submit_invoice(
            purchase_order_id=tool_input["purchase_order_id"],
            invoice_number=tool_input["invoice_number"],
            lines=tool_input["lines"],
        )
    if name == "process_payment":
        return STORE.process_payment(tool_input["invoice_id"])
    if name == "get_transaction_status":
        return STORE.get_transaction_status(
            tool_input["entity_type"],
            tool_input["entity_id"],
        )
    return {"error": "unknown_tool", "name": name}


SYSTEM_PROMPT = """You are an expert ERP QA engineer. Your job is to run a complete valid P2P workflow end to end — create vendor, create PO, submit PO, receive goods, submit invoice, run 3-way match, approve invoice. Log every step with the request, response, and your interpretation of whether the outcome was correct.

Use the provided tools in a logical order. After each tool call, use report_findings to record the request, the response, your interpretation, and whether the outcome was correct.

When the full workflow is finished and findings are logged, include the exact marker [[EXPLORATION_COMPLETE]] on its own line in your assistant text. Do not treat the conversation turn alone as completion; you must output [[EXPLORATION_COMPLETE]] explicitly.

create_purchase_order can create vendor and PO lines in one call; for later steps pass purchase_order_id with submit_purchase_order and/or receive_lines. Invoice line quantities must not exceed received quantities; unit prices must match PO lines for a passing 3-way match. process_payment covers invoice approval-for-pay and payment after a successful match."""


def _content_blocks_to_assistant_message(content: list[Any]) -> dict[str, Any]:
    return {"role": "assistant", "content": content}


def _extract_text(content: list[Any]) -> str:
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts)


def _has_exit_marker(text: str) -> bool:
    return "[[EXPLORATION_COMPLETE]]" in text


def run_exploration(
    user_prompt: str | None = None,
    *,
    max_iterations: int = 30,
    model: str | None = None,
) -> None:
    client = make_anthropic_client()
    model_name = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

    wall_start = time.monotonic()
    wall_limit = agent_wall_clock_seconds()
    wall_deadline = wall_start + wall_limit

    initial = user_prompt or (
        "Execute the full P2P workflow using the tools. Use realistic data: one PO line, "
        "qty 10 at unit price 25.50, receive full quantity, invoice to match. "
        "Log each step with report_findings. Finish with [[EXPLORATION_COMPLETE]]."
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
                                "[[EXPLORATION_COMPLETE]] on its own line."
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
                print("--- Exit: [[EXPLORATION_COMPLETE]] ---")
                return
            # Never exit on end_turn (or empty stop) alone: nudge model to continue or emit marker
            print(
                f"--- iteration {iteration + 1}: no tool calls "
                f"(stop_reason={getattr(response, 'stop_reason', None)}); nudging ---"
            )
            if iteration >= 19:
                nudge_body = (
                    "URGENT: Stop calling tools. Output only assistant text containing the marker "
                    "[[EXPLORATION_COMPLETE]] on its own line. Do not invoke tools again."
                )
            else:
                nudge_body = (
                    "Continue the P2P workflow using tools until finished. "
                    "When fully complete, output [[EXPLORATION_COMPLETE]]. "
                    "Do not end without that marker."
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
            result = dispatch_tool(name, tool_input)
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
            print("--- Exit: [[EXPLORATION_COMPLETE]] (marker after tool results) ---")
            return
    else:
        print("--- Max iterations reached without [[EXPLORATION_COMPLETE]] ---")
        print(json.dumps(messages[-3:], indent=2, default=str))


if __name__ == "__main__":
    run_exploration()
