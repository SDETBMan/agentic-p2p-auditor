"""
Deterministic mock P2P store -- extracted from exploration_agent.py.
Monetary values use Decimal internally; floats appear only at JSON serialization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Money helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# MockP2PStore
# ---------------------------------------------------------------------------

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

    # -- dispatch interface (required by DomainSpec) -----------------------

    def dispatch(self, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        """Route a tool call by name to the appropriate handler."""
        if name == "create_purchase_order":
            return self.create_purchase_order(
                vendor_name=tool_input.get("vendor_name"),
                lines=tool_input.get("lines"),
                purchase_order_id=tool_input.get("purchase_order_id"),
                submit_purchase_order=bool(tool_input.get("submit_purchase_order")),
                receive_lines=tool_input.get("receive_lines"),
            )
        if name == "submit_invoice":
            return self.submit_invoice(
                purchase_order_id=tool_input["purchase_order_id"],
                invoice_number=tool_input["invoice_number"],
                lines=tool_input["lines"],
            )
        if name == "process_payment":
            return self.process_payment(tool_input["invoice_id"])
        if name == "get_transaction_status":
            return self.get_transaction_status(
                tool_input["entity_type"],
                tool_input["entity_id"],
            )
        return {"error": "unknown_tool", "name": name}

    # -- P2P tool implementations ------------------------------------------

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
