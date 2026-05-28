"""
P2P domain tool schemas for the Anthropic Messages API.
Does NOT include report_findings -- that is universal and lives in domains._base.
"""

from __future__ import annotations

from typing import Any

DOMAIN_TOOLS: list[dict[str, Any]] = [
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
]
