from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gamma_base_url: str = Field(default="https://gamma-api.polymarket.com", alias="GAMMA_BASE_URL")
    clob_base_url: str = Field(default="https://clob.polymarket.com", alias="CLOB_BASE_URL")
    ws_market_url: str = Field(
        default="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        alias="WS_MARKET_URL",
    )
    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    sqlite_path: Path = Field(
        default=Path("C:/Users/hug0x/Desktop/polymarket-scanner-data/polymarket_scanner.db"),
        alias="SQLITE_PATH",
    )
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")
    min_net_edge: float = Field(default=0.015, alias="MIN_NET_EDGE")
    candidate_min_net_edge: float = Field(default=-0.0035, alias="CANDIDATE_MIN_NET_EDGE")
    min_liquidity: float = Field(default=1000.0, alias="MIN_LIQUIDITY")
    candidate_min_liquidity: float = Field(default=250.0, alias="CANDIDATE_MIN_LIQUIDITY")
    max_spread: float = Field(default=0.08, alias="MAX_SPREAD")
    candidate_max_spread: float = Field(default=0.12, alias="CANDIDATE_MAX_SPREAD")
    min_depth: float = Field(default=100.0, alias="MIN_DEPTH")
    candidate_min_depth: float = Field(default=10.0, alias="CANDIDATE_MIN_DEPTH")
    alert_cooldown_sec: int = Field(default=900, alias="ALERT_COOLDOWN_SEC")
    fees_bps: float = Field(default=0.0, alias="FEES_BPS")
    slippage_bps: float = Field(default=10.0, alias="SLIPPAGE_BPS")
    enable_paper_trading: bool = Field(default=False, alias="ENABLE_PAPER_TRADING")
    enable_live_trading: bool = Field(default=False, alias="ENABLE_LIVE_TRADING")
    live_auto_execute: bool = Field(default=False, alias="LIVE_AUTO_EXECUTE")
    strategy_binary_sum_enabled: bool = Field(default=False, alias="STRATEGY_BINARY_SUM_ENABLED")
    strategy_multi_outcome_enabled: bool = Field(default=False, alias="STRATEGY_MULTI_OUTCOME_ENABLED")
    strategy_related_market_enabled: bool = Field(default=False, alias="STRATEGY_RELATED_MARKET_ENABLED")
    strategy_stale_price_enabled: bool = Field(default=False, alias="STRATEGY_STALE_PRICE_ENABLED")
    discovery_event_limit: int = Field(default=100, alias="DISCOVERY_EVENT_LIMIT")
    watch_market_limit: int = Field(default=20, alias="WATCH_MARKET_LIMIT")
    watch_bucket_general_limit: int = Field(default=8, alias="WATCH_BUCKET_GENERAL_LIMIT")
    watch_bucket_event_limit: int = Field(default=4, alias="WATCH_BUCKET_EVENT_LIMIT")
    watch_bucket_recent_limit: int = Field(default=4, alias="WATCH_BUCKET_RECENT_LIMIT")
    watch_bucket_special_limit: int = Field(default=2, alias="WATCH_BUCKET_SPECIAL_LIMIT")
    watch_event_family_cap: int = Field(default=5, alias="WATCH_EVENT_FAMILY_CAP")
    watch_long_tail_min_price: float = Field(default=0.03, alias="WATCH_LONG_TAIL_MIN_PRICE")
    watch_long_tail_max_price: float = Field(default=0.97, alias="WATCH_LONG_TAIL_MAX_PRICE")
    watch_long_tail_exception_spread: float = Field(default=0.02, alias="WATCH_LONG_TAIL_EXCEPTION_SPREAD")
    watch_long_tail_exception_liquidity: float = Field(default=5000.0, alias="WATCH_LONG_TAIL_EXCEPTION_LIQUIDITY")
    watch_positive_edge_lookback_hours: int = Field(default=24, alias="WATCH_POSITIVE_EDGE_LOOKBACK_HOURS")
    scan_interval_sec: int = Field(default=60, alias="SCAN_INTERVAL_SEC")
    discovery_refresh_sec: int = Field(default=900, alias="DISCOVERY_REFRESH_SEC")
    book_fetch_concurrency: int = Field(default=5, alias="BOOK_FETCH_CONCURRENCY")
    min_minutes_to_resolution: int = Field(default=120, alias="MIN_MINUTES_TO_RESOLUTION")
    candidate_min_minutes_to_resolution: int = Field(default=30, alias="CANDIDATE_MIN_MINUTES_TO_RESOLUTION")
    allow_near_resolution: bool = Field(default=False, alias="ALLOW_NEAR_RESOLUTION")
    late_resolution_enabled: bool = Field(default=True, alias="LATE_RESOLUTION_ENABLED")
    late_resolution_min_price: float = Field(default=0.96, alias="LATE_RESOLUTION_MIN_PRICE")
    late_resolution_max_price: float = Field(default=0.985, alias="LATE_RESOLUTION_MAX_PRICE")
    late_resolution_target_price: float = Field(default=0.992, alias="LATE_RESOLUTION_TARGET_PRICE")
    late_resolution_max_spread: float = Field(default=0.0035, alias="LATE_RESOLUTION_MAX_SPREAD")
    late_resolution_min_depth: float = Field(default=25.0, alias="LATE_RESOLUTION_MIN_DEPTH")
    late_resolution_max_minutes_to_resolution: int = Field(
        default=180,
        alias="LATE_RESOLUTION_MAX_MINUTES_TO_RESOLUTION",
    )
    near_close_maker_enabled: bool = Field(default=True, alias="NEAR_CLOSE_MAKER_ENABLED")
    near_close_maker_live_enabled: bool = Field(default=False, alias="NEAR_CLOSE_MAKER_LIVE_ENABLED")
    near_close_scan_pool_enabled: bool = Field(default=True, alias="NEAR_CLOSE_SCAN_POOL_ENABLED")
    near_close_scan_event_limit: int = Field(default=750, alias="NEAR_CLOSE_SCAN_EVENT_LIMIT")
    near_close_scan_pool_limit: int = Field(default=30, alias="NEAR_CLOSE_SCAN_POOL_LIMIT")
    near_close_scan_lookahead_minutes: float = Field(default=75.0, alias="NEAR_CLOSE_SCAN_LOOKAHEAD_MINUTES")
    near_close_min_paper_signals_for_live: int = Field(default=100, alias="NEAR_CLOSE_MIN_PAPER_SIGNALS_FOR_LIVE")
    near_close_min_minutes_to_end: float = Field(default=3.0, alias="NEAR_CLOSE_MIN_MINUTES_TO_END")
    near_close_max_minutes_to_end: float = Field(default=15.0, alias="NEAR_CLOSE_MAX_MINUTES_TO_END")
    near_close_max_bid_price: float = Field(default=0.97, alias="NEAR_CLOSE_MAX_BID_PRICE")
    near_close_min_best_ask: float = Field(default=0.98, alias="NEAR_CLOSE_MIN_BEST_ASK")
    near_close_min_midpoint: float = Field(default=0.975, alias="NEAR_CLOSE_MIN_MIDPOINT")
    near_close_max_spread: float = Field(default=0.025, alias="NEAR_CLOSE_MAX_SPREAD")
    near_close_min_net_edge: float = Field(default=0.005, alias="NEAR_CLOSE_MIN_NET_EDGE")
    near_close_min_depth: float = Field(default=20.0, alias="NEAR_CLOSE_MIN_DEPTH")
    near_close_order_size: float = Field(default=5.0, alias="NEAR_CLOSE_ORDER_SIZE")
    near_close_max_market_exposure: float = Field(default=5.0, alias="NEAR_CLOSE_MAX_MARKET_EXPOSURE")
    near_close_max_total_exposure: float = Field(default=15.0, alias="NEAR_CLOSE_MAX_TOTAL_EXPOSURE")
    near_close_daily_loss_limit: float = Field(default=2.0, alias="NEAR_CLOSE_DAILY_LOSS_LIMIT")
    near_close_max_consecutive_losses: int = Field(default=2, alias="NEAR_CLOSE_MAX_CONSECUTIVE_LOSSES")
    near_close_gtd_seconds: int = Field(default=1800, alias="NEAR_CLOSE_GTD_SECONDS")
    near_close_gtd_safety_buffer_sec: int = Field(default=60, alias="NEAR_CLOSE_GTD_SAFETY_BUFFER_SEC")
    near_close_reprice_threshold: float = Field(default=0.003, alias="NEAR_CLOSE_REPRICE_THRESHOLD")
    near_close_reprice_cooldown_sec: int = Field(default=120, alias="NEAR_CLOSE_REPRICE_COOLDOWN_SEC")
    near_close_short_drop: float = Field(default=0.008, alias="NEAR_CLOSE_SHORT_DROP")
    near_close_long_drop: float = Field(default=0.012, alias="NEAR_CLOSE_LONG_DROP")
    near_close_soft_stop_offset: float = Field(default=0.005, alias="NEAR_CLOSE_SOFT_STOP_OFFSET")
    near_close_hard_stop_offset: float = Field(default=0.025, alias="NEAR_CLOSE_HARD_STOP_OFFSET")
    near_close_hard_stop_bid: float = Field(default=0.945, alias="NEAR_CLOSE_HARD_STOP_BID")
    near_close_emergency_slippage: float = Field(default=0.01, alias="NEAR_CLOSE_EMERGENCY_SLIPPAGE")
    near_close_emergency_max_loss: float = Field(default=0.05, alias="NEAR_CLOSE_EMERGENCY_MAX_LOSS")
    near_close_crypto_enabled: bool = Field(default=True, alias="NEAR_CLOSE_CRYPTO_ENABLED")
    near_close_crypto_order_size: float = Field(default=2.0, alias="NEAR_CLOSE_CRYPTO_ORDER_SIZE")
    near_close_crypto_min_minutes_to_end: float = Field(default=5.0, alias="NEAR_CLOSE_CRYPTO_MIN_MINUTES_TO_END")
    near_close_crypto_max_minutes_to_end: float = Field(default=20.0, alias="NEAR_CLOSE_CRYPTO_MAX_MINUTES_TO_END")
    near_close_crypto_min_best_ask: float = Field(default=0.985, alias="NEAR_CLOSE_CRYPTO_MIN_BEST_ASK")
    near_close_crypto_min_midpoint: float = Field(default=0.982, alias="NEAR_CLOSE_CRYPTO_MIN_MIDPOINT")
    near_close_crypto_max_spread: float = Field(default=0.015, alias="NEAR_CLOSE_CRYPTO_MAX_SPREAD")
    near_close_crypto_min_strike_distance: float = Field(default=0.02, alias="NEAR_CLOSE_CRYPTO_MIN_STRIKE_DISTANCE")
    near_close_crypto_cancel_strike_distance: float = Field(default=0.015, alias="NEAR_CLOSE_CRYPTO_CANCEL_STRIKE_DISTANCE")
    near_close_crypto_updown_enabled: bool = Field(default=True, alias="NEAR_CLOSE_CRYPTO_UPDOWN_ENABLED")
    near_close_crypto_updown_order_size: float = Field(default=5.0, alias="NEAR_CLOSE_CRYPTO_UPDOWN_ORDER_SIZE")
    near_close_crypto_updown_min_minutes_to_end: float = Field(
        default=1.5,
        alias="NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END",
    )
    near_close_crypto_updown_max_minutes_to_end: float = Field(
        default=45.0,
        alias="NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END",
    )
    near_close_crypto_updown_min_start_distance: float = Field(
        default=0.003,
        alias="NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE",
    )
    near_close_crypto_updown_cancel_start_distance: float = Field(
        default=0.002,
        alias="NEAR_CLOSE_CRYPTO_UPDOWN_CANCEL_START_DISTANCE",
    )
    near_close_crypto_updown_min_best_ask: float = Field(default=0.75, alias="NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK")
    near_close_crypto_updown_min_midpoint: float = Field(default=0.60, alias="NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT")
    near_close_crypto_updown_max_spread: float = Field(default=0.04, alias="NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD")
    near_close_crypto_updown_max_bid_price: float = Field(
        default=0.988,
        alias="NEAR_CLOSE_CRYPTO_UPDOWN_MAX_BID_PRICE",
    )
    near_close_crypto_updown_min_depth: float = Field(
        default=10.0,
        alias="NEAR_CLOSE_CRYPTO_UPDOWN_MIN_DEPTH",
    )
    near_close_crypto_updown_midpoint_discount: float = Field(
        default=0.003,
        alias="NEAR_CLOSE_CRYPTO_UPDOWN_MIDPOINT_DISCOUNT",
    )
    related_rules_path: Path = Field(
        default=Path("./rules/related_markets.example.yaml"),
        alias="RELATED_RULES_PATH",
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    web_host: str = Field(default="0.0.0.0", alias="WEB_HOST")
    port: int = Field(default=8080, alias="PORT")
    dashboard_refresh_sec: int = Field(default=30, alias="DASHBOARD_REFRESH_SEC")
    dashboard_page_size: int = Field(default=18, alias="DASHBOARD_PAGE_SIZE")
    dashboard_scan_limit: int = Field(default=250, alias="DASHBOARD_SCAN_LIMIT")
    polygon_rpc_url: str = Field(default="https://polygon-bor-rpc.publicnode.com", alias="POLYGON_RPC_URL")
    polygon_usdc_token_address: str = Field(
        default="0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
        alias="POLYGON_USDC_TOKEN_ADDRESS",
    )
    polygon_usdc_e_token_address: str = Field(
        default="0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
        alias="POLYGON_USDC_E_TOKEN_ADDRESS",
    )
    polygon_pusd_token_address: str = Field(
        default="0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",
        alias="POLYGON_PUSD_TOKEN_ADDRESS",
    )
    min_pol_balance: float = Field(default=0.1, alias="MIN_POL_BALANCE")
    min_trading_collateral: float = Field(default=1.0, alias="MIN_TRADING_COLLATERAL")
    min_exchange_allowance: float = Field(default=25.0, alias="MIN_EXCHANGE_ALLOWANCE")
    preflight_cache_sec: int = Field(default=60, alias="PREFLIGHT_CACHE_SEC")
    auto_redeem_enabled: bool = Field(default=True, alias="AUTO_REDEEM_ENABLED")
    auto_redeem_refresh_sec: int = Field(default=300, alias="AUTO_REDEEM_REFRESH_SEC")
    auto_redeem_min_usdce: float = Field(default=0.01, alias="AUTO_REDEEM_MIN_USDCE")
    pusd_pnl_baseline: float | None = Field(default=20.55, alias="PUSD_PNL_BASELINE")
    clock_drift_cache_sec: int = Field(default=20, alias="CLOCK_DRIFT_CACHE_SEC")
    max_clock_drift_sec: int = Field(default=30, alias="MAX_CLOCK_DRIFT_SEC")
    require_live_preflight: bool = Field(default=True, alias="REQUIRE_LIVE_PREFLIGHT")
    clob_v2_cutover_utc: datetime = Field(
        default=datetime(2026, 4, 28, 11, 0, tzinfo=timezone.utc),
        alias="CLOB_V2_CUTOVER_UTC",
    )
    polymarket_private_key: str | None = Field(default=None, alias="POLYMARKET_PRIVATE_KEY")
    polymarket_funder_address: str | None = Field(default=None, alias="POLYMARKET_FUNDER_ADDRESS")
    polymarket_signature_type: int = Field(default=0, alias="POLYMARKET_SIGNATURE_TYPE")
    polymarket_chain_id: int = Field(default=137, alias="POLYMARKET_CHAIN_ID")
    polymarket_exchange_spender_address: str = Field(
        default="0xE111180000d2663C0091e4f400237545B87B996B",
        alias="POLYMARKET_EXCHANGE_SPENDER_ADDRESS",
    )
    polymarket_ctf_address: str = Field(
        default="0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
        alias="POLYMARKET_CTF_ADDRESS",
    )
    polymarket_collateral_onramp_address: str = Field(
        default="0x93070a847efEf7F70739046A929D47a521F5B8ee",
        alias="POLYMARKET_COLLATERAL_ONRAMP_ADDRESS",
    )
    live_order_type: str = Field(default="FOK", alias="LIVE_ORDER_TYPE")
    live_max_order_size: float = Field(default=25.0, alias="LIVE_MAX_ORDER_SIZE")
    risk_kill_switch: bool = Field(default=False, alias="RISK_KILL_SWITCH")
    max_notional_per_plan: float = Field(default=150.0, alias="MAX_NOTIONAL_PER_PLAN")
    max_daily_paper_notional: float = Field(default=5000.0, alias="MAX_DAILY_PAPER_NOTIONAL")
    max_daily_paper_trades: int = Field(default=250, alias="MAX_DAILY_PAPER_TRADES")
    max_daily_live_notional: float = Field(default=1000.0, alias="MAX_DAILY_LIVE_NOTIONAL")
    max_daily_live_orders: int = Field(default=20, alias="MAX_DAILY_LIVE_ORDERS")

    @field_validator("sqlite_path", "related_rules_path", mode="before")
    @classmethod
    def _expand_path(cls, value: str | Path) -> Path:
        return Path(value).expanduser()

    @field_validator("near_close_gtd_seconds")
    @classmethod
    def _bound_near_close_gtd(cls, value: int) -> int:
        return min(max(int(value), 60), 1800)

    @property
    def estimated_cost_per_leg(self) -> float:
        return (self.fees_bps + self.slippage_bps) / 10_000

    @property
    def persistence_backend(self) -> str:
        return "postgresql" if self.database_url else "sqlite"


def get_settings() -> Settings:
    """Return cached-ish settings instance."""

    return Settings()
