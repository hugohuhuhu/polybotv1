from __future__ import annotations

import json

from app.config import Settings
from app.services import redeemer
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


class FakeRepository:
    def __init__(self) -> None:
        self.marked: list[tuple[list[int], str]] = []
        self.events: list[dict] = []
        self.autopsies: list[dict] = []

    def redeem_candidate_live_trades(self, limit: int = 50) -> list[dict]:
        return []

    def redeem_candidate_trade_ids_for_token(self, token_id: str) -> list[int]:
        return [42]

    def mark_live_trade_ids_status(self, trade_ids, status: str) -> int:
        trade_id_list = [int(value) for value in trade_ids]
        self.marked.append((trade_id_list, status))
        return len(trade_id_list)

    def save_execution_event(self, **kwargs) -> None:
        self.events.append(kwargs)

    def save_loss_autopsy(self, trade_ids, *, risk_settings, settlement_details) -> None:
        self.autopsies.append(
            {
                "trade_ids": list(trade_ids),
                "risk_settings": dict(risk_settings),
                "settlement_details": dict(settlement_details),
            }
        )


def test_auto_redeem_burns_zero_payout_wallet_position(monkeypatch) -> None:
    settings = Settings(
        POLYMARKET_PRIVATE_KEY="0x" + "1" * 64,
        AUTO_REDEEM_ENABLED=True,
        POLYGON_USDC_E_TOKEN_ADDRESS="0x" + "2" * 40,
        POLYMARKET_CTF_ADDRESS="0x" + "3" * 40,
    )
    token_id = "123"
    condition_id = "0x" + "4" * 64
    position = {
        "asset": token_id,
        "conditionId": condition_id,
        "slug": "btc-updown-test",
        "outcome": "Up",
        "outcomeIndex": 0,
        "oppositeAsset": "456",
        "oppositeOutcome": "Down",
        "redeemable": True,
        "size": 5,
        "avgPrice": 0.9,
    }
    sent_transactions: list[dict] = []

    monkeypatch.setattr(redeemer, "_fetch_redeemable_wallet_positions", lambda *_args: [position])
    monkeypatch.setattr(redeemer, "_rpc", lambda *_args: hex(settings.polymarket_chain_id))
    monkeypatch.setattr(
        redeemer,
        "_fetch_latest_market",
        lambda *_args, **_kwargs: {
            "closed": True,
            "outcomePrices": ["0", "1"],
            "conditionId": condition_id,
            "clobTokenIds": [token_id, "456"],
        },
    )

    def fake_call_uint(_client, _rpc_url, to, _data):
        if str(to).lower() == settings.polymarket_ctf_address.lower():
            return 5_000_000
        return 0

    def fake_send_transaction(_client, _settings, **kwargs):
        sent_transactions.append(kwargs)
        return "0xredeem"

    monkeypatch.setattr(redeemer, "_call_uint", fake_call_uint)
    monkeypatch.setattr(redeemer, "_send_transaction", fake_send_transaction)
    monkeypatch.setattr(redeemer, "_wait_receipt", lambda *_args: {"status": "0x1"})

    repository = FakeRepository()
    results = redeemer.run_auto_redeem_once(settings, repository)

    assert len(results) == 1
    assert results[0].status == "settled_lost"
    assert results[0].redeem_tx == "0xredeem"
    assert results[0].redeemed_size == 5.0
    assert repository.marked == [([42], "settled_lost")]
    assert repository.events[0]["status"] == "settled_lost"
    assert repository.events[0]["details"]["zero_payout_redeem"] is True
    assert repository.autopsies[0]["settlement_details"]["redeem_tx"] == "0xredeem"
    assert sent_transactions[0]["to"] == settings.polymarket_ctf_address


def test_auto_redeem_wraps_wallet_usdce_without_redeem_candidate(monkeypatch) -> None:
    settings = Settings(
        POLYMARKET_PRIVATE_KEY="0x" + "1" * 64,
        AUTO_REDEEM_ENABLED=True,
        AUTO_REDEEM_MIN_USDCE=0.01,
        POLYGON_USDC_E_TOKEN_ADDRESS="0x" + "2" * 40,
        POLYGON_PUSD_TOKEN_ADDRESS="0x" + "3" * 40,
        POLYMARKET_COLLATERAL_ONRAMP_ADDRESS="0x" + "4" * 40,
    )
    sent_transactions: list[dict] = []

    monkeypatch.setattr(redeemer, "_fetch_redeemable_wallet_positions", lambda *_args: [])
    monkeypatch.setattr(redeemer, "_rpc", lambda *_args: hex(settings.polymarket_chain_id))

    def fake_call_uint(_client, _rpc_url, to, data):
        if str(to).lower() == settings.polygon_usdc_e_token_address.lower():
            if str(data).startswith("0xdd62ed3e"):
                return 0
            return 20_980_674
        if str(to).lower() == settings.polygon_pusd_token_address.lower():
            return 3_901_254
        return 0

    def fake_send_transaction(_client, _settings, **kwargs):
        sent_transactions.append(kwargs)
        if str(kwargs["to"]).lower() == settings.polygon_usdc_e_token_address.lower():
            return "0xapprove"
        return "0xwrap"

    monkeypatch.setattr(redeemer, "_call_uint", fake_call_uint)
    monkeypatch.setattr(redeemer, "_send_transaction", fake_send_transaction)
    monkeypatch.setattr(redeemer, "_wait_receipt", lambda *_args: {"status": "0x1"})

    repository = FakeRepository()
    results = redeemer.run_auto_redeem_once(settings, repository)

    assert len(results) == 1
    assert results[0].status == "wrapped_usdce_to_pusd"
    assert results[0].approve_tx == "0xapprove"
    assert results[0].wrap_tx == "0xwrap"
    assert repository.events[0]["status"] == "wrapped_usdce_to_pusd"
    assert repository.events[0]["details"]["amount"] == 20.980674
    assert [item["to"] for item in sent_transactions] == [
        settings.polygon_usdc_e_token_address,
        settings.polymarket_collateral_onramp_address,
    ]
