#!/usr/bin/env python3
"""AutoGEX trading engine — runs as a separate process from the Streamlit dashboard.

Dry-run mode (cfg.dry_run=True): evaluates signals and logs decisions without placing orders.
Live mode (cfg.dry_run=False): places real paper trades via Alpaca (Phase 3).

Usage:
    python trading_engine.py [--dry-run] [--config path/to/autogex_config.json]
"""

import argparse
import json as _json
import logging
import logging.handlers
import os
import signal as _signal
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pytz

from config import load_config
from execution import AlpacaClient
from gex_utils import SymbolGexData, fetch_options_and_gex, process_symbol_gex
from main import GammaExposureScheduler
from position_manager import OpenPosition, PositionManager
from signal_engine import SignalResult, evaluate
from trade_journal import (
    compute_daily_summary,
    delete_position,
    init_db,
    insert_trade,
    set_engine_state,
    store_spy_snapshot,
    update_trade_exit,
    upsert_position,
)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

_LOG_PATH = Path(__file__).resolve().parent / "autogex.log"

def _setup_logging() -> logging.Logger:
    """Configure rotating file + console logger."""
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logger = logging.getLogger("autogex")
    logger.setLevel(level)

    if logger.handlers:
        return logger  # Already configured (e.g., in tests)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # Rotating file: 5 MB max, keep 3 backups
    fh = logging.handlers.RotatingFileHandler(
        _LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console (mirrors file output so terminal still shows logs)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


logger = _setup_logging()

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_shutdown_requested: bool = False
_previous_gex: dict = {}
_current_date: Optional[date] = None

_ET = pytz.timezone("US/Eastern")


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

def _with_retry(fn, max_attempts: int = 3, base_delay: float = 1.0, label: str = ""):
    """Call fn() with exponential backoff. Returns (result, error_str) tuple.

    On success: (result, None). On all failures: (None, last_error_message).
    """
    delay = base_delay
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(), None
        except Exception as exc:
            last_err = str(exc)
            if attempt < max_attempts:
                logger.warning(
                    "%sAttempt %d/%d failed (%s). Retrying in %.1fs...",
                    f"[{label}] " if label else "",
                    attempt,
                    max_attempts,
                    last_err,
                    delay,
                )
                time.sleep(delay)
                delay *= 2
            else:
                logger.error(
                    "%sAll %d attempts failed. Last error: %s",
                    f"[{label}] " if label else "",
                    max_attempts,
                    last_err,
                )
    return None, last_err


# ---------------------------------------------------------------------------
# Signal handler
# ---------------------------------------------------------------------------

def _handle_sigint(signum, frame):
    global _shutdown_requested
    logger.info("[Engine] Shutdown requested (SIGINT). Will close positions and exit...")
    _shutdown_requested = True


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _gex_snapshot(data: SymbolGexData) -> dict:
    """Return a compact dict of the GEX state suitable for JSON storage."""
    return {
        "spot_price": data.spot_price,
        "total_gex": round(data.total_gex, 3),
        "king_strike": data.king_strike,
        "king_gex": round(data.king_gex, 3),
        "gamma_flip_strike": data.gamma_flip_strike,
        "dist_to_flip_pct": data.dist_to_flip_pct,
        "nearest_gk_below": data.nearest_gk_below,
        "nearest_gk_above": data.nearest_gk_above,
        "gamma_delta": data.gamma_delta,
    }


def _position_to_dict(pos: OpenPosition) -> dict:
    """Convert OpenPosition to dict for upsert_position."""
    return {
        "trade_id": pos.trade_id,
        "symbol": pos.symbol,
        "direction": pos.direction,
        "strike": pos.strike,
        "entry_price": pos.entry_price,
        "entry_time": pos.entry_time.isoformat(),
        "total_qty": pos.total_qty,
        "remaining_qty": pos.remaining_qty,
        "tranche_a_closed": int(pos.tranche_a_closed),
        "high_water_mark": pos.high_water_mark,
        "current_stop": pos.current_stop,
        "conviction": pos.entry_conviction,
        "signals_json": _json.dumps(pos.entry_signals),
        "gex_entry_json": None,  # set at entry time by caller if desired
    }


# ---------------------------------------------------------------------------
# Trade execution helpers
# ---------------------------------------------------------------------------

def _execute_entry(
    signal: SignalResult,
    data: SymbolGexData,
    pm: PositionManager,
    cfg,
    scheduler: GammaExposureScheduler,
    dry_run: bool = True,
    alpaca: AlpacaClient = None,
) -> None:
    """Open a new position: select strike, build symbol, log to journal."""

    # --- Strike selection ---
    atm = round(data.spot_price)
    king_score = signal.signal_scores.get("king_node", 0)

    if king_score >= 2 and data.king_strike is not None:
        if signal.direction == "CALL":
            if data.king_strike >= atm and data.king_strike <= atm + 2:
                strike = data.king_strike
            else:
                strike = float(atm)
        else:  # PUT
            if data.king_strike <= atm and data.king_strike >= atm - 2:
                strike = data.king_strike
            else:
                strike = float(atm)
    else:
        strike = float(atm)

    # --- Expiration ---
    expiration = data.exp_date.strftime("%Y-%m-%d")

    # --- Option price (dry-run placeholder) ---
    option_price = 1.50

    # --- Nearest gatekeeper ---
    if signal.direction == "CALL":
        nearest_gk = data.nearest_gk_above
    else:
        nearest_gk = data.nearest_gk_below

    # --- Build OCC-style symbol ---
    exp_compact = data.exp_date.strftime("%y%m%d")
    cp = "C" if signal.direction == "CALL" else "P"
    strike_int = int(strike * 1000)
    symbol = f"SPY{exp_compact}{cp}{strike_int:08d}"

    # --- Open position in PositionManager ---
    pos = pm.open_position(signal, symbol, strike, expiration, option_price, nearest_gk)

    if dry_run:
        logger.info(
            "[DRY-RUN] ENTER %s %dx %s @ $%.2f | conviction=%d | stop=$%.2f",
            signal.direction, pos.total_qty, symbol, option_price,
            pos.entry_conviction, pos.current_stop,
        )
    else:
        # Get real mid price from Alpaca
        mid = alpaca.get_option_mid_price(symbol)
        if mid is None:
            logger.warning("[Engine] Could not get quote for %s, skipping entry", symbol)
            pm.positions.pop(pos.trade_id, None)
            pm.trades_today -= 1
            return
        option_price = mid
        limit_price = round(mid - 0.01, 2)  # penny inside mid
        order = alpaca.place_limit_buy(symbol, pos.total_qty, limit_price)
        if order is None:
            logger.warning("[Engine] Order failed for %s, skipping entry", symbol)
            pm.positions.pop(pos.trade_id, None)
            pm.trades_today -= 1
            return
        filled_price = order.get('filled_avg_price') or option_price
        pos.entry_price = filled_price
        pos.current_stop = round(filled_price * (1 - cfg.initial_stop_pct), 2)
        pos.high_water_mark = filled_price
        logger.info(
            "[LIVE] ENTER %s %dx %s @ $%.2f | conviction=%d | stop=$%.2f",
            signal.direction, pos.total_qty, symbol, filled_price,
            pos.entry_conviction, pos.current_stop,
        )

    insert_trade({
        "trade_id": pos.trade_id,
        "date": date.today().isoformat(),
        "direction": signal.direction,
        "symbol": pos.symbol,
        "strike": pos.strike,
        "expiration": pos.expiration,
        "entry_time": pos.entry_time.isoformat(),
        "entry_price": pos.entry_price,
        "entry_qty": pos.total_qty,
        "exit_time": None,
        "exit_price": None,
        "exit_qty": None,
        "exit_reason": None,
        "realized_pnl": None,
        "conviction": pos.entry_conviction,
        "signals_json": signal.signal_scores,
        "gex_snapshot_json": _gex_snapshot(data),
        "gex_snapshot_exit_json": None,
        "regime_at_entry": signal.regime,
        "regime_at_exit": None,
        "tranche": "full",
        "hold_seconds": None,
    })
    upsert_position(_position_to_dict(pos))


def _execute_close(
    pos: OpenPosition,
    action,
    exit_price: float,
    data: SymbolGexData,
    pm: PositionManager,
    dry_run: bool = True,
    alpaca: AlpacaClient = None,
) -> None:
    """Close a position fully, update journal, remove from PositionManager."""
    if dry_run:
        realized_pnl = (exit_price - pos.entry_price) * pos.remaining_qty * 100
        logger.info(
            "[DRY-RUN] CLOSE %s %dx %s @ $%.2f | P&L=$%.2f | reason=%s",
            pos.direction, pos.remaining_qty, pos.symbol, exit_price,
            realized_pnl, action.reason,
        )
    else:
        order = alpaca.place_market_sell(pos.symbol, pos.remaining_qty)
        filled_price = exit_price  # fallback
        if order and order.get('filled_avg_price'):
            filled_price = order['filled_avg_price']
        exit_price = filled_price
        realized_pnl = (exit_price - pos.entry_price) * pos.remaining_qty * 100
        logger.info(
            "[LIVE] CLOSE %s %dx %s @ $%.2f | P&L=$%.2f | reason=%s",
            pos.direction, pos.remaining_qty, pos.symbol, exit_price,
            realized_pnl, action.reason,
        )

    now_et = datetime.now(_ET)
    hold_seconds = int((now_et - pos.entry_time).total_seconds())
    from signal_engine import classify_regime
    current_regime_str = classify_regime(data)

    update_trade_exit(
        pos.trade_id,
        now_et.isoformat(),
        exit_price,
        pos.remaining_qty,
        action.reason,
        realized_pnl,
        current_regime_str,
        _gex_snapshot(data),
        hold_seconds,
    )
    compute_daily_summary(date.today().isoformat())
    pm.close_position(pos.trade_id, realized_pnl, action.reason)
    delete_position(pos.trade_id)


def _execute_partial_close(
    pos: OpenPosition,
    action,
    exit_price: float,
    data: SymbolGexData,
    pm: PositionManager,
    dry_run: bool = True,
    alpaca: AlpacaClient = None,
) -> None:
    """Sell Tranche A partial, update in-memory state and journal."""
    import uuid as _uuid

    if dry_run:
        partial_pnl = (exit_price - pos.entry_price) * action.qty * 100
        logger.info(
            "[DRY-RUN] PARTIAL SELL Tranche A: %dx %s @ $%.2f | partial P&L=$%.2f",
            action.qty, pos.symbol, exit_price, partial_pnl,
        )
    else:
        order = alpaca.place_market_sell(pos.symbol, action.qty)
        if order and order.get('filled_avg_price'):
            exit_price = order['filled_avg_price']
        partial_pnl = (exit_price - pos.entry_price) * action.qty * 100
        logger.info(
            "[LIVE] PARTIAL SELL Tranche A: %dx %s @ $%.2f | partial P&L=$%.2f",
            action.qty, pos.symbol, exit_price, partial_pnl,
        )

    pm.daily_realized_pnl += partial_pnl
    pos.remaining_qty -= action.qty

    insert_trade({
        "trade_id": str(_uuid.uuid4()),
        "date": date.today().isoformat(),
        "direction": pos.direction,
        "symbol": pos.symbol,
        "strike": pos.strike,
        "expiration": pos.expiration,
        "entry_time": pos.entry_time.isoformat(),
        "entry_price": pos.entry_price,
        "entry_qty": action.qty,
        "exit_time": datetime.now(_ET).isoformat(),
        "exit_price": exit_price,
        "exit_qty": action.qty,
        "exit_reason": action.reason,
        "realized_pnl": partial_pnl,
        "conviction": pos.entry_conviction,
        "signals_json": pos.entry_signals,
        "gex_snapshot_json": _gex_snapshot(data),
        "gex_snapshot_exit_json": _gex_snapshot(data),
        "regime_at_entry": None,
        "regime_at_exit": None,
        "tranche": "A",
        "hold_seconds": int((datetime.now(_ET) - pos.entry_time).total_seconds()),
    })

    upsert_position(_position_to_dict(pos))


# ---------------------------------------------------------------------------
# Core tick
# ---------------------------------------------------------------------------

def _tick(scheduler: GammaExposureScheduler, pm: PositionManager, cfg, alpaca: AlpacaClient) -> None:
    """Single engine tick: fetch GEX, evaluate signal, manage positions, maybe enter."""
    global _previous_gex, _current_date

    # --- Market hours check ---
    now_et = datetime.now(_ET)
    today = now_et.date()

    if now_et.weekday() >= 5:
        logger.debug("[Engine] Outside market hours (weekend)")
        return

    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=15, second=0, microsecond=0)
    if not (market_open <= now_et <= market_close):
        logger.debug("[Engine] Outside market hours")
        return

    # Reset daily state at the start of each new trading day
    if _current_date is None or today != _current_date:
        _current_date = today
        _previous_gex = {}
        pm.reset_daily()
        logger.info("[Engine] New trading day: %s", today.isoformat())

    # --- Fetch GEX data (with retry on transient errors) ---
    def _do_fetch():
        return fetch_options_and_gex(
            scheduler.client,
            "$SPY",
            cfg.strike_count,
            _previous_gex,
            scheduler.client_module,
        )

    fetch_result, fetch_err = _with_retry(_do_fetch, max_attempts=3, base_delay=2.0, label="GEX fetch")
    if fetch_err or fetch_result is None:
        logger.error("[Engine] GEX fetch failed after retries: %s", fetch_err)
        return

    result, api_err = fetch_result
    if api_err or result is None:
        # Check for 401 and attempt re-auth
        if api_err and "401" in str(api_err):
            logger.warning("[Engine] 401 from API — attempting token refresh and retry")
            try:
                GammaExposureScheduler._proactive_schwab_token_refresh(scheduler.client)
                result, api_err = fetch_options_and_gex(
                    scheduler.client, "$SPY", cfg.strike_count, _previous_gex, scheduler.client_module
                )
            except Exception as exc:
                logger.error("[Engine] Token refresh failed: %s", exc)
                return
            if api_err or result is None:
                logger.error("[Engine] GEX fetch still failing after token refresh: %s", api_err)
                return
        else:
            logger.error("[Engine] GEX fetch error: %s", api_err)
            return

    _previous_gex = dict(result[2])  # update previous for delta tracking

    # Store raw snapshot for backtesting (best-effort; never block the tick)
    try:
        store_spy_snapshot(result[0], now_et)
    except Exception as exc:
        logger.debug("[Engine] Snapshot store failed: %s", exc)

    data = process_symbol_gex(result, strike_range=800, gex_min_threshold=0.0)
    if data is None:
        logger.warning("[Engine] process_symbol_gex returned None")
        return

    # --- Evaluate signal ---
    signal = evaluate(data)

    logger.debug(
        "[Engine] Signal: %s | conviction=%d | regime=%s | spot=%.2f | king=%.0f",
        signal.direction, signal.conviction, signal.regime,
        data.spot_price, data.king_strike or 0,
    )

    # --- Update engine state in SQLite ---
    set_engine_state("last_signal_json", {
        "direction": signal.direction,
        "conviction": signal.conviction,
        "regime": signal.regime,
        "signal_scores": signal.signal_scores,
        "vetoed": signal.vetoed,
        "spot_price": data.spot_price,
        "king_strike": data.king_strike,
        "total_gex": round(data.total_gex, 3),
        "timestamp": signal.timestamp.isoformat(),
    })
    set_engine_state("daily_pnl", round(pm.daily_realized_pnl, 2))
    set_engine_state("trades_today", pm.trades_today)

    # --- Evaluate open positions ---
    for trade_id in list(pm.positions.keys()):
        pos = pm.positions.get(trade_id)
        if pos is None:
            continue

        if cfg.dry_run:
            if signal.direction == pos.direction:
                current_price = pos.entry_price * 1.02
            else:
                current_price = pos.entry_price * 0.98
        else:
            quote = alpaca.get_latest_option_quote(pos.symbol)
            if quote is None:
                continue
            current_price = quote['mid']

        actions = pm.evaluate_position(pos, current_price, data.spot_price)

        for action in actions:
            if cfg.dry_run:
                logger.info(
                    "[DRY-RUN] %s %d contracts of %s: %s",
                    action.action, action.qty, pos.symbol, action.reason,
                )

            if action.action in ("stop_out", "hard_close"):
                _execute_close(pos, action, current_price, data, pm, dry_run=cfg.dry_run, alpaca=alpaca)
                if pm.circuit_breaker_active and not cfg.dry_run:
                    logger.warning("[Engine] Circuit breaker active — closing all remaining positions")
                    alpaca.close_all_option_positions()
                    pm.positions.clear()
                    break
                break

            elif action.action == "sell_tranche_a":
                _execute_partial_close(pos, action, current_price, data, pm, dry_run=cfg.dry_run, alpaca=alpaca)
                if pm.circuit_breaker_active and not cfg.dry_run:
                    logger.warning("[Engine] Circuit breaker active — closing all remaining positions")
                    alpaca.close_all_option_positions()
                    pm.positions.clear()
                    break

            elif action.action == "update_stop":
                pos.current_stop = action.new_stop
                upsert_position(_position_to_dict(pos))

    # --- Entry logic ---
    if signal.direction != "NONE" and signal.conviction >= cfg.min_conviction:
        can_enter, reason = pm.can_enter()
        if not can_enter:
            logger.debug("[Engine] Cannot enter: %s", reason)
        else:
            try:
                _execute_entry(signal, data, pm, cfg, scheduler, dry_run=cfg.dry_run, alpaca=alpaca)
            except RuntimeError as e:
                logger.warning("[Engine] Entry blocked: %s", e)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global _shutdown_requested

    # --- CLI args ---
    parser = argparse.ArgumentParser(description="AutoGEX Trading Engine")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run mode")
    parser.add_argument("--config", default=None, help="Path to autogex_config.json")
    args = parser.parse_args()

    # --- Load .env ---
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")

    # --- Load config ---
    cfg = load_config()
    if args.dry_run:
        cfg.dry_run = True

    mode_label = "DRY-RUN" if cfg.dry_run else "LIVE"
    logger.info("[Engine] Starting AutoGEX Trading Engine [%s]", mode_label)
    logger.info("[Engine] Log file: %s", _LOG_PATH)
    logger.info("[Engine] Poll interval: %ds | Min conviction: %d", cfg.poll_interval_seconds, cfg.min_conviction)

    # --- Init DB ---
    init_db()
    set_engine_state("status", "starting")

    # --- Authenticate to Schwab ---
    logger.info("[Engine] Authenticating to Schwab...")
    scheduler = GammaExposureScheduler()
    scheduler.authenticate()
    logger.info("[Engine] Schwab authentication successful.")

    # --- Alpaca connectivity check ---
    alpaca = AlpacaClient()
    if not cfg.dry_run:
        acct = alpaca.get_account()
        if acct:
            logger.info("[Engine] Alpaca connected. Buying power: $%s", acct.get('buying_power', 'N/A'))
        else:
            logger.warning("[Engine] WARNING: Alpaca connectivity check failed.")

    # --- Position manager ---
    pm = PositionManager(cfg)

    # On restart, reconcile Alpaca positions with engine state
    from trade_journal import get_open_positions as _get_db_positions
    db_positions = _get_db_positions()
    if db_positions:
        logger.info("[Engine] Found %d open position(s) in journal from previous session", len(db_positions))
        alpaca.reconcile_positions(pm.positions)

    # --- SIGINT handler ---
    _signal.signal(_signal.SIGINT, _handle_sigint)

    # --- Enter main loop ---
    set_engine_state("status", "running")
    logger.info("[Engine] Running. Press Ctrl+C to stop.")

    while not _shutdown_requested:
        try:
            _tick(scheduler, pm, cfg, alpaca)
        except Exception as e:
            logger.error("[Engine] Tick error: %s", e, exc_info=True)
        time.sleep(cfg.poll_interval_seconds)

    # --- Graceful shutdown ---
    logger.info("[Engine] Shutting down...")

    if pm.positions:
        open_count = len(pm.positions)
        if cfg.dry_run:
            logger.warning(
                "[Engine] %d open position(s) at shutdown (dry-run — no orders placed).", open_count
            )
        else:
            logger.warning(
                "[Engine] %d open position(s) at shutdown. Closing at market...", open_count
            )
            try:
                alpaca.close_all_option_positions()
                logger.info("[Engine] Close-all orders submitted.")
            except Exception as exc:
                logger.error("[Engine] Failed to close positions during shutdown: %s", exc)

    set_engine_state("status", "stopped")
    compute_daily_summary(date.today().isoformat())

    total_pnl = round(pm.daily_realized_pnl, 2)
    trades = pm.trades_today
    logger.info(
        "[Engine] Final summary — Trades today: %d | Realized P&L: $%.2f",
        trades, total_pnl,
    )
    logger.info("[Engine] Stopped.")


if __name__ == "__main__":
    main()
