from __future__ import annotations

import hashlib
import json
from typing import Any

from app.models.core import Opportunity


def build_execution_claim_key(opportunity: Opportunity, *, mode: str) -> str:
    payload: dict[str, Any] = {
        "opportunity_id": opportunity.opportunity_id,
        "strategy_type": opportunity.strategy_type.value,
        "direction": opportunity.direction.value,
        "prices": opportunity.prices,
        "net_edge": round(opportunity.net_edge, 6),
        "max_safe_size": round(opportunity.max_safe_size, 4),
        "available_liquidity": round(opportunity.available_liquidity, 4),
        "order_type": opportunity.details.get("order_type"),
        "post_only": bool(opportunity.details.get("post_only", False)),
        "expiration_sec": opportunity.details.get("expiration_sec"),
    }
    if opportunity.details.get("strategy_variant") == "near_close_maker":
        payload["signal_timestamp"] = opportunity.timestamp.isoformat()
    digest = hashlib.sha1(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()[:16]
    return f"{mode}:{opportunity.opportunity_id}:{digest}"
