"""
Live HTTP adapter for P2P tools against a REST API (see p2p_api_spec.md).
Uses stdlib urllib only. Returns dicts compatible with mock tool responses.

Moved from top-level p2p_live.py into the P2P domain package.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin


def live_base_url() -> str:
    return os.environ.get("P2P_LIVE_BASE_URL", "http://localhost:8000").rstrip("/")


def _json_loads(s: str) -> Any:
    if not s.strip():
        return {}
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return {"raw": s}


def _request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    base = live_base_url()
    url = urljoin(base + "/", path.lstrip("/"))
    data: bytes | None = None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
            payload = _json_loads(raw) if raw else {}
            if isinstance(payload, dict):
                payload["_http_status"] = status
            return payload if isinstance(payload, dict) else {"_data": payload, "_http_status": status}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        parsed = _json_loads(raw)
        return {
            "error": "http_error",
            "http_status": e.code,
            "message": getattr(e, "reason", str(e)),
            "body": parsed if isinstance(parsed, dict) else {"detail": parsed},
        }
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
        return {
            "error": "http_unreachable",
            "message": f"{type(e).__name__}: {e}",
        }


def probe_live_api(base_url: str | None = None, *, timeout: float = 5.0) -> tuple[bool, str]:
    """
    Return (ok, detail). Tries GET /vendors on the API (per p2p_api_spec.md).
    """
    if base_url:
        os.environ["P2P_LIVE_BASE_URL"] = base_url.rstrip("/")
    base = live_base_url()
    url = urljoin(base + "/", "vendors")
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, f"OK (HTTP {resp.status})"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404, 405):
            return True, f"reachable but returned HTTP {e.code} (endpoint may differ)"
        return False, f"HTTP {e.code}: {e.reason}"
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
        return False, f"Cannot reach live P2P API at {base}: {e}"
    except Exception as e:
        return False, f"Unexpected error probing {base}: {e}"


def _pick_id(obj: dict[str, Any], *keys: str) -> str | None:
    for k in keys:
        v = obj.get(k)
        if v is not None and str(v).strip():
            return str(v)
    return None


def dispatch_live_http(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    if name == "create_purchase_order":
        return _live_create_purchase_order(tool_input)
    if name == "submit_invoice":
        return _live_submit_invoice(tool_input)
    if name == "process_payment":
        return _live_process_payment(tool_input)
    if name == "get_transaction_status":
        return _live_get_transaction_status(tool_input)
    return {"error": "unknown_tool", "name": name}


def _live_create_purchase_order(tool_input: dict[str, Any]) -> dict[str, Any]:
    pid = tool_input.get("purchase_order_id")
    if pid:
        out: dict[str, Any] = {}
        if tool_input.get("submit_purchase_order"):
            r = _request("POST", f"/purchase-orders/{pid}/submit", {})
            if r.get("error"):
                return r
            out["submit_response"] = r
        recv = tool_input.get("receive_lines") or []
        if recv:
            lines_body = []
            for row in recv:
                lines_body.append(
                    {
                        "line_index": int(row["line_index"]),
                        "quantity_received": str(row.get("quantity_received", "")),
                    }
                )
            r = _request("POST", f"/purchase-orders/{pid}/receive", {"lines": lines_body})
            if r.get("error"):
                return r
            out["receive_response"] = r
        gr = _request("GET", f"/purchase-orders/{pid}")
        if gr.get("error"):
            return gr
        return _normalize_po_response(gr)

    vendor_name = tool_input.get("vendor_name")
    lines = tool_input.get("lines") or []
    if not vendor_name or not lines:
        return {
            "error": "validation_error",
            "message": "vendor_name and non-empty lines are required when creating a new PO.",
        }

    vr = _request("POST", "/vendors", {"name": vendor_name, "status": "active"})
    if vr.get("error"):
        return vr
    vendor_id = _pick_id(vr, "id", "vendor_id")
    if not vendor_id:
        return {"error": "vendor_create_unexpected", "body": vr}

    api_lines: list[dict[str, Any]] = []
    for row in lines:
        api_lines.append(
            {
                "sku": str(row.get("sku", "ITEM")),
                "quantity": row.get("quantity"),
                "unit_price": row.get("unit_price"),
            }
        )

    pr = _request("POST", "/purchase-orders", {"vendor_id": vendor_id, "lines": api_lines})
    if pr.get("error"):
        return pr
    po_id = _pick_id(pr, "id", "purchase_order_id")
    if not po_id:
        return {"error": "po_create_unexpected", "body": pr}

    merged = dict(pr)
    if tool_input.get("submit_purchase_order"):
        sr = _request("POST", f"/purchase-orders/{po_id}/submit", {})
        if sr.get("error"):
            return sr
        merged["submit_response"] = sr

    recv = tool_input.get("receive_lines") or []
    if recv:
        lines_body = []
        for row in recv:
            lines_body.append(
                {
                    "line_index": int(row["line_index"]),
                    "quantity_received": str(row.get("quantity_received", "")),
                }
            )
        rr = _request("POST", f"/purchase-orders/{po_id}/receive", {"lines": lines_body})
        if rr.get("error"):
            return rr
        merged["receive_response"] = rr

    gr = _request("GET", f"/purchase-orders/{po_id}")
    if gr.get("error"):
        return gr
    return _normalize_po_response(gr, vendor_id=vendor_id)


def _normalize_po_response(api_po: dict[str, Any], vendor_id: str | None = None) -> dict[str, Any]:
    po_id = _pick_id(api_po, "id", "purchase_order_id") or ""
    vid = vendor_id or _pick_id(api_po, "vendor_id", "vendorId") or ""
    lines = api_po.get("lines") or []
    out_lines: list[dict[str, Any]] = []
    for i, ln in enumerate(lines):
        if isinstance(ln, dict):
            out_lines.append(
                {
                    "line_index": int(ln.get("line_index", i)),
                    "sku": str(ln.get("sku", ln.get("item", ""))),
                    "quantity_ordered": str(ln.get("quantity_ordered", ln.get("quantity", ""))),
                    "quantity_received": str(ln.get("quantity_received", ln.get("received", "0"))),
                    "unit_price": str(ln.get("unit_price", "")),
                }
            )
    return {
        "vendor_id": vid,
        "purchase_order_id": po_id,
        "status": str(api_po.get("status", "")),
        "lines": out_lines,
        "purchase_order_total": str(api_po.get("total", api_po.get("purchase_order_total", ""))),
        "partial_receipt_flags": api_po.get("partial_receipt_flags", []),
        "_live": True,
        "_raw": api_po,
    }


def _live_submit_invoice(tool_input: dict[str, Any]) -> dict[str, Any]:
    po_id = tool_input["purchase_order_id"]
    po = _request("GET", f"/purchase-orders/{po_id}")
    if po.get("error"):
        return po
    vendor_id = _pick_id(po, "vendor_id", "vendorId")
    if not vendor_id:
        return {"error": "po_missing_vendor_id", "body": po}

    inv_lines: list[dict[str, Any]] = []
    for row in tool_input.get("lines") or []:
        inv_lines.append(
            {
                "line_index": int(row["line_index"]),
                "quantity": row.get("quantity"),
                "unit_price": row.get("unit_price"),
            }
        )

    body = {
        "vendor_id": vendor_id,
        "purchase_order_id": po_id,
        "invoice_number": tool_input["invoice_number"],
        "lines": inv_lines,
    }
    ir = _request("POST", "/invoices", body)
    if ir.get("error"):
        return ir
    inv_id = _pick_id(ir, "id", "invoice_id")
    if not inv_id:
        return {"error": "invoice_create_unexpected", "body": ir}

    mr = _request("POST", f"/invoices/{inv_id}/match", {})
    if mr.get("error"):
        return {**ir, "match_error": mr}
    ms = str(mr.get("match_status", mr.get("status", ""))).lower()
    variances = mr.get("variance_lines") or []
    gate_ok = ms in ("matched", "ok", "success", "complete") and not variances
    return {
        "invoice_id": inv_id,
        "purchase_order_id": po_id,
        "invoice_number": tool_input["invoice_number"],
        "three_way_match_gate": gate_ok,
        "match_status": mr.get("match_status", mr.get("status")),
        "variance_lines": variances,
        "invoice_total": str(ir.get("invoice_total", ir.get("total", ""))),
        "match_response": mr,
        "_live": True,
    }


def _live_process_payment(tool_input: dict[str, Any]) -> dict[str, Any]:
    iid = tool_input["invoice_id"]
    r = _request("POST", f"/invoices/{iid}/approve", {})
    if r.get("error"):
        return r
    return {
        "payment_id": r.get("payment_id", r.get("id")),
        "invoice_id": iid,
        "amount": r.get("amount"),
        "status": r.get("status", "completed"),
        "gl_balance_check": r.get("gl_balance_check", "balanced"),
        "_live": True,
        "_raw": r,
    }


def _live_get_transaction_status(tool_input: dict[str, Any]) -> dict[str, Any]:
    et = tool_input["entity_type"]
    eid = tool_input["entity_id"]
    if et == "vendor":
        return _request("GET", f"/vendors/{eid}/exposure")
    if et == "purchase_order":
        return _request("GET", f"/purchase-orders/{eid}")
    if et == "invoice":
        return {
            "error": "live_mode_limitation",
            "message": (
                "The P2P API spec does not define GET /invoices/{id}. "
                "Use entity_type purchase_order with the PO id, or extend the server."
            ),
        }
    if et == "payment":
        return {
            "error": "live_mode_limitation",
            "message": "The P2P API spec does not define GET /payments/{id}.",
        }
    return {"error": "unknown_entity_type", "entity_type": et}
