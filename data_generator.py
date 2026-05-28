"""
Generate 50 randomized synthetic P2P test scenarios with controlled violations.
All monetary math uses Decimal + ROUND_HALF_UP; JSON stores money as strings.
"""

from __future__ import annotations

import argparse
import json
import random
import secrets
import string
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

Violation = Literal[
    "none",
    "overpayment_protection",
    "duplicate_invoice",
    "inactive_vendor",
    "unmatched_invoice",
    "partial_receipt_overpayment",
    "gl_imbalance",
]


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def q2(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def rand_vendor_name(rng: random.Random) -> str:
    adj = rng.choice(
        ["Acme", "Northwind", "Globex", "Initech", "Umbrella", "Stark", "Wayne", "Cyberdyne"]
    )
    suffix = "".join(rng.choices(string.ascii_uppercase + string.digits, k=4))
    return f"{adj} Supply {suffix}"


def rand_sku(rng: random.Random) -> str:
    return f"SKU-{rng.randint(1000, 9999)}-{rng.choice(['A', 'B', 'C'])}"


def po_line_total(qty: Decimal, unit: Decimal) -> Decimal:
    return q2(qty * unit)


def build_violation_list(rng: random.Random) -> list[Violation]:
    """50 scenarios: 8 clean, 7 per violation type (6 types)."""
    counts: dict[Violation, int] = {
        "none": 8,
        "overpayment_protection": 7,
        "duplicate_invoice": 7,
        "inactive_vendor": 7,
        "unmatched_invoice": 7,
        "partial_receipt_overpayment": 7,
        "gl_imbalance": 7,
    }
    out: list[Violation] = []
    for v, n in counts.items():
        out.extend([v] * n)
    rng.shuffle(out)
    if out[0] == "duplicate_invoice":
        for j in range(1, len(out)):
            if out[j] != "duplicate_invoice":
                out[0], out[j] = out[j], out[0]
                break
    return out


def generate_scenario(
    idx: int,
    violation: Violation,
    rng: random.Random,
    prior_scenarios: list[dict[str, Any]],
) -> dict[str, Any]:
    sid = f"S-{idx+1:03d}"
    src: dict[str, Any] | None = None

    n_lines = rng.randint(1, 4)
    lines: list[dict[str, Any]] = []
    po_total = money(0)

    for li in range(n_lines):
        qty_ord = money(rng.randint(5, 120))
        unit = money(rng.uniform(1.25, 350.0))
        ext = po_line_total(qty_ord, unit)
        po_total = q2(po_total + ext)
        lines.append(
            {
                "line_index": li,
                "sku": rand_sku(rng),
                "quantity_ordered": str(qty_ord),
                "unit_price": str(unit),
                "line_total": str(ext),
            }
        )

    fulfill_pct = money(rng.uniform(40.0, 100.0))
    receipt_lines: list[dict[str, Any]] = []
    received_value = money(0)

    for pl in lines:
        qo = money(pl["quantity_ordered"])
        recv = q2(qo * fulfill_pct / money(100))
        if recv > qo:
            recv = qo
        if recv < money(0):
            recv = money(0)
        unit = money(pl["unit_price"])
        rv = po_line_total(recv, unit)
        received_value = q2(received_value + rv)
        receipt_lines.append(
            {
                "line_index": pl["line_index"],
                "quantity_received": str(recv),
                "unit_price": str(unit),
                "line_value": str(rv),
            }
        )

    vendor_status: Literal["active", "inactive"] = "active"
    if violation == "inactive_vendor":
        vendor_status = "inactive"

    vendor = {
        "vendor_id": f"V-{idx+1:04d}",
        "name": rand_vendor_name(rng),
        "status": vendor_status,
    }

    dup_of: str | None = None
    dup_inv_num: str | None = None

    if violation == "duplicate_invoice":
        candidates = [p for p in prior_scenarios if p.get("expected_violation") == "none"]
        if not candidates:
            candidates = prior_scenarios[:]
        src = rng.choice(candidates)
        dup_of = src["scenario_id"]
        dup_inv_num = src["invoice"]["invoice_number"]
        vendor = json.loads(json.dumps(src["vendor"]))
        vendor["vendor_id"] = f"V-{idx+1:04d}"
        lines = json.loads(json.dumps(src["purchase_order"]["lines"]))
        receipt_lines = json.loads(json.dumps(src["goods_receipt"]["lines"]))
        fulfill_pct = money(src["goods_receipt"]["fulfillment_percent"])
        po_total = money(0)
        for pl in lines:
            po_total = q2(po_total + money(pl["line_total"]))
        received_value = money(src["goods_receipt"]["received_value_total"])

    invoice_lines: list[dict[str, Any]] = []

    for pl in lines:
        li = int(pl["line_index"])
        qo = money(pl["quantity_ordered"])
        unit = money(pl["unit_price"])
        recv = money(next(r["quantity_received"] for r in receipt_lines if int(r["line_index"]) == li))

        inv_qty: Decimal
        inv_unit: Decimal

        if violation == "duplicate_invoice" and src is not None:
            inv_line = next(x for x in src["invoice"]["lines"] if int(x["line_index"]) == li)
            inv_qty = money(inv_line["quantity"])
            inv_unit = money(inv_line["unit_price"])
        elif violation == "unmatched_invoice":
            inv_qty = recv
            inv_unit = unit
            if rng.random() < 0.5:
                inv_unit = q2(unit + money(rng.choice([0.01, 0.03, 1.0, 2.5])))
            else:
                inv_qty = q2(recv + money(rng.randint(1, 5)))
        elif violation == "partial_receipt_overpayment":
            bump = money(rng.randint(1, 20))
            inv_qty = q2(min(qo, recv + bump))
            inv_unit = unit
        elif violation == "overpayment_protection":
            inv_qty = q2(qo)
            inv_unit = unit
        else:
            inv_qty = recv
            inv_unit = unit

        lt = q2(inv_qty * inv_unit)
        invoice_lines.append(
            {
                "line_index": li,
                "quantity": str(inv_qty),
                "unit_price": str(inv_unit),
                "line_total": str(lt),
            }
        )

    invoice_total = money(0)
    for il in invoice_lines:
        invoice_total = q2(invoice_total + money(il["line_total"]))

    if violation == "overpayment_protection":
        bump = money(rng.uniform(25.0, 250.0))
        if invoice_lines:
            last = invoice_lines[-1]
            new_lt = q2(money(last["line_total"]) + bump)
            last["line_total"] = str(new_lt)
        invoice_total = money(0)
        for il in invoice_lines:
            invoice_total = q2(invoice_total + money(il["line_total"]))

    if violation == "gl_imbalance":
        debit = invoice_total
        credit = q2(debit - money(rng.uniform(0.05, 5.0)))
        gl_entries = {
            "currency": "USD",
            "debit_total": str(debit),
            "credit_total": str(credit),
            "imbalance": str(q2(debit - credit)),
        }
    else:
        debit = invoice_total
        credit = invoice_total
        gl_entries = {
            "currency": "USD",
            "debit_total": str(debit),
            "credit_total": str(credit),
            "imbalance": "0.00",
        }

    inv_num = dup_inv_num if violation == "duplicate_invoice" else f"INV-{idx+1:05d}-{rng.randint(100, 999)}"

    return {
        "scenario_id": sid,
        "expected_violation": violation,
        "vendor": vendor,
        "purchase_order": {
            "lines": lines,
            "total": str(po_total),
        },
        "goods_receipt": {
            "fulfillment_percent": str(fulfill_pct),
            "lines": receipt_lines,
            "received_value_total": str(received_value),
        },
        "invoice": {
            "invoice_number": inv_num,
            "lines": invoice_lines,
            "invoice_total": str(invoice_total),
            "duplicate_of_scenario_id": dup_of,
        },
        "gl": gl_entries,
        "notes": _note(violation, fulfill_pct, dup_of),
    }


def _note(v: Violation, fulfill_pct: Decimal, dup_of: str | None) -> str:
    if v == "none":
        return "Clean path: invoice matches received qty and PO price; totals align."
    if v == "overpayment_protection":
        return "Invoice total exceeds PO scope / authorized amount."
    if v == "duplicate_invoice":
        return f"Second submission duplicates vendor+invoice_number from {dup_of}."
    if v == "inactive_vendor":
        return "Vendor master is inactive; PO/invoice should be blocked."
    if v == "unmatched_invoice":
        return "Invoice qty or price does not match PO/receipt (3-way match failure)."
    if v == "partial_receipt_overpayment":
        return f"Fulfillment {fulfill_pct}%; invoice bills beyond received quantities."
    if v == "gl_imbalance":
        return "GL debits and credits do not net to zero for the invoice posting."
    return ""


def generate_payload(rng: random.Random | None = None) -> dict[str, Any]:
    if rng is None:
        rng = random.Random(int.from_bytes(secrets.token_bytes(8), "big"))
    violations = build_violation_list(rng)
    scenarios: list[dict[str, Any]] = []
    for i, v in enumerate(violations):
        scenarios.append(generate_scenario(i, v, rng, scenarios))
    return {
        "version": 1,
        "generated_scenario_count": len(scenarios),
        "scenarios": scenarios,
    }


def summarize_violations(scenarios: list[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for s in scenarios:
        key = str(s["expected_violation"])
        summary[key] = summary.get(key, 0) + 1
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate synthetic P2P scenarios JSON.")
    ap.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for reproducible output (omit for cryptographically random).",
    )
    ap.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output JSON path (default: synthetic_scenarios.json next to this script).",
    )
    args = ap.parse_args()

    if args.seed is not None:
        rng = random.Random(args.seed)
    else:
        rng = random.Random(int.from_bytes(secrets.token_bytes(8), "big"))

    payload = generate_payload(rng)
    scenarios = payload["scenarios"]

    out_path = args.output.resolve() if args.output else Path(__file__).resolve().parent / "synthetic_scenarios.json"
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    summary = summarize_violations(scenarios)

    print(f"Wrote {out_path.resolve()}")
    print()
    print("Scenarios per violation type:")
    for k in sorted(summary.keys(), key=lambda x: (x != "none", x)):
        print(f"  {k}: {summary[k]}")
    print()
    print(f"Total: {len(scenarios)}")


if __name__ == "__main__":
    main()
