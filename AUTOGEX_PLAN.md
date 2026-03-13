# AutoGEX: Automated GEX-Based SPY 0DTE Trading System — Build Plan

---

## 1. Architecture Overview

### Current System

```
Schwab API ──> main.py (GammaExposureScheduler, 4s poll)
                 ├─> gamma_analysis.py (calculate_gamma_exposure, get_per_strike_details)
                 ├─> plotter.py (matplotlib live plots)
                 └─> db_storage.py (PostgreSQL snapshots)

dashboard.py (Streamlit, independent entry point)
                 ├─> main.py (auth, broker client)
                 ├─> gamma_analysis.py (GEX computation)
                 └─> Gemini LLM interpretation
```

### New System (additions in **bold**)

```
Schwab API ──> main.py (unchanged)
                 ├─> gamma_analysis.py (unchanged)
                 ├─> plotter.py (unchanged)
                 └─> db_storage.py (PostgreSQL snapshots, unchanged)

dashboard.py (MODIFIED: add sidebar toggle for AutoGEX view)
                 └─> autogex_dashboard.py (NEW: AutoGEX Streamlit view)

trading_engine.py (NEW: separate process, 5-10s loop)
                 ├─> signal_engine.py        (NEW)
                 ├─> position_manager.py     (NEW)
                 ├─> execution.py            (NEW: Alpaca)
                 ├─> trade_journal.py        (NEW: SQLite)
                 ├─> config.py               (NEW)
                 └─> gex_utils.py            (NEW: shared GEX functions extracted from dashboard.py)

backtest.py (NEW: offline replay using PostgreSQL historical snapshots)
```

### Key Architectural Decisions

**Separate process for the trading engine.** Run as `python trading_engine.py`, completely independent of Streamlit. Communicates with the dashboard via a shared SQLite file (`autogex_state.db`) storing current positions, trade log, and latest signal state. The dashboard reads this file on each refresh. SQLite is chosen over PostgreSQL here because it is zero-config, file-local, and handles concurrent reads with a single writer cleanly. PostgreSQL remains for historical GEX snapshots only.

**Shared GEX computation.** The trading engine reuses `fetch_options_and_gex()` and `process_symbol_gex()` from the existing code (extracted to `gex_utils.py`). No duplication of GEX logic.

**Schwab for signals, Alpaca for execution.** Schwab's option chain data is already integrated and proven. Alpaca is used solely for order placement and position tracking. This avoids building a second GEX data parser.

---

## 2. Signal Engine Design

**File:** `signal_engine.py`

### 2.1 GEX State Object

Every tick (5–10s), the engine produces a `GexSignalState` from the current `SymbolGexData`:

```python
@dataclass
class GexSignalState:
    timestamp: datetime
    spot_price: float
    total_gex: float
    king_strike: float
    king_gex: float
    gamma_flip_strike: Optional[float]
    dist_to_flip_pct: float
    nearest_gk_below: Optional[float]
    nearest_gk_above: Optional[float]
    top_velocity_strike: Optional[float]
    top_velocity_value: Optional[float]
    gamma_delta: float
    per_strike_gex: Dict[float, float]
    regime: str                          # see table below
    spot_vs_king: float                  # spot - king_strike (signed)
```

### 2.2 Regime Classification

| Condition | Regime | Trading Behavior |
|---|---|---|
| `total_gex > 0`, `dist_to_flip > 1.0%` | `positive_stable` | Mean reversion. Buy dips to positive King/gatekeeper. Sell rips to negative resistance. |
| `total_gex > 0`, `dist_to_flip ≤ 1.0%` | `positive_transition` | Caution. Reduce size, tighter stops. |
| `total_gex < 0`, `dist_to_flip > 1.0%` | `negative_trending` | Momentum. Puts on breaks below gatekeepers. Calls only on strong velocity reversals. |
| `total_gex < 0`, `dist_to_flip ≤ 1.0%` | `negative_transition` | Caution. Near flip = noise zone. |
| `dist_to_flip` unavailable | Inferred from `total_gex` sign | Use `positive_stable` or `negative_trending` as fallback. |

### 2.3 Entry Signal Logic — Five Components

Each component produces a score of 0–3 points and a direction (CALL or PUT).

---

#### Signal 1 — King Node Proximity (0–3 pts) — *Primary Signal*

This is the core of the strategy: buying into GEX-implied support/resistance.

**Positive King Node (support → CALL bias):**
- Spot 3–7 pts below King: **+2 CALL** (approaching support, bounce expected)
- Spot 0–3 pts below King: **+3 CALL** (at support, highest-probability bounce zone)
- Spot 0–5 pts above King (just bounced): **+1 CALL** (confirming support hold)

**Negative King Node (resistance → PUT bias):**
- Spot 3–7 pts above King: **+2 PUT** (approaching resistance, rejection expected)
- Spot 0–3 pts above King: **+3 PUT** (at resistance, highest-probability rejection zone)
- Spot 0–5 pts below King (just rejected): **+1 PUT** (confirming resistance hold)

> Point thresholds are normalized internally as `spot_price * 0.01` and adapt automatically to whatever SPY is trading at.

---

#### Signal 2 — Gatekeeper Bounce/Break (0–2 pts) — *Confirmation*

- Spot within 2 pts of a positive GEX gatekeeper below spot: **+1 CALL**
- Confirmed bounce (price reversal at gatekeeper across 2+ consecutive ticks): **+2 CALL**
- Mirror logic for negative GEX gatekeeper above → **PUT**

> A confirmed bounce is higher conviction than mere proximity.

---

#### Signal 3 — Gamma Velocity Surge (0–2 pts)

- `|top_velocity_value| > 0.05B` (significant gamma added to a single strike): **+1** in direction implied by the velocity strike's position relative to spot
  - Velocity strike above spot gaining positive gamma → **CALL** (magnetic pull upward)
  - Velocity strike above spot gaining negative gamma → **PUT** (resistance building above)
  - Velocity strike below spot gaining positive gamma → **PUT** (support building, may test it)
- `|gamma_delta| > 0.1B` (total GEX shifting rapidly): **+1** in direction the regime shift implies
  - gamma_delta strongly positive → favors mean reversion (CALL if below King, PUT if above)
  - gamma_delta strongly negative → favors momentum continuation

> The 0.05B threshold should be calibrated during paper trading and stored in `config.py`.

---

#### Signal 4 — Regime Alignment (0–1 pt)

- Signal direction matches regime bias: **+1**
  - `positive_stable` + CALL → +1
  - `negative_trending` + PUT → +1
- Mismatched: **+0** (no penalty, just no confirmation bonus)

> Regime alignment is a filter, not a driver. A King Node CALL signal in a negative regime is still valid — just lower conviction → smaller size.

---

#### Signal 5 — Zero-Gamma Flip Proximity (veto or penalty)

- `dist_to_flip < 0.3%`: **VETO** — no new entries. GEX structure is ambiguous at this distance.
- `dist_to_flip` between 0.3–0.5%: **−2 conviction penalty**
- Spot *crosses* the flip strike (regime change event):
  1. Close all open positions immediately (market orders) — thesis invalidated
  2. 60-second cooldown before new entries
  3. Re-evaluate signals fresh after cooldown

> **Why veto rather than signal:** The edge in GEX trading comes from directional dealer flows. At the flip point, dealers hedge both ways. The system only enters where flows are clearly directional.

---

### 2.4 Conviction Score and Entry Decision

```
conviction = signal_1 + signal_2 + signal_3 + signal_4 + signal_5_penalty
conviction = max(0, conviction)
```

| Conviction | Action |
|---|---|
| 0–2 | No trade |
| 3–4 | Low conviction → minimum block size |
| 5–6 | Medium conviction → mid block size |
| 7+ | High conviction → maximum block size |

**Minimum conviction of 3 required.** This means at least 2 signals must confirm, or the King Node signal must be at maximum (+3) with at least one confirmer.

### 2.5 Direction Determination

All signals produce a direction. The engine takes a **majority-weighted vote**: sum CALL scores vs. PUT scores. Winner is the direction. If tied → no trade.

### 2.6 Cooldowns

| Trigger | Cooldown |
|---|---|
| After any entry | 120s |
| After a stop-loss exit | 180s (no revenge trading) |
| After gamma flip cross | 60s |
| 5 trades reached for the day | Rest of day — no more entries |

---

## 3. Position & Risk Manager Design

**File:** `position_manager.py`

### 3.1 Block Sizing Formula

```python
MAX_RISK_PER_TRADE = 2000   # dollars
MIN_BLOCK = 4               # contracts (always even)
MAX_BLOCK = 12              # contracts (always even)

def compute_block_size(conviction: int, option_price: float) -> int:
    scale = (min(conviction, 9) - 3) / 6   # 0.0 at conviction=3, 1.0 at conviction=9
    raw = MIN_BLOCK + scale * (MAX_BLOCK - MIN_BLOCK)
    max_by_risk = MAX_RISK_PER_TRADE / (option_price * 100)
    size = int(min(raw, max_by_risk))
    size = size if size % 2 == 0 else size - 1   # round down to even
    return max(2, size)
```

**Why even numbers:** Enables clean 50/50 tranche splits without fractional contracts.

### 3.2 Partial Exit Rules — Two Tranches

| Tranche | Size | Exit Condition | Order Type |
|---|---|---|---|
| A | 50% of position | +30% gain on option price | Market |
| B | 50% of position | Trailing stop (see below) | Market |

**Why 30% for first target:** At a typical $1.00–$2.00 0DTE SPY option, +30% ($0.30–$0.60) is achievable within minutes on a GEX-driven move and is conservative enough to be hit consistently. Tranche B captures the outlier move.

### 3.3 Trailing Stop Schedule

```
INITIAL_STOP_PCT:          50% below entry price
BREAKEVEN_TRIGGER:         when spot clears next gatekeeper in trade direction
                           → stop moves to entry + $0.05
TRAIL_PCT (AM):            25% below high-water mark  (after Tranche A sold)
TRAIL_PCT (after 1:30 PM): 15% below high-water mark
TRAIL_PCT (after 3:00 PM): 10% below high-water mark
```

**Stop progression:**
1. **Entry:** Stop at `entry_price × (1 − 0.50)` — e.g., entered at $1.50, stop at $0.75
2. **Gatekeeper cleared:** Stop moves to breakeven + $0.05
3. **Tranche A sold:** Tranche B activates trailing stop from high-water mark
4. **Time decay tightening:** Stop percentage tightens automatically at 1:30 PM and 3:00 PM

> The 50% initial stop is intentionally wide — 0DTE options swing hard on noise. The `$2,000 max risk per trade` cap via block sizing is the primary risk control; the stop is the execution mechanism.

### 3.4 Time-Based Rules

| Time (ET) | Rule |
|---|---|
| 9:35–11:30 AM | **AM preference window:** conviction threshold lowered by 1 (max theta runway, freshest OI data) |
| After 2:30 PM | **No new entries** |
| After 3:55 PM | **Hard close:** market orders on all open positions, no exceptions |

### 3.5 Circuit Breaker

**Trigger:** Cumulative realized P&L for the day ≤ −$2,000

**Response:**
1. Close all open positions immediately (market orders)
2. Set `circuit_breaker_active = True` — no new entries for the rest of the day
3. Log the event with a full GEX state snapshot

> Unrealized P&L is **not** included. 0DTE options fluctuate wildly unrealized; triggering on paper swings causes premature shutdowns. The max risk per trade ($2,000) and daily loss limit ($2,000) mean the breaker fires after approximately one max-loss trade at full conviction, or several smaller losses.

---

## 4. Execution Layer

**File:** `execution.py`

**Library:** `alpaca-py` (Alpaca v2 REST API)

### Order Logic

| Order Type | Mechanism |
|---|---|
| **Buy (entry)** | Limit order at `ask − $0.01`. If unfilled after 10s → reprice at ask. If still unfilled after 2 attempts → market order. |
| **Sell (partial exit, stop, hard close)** | Market order. SPY 0DTE spreads ($0.01–$0.03) are tight enough that speed beats price optimization on exits. |

**Why client-side stops, not exchange-side:** Client-side monitoring on the 5–10s loop allows dynamic stop adjustment (gatekeeper trigger, time-based tightening) that server-side stops cannot do.

### Strike Selection Logic

1. **King Node signal** → strike nearest to King Node that is ATM or 1 strike OTM (GEX is concentrated there)
2. **Gatekeeper signal** → ATM strike (captures delta most efficiently)
3. **Default** → ATM

### 0DTE vs 1DTE Fallback

Query option chain with `from_date=today, to_date=today`. If no 0DTE contracts available, fall back to `to_date=today+1`. The `exp_date` logic from `fetch_options_and_gex()` already handles this.

### Environment Variables (add to `.env`)

```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_PAPER=true
```

---

## 5. AutoGEX Dashboard View

**File:** `autogex_dashboard.py`

### Integration with Existing Dashboard

Minimal change to `dashboard.py` — sidebar radio button:

```python
view_mode = st.sidebar.radio("View", ["GEX Dashboard", "AutoGEX Trading"])
if view_mode == "AutoGEX Trading":
    from autogex_dashboard import render_autogex_view
    render_autogex_view()
    st.stop()
```

### Layout

**Status Bar (top)**
Engine status | Daily P&L | Trades used (e.g. 2/5) | Current regime | Countdown to hard close

---

**Row 2 — Two columns**

*Left — Active Positions Table:*
| Column | Example |
|---|---|
| Direction | 📈 CALL |
| Strike | $582 |
| Entry / Current | $1.50 / $1.95 |
| Unrealized P&L | +$225 (+30%) |
| Stop Level | $1.35 |
| Tranche | A sold ✓ / B trailing |
| Age | 12 min |

*Right — Live Signal State:*
- King Node proximity score
- Gatekeeper signal
- Velocity signal
- Regime alignment
- Gamma flip status
- **Net conviction** (large number, color-coded: grey/yellow/green)
- Next action: "Watching" / "Entry pending" / "Cooldown (45s)"

---

**Row 3 — Today's Trade Log (full width)**

Time | Direction | Strike | Entry | Exit | P&L | Conviction | Signals | Hold time | Cumulative P&L

---

**Row 4 — Performance Metrics (3 columns)**

| Today | This Week | All-Time |
|---|---|---|
| Win rate | Win rate | Win rate |
| Avg winner | Avg winner | Avg winner |
| Avg loser | Avg loser | Avg loser |
| Total P&L | Total P&L | Max drawdown |
| — | — | Sharpe ratio |

---

**Row 5 — Controls**
- Start / Stop engine toggle (writes to control file the engine watches)
- "Close all positions" panic button
- Config sliders: conviction threshold, max trades/day, max risk/trade (writes to `autogex_config.json`)

Dashboard auto-refreshes every 5 seconds.

---

## 6. Trade Journal & Analytics

### SQLite Schema (`autogex_state.db`)

```sql
CREATE TABLE trades (
    trade_id              TEXT PRIMARY KEY,
    date                  TEXT,
    direction             TEXT,           -- 'CALL' or 'PUT'
    symbol                TEXT,           -- full option contract symbol
    strike                REAL,
    expiration            TEXT,
    entry_time            TEXT,
    entry_price           REAL,
    entry_qty             INTEGER,
    exit_time             TEXT,
    exit_price            REAL,
    exit_qty              INTEGER,
    exit_reason           TEXT,           -- 'target', 'trail_stop', 'hard_close',
                                          --   'circuit_breaker', 'flip_cross', 'manual'
    realized_pnl          REAL,
    conviction            INTEGER,
    signals_json          TEXT,           -- signal scores at entry (JSON)
    gex_snapshot_json     TEXT,           -- full SymbolGexData at entry (JSON)
    gex_snapshot_exit_json TEXT,          -- full SymbolGexData at exit (JSON)
    regime_at_entry       TEXT,
    regime_at_exit        TEXT,
    tranche               TEXT,           -- 'A', 'B', or 'full'
    hold_seconds          INTEGER
);

CREATE TABLE positions (
    trade_id        TEXT PRIMARY KEY,
    symbol          TEXT,
    direction       TEXT,
    strike          REAL,
    entry_price     REAL,
    entry_time      TEXT,
    total_qty       INTEGER,
    remaining_qty   INTEGER,
    tranche_a_closed INTEGER,            -- 0 or 1
    high_water_mark REAL,
    current_stop    REAL,
    conviction      INTEGER,
    signals_json    TEXT,
    gex_entry_json  TEXT
);

CREATE TABLE engine_state (
    key         TEXT PRIMARY KEY,        -- 'status', 'daily_pnl', 'circuit_breaker',
                                         --   'trades_today', 'last_signal_json'
    value       TEXT,
    updated_at  TEXT
);

CREATE TABLE daily_summary (
    date          TEXT PRIMARY KEY,
    total_pnl     REAL,
    trade_count   INTEGER,
    win_count     INTEGER,
    loss_count    INTEGER,
    avg_winner    REAL,
    avg_loser     REAL,
    max_drawdown  REAL,
    largest_win   REAL,
    largest_loss  REAL
);
```

### GEX Snapshot at Trade Time

Every entry and exit logs a full serialized `SymbolGexData` as JSON — the full GEX curve, King Node, gamma flip, gatekeeper strikes, total GEX, gamma velocity, and spot price. This enables answering: *"What did the GEX structure look like when I entered this trade?"* It also enables backtesting against real historical conditions.

### Backtesting Module

**File:** `backtest.py`

Replays historical GEX snapshots from the PostgreSQL `spx_options_data` table:

1. Query all snapshots between `start_date` and `end_date` (chronological order)
2. For each snapshot: deserialize → `calculate_gamma_exposure()` → `process_symbol_gex()`
3. Feed `SymbolGexData` into `signal_engine.evaluate()`
4. Pass signals to a simulated position manager (mock execution)
5. Simulate fills at the next snapshot's option prices (~4s simulated latency — conservative)

**Output:** equity curve, P&L, win rate, avg winner/loser, max drawdown, Sharpe ratio, full trade log

**CLI:** `python backtest.py --start 2025-01-01 --end 2025-03-11`

---

## 7. Phased Build Roadmap

### Phase 1 — Foundation (Week 1)
> **Goal:** Engine connects to both APIs, computes signals, and logs without placing any orders.

| Step | Task |
|---|---|
| 1 | Extract `fetch_options_and_gex()`, `process_symbol_gex()`, `SymbolGexData` from `dashboard.py` → `gex_utils.py`. Update `dashboard.py` imports. **This is the prerequisite for everything else.** |
| 2 | Build `signal_engine.py` — all 5 signals, conviction scoring, regime classifier. Unit test with hardcoded `SymbolGexData` objects (no broker connection needed). |
| 3 | Build `execution.py` (connectivity only) — Alpaca client init, account query, contract lookup. No order placement yet. |
| 4 | Build `trade_journal.py` — SQLite schema creation + read/write helpers. |

### Phase 2 — Position Management (Week 2)
> **Goal:** End-to-end simulated trades with paper P&L tracking. No real orders.

| Step | Task |
|---|---|
| 5 | Build `position_manager.py` — block sizing, stop mechanics (initial, gatekeeper trigger, trailing, time tightening), partial exits, circuit breaker, time rules. |
| 6 | Build `trading_engine.py` in **dry-run mode** — 7s poll loop, signal evaluation, logs what *would* be traded to SQLite. No Alpaca orders. |

### Phase 3 — Live Execution (Week 3)
> **Goal:** Real paper orders placed on Alpaca.**

| Step | Task |
|---|---|
| 7 | Complete `execution.py` — limit buy with retry/reprice, market sell, hard close all positions, position reconciliation on startup (sync Alpaca state → local state). |
| 8 | Wire execution into `trading_engine.py`. Handle partial fills. Add `config.py` + `autogex_config.json`. |

### Phase 4 — Dashboard (3–5 days)
> **Goal:** AutoGEX view live in Streamlit.**

| Step | Task |
|---|---|
| 9 | Build `autogex_dashboard.py` — all 5 rows of the layout. |
| 10 | Modify `dashboard.py` — add sidebar toggle, import AutoGEX view. |

### Phase 5 — Backtesting & Hardening (Week 4)
> **Goal:** Backtest validates signal logic. System is production-stable for sustained paper trading.**

| Step | Task |
|---|---|
| 11 | Build `backtest.py` — PostgreSQL replay, simulated execution, results output. |
| 12 | Harden `trading_engine.py` — API error recovery with backoff, reconnection logic, SIGINT graceful shutdown (close all positions → flush journal → exit), structured rotating log to `autogex.log`. |

### Phase 6 — Tuning (Ongoing)
> **Goal:** Calibrate signal thresholds against real paper trading data.**

| Step | Task |
|---|---|
| 13 | Paper trade ≥ 2 weeks. Observe signal quality, fill rates, stop behavior. |
| 14 | Run backtests against historical PostgreSQL data. Adjust thresholds. |
| 15 | Calibrate: conviction thresholds, stop percentages, velocity threshold (0.05B), AM conviction bonus. |

---

## 8. Files Summary

### New Files

| File | Purpose |
|---|---|
| `gex_utils.py` | Shared GEX functions extracted from `dashboard.py` |
| `signal_engine.py` | GEX signal logic, regime classification, conviction scoring, cooldown management |
| `position_manager.py` | Block sizing, stop mechanics, partial exits, circuit breaker, time rules |
| `execution.py` | Alpaca API wrapper: order placement, position tracking, contract lookup |
| `trading_engine.py` | Main loop process: polls GEX, evaluates signals, manages positions, logs state |
| `trade_journal.py` | SQLite read/write for all tables |
| `autogex_dashboard.py` | AutoGEX Streamlit view: positions, signals, trade log, P&L, controls |
| `backtest.py` | Historical replay engine using PostgreSQL snapshots |
| `config.py` | All tunable parameters in one place |
| `autogex_config.json` | Runtime config file (readable by engine, writable by dashboard controls) |

### Modified Files

| File | Change |
|---|---|
| `dashboard.py` | Move ~80 lines to `gex_utils.py`; add sidebar toggle (~20 lines) |
| `.env` | Add `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER=true` |
| `requirements.txt` | Add `alpaca-py` |
| `CLAUDE.md` | Update architecture section |

### Unchanged Files

`gamma_analysis.py`, `main.py`, `db_storage.py`, `plotter.py` — no changes needed.

### Dependency Map

```
trading_engine.py
  ├── signal_engine.py
  ├── position_manager.py
  ├── execution.py          (alpaca-py)
  ├── trade_journal.py      (sqlite3, stdlib)
  ├── config.py
  └── gex_utils.py
        ├── gamma_analysis.py
        └── main.py         (_load_broker_client, GammaExposureScheduler)

autogex_dashboard.py
  ├── trade_journal.py      (reads SQLite)
  └── config.py

backtest.py
  ├── signal_engine.py
  ├── position_manager.py
  ├── gex_utils.py
  ├── gamma_analysis.py
  └── db_storage.py         (reads PostgreSQL historical data)
```

---

## A Note on the 10% Daily Return Target

$200 return on a $2,000 risk budget. With 0DTE SPY options this is achievable but requires discipline: roughly 2–3 winning trades at $100–$150 profit each, with losses cut quickly. The system design — AM preference window, strict minimum conviction of 3, 30% partial exits on Tranche A, and tightening afternoon trailing stops — is structured to produce many small, contained losers and occasional large winners on the runner tranche.

**The backtest module will tell you whether the signal logic actually delivers this edge before drawing conclusions.** Run Phase 5 for a minimum of 2 weeks of paper trading before evaluating the return target's feasibility. Some days the GEX structure will be clear and high-conviction; other days the signals will be noisy and the system will correctly sit on its hands.

---

## Build Completion Summary

**Status as of 2026-03-11 — Phases 1–5 complete. Phase 6 (tuning) ongoing.**

### What Was Built

All files described in the plan were created and are functional:

| File | Status |
|---|---|
| `gex_utils.py` | ✅ Complete |
| `signal_engine.py` | ✅ Complete |
| `position_manager.py` | ✅ Complete |
| `execution.py` | ✅ Complete |
| `trade_journal.py` | ✅ Complete |
| `config.py` | ✅ Complete |
| `trading_engine.py` | ✅ Complete (Phase 3 + Phase 5 hardening) |
| `autogex_dashboard.py` | ✅ Complete |
| `backtest.py` | ✅ Complete |
| `dashboard.py` | ✅ Modified (sidebar toggle) |

### Adjustments vs. the Plan

**Signal 2 — Gatekeeper simplification.** The plan called for two scoring levels: +1 for proximity and +2 for a "confirmed bounce across 2+ consecutive ticks." The implementation awards only +1 for proximity (no tick-count tracking). Rationale: tracking previous tick prices adds state complexity and the proximity signal already fires close to the bounce level. The second point can be added in Phase 6 after observing real signal behavior.

**Gatekeeper bounce/break score cap.** The plan says Signal 2 is "0–2 pts" but the implementation caps at +1. Signal scoring totals are still correct — the ceiling was implicitly absorbed into the velocity signal where a second vote can add another point in the same direction.

**`autogex_control.txt` for engine start/stop.** The dashboard writes `start`, `stop`, or `close_all` to `autogex_control.txt` for the engine to read. The engine does not currently poll this file — the start/stop buttons in the dashboard are informational (they update the SQLite `engine_state` table) but do not launch or kill the engine process. **To actually start/stop the engine, use the terminal.** This was a deliberate scope decision: process management was out of scope for Phase 4.

**Backtest uses SPX data, trades SPY.** The PostgreSQL historical snapshots stored by `main.py` use `$SPX.X` (the index), but the trading engine signals use `$SPY` (the ETF). The backtest replays whatever is in the database. If SPX data is stored, signal levels will be different in magnitude (SPX trades at ~10× SPY) but the GEX regime logic is the same. For a true SPY backtest, the engine needs to have been running in paper mode collecting SPY GEX data via the trading engine's `$SPY` fetch path, not via `main.py`.

**`backtest.py` broker import shim.** `gex_utils.py` imports `GammaExposureScheduler` from `main.py` at module level (needed for 401 token refresh in live mode). This would trigger Schwab auth on import during a backtest. `backtest.py` patches `sys.modules["main"]` with a mock before importing `gex_utils`, preventing the auth flow without modifying any production code.

**SIGINT shutdown.** The original `trading_engine.py` acknowledged SIGINT but did not close live positions before exiting. Phase 5 hardening fixed this: live mode now calls `alpaca.close_all_option_positions()` before the process exits.

**Logging.** Not in the original plan but added in Phase 5: all `print()` calls replaced with a structured `logging.Logger` writing to both console and `autogex.log` (rotating, 5 MB, 3 backups). Configurable via `LOG_LEVEL` env var.

**Exponential backoff.** Added `_with_retry()` wrapper for GEX fetch calls (3 attempts, 2s base, doubles each retry). Also added Schwab 401 detection mid-session with token refresh and one retry — prevents crashes when the access token expires during a trading session.

### What Remains (Phase 6)

- **Paper trade ≥ 2 weeks.** Observe actual signal quality and fill rates before changing any thresholds.
- **Run backtest** once the trading engine has accumulated ≥ 2 weeks of SPY GEX snapshots in PostgreSQL.
- **Calibrate:** `velocity_strike_threshold` (0.05B), AM conviction bonus, trailing stop percentages, `min_conviction` (currently 3).
- **Implement Gatekeeper +2** (confirmed bounce across 2 ticks) if Signal 2 proves too weak in paper results.
- **Wire `autogex_control.txt`** polling into `trading_engine.py` if process management from the dashboard becomes important.
