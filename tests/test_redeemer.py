from __future__ import annotations

import json

from app.services.redeemer import (
    _extract_gamma_market_from_event,
    _is_closed_losing_market,
    _is_winning_market,
    _merge_wallet_redeem_candidates,
    _outcome_index_for_token,
    _wallet_position_to_redeem_candidate,
)
from app.storage.db import connect_db
from app.storage.repositories import ScannerRepository


def test_wallet_position_builds_redeem_candidate_without_local_market() -> None:
    position = {
        "asset": "up-token",
        "conditionId": "0x" + "a" * 64,
        "size": 5,
        "avgPrice": 0.78,
        "redeemable": True,
        "slug": "eth-updown-5m-test",
        "outcome": "Up",
        "outcomeIndex": 0,
        "oppositeOutcome": "Down",
        "oppositeAsset": "down-token",
    }

    candidate = _wallet_position_to_redeem_candidate(position, trade_ids=[12])

    assert candidate is not None
    assert candidate["token_id"] == "up-token"
    assert candidate["market"]["slug"] == "eth-updown-5m-test"
    assert candidate["market"]["raw"]["conditionId"] == "0x" + "a" * 64
    assert candidate["outcome_index"] == 0
    assert candidate["trade_ids"] == [12]


def test_merge_wallet_redeem_candidates_adds_wallet_only_position(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "wallet-redeem-candidate.db"))
    position = {
        "asset": "wallet-only-token",
        "conditionId": "0x" + "b" * 64,
        "size": 5,
        "avgPrice": 0.82,
        "redeemable": True,
        "slug": "btc-updown-5m-test",
        "outcome": "Up",
        "outcomeIndex": 0,
        "oppositeOutcome": "Down",
        "oppositeAsset": "btc-down-token",
    }

    candidates = _merge_wallet_redeem_candidates([], [position], repository)

    assert len(candidates) == 1
    assert candidates[0]["token_id"] == "wallet-only-token"
    assert candidates[0]["trade_ids"] == []


def test_merge_wallet_redeem_candidates_enriches_existing_trade_candidate(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "wallet-redeem-existing.db"))
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "entry",
                1,
                "BUY",
                "up-token",
                "eth-updown-5m-test",
                "Up",
                0.78,
                5.0,
                "0xorder",
                "CONFIRMED",
                "{}",
                "2026-05-31T00:00:00+00:00",
            ),
        )
    existing = {
        "token_id": "up-token",
        "market": {"market_id": "", "slug": "eth-updown-5m-test", "raw": {}},
        "trade_ids": [],
    }
    position = {
        "asset": "up-token",
        "conditionId": "0x" + "c" * 64,
        "size": 5,
        "avgPrice": 0.78,
        "redeemable": True,
        "slug": "eth-updown-5m-test",
        "outcome": "Up",
        "outcomeIndex": 0,
    }

    candidates = _merge_wallet_redeem_candidates([existing], [position], repository)

    assert len(candidates) == 1
    assert candidates[0]["trade_ids"] == [1]
    assert candidates[0]["market"]["raw"]["conditionId"] == "0x" + "c" * 64


def test_gamma_event_market_and_token_index_drive_settlement_decision() -> None:
    event_payload = [
        {
            "markets": [
                {
                    "slug": "sol-updown-5m-test",
                    "closed": True,
                    "outcomes": json.dumps(["Up", "Down"]),
                    "outcomePrices": json.dumps(["0", "1"]),
                    "clobTokenIds": json.dumps(["up-token", "down-token"]),
                }
            ]
        }
    ]

    market = _extract_gamma_market_from_event(event_payload, "sol-updown-5m-test")
    assert market is not None
    assert _outcome_index_for_token(market, "down-token", 0) == 1
    assert _is_winning_market(market, 1) is True
    assert _is_closed_losing_market(market, 0) is True

