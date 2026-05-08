# Polymarket Mispricing Scanner

Production-style Polymarket scanner focused on structurally explainable mispricing instead of prediction alpha. The project ships with a browser dashboard, scanner-first execution flow, and a disabled-by-default live trading adapter.

## What It Does

Implemented scanners:

- Binary YES/NO sum arbitrage
- Multi-outcome sum arbitrage
- Related-market logical inconsistency rules
- Stale-price / lag detection
- Late-resolution stale quote scan for high-probability outcomes that still look underpriced

Implemented execution modes:

- `scanner + alert` by default
- optional paper trading
- optional live trading adapter behind feature flags

Persisted data:

- discovered markets
- orderbook snapshots
- opportunities
- alerts
- paper trades
- live execution legs
- runtime trading controls
- execution claims
- execution audit log

## Browser UI

Current dashboard features:

- Traditional Chinese interface
- live browser dashboard
- auto-refresh
- one-click scan
- persisted runtime controls for `Live` and `自動下單`
- emergency stop / kill switch button
- wallet status with address, POL, USDC, USDC.e, and pUSD balances
- live-trading preflight checklist
- opportunity table
- strategy distribution
- recent alerts
- execution audit log
- top-liquidity markets

Run locally:

```bash
python -m app.main serve
```

Open:

```text
http://localhost:8080
```

## Commands

```bash
python -m app.main discover
python -m app.main scan
python -m app.main watch
python -m app.main backfill
python -m app.main report
python -m app.main serve
```

## Setup

Recommended:

```bash
uv venv --python 3.12
uv sync --extra dev
```

Optional analysis extras:

```bash
uv sync --extra dev --extra analysis
```

Copy environment variables:

```bash
cp .env.example .env
```

PowerShell:

```powershell
Copy-Item .env.example .env
```

## Environment Variables

Core scanner:

- `GAMMA_BASE_URL`
- `CLOB_BASE_URL`
- `WS_MARKET_URL`
- `DATABASE_URL`
- `SQLITE_PATH`
- `MIN_NET_EDGE`
- `CANDIDATE_MIN_NET_EDGE`
- `MIN_LIQUIDITY`
- `CANDIDATE_MIN_LIQUIDITY`
- `MAX_SPREAD`
- `CANDIDATE_MAX_SPREAD`
- `MIN_DEPTH`
- `CANDIDATE_MIN_DEPTH`
- `ALERT_COOLDOWN_SEC`
- `FEES_BPS`
- `SLIPPAGE_BPS`

Scanner behavior:

- `DISCOVERY_EVENT_LIMIT`
- `WATCH_MARKET_LIMIT`
- `SCAN_INTERVAL_SEC`
- `BOOK_FETCH_CONCURRENCY`
- `MIN_MINUTES_TO_RESOLUTION`
- `CANDIDATE_MIN_MINUTES_TO_RESOLUTION`
- `ALLOW_NEAR_RESOLUTION`
- `RELATED_RULES_PATH`

Alerts:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Web UI and preflight:

- `WEB_HOST`
- `PORT`
- `DASHBOARD_REFRESH_SEC`
- `DASHBOARD_PAGE_SIZE`
- `DASHBOARD_SCAN_LIMIT`
- `POLYGON_RPC_URL`
- `POLYGON_USDC_TOKEN_ADDRESS`
- `POLYGON_USDC_E_TOKEN_ADDRESS`
- `POLYGON_PUSD_TOKEN_ADDRESS`
- `MIN_POL_BALANCE`
- `MIN_TRADING_COLLATERAL`
- `MIN_EXCHANGE_ALLOWANCE`
- `PREFLIGHT_CACHE_SEC`
- `CLOCK_DRIFT_CACHE_SEC`
- `MAX_CLOCK_DRIFT_SEC`
- `REQUIRE_LIVE_PREFLIGHT`
- `CLOB_V2_CUTOVER_UTC`

Feature flags:

- `ENABLE_PAPER_TRADING`
- `ENABLE_LIVE_TRADING`
- `LIVE_AUTO_EXECUTE`

Live trading:

- `POLYMARKET_PRIVATE_KEY`
- `POLYMARKET_FUNDER_ADDRESS`
- `POLYMARKET_SIGNATURE_TYPE`
- `POLYMARKET_CHAIN_ID`
- `POLYMARKET_CTF_ADDRESS`
- `POLYMARKET_COLLATERAL_ONRAMP_ADDRESS`
- `LIVE_ORDER_TYPE`
- `LIVE_MAX_ORDER_SIZE`

Risk controls:

- `RISK_KILL_SWITCH`
- `MAX_NOTIONAL_PER_PLAN`
- `MAX_DAILY_PAPER_NOTIONAL`
- `MAX_DAILY_PAPER_TRADES`
- `MAX_DAILY_LIVE_NOTIONAL`
- `MAX_DAILY_LIVE_ORDERS`

## Persistence

Two persistence modes are supported:

- `SQLite` by default for local development
- `PostgreSQL` when `DATABASE_URL` is provided

Recommendation:

- local development: SQLite is fine
- Cloud Run / multi-instance deployment: use PostgreSQL / Cloud SQL

## Live Trading

The current live adapter is intentionally pinned to the legacy production stack:

- SDK: `py-clob-client`
- collateral: `USDC.e`
- allowance source of truth: legacy CLOB `balance-allowance` endpoint

This is deliberate. Mixing a V2-style preflight with the current V1 execution client caused wrong approval targets, misleading readiness checks, and oversized BUY orders during live testing.

Important behavior:

- live trading remains disabled by default
- scanner / alert mode is still the default operating mode
- dashboard buttons persist their state in the database instead of process memory
- `watch` and `serve` use execution claims to avoid duplicate submission of the same opportunity snapshot
- the adapter reuses an authenticated CLOB client instead of rebuilding it for every execution
- if a multi-leg submission fails after earlier legs were already posted, the adapter attempts to cancel submitted orders
- a `partial_failure` automatically triggers kill switch and disarms live / auto execution
- legacy BUY legs are submitted using collateral notional, while SELL legs remain share-sized, matching the current `py-clob-client` behavior observed in live testing

Before arming `Live` or `自動下單`, the backend performs read-only preflight checks for:

- current live trading stack and cutover deadline
- private key readability
- funder address
- Polygon chain ID
- POL gas
- active `USDC.e` collateral balance
- legacy CLOB balance/allowance visibility
- legacy exchange allowance readiness
- conditional-token sell allowance reminder
- CLOB API credentials
- clock drift

The bot does not auto-approve allowances and does not auto-wrap collateral. Those steps remain manual on purpose.

Important limitation:

- once the official V2 cutover time passes, this legacy live adapter should be treated as blocked until the project is migrated to `py-clob-client-v2`

## Runtime Controls

Runtime controls are persisted in the database:

- `Live 模式`
- `自動下單`
- `Kill switch`

That means:

- dashboard restart no longer resets runtime state back to memory defaults
- `watch` can see changes made from the dashboard if both share the same database
- emergency stop works across processes when persistence is shared

## Validation

Useful local validation commands:

```bash
python -m pytest
python -m app.main scan --limit 5
python -m app.main serve
```

## Known Limitations

- there is still no portfolio inventory model or position netting engine
- the live adapter is conservative and disables itself on partial failure instead of trying to fully self-heal
- related-market logic still depends on manually maintained YAML rules
- stale-price signals remain lower-confidence review candidates
- PostgreSQL mode is supported by `DATABASE_URL`, but production rollout still assumes you provide and operate that database yourself
- the current live adapter is legacy-stack only and blocks after the published V2 cutover time

## Polymarket References

- [Authentication](https://docs.polymarket.com/api-reference/authentication)
- [Clients & SDKs](https://docs.polymarket.com/developers/CLOB/clients)
- [V2 migration](https://docs.polymarket.com/v2-migration)
