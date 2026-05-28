"""
Deterministic mock medical lien store for QA testing.

Enforces six controls:
1. Lien priority enforcement (federal liens before private)
2. Balance cap (lien balance cannot exceed original billed amount)
3. Duplicate lien detection (same provider + case + service date)
4. Provider status gate (suspended/excluded providers rejected)
5. Settlement waterfall order (legal priority order for disbursement)
6. Reduction negotiation cap (max 50% reduction by policy)

All monetary values use Decimal internally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Money helpers
# ---------------------------------------------------------------------------

def _money(value: Any) -> Decimal:
    """Convert arbitrary numeric input to a 2dp Decimal via str() to avoid float noise."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _json_safe(obj: Any) -> Any:
    """Recursively convert Decimals to float for JSON/tool outputs."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Provider:
    provider_id: str
    name: str
    status: Literal["active", "suspended", "excluded"]
    provider_type: Literal["federal", "state", "private"]


@dataclass
class Lien:
    lien_id: str
    case_id: str
    provider_id: str
    service_date: str
    original_billed: Decimal
    current_balance: Decimal
    lien_type: Literal["federal", "state", "private"]
    status: Literal["active", "negotiated", "paid", "void"]
    negotiated_reduction_pct: Decimal = field(default_factory=lambda: _money(0))


@dataclass
class Settlement:
    settlement_id: str
    case_id: str
    total_amount: Decimal
    disbursements: list[dict[str, Any]] = field(default_factory=list)
    status: Literal["pending", "distributed", "closed"] = "pending"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum allowed reduction percentage (policy cap)
MAX_REDUCTION_PCT = Decimal("50.00")

# Legal priority order for settlement waterfall
PRIORITY_ORDER = ["federal", "state", "private"]


# ---------------------------------------------------------------------------
# MockLienStore
# ---------------------------------------------------------------------------

class MockLienStore:
    """In-memory deterministic mock for medical lien tool handlers."""

    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}
        self._liens: dict[str, Lien] = {}
        self._settlements: dict[str, Settlement] = {}
        self._cases: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter:04d}"

    # -- dispatch interface (required by DomainSpec) -----------------------

    def dispatch(self, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        """Route a tool call by name to the appropriate handler."""
        if name == "create_lien":
            return self.create_lien(
                provider_name=tool_input.get("provider_name", ""),
                provider_type=tool_input.get("provider_type", "private"),
                provider_status=tool_input.get("provider_status", "active"),
                case_id=tool_input.get("case_id", ""),
                service_date=tool_input.get("service_date", ""),
                billed_amount=tool_input.get("billed_amount", "0"),
            )
        if name == "adjust_lien_balance":
            return self.adjust_lien_balance(
                lien_id=tool_input["lien_id"],
                new_balance=tool_input["new_balance"],
            )
        if name == "negotiate_reduction":
            return self.negotiate_reduction(
                lien_id=tool_input["lien_id"],
                reduction_percentage=tool_input["reduction_percentage"],
            )
        if name == "distribute_settlement":
            return self.distribute_settlement(
                case_id=tool_input["case_id"],
                total_settlement_amount=tool_input["total_settlement_amount"],
                disbursements=tool_input.get("disbursements", []),
            )
        if name == "get_entity_status":
            return self.get_entity_status(
                entity_type=tool_input["entity_type"],
                entity_id=tool_input["entity_id"],
            )
        if name == "check_lien_compliance":
            return self.check_lien_compliance(
                case_id=tool_input["case_id"],
            )
        return {"error": "unknown_tool", "name": name}

    # -- Tool implementations ----------------------------------------------

    def create_lien(
        self,
        provider_name: str,
        provider_type: str,
        provider_status: str,
        case_id: str,
        service_date: str,
        billed_amount: str,
    ) -> dict[str, Any]:
        """Create a provider and lien. Enforces provider status gate and duplicate detection."""
        if not provider_name or not case_id or not service_date:
            return {
                "error": "validation_error",
                "message": "provider_name, case_id, and service_date are required.",
            }

        billed = _money(billed_amount)
        if billed <= 0:
            return {
                "error": "validation_error",
                "message": "billed_amount must be positive.",
            }

        # Normalize provider_type
        if provider_type not in ("federal", "state", "private"):
            provider_type = "private"
        if provider_status not in ("active", "suspended", "excluded"):
            provider_status = "active"

        # Create or find provider
        provider_id = self._next_id("prov")
        provider = Provider(
            provider_id=provider_id,
            name=provider_name,
            status=provider_status,
            provider_type=provider_type,
        )
        self._providers[provider_id] = provider

        # CONTROL 4: Provider status gate
        if provider.status in ("suspended", "excluded"):
            return _json_safe({
                "error": "provider_status_rejected",
                "message": f"Cannot file lien: provider '{provider_name}' is {provider.status}.",
                "provider_id": provider_id,
                "provider_status": provider.status,
                "provider_status_gate": False,
            })

        # CONTROL 3: Duplicate lien detection
        for existing in self._liens.values():
            if (
                existing.case_id == case_id
                and existing.service_date == service_date
                and self._providers[existing.provider_id].name == provider_name
                and existing.status != "void"
            ):
                return _json_safe({
                    "error": "duplicate_lien",
                    "message": "Duplicate lien: same provider, case, and service date already exists.",
                    "existing_lien_id": existing.lien_id,
                    "duplicate_check_key": {
                        "provider_name": provider_name,
                        "case_id": case_id,
                        "service_date": service_date,
                    },
                })

        # Create lien
        lien_id = self._next_id("lien")
        lien = Lien(
            lien_id=lien_id,
            case_id=case_id,
            provider_id=provider_id,
            service_date=service_date,
            original_billed=billed,
            current_balance=billed,
            lien_type=provider_type,
            status="active",
        )
        self._liens[lien_id] = lien

        # Track case
        if case_id not in self._cases:
            self._cases[case_id] = {"case_id": case_id, "lien_ids": []}
        self._cases[case_id]["lien_ids"].append(lien_id)

        return _json_safe({
            "lien_id": lien_id,
            "provider_id": provider_id,
            "provider_name": provider_name,
            "provider_type": provider_type,
            "case_id": case_id,
            "service_date": service_date,
            "original_billed": billed,
            "current_balance": billed,
            "lien_type": provider_type,
            "status": "active",
        })

    def adjust_lien_balance(
        self,
        lien_id: str,
        new_balance: str,
    ) -> dict[str, Any]:
        """Adjust lien balance. Enforces balance cap control."""
        lien = self._liens.get(lien_id)
        if not lien:
            return {"error": "not_found", "message": f"lien_id not found: {lien_id}"}

        new_bal = _money(new_balance)

        # CONTROL 2: Balance cap -- cannot exceed original billed amount
        if new_bal > lien.original_billed:
            return _json_safe({
                "error": "balance_cap_violation",
                "message": "Adjusted balance cannot exceed original billed amount.",
                "balance_cap_exceeded": True,
                "original_billed": lien.original_billed,
                "requested_balance": new_bal,
            })

        if new_bal < 0:
            return _json_safe({
                "error": "validation_error",
                "message": "Balance cannot be negative.",
                "requested_balance": new_bal,
            })

        old_balance = lien.current_balance
        lien.current_balance = new_bal

        return _json_safe({
            "lien_id": lien_id,
            "old_balance": old_balance,
            "new_balance": new_bal,
            "original_billed": lien.original_billed,
            "balance_cap_exceeded": False,
        })

    def negotiate_reduction(
        self,
        lien_id: str,
        reduction_percentage: str,
    ) -> dict[str, Any]:
        """Negotiate a reduction on a lien. Enforces reduction cap control."""
        lien = self._liens.get(lien_id)
        if not lien:
            return {"error": "not_found", "message": f"lien_id not found: {lien_id}"}

        pct = _money(reduction_percentage)

        # CONTROL 6: Reduction negotiation cap
        if pct > MAX_REDUCTION_PCT:
            return _json_safe({
                "error": "reduction_cap_violation",
                "message": f"Reduction of {pct}% exceeds maximum allowed {MAX_REDUCTION_PCT}%.",
                "reduction_cap_exceeded": True,
                "requested_reduction_pct": pct,
                "max_allowed_pct": MAX_REDUCTION_PCT,
            })

        if pct < 0:
            return _json_safe({
                "error": "validation_error",
                "message": "Reduction percentage cannot be negative.",
            })

        # Apply reduction
        reduction_factor = (Decimal("100") - pct) / Decimal("100")
        old_balance = lien.current_balance
        new_balance = (lien.original_billed * reduction_factor).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        lien.current_balance = new_balance
        lien.negotiated_reduction_pct = pct
        lien.status = "negotiated"

        return _json_safe({
            "lien_id": lien_id,
            "reduction_percentage": pct,
            "old_balance": old_balance,
            "new_balance": new_balance,
            "original_billed": lien.original_billed,
            "reduction_cap_exceeded": False,
            "status": "negotiated",
        })

    def distribute_settlement(
        self,
        case_id: str,
        total_settlement_amount: str,
        disbursements: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Distribute settlement funds. Enforces waterfall order and priority controls."""
        if case_id not in self._cases:
            return {"error": "not_found", "message": f"case_id not found: {case_id}"}

        total = _money(total_settlement_amount)
        if total <= 0:
            return {
                "error": "validation_error",
                "message": "total_settlement_amount must be positive.",
            }

        if not disbursements:
            return {
                "error": "validation_error",
                "message": "disbursements list is required and must not be empty.",
            }

        # Validate all lien_ids exist and belong to this case
        case_lien_ids = set(self._cases[case_id]["lien_ids"])
        for i, d in enumerate(disbursements):
            lid = d.get("lien_id")
            if lid not in self._liens:
                return {"error": "not_found", "message": f"disbursement[{i}].lien_id not found: {lid}"}
            if lid not in case_lien_ids:
                return {
                    "error": "validation_error",
                    "message": f"disbursement[{i}].lien_id {lid} does not belong to case {case_id}.",
                }

        # CONTROL 1 + 5: Lien priority enforcement + settlement waterfall order
        # Disbursements must be in priority order: federal -> state -> private
        prev_priority = -1
        for i, d in enumerate(disbursements):
            lid = d.get("lien_id")
            lien = self._liens[lid]
            current_priority = PRIORITY_ORDER.index(lien.lien_type) if lien.lien_type in PRIORITY_ORDER else len(PRIORITY_ORDER)
            if current_priority < prev_priority:
                return _json_safe({
                    "error": "waterfall_order_violation",
                    "message": (
                        f"Disbursement order violates legal priority: {lien.lien_type} lien "
                        f"(lien_id={lid}) must be paid before lower-priority liens."
                    ),
                    "waterfall_violation": True,
                    "priority_violation": True,
                    "violating_lien_id": lid,
                    "violating_lien_type": lien.lien_type,
                    "expected_order": PRIORITY_ORDER,
                })
            prev_priority = current_priority

        # Validate disbursement amounts
        total_disbursed = _money(0)
        settlement_details: list[dict[str, Any]] = []
        for d in disbursements:
            lid = d.get("lien_id")
            amount = _money(d.get("amount", "0"))
            lien = self._liens[lid]

            if amount > lien.current_balance:
                return _json_safe({
                    "error": "overpayment",
                    "message": f"Disbursement amount {amount} exceeds lien balance {lien.current_balance} for {lid}.",
                    "lien_id": lid,
                    "amount": amount,
                    "current_balance": lien.current_balance,
                })

            total_disbursed += amount
            settlement_details.append({
                "lien_id": lid,
                "provider_id": lien.provider_id,
                "lien_type": lien.lien_type,
                "amount": amount,
                "balance_before": lien.current_balance,
                "balance_after": lien.current_balance - amount,
            })

        if total_disbursed > total:
            return _json_safe({
                "error": "disbursement_exceeds_settlement",
                "message": "Total disbursements exceed settlement amount.",
                "total_settlement": total,
                "total_disbursed": total_disbursed,
            })

        # Apply disbursements
        settlement_id = self._next_id("stl")
        for detail in settlement_details:
            lid = detail["lien_id"]
            lien = self._liens[lid]
            lien.current_balance -= _money(detail["amount"])
            if lien.current_balance == _money(0):
                lien.status = "paid"

        settlement = Settlement(
            settlement_id=settlement_id,
            case_id=case_id,
            total_amount=total,
            disbursements=settlement_details,
            status="distributed",
        )
        self._settlements[settlement_id] = settlement

        return _json_safe({
            "settlement_id": settlement_id,
            "case_id": case_id,
            "total_settlement_amount": total,
            "total_disbursed": total_disbursed,
            "remaining_funds": total - total_disbursed,
            "disbursements": settlement_details,
            "waterfall_violation": False,
            "priority_violation": False,
            "status": "distributed",
        })

    def get_entity_status(
        self,
        entity_type: str,
        entity_id: str,
    ) -> dict[str, Any]:
        """Fetch status for provider, lien, case, or settlement by id."""
        if entity_type == "provider":
            p = self._providers.get(entity_id)
            if not p:
                return {"error": "not_found"}
            return _json_safe({
                "provider_id": p.provider_id,
                "name": p.name,
                "status": p.status,
                "provider_type": p.provider_type,
            })
        if entity_type == "lien":
            lien = self._liens.get(entity_id)
            if not lien:
                return {"error": "not_found"}
            provider = self._providers.get(lien.provider_id)
            return _json_safe({
                "lien_id": lien.lien_id,
                "case_id": lien.case_id,
                "provider_id": lien.provider_id,
                "provider_name": provider.name if provider else "unknown",
                "service_date": lien.service_date,
                "original_billed": lien.original_billed,
                "current_balance": lien.current_balance,
                "lien_type": lien.lien_type,
                "status": lien.status,
                "negotiated_reduction_pct": lien.negotiated_reduction_pct,
            })
        if entity_type == "case":
            case = self._cases.get(entity_id)
            if not case:
                return {"error": "not_found"}
            liens_summary = []
            for lid in case["lien_ids"]:
                lien = self._liens.get(lid)
                if lien:
                    liens_summary.append(_json_safe({
                        "lien_id": lid,
                        "lien_type": lien.lien_type,
                        "current_balance": lien.current_balance,
                        "status": lien.status,
                    }))
            return {
                "case_id": entity_id,
                "lien_count": len(case["lien_ids"]),
                "liens": liens_summary,
            }
        if entity_type == "settlement":
            s = self._settlements.get(entity_id)
            if not s:
                return {"error": "not_found"}
            return _json_safe({
                "settlement_id": s.settlement_id,
                "case_id": s.case_id,
                "total_amount": s.total_amount,
                "disbursements": s.disbursements,
                "status": s.status,
            })
        return {"error": "unknown_entity_type", "entity_type": entity_type}

    def check_lien_compliance(
        self,
        case_id: str,
    ) -> dict[str, Any]:
        """Run compliance checks on all liens for a case."""
        case = self._cases.get(case_id)
        if not case:
            return {"error": "not_found", "message": f"case_id not found: {case_id}"}

        issues: list[dict[str, Any]] = []
        liens = [self._liens[lid] for lid in case["lien_ids"] if lid in self._liens]

        for lien in liens:
            provider = self._providers.get(lien.provider_id)

            # Check provider status
            if provider and provider.status in ("suspended", "excluded"):
                issues.append({
                    "lien_id": lien.lien_id,
                    "issue": "provider_status",
                    "detail": f"Provider {provider.name} is {provider.status}.",
                })

            # Check balance cap
            if lien.current_balance > lien.original_billed:
                issues.append({
                    "lien_id": lien.lien_id,
                    "issue": "balance_cap",
                    "detail": f"Current balance {lien.current_balance} exceeds original billed {lien.original_billed}.",
                })

            # Check reduction cap
            if lien.negotiated_reduction_pct > MAX_REDUCTION_PCT:
                issues.append({
                    "lien_id": lien.lien_id,
                    "issue": "reduction_cap",
                    "detail": f"Reduction {lien.negotiated_reduction_pct}% exceeds max {MAX_REDUCTION_PCT}%.",
                })

        # Check for duplicates
        seen: set[tuple[str, str, str]] = set()
        for lien in liens:
            if lien.status == "void":
                continue
            provider = self._providers.get(lien.provider_id)
            key = (provider.name if provider else "", lien.case_id, lien.service_date)
            if key in seen:
                issues.append({
                    "lien_id": lien.lien_id,
                    "issue": "duplicate_lien",
                    "detail": f"Duplicate: provider={key[0]}, case={key[1]}, date={key[2]}.",
                })
            seen.add(key)

        compliance_status = "compliant" if not issues else "non_compliant"
        return _json_safe({
            "case_id": case_id,
            "lien_count": len(liens),
            "compliance_status": compliance_status,
            "issues": issues,
            "total_outstanding": sum(l.current_balance for l in liens if l.status in ("active", "negotiated")),
        })
