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
- optional near-close post-fill hedge shadow/live flow behind feature flags

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
- `WATCH_LIVE_FILL_SYNC_ENABLED`
- `BOOK_FETCH_CONCURRENCY`
- `BOOK_FETCH_TIMEOUT_SEC`
- `BOOK_FETCH_RETRIES`
- `GAMMA_TIMEOUT_SEC`
- `GAMMA_RETRIES`
- `CRYPTO_PRICE_TIMEOUT_SEC`
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
- `MAX_DAILY_LIVE_NOTIONAL` (`<=0` disables this daily notional cap)
- `MAX_DAILY_LIVE_ORDERS`

## Near-Close Crypto Up/Down Hedge Branch

The crypto Up/Down near-close maker scan keeps the existing spread, depth, midpoint, start-distance, risk, preflight, and kill-switch filters. This branch tightens the entry price band for crypto Up/Down markets:

- Entry target must be at least `NEAR_CLOSE_CRYPTO_UPDOWN_MIN_ENTRY_PRICE` (`0.86` by default).
- Entry target is capped by `NEAR_CLOSE_CRYPTO_UPDOWN_MAX_ENTRY_PRICE` (`0.95` by default).
- The legacy `NEAR_CLOSE_CRYPTO_UPDOWN_MAX_BID_PRICE` still applies, so the effective cap is the lower of the two.

The local live launch scripts enable a time-funnel start-distance rule for crypto Up/Down entries. The farther the market is from close, the stronger the move away from the start price must be:

```text
6-7 min to close     -> start distance >= 0.0024
5-6 min to close     -> start distance >= 0.0018
3.5-5 min to close   -> start distance >= 0.00121
1.5-3.5 min to close -> start distance >= 0.0010
0.35-1.5 min to close -> no new live entries by default
```

Relevant settings:

- `NEAR_CLOSE_LIVE_MAX_MINUTES_TO_END=7` for the live launch scripts.
- `NEAR_CLOSE_CRYPTO_UPDOWN_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT` limits the live crypto Up/Down universe to BTC, ETH, and SOL.
- `NEAR_CLOSE_CRYPTO_UPDOWN_NO_NEW_ENTRY_LAST_SECONDS=90` blocks new crypto Up/Down entries inside the final 90 seconds.
- `NEAR_CLOSE_CRYPTO_UPDOWN_DYNAMIC_START_DISTANCE_ENABLED=true` enables the ladder.
- `NEAR_CLOSE_CRYPTO_UPDOWN_START_DISTANCE_LADDER=7:0.0024,6:0.0018,5:0.00121,3.5:0.0010,1.5:0.00085,0.35:0.00085`
- `NEAR_CLOSE_CRYPTO_UPDOWN_CANCEL_START_DISTANCE=0.00012` remains the cancellation line for already-active maker orders in the local launch scripts.
- `NEAR_CLOSE_OPEN_POSITION_MONITOR_SEC=2` makes watch check only open-position orderbooks during scan waits and delay windows.
- `NEAR_CLOSE_SECOND_CHANCE_EXIT_ENABLED=false` keeps panic exits to one immediate FAK taker attempt; no maker repost or second-chance order is attempted.

Survival-first stop-exit behavior:

- Panic exit is triggered from the observed open-position orderbook, not the full market discovery pass.
- When the stop threshold breaks, the bot cancels active maker and profit-taking orders for that position, then immediately sends a FAK taker SELL.
- The exit audit records the observed best bid, best ask, midpoint, spread, top bid size, target limit price, and CLOB execution response for later loss autopsy.
- The local PC is not assumed to be low-latency infrastructure; the strategy gives up the final 90 seconds instead of fighting for the last theoretical edge.

Weekend light mode is enabled by the local dashboard and watch launch scripts. At startup the scripts enable U.S. equity session mode, so the bot checks the NYSE regular core session (`9:30-16:00 America/New_York`, with weekends and market holidays closed). When U.S. equities are closed it keeps the normal entry path but uses effective near-close limits; when U.S. equities are open, the dashboard lights `高頻模式` and the unscaled near-close limits apply.

- order size is multiplied by `NEAR_CLOSE_WEEKEND_ORDER_SIZE_MULTIPLIER` (`0.5` in the launch scripts)
- crypto Up/Down order size is overridden to `NEAR_CLOSE_WEEKEND_CRYPTO_UPDOWN_ORDER_SIZE=5` in the launch scripts because the live CLOB rejects smaller sizes
- market, total, and position exposure limits are multiplied by `NEAR_CLOSE_WEEKEND_EXPOSURE_MULTIPLIER` (`0.7`)
- max spread is multiplied by `NEAR_CLOSE_WEEKEND_SPREAD_MULTIPLIER` (`0.8`)
- crypto Up/Down start-distance requirements are multiplied by `NEAR_CLOSE_WEEKEND_START_DISTANCE_MULTIPLIER` (`0.15` in the launch scripts)
- crypto Up/Down price heat is relaxed only in weekend light mode: min ask `0.78`, min midpoint `0.76`, and min entry `0.78`

These weekend-only overrides do not apply in high-frequency mode. Spread remains tightened by the weekend multiplier, depth stays unchanged, and the bid skip still prevents chasing `best_bid >= 0.96`. Weekend qualification uses the effective order size, so scanner sizing and actionable sizing stay aligned. The lowest dynamic start-distance rung remains above the cancel line (`0.00085 * 0.15 = 0.0001275 > 0.00012`) so a newly eligible order does not immediately trip the start-distance live guard.

For the continuous-order weekend test mode, the local launch scripts set `MAX_DAILY_LIVE_NOTIONAL=0`, which disables only the daily live notional cap. The kill switch, per-order cap, daily order count cap, post-only checks, spread/depth filters, exposure limits, and live preflight still apply.

The post-fill hedge flow is intentionally sequential. The bot never places the opposite-side hedge at the same time as the entry. It waits until the original near-close maker BUY is confirmed filled in `live_trades`, then evaluates an opposite-outcome BUY hedge.

Example:

```text
Entry YES @ 0.90
Hedge NO @ 0.03
Locked profit if hedge fills = 1.00 - 0.90 - 0.03 = 0.07
```

Key hedge settings:

- `NEAR_CLOSE_POST_FILL_HEDGE_ENABLED=false` by default; live hedge placement must be explicitly enabled.
- `NEAR_CLOSE_POST_FILL_HEDGE_SHADOW_ENABLED=true` logs counterfactual hedge decisions when live placement is disabled.
- `NEAR_CLOSE_HEDGE_DEFAULT_PRICE=0.03`
- `NEAR_CLOSE_HEDGE_MIN_LOCKED_PROFIT=0.02`
- `NEAR_CLOSE_HEDGE_MAX_BEST_ASK=0.03`
- `NEAR_CLOSE_HEDGE_MIN_DEPTH=5`
- `NEAR_CLOSE_HEDGE_MAX_SPREAD=0.05`
- `NEAR_CLOSE_HEDGE_MIN_MINUTES_TO_END=0.25`
- `NEAR_CLOSE_HEDGE_DYNAMIC_PRICING_ENABLED=false`

The hedge respects the kill switch, daily/per-plan risk checks, market close checks, opposite-side liquidity, max ask, spread, and duplicate-order guard. Current risk accounting treats the hedge conservatively as another live order for daily limits; it does not weaken existing exposure controls.

Risks: the hedge may not fill, the market can reverse before the hedge is submitted, opposite liquidity can disappear, the orderbook can be stale, and the CLOB V2 adapter can reject or normalize order details differently than expected. Shadow mode should be used first for counterfactual analysis.

## Near-Close Post-Fill Profit Take

The profit-take flow is also sequential. The bot does not place a SELL before the entry BUY is filled because SELL orders must be backed by outcome-token balance. After a near-close maker BUY is confirmed filled, the bot can place a position-backed SELL to take profit before resolution.

Default ladder:

```text
entry <= 0.865 -> sell 0.950
entry <= 0.885 -> sell 0.955
entry <= 0.905 -> sell 0.965
entry <= 0.925 -> sell 0.970
entry <= 0.940 -> sell 0.985
entry >  0.940 -> skip
```

Key profit-take settings:

- `NEAR_CLOSE_PROFIT_TAKE_ENABLED=true`
- `NEAR_CLOSE_PROFIT_TAKE_LIVE_ENABLED=false` by default; local launch scripts enable it for live testing.
- `NEAR_CLOSE_PROFIT_TAKE_SHADOW_ENABLED=true`
- `NEAR_CLOSE_PROFIT_TAKE_ORDER_TYPE=GTD`
- `NEAR_CLOSE_PROFIT_TAKE_GTD_SECONDS=240`
- `NEAR_CLOSE_PROFIT_TAKE_MIN_NET_PROFIT=0.20`
- `NEAR_CLOSE_PROFIT_TAKE_MIN_DEPTH=5`
- `NEAR_CLOSE_PROFIT_TAKE_MAX_SPREAD=0.08`

Stop-exit remains the downside protection path. If stop-exit is triggered while a profit-taking SELL is still active, the bot first attempts to cancel the profit-taking SELL; if cancellation is unconfirmed, it skips the stop-exit submission to avoid double-selling the same token balance.

Strategy-level changes should be snapshotted before implementation: run validation, commit the current working strategy, push the branch, push an annotated version tag, then create a new strategy branch for the next experiment.
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
