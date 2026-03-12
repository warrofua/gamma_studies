# Gamma Studies — GEX Dashboard & AutoGEX Trading System

Real-time gamma exposure analytics for SPX/SPY options, with an automated GEX-signal-based trading engine for SPY 0DTE options on Alpaca paper trading.

![5-8-24_build](https://github.com/warrofua/gamma_studies/assets/41028474/a3bd0271-5b8d-488d-a09e-c81f1c0f4da7)

---

## Prerequisites

- Python 3.10+
- A [Charles Schwab developer account](https://developer.schwab.com) with an approved app (for GEX data)
- An [Alpaca paper trading account](https://alpaca.markets) (for AutoGEX order execution)
- PostgreSQL running locally (for historical snapshot storage and backtesting)

**Install dependencies:**
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Configure credentials** — copy and edit the secrets file and `.env`:
```bash
# secretsSchwab.py — already templated, fill in your values
# .env — create this file:
SCHWAB_API_KEY=your_client_id
SCHWAB_APP_SECRET=your_app_secret
ALPACA_API_KEY=your_alpaca_key
ALPACA_SECRET_KEY=your_alpaca_secret
```

---

## System Overview

There are two independent entry points:

| Entry point | Purpose | When to run |
|---|---|---|
| `streamlit run dashboard.py` | Interactive GEX dashboard (browser) | Always — primary interface |
| `python trading_engine.py` | Automated trading engine (terminal) | When you want live paper trades |
| `python main.py` | Matplotlib live plotter | Legacy; dashboard is preferred |
| `python backtest.py` | Historical signal replay | Offline analysis |

The dashboard and trading engine run as separate processes and communicate via a shared SQLite file (`~/autogex_state.db` by default, configurable via `AUTOGEX_DB_PATH`).

---

## Launching the GEX Dashboard

```bash
source venv/bin/activate
streamlit run dashboard.py
```

Opens at `http://localhost:8501`. On first run it will authenticate to Schwab via a browser popup (or the manual OAuth flow if using the command-line).

### GEX Dashboard View

The default view shows SPX/SPY gamma exposure in real time:

- **Per Strike Gamma Exposure** (top bar chart) — each bar is one strike. Blue bars = net positive GEX at that strike (dealers are long gamma → support/mean reversion force). The tallest positive bar is the **King Node** — the primary GEX anchor.
- **Change in Gamma per Strike** (middle bar chart) — how GEX at each strike changed since the last tick. Red bars = largest movers; these indicate where new option premium is flowing.
- **Total GEX + Spot Price over Time** (bottom line chart) — blue line tracks cumulative net GEX (positive = pinning regime, negative = trending regime); green line is SPX spot. Dots on the spot line mark strikes where the largest gamma changes occurred (red = negative change, green = positive).

**Sidebar controls:**
- **Symbol selector** — switch between tracked symbols
- **Strike range** — how many strikes to display around ATM
- **GEX min threshold** — noise floor filter; raise it to see only significant strikes
- **LLM Interpretation** — optional Gemini-powered GEX narrative (requires a Gemini API key)

**Key GEX indicators in the sidebar:**
- **King Node** — strike with the highest |GEX|; the primary support/resistance magnet
- **Gamma Flip** — the strike where cumulative GEX crosses zero; above = pinning regime, below = trending regime
- **Distance to Flip (DtF)** — how far spot is from the flip, as % of spot price. >1% = stable regime; 0.5–1% = transitional; <0.5% = volatile/noisy
- **Gatekeeper strikes** — top support and resistance levels by GEX magnitude
- **Gamma velocity** — the strike gaining GEX the fastest this tick

### AutoGEX Trading View

Switch to this view using the **Mode** radio button at the top of the sidebar.

**Row 1 — Status Bar:**
- Engine status badge (🟢 Running / 🔴 Stopped / 🚨 Circuit breaker)
- Daily realized P&L
- Trades used today (e.g. `2 / 5`)
- Current GEX regime (`positive_stable`, `positive_transition`, `negative_trending`, `negative_transition`)
- Countdown to 3:55 PM hard close

**Row 2 — Positions + Live Signal:**
- Left: open position cards — direction, strike, entry price, current stop, tranche A status, age, conviction
- Right: all five signal scores (King Node, Gatekeeper, Velocity, Regime alignment, Flip penalty), net conviction, direction, regime

**Row 3 — Trade Log:**
- Every completed trade for the day: direction, strike, entry/exit price, hold time, P&L, cumulative P&L
- Tranche A and B exits appear as separate rows

**Row 4 — Performance Metrics:**
- Today / This Week / All-Time columns with win rate, average winner, average loser, total P&L, max drawdown, Sharpe ratio

**Row 5 — Controls:**
- Start / Stop engine (informational — does not launch/kill the process; use the terminal for that)
- **Close All** — two-click confirmation; writes a close command to `autogex_control.txt`
- Config sliders (conviction threshold, max trades/day, max risk/trade) — auto-save to `autogex_config.json`

The AutoGEX view auto-refreshes every 5 seconds.

---

## Launching the Trading Engine

The engine runs as a **separate terminal process** — do not run it inside the Streamlit process.

```bash
source venv/bin/activate
python trading_engine.py
```

**Options:**
```bash
python trading_engine.py --dry-run       # Force dry-run (no real orders) regardless of config
python trading_engine.py --config path/to/autogex_config.json
```

**Dry-run is the default** (`dry_run: true` in `autogex_config.json`). To place real Alpaca paper orders, set `dry_run: false` in the config file, or edit it via the dashboard controls.

### Engine startup sequence

1. Loads `.env` and `autogex_config.json`
2. Authenticates to Schwab (reuses stored token; prompts for OAuth if token is missing or expired)
3. Connects to Alpaca and checks buying power
4. Initializes SQLite journal (`~/autogex_state.db`)
5. Reconciles any open positions found in the journal from a previous session
6. Enters the main poll loop (default: every 7 seconds)

### Engine terminal output

All output also writes to `autogex.log` (rotating, 5 MB max, 3 backups kept). Set `LOG_LEVEL=DEBUG` in your environment for verbose output including every signal evaluation.

```
2026-03-11 09:35:02 [INFO] [Engine] Starting AutoGEX Trading Engine [DRY-RUN]
2026-03-11 09:35:04 [INFO] [Engine] Schwab authentication successful.
2026-03-11 09:35:05 [INFO] [Engine] New trading day: 2026-03-11
2026-03-11 09:35:12 [INFO] [DRY-RUN] ENTER CALL 4x SPY260311C00582000 @ $1.50 | conviction=4 | stop=$0.75
2026-03-11 09:47:33 [INFO] [DRY-RUN] PARTIAL SELL Tranche A: 2x SPY260311C00582000 @ $1.95 | partial P&L=$90.00
2026-03-11 10:02:11 [INFO] [DRY-RUN] CLOSE CALL 2x SPY260311C00582000 @ $1.41 | P&L=-$18.00 | reason=Stop hit at 1.41
```

### Stopping the engine

Press `Ctrl+C`. In live mode the engine will submit market orders to close all open positions before exiting. In dry-run mode it logs the open positions and exits cleanly.

---

## Understanding the Signals

The engine scores five GEX signals each tick and combines them into a single conviction score. A trade is only entered when conviction ≥ 3 and at least two signals agree on direction.

### Signal Scores

| Signal | Max | What it measures |
|---|---|---|
| **King Node** | +3 | Spot proximity to the dominant GEX strike |
| **Gatekeeper** | +1 | Spot within 0.3% of a major support/resistance GEX strike |
| **Velocity** | +2 | GEX building rapidly at a nearby strike; total gamma delta |
| **Regime** | +1 | Signal direction matches the current GEX regime |
| **Flip Penalty** | 0 or −2 | Spot within 0.5% of the zero-gamma flip → penalty; within 0.3% → veto |

**Conviction to block size mapping:**

| Conviction | Block size | Max risk |
|---|---|---|
| 3 | 4 contracts | ~$600 |
| 5–6 | 6–8 contracts | ~$900–$1,200 |
| 8+ | 10–12 contracts | up to $2,000 cap |

Block size also caps at `$2,000 / (option_price × 100)` regardless of conviction.

### Regime Guide

| Regime | Meaning | System behavior |
|---|---|---|
| `positive_stable` | Dealers long gamma, DtF > 1% | Mean reversion trades; CALL bias on dips to King |
| `positive_transition` | Dealers long gamma, DtF ≤ 1% | Near the flip — size is reduced, stops tighter |
| `negative_trending` | Dealers short gamma, DtF > 1% | Momentum trades; PUT bias on breaks below gatekeepers |
| `negative_transition` | Dealers short gamma, DtF ≤ 1% | Near the flip — most cautious regime |

---

## Trade Journal (SQLite)

The trading engine writes all activity to `~/autogex_state.db` (path configurable via `AUTOGEX_DB_PATH` env var). You can inspect it directly with any SQLite client.

```bash
sqlite3 ~/autogex_state.db
```

**Key tables:**

`trades` — one row per entry or partial/full exit:
```sql
SELECT date, direction, strike, entry_price, exit_price, realized_pnl, exit_reason, conviction
FROM trades ORDER BY entry_time DESC LIMIT 20;
```

`positions` — currently open positions (cleared when closed):
```sql
SELECT symbol, direction, strike, entry_price, current_stop, remaining_qty FROM positions;
```

`engine_state` — live engine status written each tick:
```sql
SELECT key, value, updated_at FROM engine_state;
-- key='status': 'running' | 'stopped' | 'starting'
-- key='last_signal_json': full signal evaluation as JSON
-- key='daily_pnl': today's realized P&L
-- key='trades_today': number of completed trades today
```

`daily_summary` — aggregated per-day performance (computed at close and on shutdown):
```sql
SELECT date, total_pnl, trade_count, win_count, avg_winner, avg_loser, max_drawdown
FROM daily_summary ORDER BY date DESC;
```

**GEX snapshots** — every trade row stores the full GEX state at entry (`gex_snapshot_json`) and exit (`gex_snapshot_exit_json`) as JSON. This lets you reconstruct exactly what the GEX curve, King Node, and regime looked like at the moment of each trade decision.

---

## Running a Backtest

The backtest replays historical GEX snapshots from PostgreSQL through the full signal and position management pipeline.

**Requirement:** You need historical data in the `spx_options_data` PostgreSQL table. This is populated automatically while `main.py` runs during market hours.

```bash
# Basic run
python backtest.py --start 2025-01-01 --end 2025-03-11

# Save trade log to CSV
python backtest.py --start 2025-01-01 --end 2025-03-11 --out results.csv

# Use a specific config
python backtest.py --start 2025-01-01 --end 2025-03-11 --config /path/to/autogex_config.json
```

**Note on data:** `main.py` collects SPX option chain data (the index). The trading engine fetches SPY (the ETF) separately. If you want a true SPY backtest, you need at least a few weeks of the trading engine running in dry-run mode so the engine's SPY GEX snapshots accumulate in the journal — then run a custom replay from those. The backtest currently replays whatever is in PostgreSQL.

**Sample output:**
```
[Backtest] Loaded 4,823 rows. Starting replay...
[Replay] Ticks processed: 4,612  |  Skipped: 211

=== TRADE LOG ===
...

=== Backtest Summary ===
Period: 2025-01-01 to 2025-03-11
Total trades:    42
Winners:         28  (66.7%)
Losers:          14  (33.3%)
Avg winner:      +$312.50
Avg loser:       -$180.00
Total P&L:       +$6,225.00
Max drawdown:    -$1,240.00
Ticks processed: 4,612
```

---

## Configuration

All tunable parameters live in `autogex_config.json` (auto-created with defaults on first run). You can edit it directly or use the sliders in the AutoGEX dashboard view.

**Key parameters:**

| Parameter | Default | Description |
|---|---|---|
| `dry_run` | `true` | Set to `false` to place real Alpaca paper orders |
| `min_conviction` | `3` | Minimum score to enter a trade |
| `max_risk_per_trade` | `2000.0` | Max dollars at risk per trade (caps block size) |
| `initial_stop_pct` | `0.50` | Initial stop at 50% below entry price |
| `tranche_a_target_pct` | `0.30` | Sell first 50% at +30% gain |
| `daily_loss_limit` | `2000.0` | Circuit breaker threshold |
| `max_trades_per_day` | `5` | Hard cap on entries per day |
| `no_new_entries_after` | `"14:30"` | No new entries after this time ET |
| `hard_close_time` | `"15:55"` | Close all positions at this time ET |
| `poll_interval_seconds` | `7` | How often the engine ticks |
| `strike_count` | `50` | Strikes to fetch per side from Schwab |
| `velocity_strike_threshold` | `0.05` | Min |GEX change| ($B) to score velocity signal |

---

## File Map

```
main.py                  — Legacy matplotlib live plotter (GammaExposureScheduler)
dashboard.py             — Streamlit dashboard entry point
  ├── gex_utils.py       — Shared GEX fetch + processing functions
  ├── gamma_analysis.py  — Core GEX math (calculate_gamma_exposure, get_per_strike_details)
  └── autogex_dashboard.py — AutoGEX Streamlit view (render_autogex_view)

trading_engine.py        — AutoGEX trading engine (separate process)
  ├── signal_engine.py   — Five GEX signals, conviction scoring, regime classifier
  ├── position_manager.py — Block sizing, stop mechanics, tranche exits, circuit breaker
  ├── execution.py       — Alpaca API: limit buy (with retry), market sell, close all
  ├── trade_journal.py   — SQLite read/write (trades, positions, engine_state, daily_summary)
  └── config.py          — AutoGexConfig dataclass + load/save

backtest.py              — PostgreSQL historical replay engine
db_storage.py            — PostgreSQL write (raw option chain snapshots)
plotter.py               — Matplotlib subplot manager (used by main.py)
secretsSchwab.py         — Schwab credentials (not committed)
autogex_config.json      — Runtime config (created on first run)
autogex_state.db         — SQLite journal (created on first run, default path ~/autogex_state.db)
autogex.log              — Rotating engine log (created when trading_engine.py runs)
```

---

## Troubleshooting

**Schwab token expired:**
```
Delete token file and re-authenticate:
rm /path/to/schwab_token.json
python trading_engine.py   # will prompt for OAuth flow
```

**Alpaca not connecting:**
- Verify `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are set in `.env`
- Paper trading uses `https://paper-api.alpaca.markets` automatically when `alpaca-py` is configured with `paper=True`

**Dashboard shows "Engine stopped" even though engine is running:**
- The engine and dashboard share `autogex_state.db`. Verify they're using the same path (check `AUTOGEX_DB_PATH` env var on both processes).

**Backtest fails to connect to PostgreSQL:**
- Set `PGHOST`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` env vars, or verify the defaults match your local Postgres setup (default: `host=localhost`, `dbname=spx_options_data`, `user=postgres`, `password=password`).

**No signals firing / conviction always 0:**
- Markets may be closed (engine checks 9:30–4:15 ET, Mon–Fri)
- DtF may be < 0.3% triggering the flip veto — check the regime tile in the dashboard
- Raise `LOG_LEVEL=DEBUG` to see per-signal scores each tick
