"""AutoGEX trading view for the Streamlit dashboard.

Renders a complete read-only view of the AutoGEX engine state from SQLite and
autogex_config.json.  Does NOT import or run the trading engine.
"""

import os
import time
import sqlite3
import json
from pathlib import Path
from datetime import datetime, date, timedelta, time as dtime

import pytz
import streamlit as st

from trade_journal import (
    get_open_positions,
    get_engine_state,
    set_engine_state,
    get_trades_for_date,
    get_daily_summary,
    compute_daily_summary,
)
from config import load_config, save_config, AutoGexConfig


# ---------------------------------------------------------------------------
# Helper: all-time daily summaries (local query — not in trade_journal.py)
# ---------------------------------------------------------------------------

def _get_all_daily_summaries() -> list[dict]:
    db = os.environ.get("AUTOGEX_DB_PATH", str(Path.home() / "autogex_state.db"))
    try:
        with sqlite3.connect(db) as conn:
            cur = conn.execute("SELECT * FROM daily_summary ORDER BY date")
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

EASTERN = pytz.timezone("America/New_York")

REGIME_COLORS = {
    "positive_stable": ("🟢", "#28a745"),
    "positive_transition": ("🟡", "#ffc107"),
    "negative_trending": ("🔴", "#dc3545"),
    "negative_transition": ("🟠", "#fd7e14"),
    "unknown": ("⚪", "#6c757d"),
}

CONTROL_FILE = Path(__file__).resolve().parent / "autogex_control.txt"


def _write_control(command: str) -> None:
    CONTROL_FILE.write_text(command)


def _et_now() -> datetime:
    return datetime.now(EASTERN)


def _fmt_hold(seconds) -> str:
    if seconds is None:
        return "—"
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "—"
    m, s = divmod(s, 60)
    if m == 0:
        return f"{s}s"
    return f"{m}m {s}s"


def _fmt_age(entry_time_iso: str) -> str:
    try:
        entry = datetime.fromisoformat(entry_time_iso)
        if entry.tzinfo is None:
            entry = EASTERN.localize(entry)
        delta = _et_now() - entry
        total_s = int(delta.total_seconds())
        if total_s < 0:
            return "—"
        h, rem = divmod(total_s, 3600)
        m, s = divmod(rem, 60)
        if h > 0:
            return f"{h}h {m}m"
        if m > 0:
            return f"{m}m {s}s"
        return f"{s}s"
    except Exception:
        return "—"


def _conviction_color(conviction: int) -> str:
    if conviction >= 7:
        return "#00ff88"
    if conviction >= 5:
        return "#28a745"
    if conviction >= 3:
        return "#ffc107"
    return "#6c757d"


def _conviction_label(conviction: int) -> str:
    if conviction >= 7:
        return "High Conviction"
    if conviction >= 5:
        return "Medium Conviction"
    if conviction >= 3:
        return "Low Conviction"
    return "No Trade"


def _king_score_color(score: int) -> str:
    if score >= 3:
        return "#28a745"
    if score == 2:
        return "#fd7e14"
    if score == 1:
        return "#ffc107"
    return "#6c757d"


def _pnl_str(value: float) -> str:
    if value >= 0:
        return f"+${value:,.2f}"
    return f"-${abs(value):,.2f}"


# ---------------------------------------------------------------------------
# Row 1 — Status bar
# ---------------------------------------------------------------------------

def _render_status_bar() -> None:
    st.subheader("Engine Status")
    c1, c2, c3, c4, c5 = st.columns(5)

    # Engine status
    status = get_engine_state("status", "stopped")
    with c1:
        st.caption("Engine")
        if status == "running":
            st.markdown('<span style="color:#28a745; font-weight:bold">🟢 Running</span>', unsafe_allow_html=True)
        elif status == "circuit_breaker":
            st.markdown('<span style="color:#dc3545; font-weight:bold">🚨 Circuit Breaker</span>', unsafe_allow_html=True)
        else:
            st.markdown('<span style="color:#dc3545; font-weight:bold">🔴 Stopped</span>', unsafe_allow_html=True)

    # Daily P&L
    daily_pnl = get_engine_state("daily_pnl", 0.0)
    try:
        daily_pnl = float(daily_pnl)
    except (TypeError, ValueError):
        daily_pnl = 0.0
    with c2:
        st.caption("Daily P&L")
        color = "#28a745" if daily_pnl >= 0 else "#dc3545"
        st.markdown(
            f'<span style="color:{color}; font-weight:bold; font-size:1.1em">{_pnl_str(daily_pnl)}</span>',
            unsafe_allow_html=True,
        )

    # Trades today
    trades_today = get_engine_state("trades_today", 0)
    try:
        trades_today = int(trades_today)
    except (TypeError, ValueError):
        trades_today = 0
    cfg_tmp = load_config()
    with c3:
        st.caption("Trades Today")
        st.markdown(
            f'<span style="font-weight:bold; font-size:1.1em">{trades_today} / {cfg_tmp.max_trades_per_day}</span>',
            unsafe_allow_html=True,
        )

    # Current regime (from last_signal_json)
    last_signal_raw = get_engine_state("last_signal_json", {})
    if isinstance(last_signal_raw, str):
        try:
            last_signal_raw = json.loads(last_signal_raw)
        except Exception:
            last_signal_raw = {}
    last_signal = last_signal_raw if isinstance(last_signal_raw, dict) else {}
    regime = last_signal.get("regime", "unknown")
    regime_icon, regime_color = REGIME_COLORS.get(regime, ("⚪", "#6c757d"))
    with c4:
        st.caption("Regime")
        st.markdown(
            f'<span style="color:{regime_color}; font-weight:bold">{regime_icon} {regime.replace("_", " ").title()}</span>',
            unsafe_allow_html=True,
        )

    # Time to close
    now_et = _et_now()
    close_time = now_et.replace(hour=15, minute=55, second=0, microsecond=0)
    with c5:
        st.caption("Market")
        if now_et >= close_time:
            st.markdown('<span style="color:#6c757d; font-weight:bold">Market closed</span>', unsafe_allow_html=True)
        else:
            delta = close_time - now_et
            total_s = int(delta.total_seconds())
            h, rem = divmod(total_s, 3600)
            m = rem // 60
            st.markdown(
                f'<span style="font-weight:bold">{h}h {m}m to close</span>',
                unsafe_allow_html=True,
            )


# ---------------------------------------------------------------------------
# Row 2 — Positions (left) + Live signal (right)
# ---------------------------------------------------------------------------

def _render_positions() -> None:
    positions = get_open_positions()
    if not positions:
        st.info("No open positions.")
        return

    for pos in positions:
        direction = pos.get("direction", "")
        icon = "📈" if direction == "CALL" else "📉"
        symbol = pos.get("symbol", "—")
        strike = pos.get("strike", "—")
        entry_price = pos.get("entry_price", 0.0)
        current_stop = pos.get("current_stop", 0.0)
        tranche_a_closed = pos.get("tranche_a_closed", 0)
        conviction = pos.get("conviction", 0)
        entry_time = pos.get("entry_time", "")
        remaining_qty = pos.get("remaining_qty", "—")
        total_qty = pos.get("total_qty", "—")

        with st.container(border=True):
            st.markdown(
                f"**{icon} {direction} {symbol} @ ${strike}**  "
                f"&nbsp;&nbsp;`Qty: {remaining_qty}/{total_qty}`",
                unsafe_allow_html=True,
            )
            pc1, pc2, pc3 = st.columns(3)
            with pc1:
                st.metric("Entry $", f"${entry_price:.2f}")
            with pc2:
                st.metric("Current $", "—")
            with pc3:
                st.metric("Stop $", f"${current_stop:.2f}" if current_stop else "—")

            ta_status = "✅ Sold" if tranche_a_closed else "⏳ Pending"
            age_str = _fmt_age(entry_time)
            conv_color = _conviction_color(conviction)

            st.markdown(
                f"Tranche A: **{ta_status}** &nbsp;|&nbsp; "
                f"Age: **{age_str}** &nbsp;|&nbsp; "
                f'Conviction: <span style="color:{conv_color}; font-weight:bold">{conviction}</span>',
                unsafe_allow_html=True,
            )


def _render_live_signal(last_signal: dict) -> None:
    if not last_signal:
        st.info("Engine not running.")
        return

    spot = last_signal.get("spot_price")
    king = last_signal.get("king_strike")
    total_gex = last_signal.get("total_gex")
    signal_scores = last_signal.get("signal_scores", {})
    conviction = last_signal.get("conviction", 0)
    direction = last_signal.get("direction", "NONE")
    regime = last_signal.get("regime", "unknown")
    ts_raw = last_signal.get("timestamp")

    # Spot / King Node
    sc1, sc2 = st.columns(2)
    with sc1:
        st.metric("Spot Price", f"${spot:.2f}" if spot is not None else "—")
    with sc2:
        st.metric("King Node", f"${king:.0f}" if king is not None else "—")

    # Total GEX
    if total_gex is not None:
        gex_color = "#28a745" if total_gex >= 0 else "#dc3545"
        gex_sign = "+" if total_gex >= 0 else ""
        st.markdown(
            f'**Total GEX:** <span style="color:{gex_color}; font-weight:bold">{gex_sign}{total_gex:.3f} $B</span>',
            unsafe_allow_html=True,
        )

    # Signal scores table
    st.markdown("**Signal Scores**")
    score_rows = []
    score_labels = {
        "king_node": "King Node",
        "gatekeeper": "Gatekeeper",
        "velocity": "Velocity",
        "regime": "Regime Alignment",
        "flip_penalty": "Flip Penalty",
    }
    for key, label in score_labels.items():
        score = signal_scores.get(key, 0)
        if key == "flip_penalty":
            color = "#dc3545" if score < 0 else "#6c757d"
        else:
            color = _king_score_color(score)
        score_rows.append((label, score, color))

    for label, score, color in score_rows:
        col_a, col_b = st.columns([3, 1])
        with col_a:
            st.caption(label)
        with col_b:
            st.markdown(
                f'<span style="color:{color}; font-weight:bold">{score:+d}</span>',
                unsafe_allow_html=True,
            )

    # Net conviction
    conv_color = _conviction_color(conviction)
    conv_label = _conviction_label(conviction)
    st.markdown(
        f'<div style="text-align:center; font-size:1.4em; font-weight:bold; color:{conv_color}; '
        f'margin:8px 0">{conviction} — {conv_label}</div>',
        unsafe_allow_html=True,
    )

    # Direction
    dir_icon = "📈" if direction == "CALL" else ("📉" if direction == "PUT" else "—")
    st.markdown(f"**Direction:** {dir_icon} {direction}")

    # Regime
    regime_icon, regime_color = REGIME_COLORS.get(regime, ("⚪", "#6c757d"))
    st.markdown(
        f'**Regime:** <span style="color:{regime_color}; font-weight:bold">{regime_icon} {regime.replace("_", " ").title()}</span>',
        unsafe_allow_html=True,
    )

    # Timestamp
    if ts_raw:
        try:
            if isinstance(ts_raw, str):
                ts_dt = datetime.fromisoformat(ts_raw)
            else:
                ts_dt = ts_raw
            st.caption(f"Last signal: {ts_dt.strftime('%H:%M:%S')}")
        except Exception:
            st.caption(f"Last signal: {ts_raw}")


def _render_positions_and_signals(last_signal: dict) -> None:
    left, right = st.columns(2)
    with left:
        st.markdown("### Active Positions")
        _render_positions()
    with right:
        st.markdown("### Live Signal State")
        _render_live_signal(last_signal)


# ---------------------------------------------------------------------------
# Row 3 — Today's trade log
# ---------------------------------------------------------------------------

def _render_trade_log() -> None:
    st.markdown("### Today's Trade Log")
    trades = get_trades_for_date(date.today().isoformat())

    if not trades:
        st.info("No trades today.")
        return

    # Build display rows
    rows = []
    cumulative = 0.0
    for t in trades:
        pnl = t.get("realized_pnl")
        if pnl is None:
            pnl_val = None
        else:
            try:
                pnl_val = float(pnl)
            except (TypeError, ValueError):
                pnl_val = None

        if pnl_val is not None:
            cumulative += pnl_val

        # Format entry time
        entry_raw = t.get("entry_time", "")
        try:
            entry_dt = datetime.fromisoformat(entry_raw)
            time_str = entry_dt.strftime("%H:%M:%S")
        except Exception:
            time_str = entry_raw or "—"

        exit_price = t.get("exit_price")

        rows.append(
            {
                "Time": time_str,
                "Direction": t.get("direction", "—"),
                "Strike": t.get("strike"),
                "Entry $": t.get("entry_price"),
                "Exit $": float(exit_price) if exit_price is not None else None,
                "P&L": pnl_val,
                "Conviction": t.get("conviction"),
                "Exit Reason": t.get("exit_reason") or "—",
                "Hold": _fmt_hold(t.get("hold_seconds")),
                "Cumulative P&L": round(cumulative, 2) if pnl_val is not None else None,
            }
        )

    import pandas as pd
    df = pd.DataFrame(rows)

    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "P&L": st.column_config.NumberColumn(
                "P&L",
                format="$%.2f",
                help="Realized P&L for this trade",
            ),
            "Cumulative P&L": st.column_config.NumberColumn(
                "Cumulative P&L",
                format="$%.2f",
                help="Running total P&L",
            ),
            "Entry $": st.column_config.NumberColumn("Entry $", format="$%.2f"),
            "Exit $": st.column_config.NumberColumn("Exit $", format="$%.2f"),
            "Strike": st.column_config.NumberColumn("Strike", format="%.0f"),
        },
    )


# ---------------------------------------------------------------------------
# Row 4 — Performance metrics
# ---------------------------------------------------------------------------

def _metrics_from_trades(trades: list[dict]) -> dict:
    """Aggregate raw metrics from a list of trade dicts."""
    closed = [t for t in trades if t.get("exit_time") is not None]
    pnls = []
    for t in closed:
        try:
            pnls.append(float(t["realized_pnl"]))
        except (TypeError, ValueError, KeyError):
            pass

    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]
    return {
        "total_pnl": sum(pnls),
        "trade_count": len(trades),
        "win_count": len(winners),
        "avg_winner": sum(winners) / len(winners) if winners else 0.0,
        "avg_loser": sum(losers) / len(losers) if losers else 0.0,
    }


def _render_performance_metrics() -> None:
    st.markdown("### Performance Metrics")
    col_today, col_week, col_alltime = st.columns(3)

    # ---- Today ----
    today_str = date.today().isoformat()
    today_summary = get_daily_summary(today_str)
    if today_summary is None:
        today_trades = get_trades_for_date(today_str)
        today_m = _metrics_from_trades(today_trades)
    else:
        today_m = {
            "total_pnl": today_summary.get("total_pnl", 0.0),
            "trade_count": today_summary.get("trade_count", 0),
            "win_count": today_summary.get("win_count", 0),
            "avg_winner": today_summary.get("avg_winner", 0.0),
            "avg_loser": today_summary.get("avg_loser", 0.0),
        }

    tc = today_m["trade_count"]
    wc = today_m["win_count"]
    today_wr = (wc / tc * 100) if tc > 0 else 0.0

    with col_today:
        st.markdown("**Today**")
        st.metric("Total P&L", _pnl_str(today_m["total_pnl"]))
        st.metric("Win Rate", f"{today_wr:.0f}%" if tc > 0 else "—")
        st.metric("Avg Winner", f"${today_m['avg_winner']:.2f}" if today_m["avg_winner"] else "—")
        st.metric("Avg Loser", f"${today_m['avg_loser']:.2f}" if today_m["avg_loser"] else "—")

    # ---- This Week (Mon → today) ----
    today_date = date.today()
    monday = today_date - timedelta(days=today_date.weekday())
    week_trades: list[dict] = []
    cursor = monday
    while cursor <= today_date:
        week_trades.extend(get_trades_for_date(cursor.isoformat()))
        cursor += timedelta(days=1)

    week_m = _metrics_from_trades(week_trades)
    wtc = week_m["trade_count"]
    wwc = week_m["win_count"]
    week_wr = (wwc / wtc * 100) if wtc > 0 else 0.0

    with col_week:
        st.markdown("**This Week**")
        st.metric("Total P&L", _pnl_str(week_m["total_pnl"]))
        st.metric("Win Rate", f"{week_wr:.0f}%" if wtc > 0 else "—")
        st.metric("Avg Winner", f"${week_m['avg_winner']:.2f}" if week_m["avg_winner"] else "—")
        st.metric("Avg Loser", f"${week_m['avg_loser']:.2f}" if week_m["avg_loser"] else "—")

    # ---- All-Time ----
    all_summaries = _get_all_daily_summaries()

    at_total_pnl = sum(s.get("total_pnl", 0.0) for s in all_summaries)
    at_trade_count = sum(s.get("trade_count", 0) for s in all_summaries)
    at_win_count = sum(s.get("win_count", 0) for s in all_summaries)
    at_wr = (at_win_count / at_trade_count * 100) if at_trade_count > 0 else 0.0
    at_max_dd = min((s.get("max_drawdown", 0.0) for s in all_summaries), default=0.0)

    # Sharpe: use daily total_pnl as daily returns
    daily_pnls = [s.get("total_pnl", 0.0) for s in all_summaries]
    if len(daily_pnls) >= 2:
        import statistics
        mean_r = statistics.mean(daily_pnls)
        std_r = statistics.stdev(daily_pnls)
        sharpe = (mean_r / std_r * (252 ** 0.5)) if std_r > 0 else 0.0
        sharpe_str = f"{sharpe:.2f}"
    else:
        sharpe_str = "—"

    with col_alltime:
        st.markdown("**All-Time**")
        st.metric("Total P&L", _pnl_str(at_total_pnl))
        st.metric("Win Rate", f"{at_wr:.0f}%" if at_trade_count > 0 else "—")
        st.metric("Max Day Drawdown", f"${at_max_dd:.2f}" if at_max_dd else "—")
        st.metric("Total Trades", str(at_trade_count))
        st.metric("Sharpe (ann.)", sharpe_str)


# ---------------------------------------------------------------------------
# Row 5 — Controls
# ---------------------------------------------------------------------------

def _render_controls() -> None:
    st.markdown("### Controls")

    # Sub-row 1: Engine start/stop + close all
    ctrl_left, ctrl_right = st.columns(2)
    status = get_engine_state("status", "stopped")

    with ctrl_left:
        if status != "running":
            if st.button("▶ Start Engine", type="primary", key="btn_start"):
                _write_control("start")
                st.success("Start command sent.")
        else:
            if st.button("⏹ Stop Engine", type="secondary", key="btn_stop"):
                _write_control("stop")
                st.success("Stop command sent.")
        st.caption("Start/stop takes effect on the engine's next poll cycle (~7s)")

    with ctrl_right:
        if st.button("🚨 Close All Positions", key="btn_close_all"):
            st.warning(
                "Are you sure you want to close all positions? "
                "Click the button again to confirm."
            )
            if st.button("Confirm: Close All", key="btn_close_all_confirm", type="primary"):
                _write_control("close_all")
                st.error("Close all positions command sent.")

    # Sub-row 2: Config sliders
    with st.expander("⚙️ Configuration", expanded=False):
        cfg = load_config()
        changed = False

        new_min_conviction = st.slider(
            "Min Conviction Threshold",
            min_value=1, max_value=7, step=1,
            value=int(cfg.min_conviction),
            key="cfg_min_conviction",
        )
        new_max_trades = st.slider(
            "Max Trades Per Day",
            min_value=1, max_value=10, step=1,
            value=int(cfg.max_trades_per_day),
            key="cfg_max_trades",
        )
        new_max_risk = st.slider(
            "Max Risk Per Trade ($)",
            min_value=500, max_value=5000, step=250,
            value=int(cfg.max_risk_per_trade),
            key="cfg_max_risk",
        )
        new_stop_pct = st.slider(
            "Initial Stop %",
            min_value=0.20, max_value=0.80, step=0.05,
            value=float(cfg.initial_stop_pct),
            format="%.2f",
            key="cfg_stop_pct",
        )
        new_tranche_a = st.slider(
            "Tranche A Target %",
            min_value=0.10, max_value=0.60, step=0.05,
            value=float(cfg.tranche_a_target_pct),
            format="%.2f",
            key="cfg_tranche_a",
        )

        if (
            new_min_conviction != cfg.min_conviction
            or new_max_trades != cfg.max_trades_per_day
            or new_max_risk != cfg.max_risk_per_trade
            or abs(new_stop_pct - cfg.initial_stop_pct) > 1e-9
            or abs(new_tranche_a - cfg.tranche_a_target_pct) > 1e-9
        ):
            cfg.min_conviction = new_min_conviction
            cfg.max_trades_per_day = new_max_trades
            cfg.max_risk_per_trade = float(new_max_risk)
            cfg.initial_stop_pct = new_stop_pct
            cfg.tranche_a_target_pct = new_tranche_a
            save_config(cfg)
            st.success("Config saved.")


# ---------------------------------------------------------------------------
# Main render function
# ---------------------------------------------------------------------------

def render_autogex_view() -> None:
    """Render the complete AutoGEX trading view."""
    st.title("AutoGEX Trading View")

    # --- Row 1: Status bar ---
    _render_status_bar()
    st.markdown("---")

    # Pre-fetch last_signal_json once (used in row 1 regime and row 2 signal panel)
    last_signal_raw = get_engine_state("last_signal_json", {})
    if isinstance(last_signal_raw, str):
        try:
            last_signal_raw = json.loads(last_signal_raw)
        except Exception:
            last_signal_raw = {}
    last_signal: dict = last_signal_raw if isinstance(last_signal_raw, dict) else {}

    # --- Row 2: Positions + Signal ---
    _render_positions_and_signals(last_signal)
    st.markdown("---")

    # --- Row 3: Trade log ---
    _render_trade_log()
    st.markdown("---")

    # --- Row 4: Performance metrics ---
    _render_performance_metrics()
    st.markdown("---")

    # --- Row 5: Controls ---
    _render_controls()

    # --- Auto-refresh ---
    st.markdown("---")
    st.caption("Auto-refreshing every 5 seconds.")
    time.sleep(5)
    st.rerun()
