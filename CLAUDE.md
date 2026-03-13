# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Real-time gamma exposure (GEX) analysis and automated 0DTE options trading system for SPY/SPX. Fetches live option chain data from Charles Schwab, calculates per-strike gamma exposure, and runs a signal-driven trading engine (AutoGEX) with position management, Alpaca paper execution, trade journaling, backtesting, and EOD reporting. Visualized via matplotlib (live plots) or Streamlit (web dashboard).

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run AutoGEX trading engine (separate process, polls every 7s)
python trading_engine.py

# Run web dashboard (Streamlit — unified entry point with sidebar view toggle)
streamlit run dashboard.py

# Run backtest on historical GEX snapshots
python backtest.py --start 2025-01-01 --end 2025-03-11 [--out results.csv]

# Run live matplotlib plotter (market hours only)
python main.py

# Validate Schwab credentials/config
python check_schwab_config.py

# Run tests
python -m pytest test_position_manager.py test_eod_report.py test_trading_engine.py
```

## Architecture

### System Overview

Two independent processes communicate via SQLite:

```
Schwab API ──→ trading_engine.py ──→ signal_engine.py ──→ position_manager.py ──→ execution.py (Alpaca)
                     │                                                                     │
                     └──→ gex_utils.py                                                    │
                     └──→ trade_journal.py (SQLite: trades, positions, state) ←───────────┘
                     └──→ eod_report.py (fires once after market close)

Streamlit dashboard.py ──→ reads SQLite (read-only) + autogex_dashboard.py view
                        ──→ writes autogex_config.json (via sliders)

Schwab API ──→ main.py ──→ gamma_analysis.py ──→ plotter.py (matplotlib)
                      └──→ db_storage.py (PostgreSQL, optional)
```

### Key Files

#### AutoGEX Trading Engine

- **`trading_engine.py`** — Main 7-second poll loop (separate process from Streamlit). Orchestrates: fetch SPY GEX from Schwab via `gex_utils.py`, evaluate signals, manage open positions, place orders via Alpaca (or dry-run log), update SQLite journal. SIGINT handler closes all positions before exit. Exponential backoff on fetch failures; Schwab 401 token refresh mid-session.

- **`signal_engine.py`** — `GexSignalState` (immutable GEX snapshot) + `evaluate()` scoring five components:
  1. King Node Proximity (0–3 pts): buy at support / sell at resistance
  2. Gatekeeper Bounce/Break (0–1 pt): proximity to nearby GEX extremes
  3. Gamma Velocity Surge (0–2 pts): strike gaining gamma or total GEX shifting
  4. Regime Alignment (0–1 pt): bonus if direction matches GEX regime
  5. Gamma Flip Proximity: veto or penalty near zero-crossing
  Returns `SignalResult` with conviction (0–9+), direction (CALL/PUT/NONE), and per-component scores.

- **`position_manager.py`** — `PositionManager` + `OpenPosition` manage full position lifecycle:
  - Block sizing: 4–12 contracts scaled by conviction (3–9), capped by `max_risk_per_trade` ($2,000)
  - Stops: initial (50% below entry) → breakeven trigger (gatekeeper clears) → trailing (time-dependent: AM 30%, afternoon 15%, final 10%)
  - Partial exits: Tranche A (60%) at +30% gain; Tranche B trails to max profit
  - Time rules: AM conviction bonus (9:35–11:30), no entries after 2:30 PM, hard close at 3:55 PM
  - Circuit breaker: closes all if daily realized P&L ≤ −$2,000
  - Cooldowns: 300s after entry, 300s after stop-out, 60s after flip cross

- **`execution.py`** — `AlpacaClient` wrapping Alpaca trading API. Limit buy with smart retry (reprice +$0.02, then market fallback). Market sell for exits/stops. Guards against overselling. `reconcile_positions()` syncs local state to Alpaca on engine startup.

- **`trade_journal.py`** — SQLite (`autogex.db` by default) with 6 tables: `trades`, `positions`, `engine_state`, `daily_summary`, `gex_snapshots`, `eod_reports`. Includes migration logic for schema evolution. Tracks tranches via `parent_trade_id`; stores full GEX snapshots at entry/exit for post-analysis.

- **`eod_report.py`** — Fires once per day after market close if ≥1 trade executed (idempotent via `eod_reports` table). Groups Tranche A+B rows into logical trades, computes R-multiples (P&L ÷ capital risked), weekly/monthly P&L, optionally sends SMTP email.

- **`config.py`** — `AutoGexConfig` dataclass with ~25 tunable parameters. `load_config()` reads `autogex_config.json` or returns hardcoded defaults. `save_config()` persists to JSON (used by dashboard sliders).

- **`gex_utils.py`** — Shared GEX utilities used by both engine and dashboard. `fetch_options_and_gex()` polls Schwab with retry + 401 refresh. `process_symbol_gex()` transforms raw chain into `SymbolGexData` (king strike, gatekeepers, gamma flip, velocity, regime).

- **`backtest.py`** — Offline historical replay. Loads SPY GEX snapshots from SQLite `gex_snapshots` table, runs signal evaluation and position management on each tick with simulated fills at next snapshot's option prices (~4s latency). Outputs equity curve, P&L, win rate, Sharpe, and trade CSV.

#### Dashboard

- **`dashboard.py`** — Unified Streamlit entry point. Sidebar toggle switches between "GEX Dashboard" (original) and "AutoGEX Trading" view (loads `autogex_dashboard.py`). GEX view: interactive symbol selector, Plotly heatmap of GEX by strike/expiration, Gemini LLM interpretation, King Node / support / resistance identification.

- **`autogex_dashboard.py`** — AutoGEX Streamlit view. Engine status bar, active positions table, live signal state card (per-component scores), today's trade log, performance metrics (win rate, Sharpe, drawdown). Controls: start/stop engine, close all (panic), config sliders. Reports tab: live session view, EOD summary, weekly/monthly P&L sub-tabs, CSV export.

#### GEX Plotter (Original)

- **`main.py`** — `GammaExposureScheduler` poll loop (every 4s, market hours 9:30–4:15 ET). Broker auto-detection (Schwab primary, TDA fallback). Manual OAuth for Schwab; Selenium login for TDA.

- **`gamma_analysis.py`** — `calculate_gamma_exposure()`: `multiplier × spot × gamma × volume × contract_size × spot × 0.01 / 1B`. `get_per_strike_details()` aggregates call/put OI and volume by strike.

- **`plotter.py`** — `RealTimeGammaPlotter`: 3-panel matplotlib figure (GEX histogram, Δ GEX per strike, total GEX + spot with rolling mean±StdDev bands and top-5 velocity dots).

- **`db_storage.py`** — Optional PostgreSQL raw snapshot storage. Fails silently if unavailable.

- **`secretsSchwab.py`** — Credentials module (gitignored). Loads from `.env`; never hardcode values.

### Configuration

#### `.env` (gitignored) — all secrets

| Variable | Purpose |
|---|---|
| `SCHWAB_API_KEY` / `SCHWAB_APP_SECRET` | OAuth credentials |
| `SCHWAB_REDIRECT_URI` | Default: `https://127.0.0.1` |
| `SCHWAB_TOKEN_PATH` | Token file location |
| `SCHWAB_OPTION_SYMBOL` | Default: `$SPX` |
| `SCHWAB_STRIKE_COUNT` | Number of strikes to fetch |
| `GEMINI_API_KEY` | Dashboard LLM interpretation |
| `DB_STORE_ENABLED` | Set `0` to disable PostgreSQL |
| `BROKER` | Force `schwab` or `tda` |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Alpaca paper trading |
| `ALPACA_PAPER` | Set `true` for paper mode |
| `EOD_EMAIL_TO/FROM`, `SMTP_HOST/PORT/USER/PASS` | EOD email (all optional) |
| `AUTOGEX_DB_PATH` | Override SQLite path |
| `LOG_LEVEL` | Default: `INFO` |

#### `autogex_config.json` — runtime tuning (editable via dashboard sliders)

Key parameters: `min_conviction` (currently 6), `max_trades_per_day` (10), `max_concurrent_positions` (3), `daily_loss_limit` ($2,000), `poll_interval_seconds` (7), `velocity_strike_threshold` (0.05), `cooldown_after_entry/stop` (300s), `dry_run` (true = log only, no Alpaca orders).

#### State files

- `autogex.db` — SQLite trade journal and GEX snapshot store
- `autogex_engine.pid` — PID of running engine process (checked by dashboard for liveness)
- `autogex.log` — Rotating log (5 MB max, 3 backups)

### Broker / Execution Support

- **Schwab** (primary data source): `schwab-py` library, OAuth with 7-day token refresh
- **TDA** (fallback data source): `tda-api` library, Selenium-automated login
- **Alpaca** (execution): `alpaca-py` v2 REST, paper trading mode

### Outstanding Items (TODOS.md)

1. **Trailing stop immediate-trigger risk** (low priority): `trail_stop = high_water_mark * (1 - trail_pct)` could theoretically set stop above current price if HWM spikes between ticks. Fix: add `and trail_stop < current_price` guard.
2. **DRY refactor**: `eod_report._fmt_pnl()` and `autogex_dashboard._pnl_str()` are identical — move to shared utility.

## gstack

Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

Available gstack skills:
- `/browse` — fast headless Chromium browsing (~100ms/command after first call)
- `/plan-ceo-review` — CEO/founder-mode plan review
- `/plan-eng-review` — Eng manager-mode plan review
- `/review` — pre-landing PR review
- `/ship` — merge, test, version bump, changelog, PR creation
- `/retro` — weekly engineering retrospective
