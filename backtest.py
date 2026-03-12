"""PostgreSQL historical replay engine for the AutoGEX trading system.

Usage:
    python backtest.py --start 2025-01-01 --end 2025-03-11 [--config path] [--out results.csv]
"""

import sys
import types

# Prevent gex_utils from triggering Schwab auth on import
_mock_main = types.ModuleType("main")


class _MockScheduler:
    @staticmethod
    def _proactive_schwab_token_refresh(*a, **kw):
        pass


_mock_main.GammaExposureScheduler = _MockScheduler
sys.modules.setdefault("main", _mock_main)

import argparse
import csv
import os
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
import pytz

from config import AutoGexConfig, load_config
from gamma_analysis import calculate_gamma_exposure, get_per_strike_details
from gex_utils import SymbolGexData, process_symbol_gex
from position_manager import OpenPosition, PositionAction, PositionManager
from signal_engine import SignalResult, evaluate

EASTERN = pytz.timezone("US/Eastern")

# Market session bounds (ET)
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 15)
NO_NEW_ENTRIES_ET = time(14, 30)
HARD_CLOSE_ET = time(15, 55)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def _db_connect():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", 5432)),
        dbname=os.environ.get("PGDATABASE", "spx_options_data"),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ.get("PGPASSWORD", "password"),
    )


def load_rows(start: date, end: date) -> List[Tuple[int, dict, datetime]]:
    """Return all option-chain rows between start and end dates, ordered by fetched_at."""
    conn = _db_connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, data, fetched_at
                FROM spx_options_data
                WHERE fetched_at >= %s AND fetched_at < %s + INTERVAL '1 day'
                ORDER BY fetched_at ASC
                """,
                (start, end),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    result = []
    for r in rows:
        fa = r["fetched_at"]
        if fa.tzinfo is None:
            fa = pytz.utc.localize(fa)
        result.append((r["id"], r["data"], fa))
    return result


# ---------------------------------------------------------------------------
# Option price lookup
# ---------------------------------------------------------------------------


def _get_option_price(
    data_json: dict,
    strike: float,
    direction: str,
    exp_date,
) -> Optional[float]:
    """Look up mid-price for a given strike/direction from the option chain data."""
    exp_str = exp_date.strftime("%Y-%m-%d") if hasattr(exp_date, "strftime") else str(exp_date)[:10]

    chain_map_key = "callExpDateMap" if direction == "CALL" else "putExpDateMap"
    exp_map = data_json.get(chain_map_key, {})

    # Find the matching expiration bucket (key starts with exp_str)
    strikes_dict = None
    for key, strikes in exp_map.items():
        if key.startswith(exp_str):
            strikes_dict = strikes
            break

    if strikes_dict is None:
        # Fall back to any expiration bucket
        for key, strikes in exp_map.items():
            strikes_dict = strikes
            break

    if not strikes_dict:
        return None

    # Try exact match, then nearest strike; Schwab keys are like "5900.0"
    candidates = [s for s in strikes_dict.keys() if abs(float(s) - strike) < 0.5]
    if not candidates:
        # nearest
        try:
            candidates = [min(strikes_dict.keys(), key=lambda s: abs(float(s) - strike))]
        except (ValueError, TypeError):
            return None

    options = strikes_dict.get(candidates[0], [])
    if not options:
        return None

    opt = options[0]
    bid = opt.get("bid", 0) or 0
    ask = opt.get("ask", 0) or 0
    last = opt.get("last", 0) or 0

    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    if last and last > 0:
        return float(last)
    return None


# ---------------------------------------------------------------------------
# GEX replay helpers
# ---------------------------------------------------------------------------


def _parse_exp_date_from_chain(data_json: dict) -> Optional[date]:
    """Find the nearest expiration date present in the option chain."""
    call_map = data_json.get("callExpDateMap", {})
    put_map = data_json.get("putExpDateMap", {})
    all_keys = list(call_map.keys()) + list(put_map.keys())

    dates = []
    for key in all_keys:
        try:
            part = (key.split(":")[0] if ":" in key else key)[:10]
            if len(part) >= 10:
                dates.append(datetime.strptime(part[:10], "%Y-%m-%d").date())
        except (ValueError, TypeError):
            pass

    return min(dates) if dates else None


def _filter_to_nearest_exp(data_json: dict, exp_date: date) -> dict:
    """Return a copy of data_json filtered to the given expiration only."""
    exp_str = exp_date.strftime("%Y-%m-%d")

    def _filter(m: dict) -> dict:
        return {k: v for k, v in m.items() if k.startswith(exp_str)}

    filtered = dict(data_json)
    filtered_calls = _filter(data_json.get("callExpDateMap", {}))
    filtered_puts = _filter(data_json.get("putExpDateMap", {}))

    # Fall back to full map if filter yields nothing
    filtered["callExpDateMap"] = filtered_calls or data_json.get("callExpDateMap", {})
    filtered["putExpDateMap"] = filtered_puts or data_json.get("putExpDateMap", {})
    return filtered


def _build_result_tuple(
    data_filtered: dict,
    previous_gamma: Optional[Dict[float, float]],
    exp_date: date,
    is_first_fetch: bool,
) -> Tuple:
    """Build the result tuple expected by process_symbol_gex()."""
    total_gex, per_strike_gex, change_in_gamma, _largest, spot_price = calculate_gamma_exposure(
        data_filtered, previous_gamma or {}
    )
    gamma_delta = sum(change_in_gamma.values()) if change_in_gamma else 0.0
    details = get_per_strike_details(data_filtered)
    return (
        data_filtered,
        total_gex,
        per_strike_gex,
        details,
        spot_price,
        exp_date,
        gamma_delta,
        is_first_fetch,
        change_in_gamma,
    )


# ---------------------------------------------------------------------------
# Trade record (for output)
# ---------------------------------------------------------------------------


@dataclass
class TradeRecord:
    trade_id: str
    trade_date: date
    direction: str
    strike: float
    exp_date: date
    entry_price: float
    exit_price: float
    qty: int
    pnl: float
    reason: str


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------


@dataclass
class BacktestState:
    previous_gamma: Dict[float, float] = field(default_factory=dict)
    is_first_fetch: bool = True
    current_date: Optional[date] = None
    # Time-based flags (set from fetched_at, not wall clock)
    past_no_new_entries: bool = False
    past_hard_close: bool = False


def _et_time(ts: datetime) -> time:
    return ts.astimezone(EASTERN).time()


def _et_date(ts: datetime) -> date:
    return ts.astimezone(EASTERN).date()


def _can_enter_backtest(
    pm: PositionManager,
    fetched_at: datetime,
) -> Tuple[bool, str]:
    """Check entry conditions using historical timestamp instead of wall clock."""
    if pm.circuit_breaker_active:
        return (False, "Circuit breaker active")

    if pm.trades_today >= pm.cfg.max_trades_per_day:
        return (False, "Max trades reached")

    tick_time = _et_time(fetched_at)
    if tick_time >= NO_NEW_ENTRIES_ET:
        return (False, "Past 14:30 ET entry cutoff")

    # Cooldown check using fetched_at
    if pm.cooldown_until is not None and fetched_at.astimezone(EASTERN) < pm.cooldown_until:
        return (False, f"Cooldown until {pm.cooldown_until.strftime('%H:%M:%S')}")

    return (True, "")


def _apply_action(
    action: PositionAction,
    pos: OpenPosition,
    current_price: float,
    fetched_at: datetime,
    pm: PositionManager,
    trade_records: List[TradeRecord],
    realized_pnl_acc: List[float],
) -> None:
    """Apply a PositionAction, updating position state and recording trades."""
    if action.action in ("stop_out", "hard_close"):
        qty = action.qty
        pnl = (current_price - pos.entry_price) * qty * 100
        realized_pnl_acc.append(pnl)
        pm.close_position(pos.trade_id, pnl, action.reason)
        trade_records.append(
            TradeRecord(
                trade_id=pos.trade_id,
                trade_date=_et_date(fetched_at),
                direction=pos.direction,
                strike=pos.strike,
                exp_date=date.fromisoformat(pos.expiration),
                entry_price=pos.entry_price,
                exit_price=current_price,
                qty=qty,
                pnl=pnl,
                reason=action.reason,
            )
        )
        pos.remaining_qty = 0

    elif action.action == "sell_tranche_a":
        qty = action.qty
        pnl = (current_price - pos.entry_price) * qty * 100
        realized_pnl_acc.append(pnl)
        pos.remaining_qty -= qty
        trade_records.append(
            TradeRecord(
                trade_id=pos.trade_id + "-A",
                trade_date=_et_date(fetched_at),
                direction=pos.direction,
                strike=pos.strike,
                exp_date=date.fromisoformat(pos.expiration),
                entry_price=pos.entry_price,
                exit_price=current_price,
                qty=qty,
                pnl=pnl,
                reason=action.reason,
            )
        )

    # 'hold' and 'update_stop' require no position-removal work here


def run_backtest(
    rows: List[Tuple[int, dict, datetime]],
    cfg: AutoGexConfig,
) -> Tuple[List[TradeRecord], List[float]]:
    """Main replay loop. Returns (trade_records, equity_curve)."""
    # Override time rules so PositionManager's wall-clock checks don't block
    cfg.no_new_entries_after = "23:59"
    cfg.hard_close_time = "23:58"

    pm = PositionManager(cfg)
    state = BacktestState()
    trade_records: List[TradeRecord] = []
    realized_pnl_acc: List[float] = []
    equity_curve: List[float] = [0.0]

    ticks_processed = 0
    ticks_skipped = 0

    for row_id, data_json, fetched_at in rows:
        tick_et = fetched_at.astimezone(EASTERN)
        tick_time = tick_et.time()
        tick_date = tick_et.date()

        # Skip outside market hours
        if tick_time < SESSION_OPEN or tick_time > SESSION_CLOSE:
            continue

        # Detect new day
        if state.current_date != tick_date:
            if state.current_date is not None:
                # Hard-close any open positions at end of previous session
                for tid in list(pm.positions.keys()):
                    pos = pm.positions[tid]
                    if pos.remaining_qty > 0:
                        # Try to get last price from current tick's data
                        exp_d = _parse_exp_date_from_chain(data_json) or tick_date
                        last_price = _get_option_price(data_json, pos.strike, pos.direction, exp_d)
                        if last_price is None:
                            last_price = pos.entry_price  # fallback: flat
                        pnl = (last_price - pos.entry_price) * pos.remaining_qty * 100
                        realized_pnl_acc.append(pnl)
                        pm.close_position(tid, pnl, "EOD close")
                        trade_records.append(
                            TradeRecord(
                                trade_id=tid + "-EOD",
                                trade_date=state.current_date,
                                direction=pos.direction,
                                strike=pos.strike,
                                exp_date=date.fromisoformat(pos.expiration),
                                entry_price=pos.entry_price,
                                exit_price=last_price,
                                qty=pos.remaining_qty,
                                pnl=pnl,
                                reason="EOD close",
                            )
                        )
            # Reset for new day
            pm.reset_daily()
            state.previous_gamma = {}
            state.is_first_fetch = True
            state.current_date = tick_date

        # Parse and validate the stored data
        try:
            if not isinstance(data_json, dict):
                raise ValueError("data_json is not a dict")
            if not data_json.get("callExpDateMap") and not data_json.get("putExpDateMap"):
                raise ValueError("No option maps in data")

            exp_date = _parse_exp_date_from_chain(data_json) or tick_date
            data_filtered = _filter_to_nearest_exp(data_json, exp_date)

            result_tuple = _build_result_tuple(
                data_filtered,
                state.previous_gamma,
                exp_date,
                state.is_first_fetch,
            )

            gex_data = process_symbol_gex(
                result_tuple,
                strike_range=cfg.strike_count * 5,  # generous window
                gex_min_threshold=0.0,
            )
            if gex_data is None:
                raise ValueError("process_symbol_gex returned None (no strikes)")

            # Attach the historical timestamp so signal_engine uses it
            gex_data.prev_fetch_timestamp = tick_et

            # Update rolling gamma state for next tick
            state.previous_gamma = result_tuple[2]  # per_strike_gex from calculate_gamma_exposure
            state.is_first_fetch = False

        except Exception as exc:
            print(f"[WARN] Skipping row {row_id} ({fetched_at}): {exc}")
            ticks_skipped += 1
            continue

        ticks_processed += 1
        spot = gex_data.spot_price

        # Evaluate open positions first
        for tid in list(pm.positions.keys()):
            pos = pm.positions.get(tid)
            if pos is None or pos.remaining_qty == 0:
                continue

            current_price = _get_option_price(data_json, pos.strike, pos.direction, pos.expiration)
            if current_price is None:
                current_price = spot * 0.005

            # Hard-close override based on historical time
            if tick_time >= HARD_CLOSE_ET and pos.remaining_qty > 0:
                pnl = (current_price - pos.entry_price) * pos.remaining_qty * 100
                realized_pnl_acc.append(pnl)
                pm.close_position(tid, pnl, "Hard close 15:55 ET")
                trade_records.append(
                    TradeRecord(
                        trade_id=tid + "-HC",
                        trade_date=tick_date,
                        direction=pos.direction,
                        strike=pos.strike,
                        exp_date=date.fromisoformat(pos.expiration),
                        entry_price=pos.entry_price,
                        exit_price=current_price,
                        qty=pos.remaining_qty,
                        pnl=pnl,
                        reason="Hard close 15:55 ET",
                    )
                )
                pos.remaining_qty = 0
                continue

            actions = pm.evaluate_position(pos, current_price, spot)
            for action in actions:
                _apply_action(action, pos, current_price, fetched_at, pm, trade_records, realized_pnl_acc)

        # Evaluate signal for new entry
        signal = evaluate(gex_data)

        can_enter, reason = _can_enter_backtest(pm, fetched_at)
        if (
            can_enter
            and signal.direction != "NONE"
            and signal.conviction >= cfg.min_conviction
            and tick_time < NO_NEW_ENTRIES_ET
        ):
            entry_price = _get_option_price(data_json, gex_data.king_strike or spot, signal.direction, exp_date)
            if entry_price is None:
                entry_price = spot * 0.005

            if entry_price > 0:
                try:
                    nearest_gk = pm.get_nearest_gatekeeper(signal, gex_data, signal.direction)
                    pos = pm.open_position(
                        signal=signal,
                        symbol=f"SPX_{exp_date}_{signal.direction[0]}_{gex_data.king_strike or spot:.0f}",
                        strike=gex_data.king_strike or spot,
                        expiration=exp_date.isoformat(),
                        entry_price=entry_price,
                        nearest_gk=nearest_gk,
                    )
                    # Override entry_time with historical timestamp
                    pos.entry_time = tick_et
                    # Override cooldown_until to use historical time
                    pm.cooldown_until = tick_et + timedelta(seconds=cfg.cooldown_after_entry)
                except RuntimeError as exc:
                    pass  # can_enter was stale; skip silently

        # Track equity curve (total realized P&L so far)
        equity_curve.append(sum(realized_pnl_acc))

    # Close any still-open positions at end of backtest
    last_data = rows[-1][1] if rows else {}
    last_ts = rows[-1][2] if rows else datetime.now(pytz.utc)
    last_date = _et_date(last_ts)
    for tid in list(pm.positions.keys()):
        pos = pm.positions.get(tid)
        if pos is None or pos.remaining_qty == 0:
            continue
        exp_d = _parse_exp_date_from_chain(last_data) or last_date
        last_price = _get_option_price(last_data, pos.strike, pos.direction, exp_d)
        if last_price is None:
            last_price = pos.entry_price
        pnl = (last_price - pos.entry_price) * pos.remaining_qty * 100
        realized_pnl_acc.append(pnl)
        pm.close_position(tid, pnl, "End of backtest")
        trade_records.append(
            TradeRecord(
                trade_id=tid + "-EBT",
                trade_date=last_date,
                direction=pos.direction,
                strike=pos.strike,
                exp_date=date.fromisoformat(pos.expiration),
                entry_price=pos.entry_price,
                exit_price=last_price,
                qty=pos.remaining_qty,
                pnl=pnl,
                reason="End of backtest",
            )
        )

    print(f"\n[Replay] Ticks processed: {ticks_processed:,}  |  Skipped: {ticks_skipped:,}")
    return trade_records, equity_curve


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _fmt_pnl(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}${v:,.2f}"


def print_trade_log(trades: List[TradeRecord]) -> None:
    header = f"{'Trade ID':<38} {'Date':<12} {'Dir':<5} {'Strike':>8} {'Entry':>7} {'Exit':>7} {'Qty':>4} {'P&L':>10} Reason"
    print("\n" + "=" * len(header))
    print("TRADE LOG")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for t in trades:
        print(
            f"{t.trade_id:<38} {t.trade_date!s:<12} {t.direction:<5} "
            f"{t.strike:>8.1f} {t.entry_price:>7.2f} {t.exit_price:>7.2f} "
            f"{t.qty:>4}  {_fmt_pnl(t.pnl):>10}  {t.reason}"
        )
    print("=" * len(header))


def print_summary(
    trades: List[TradeRecord],
    equity_curve: List[float],
    start: date,
    end: date,
    ticks_total: int,
) -> None:
    if not trades:
        print("\n=== Backtest Summary ===")
        print("No trades executed.")
        return

    pnls = [t.pnl for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)

    win_pct = len(winners) / len(pnls) * 100 if pnls else 0
    loss_pct = len(losers) / len(pnls) * 100 if pnls else 0
    avg_win = sum(winners) / len(winners) if winners else 0
    avg_loss = sum(losers) / len(losers) if losers else 0

    # Max drawdown from equity curve
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = v - peak
        if dd < max_dd:
            max_dd = dd

    print("\n=== Backtest Summary ===")
    print(f"Period: {start} to {end}")
    print(f"Total trades:    {len(trades)}")
    print(f"Winners:         {len(winners)}  ({win_pct:.1f}%)")
    print(f"Losers:          {len(losers)}  ({loss_pct:.1f}%)")
    print(f"Avg winner:      {_fmt_pnl(avg_win)}")
    print(f"Avg loser:       {_fmt_pnl(avg_loss)}")
    print(f"Total P&L:       {_fmt_pnl(total_pnl)}")
    print(f"Max drawdown:    {_fmt_pnl(max_dd)}")
    print(f"Ticks processed: {ticks_total:,}")


def print_equity_curve(equity_curve: List[float]) -> None:
    width = shutil.get_terminal_size((80, 24)).columns
    if width <= 80 or len(equity_curve) < 2:
        return

    chart_width = min(width - 12, 100)
    chart_height = 10

    min_v = min(equity_curve)
    max_v = max(equity_curve)
    span = max_v - min_v or 1.0

    # Downsample to chart_width points
    n = len(equity_curve)
    indices = [int(i * (n - 1) / (chart_width - 1)) for i in range(chart_width)]
    sampled = [equity_curve[i] for i in indices]

    # Build grid
    grid = [[" "] * chart_width for _ in range(chart_height)]
    for col, val in enumerate(sampled):
        row = chart_height - 1 - int((val - min_v) / span * (chart_height - 1))
        row = max(0, min(chart_height - 1, row))
        grid[row][col] = "*" if val >= 0 else "."

    zero_row = chart_height - 1 - int((0 - min_v) / span * (chart_height - 1))
    zero_row = max(0, min(chart_height - 1, zero_row))

    print("\n--- Equity Curve ---")
    for r, row in enumerate(grid):
        prefix = f"{min_v + (max_v - min_v) * (chart_height - 1 - r) / (chart_height - 1):>10,.0f} |"
        marker = "=" if r == zero_row else " "
        line = "".join(row)
        # Replace spaces on zero row with dashes
        if r == zero_row:
            line = line.replace(" ", "-")
        print(prefix + line)
    print(" " * 11 + "-" * chart_width)


def write_csv(trades: List[TradeRecord], path: str) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["trade_id", "date", "direction", "strike", "exp_date", "entry_price", "exit_price", "qty", "pnl", "reason"])
        for t in trades:
            writer.writerow([t.trade_id, t.trade_date, t.direction, t.strike, t.exp_date, t.entry_price, t.exit_price, t.qty, round(t.pnl, 2), t.reason])
    print(f"[CSV] Written to {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="AutoGEX Historical Backtest Engine")
    parser.add_argument("--start", required=True, type=date.fromisoformat, help="Start date YYYY-MM-DD (inclusive)")
    parser.add_argument("--end", required=True, type=date.fromisoformat, help="End date YYYY-MM-DD (inclusive)")
    parser.add_argument("--config", default=None, help="Path to autogex_config.json (optional)")
    parser.add_argument("--out", default=None, help="CSV output path for trade log")
    args = parser.parse_args()

    if args.start > args.end:
        print("Error: --start must be <= --end")
        sys.exit(1)

    # Load config
    if args.config:
        import json
        from pathlib import Path
        from config import AutoGexConfig
        try:
            with open(args.config) as f:
                data = json.load(f)
            valid = {k: v for k, v in data.items() if k in AutoGexConfig.__dataclass_fields__}
            cfg = AutoGexConfig(**valid)
        except Exception as e:
            print(f"[WARN] Could not load config from {args.config}: {e}. Using defaults.")
            cfg = load_config()
    else:
        cfg = load_config()

    print(f"[Backtest] Loading rows from {args.start} to {args.end} ...")
    try:
        rows = load_rows(args.start, args.end)
    except Exception as exc:
        print(f"[ERROR] Database connection failed: {exc}")
        sys.exit(1)

    if not rows:
        print("[Backtest] No rows found for the specified date range.")
        sys.exit(0)

    print(f"[Backtest] Loaded {len(rows):,} rows. Starting replay...")
    trade_records, equity_curve = run_backtest(rows, cfg)

    print_trade_log(trade_records)
    print_summary(trade_records, equity_curve, args.start, args.end, ticks_total=len(equity_curve) - 1)
    print_equity_curve(equity_curve)

    if args.out:
        write_csv(trade_records, args.out)


if __name__ == "__main__":
    main()
