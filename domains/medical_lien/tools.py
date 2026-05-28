"""
Medical lien domain tool schemas for the Anthropic Messages API.
Does NOT include report_findings -- that is universal and lives in domains._base.
"""

from __future__ import annotations

from typing import Any

DOMAIN_TOOLS: list[dict[str, Any]] = [
    {
        "name": "create_lien",
        "description": (
            "Create a provider and file a medical lien against a case. "
            "Enforces provider status gate (rejects suspended/excluded providers) "
            "and duplicate lien detection (same provider + case + service date)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "provider_name": {
                    "type": "string",
                    "description": "Name of the medical provider filing the lien.",
                },
                "provider_type": {
                    "type": "string",
                    "enum": ["federal", "state", "private"],
                    "description": "Provider type determines lien priority. Federal (Medicare/Medicaid) has highest priority.",
                },
                "provider_status": {
                    "type": "string",
                    "enum": ["active", "suspended", "excluded"],
                    "description": "Provider status. Suspended/excluded providers are rejected.",
                },
                "case_id": {
                    "type": "string",
                    "description": "Case identifier for the personal injury or settlement case.",
                },
                "service_date": {
                    "type": "string",
                    "description": "Date of medical service (YYYY-MM-DD format).",
                },
                "billed_amount": {
                    "type": "string",
                    "description": "Original billed amount as string for Decimal parsing.",
                },
            },
            "required": ["provider_name", "provider_type", "case_id", "service_date", "billed_amount"],
        },
    },
    {
        "name": "adjust_lien_balance",
        "description": (
            "Adjust the current balance of an existing lien. "
            "Enforces balance cap: adjusted balance cannot exceed original billed amount."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lien_id": {"type": "string", "description": "The lien to adjust."},
                "new_balance": {
                    "type": "string",
                    "description": "New balance amount as string for Decimal parsing.",
                },
            },
            "required": ["lien_id", "new_balance"],
        },
    },
    {
        "name": "negotiate_reduction",
        "description": (
            "Negotiate a percentage reduction on a lien balance. "
            "Enforces reduction negotiation cap: maximum 50% reduction allowed by policy."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lien_id": {"type": "string", "description": "The lien to negotiate."},
                "reduction_percentage": {
                    "type": "string",
                    "description": "Reduction percentage (e.g., '25' for 25%). Max 50% allowed.",
                },
            },
            "required": ["lien_id", "reduction_percentage"],
        },
    },
    {
        "name": "distribute_settlement",
        "description": (
            "Distribute settlement funds to lien holders for a case. "
            "Enforces settlement waterfall order: federal liens must be paid before state, "
            "state before private. Also enforces lien priority: disbursement order must "
            "follow legal priority."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "The case to settle."},
                "total_settlement_amount": {
                    "type": "string",
                    "description": "Total settlement amount as string for Decimal parsing.",
                },
                "disbursements": {
                    "type": "array",
                    "description": "Ordered list of disbursements. Must follow priority order: federal -> state -> private.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "lien_id": {"type": "string"},
                            "amount": {"type": "string", "description": "Amount to pay this lien."},
                        },
                        "required": ["lien_id", "amount"],
                    },
                },
            },
            "required": ["case_id", "total_settlement_amount", "disbursements"],
        },
    },
    {
        "name": "get_entity_status",
        "description": "Fetch status for provider, lien, case, or settlement by id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_type": {
                    "type": "string",
                    "enum": ["provider", "lien", "case", "settlement"],
                },
                "entity_id": {"type": "string"},
            },
            "required": ["entity_type", "entity_id"],
        },
    },
    {
        "name": "check_lien_compliance",
        "description": (
            "Run compliance checks on all liens for a case. "
            "Checks provider status, balance caps, reduction caps, and duplicate liens."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "Case to check compliance for."},
            },
            "required": ["case_id"],
        },
    },
]
