"""EOD (end-of-day) report generator for AutoGEX trading sessions.

Generates a per-session summary with combined trade view (tranche A + B grouped
into one logical trade), R-multiples, capital risk context, weekly/monthly P&L,
and optional email notification.

Primary trigger: launchd job at 4:00 PM ET (eod_report.py __main__), which
fires regardless of engine state. Secondary trigger: trading_engine.py shutdown
fallback after hard_close_time, in case the engine is still running at EOD.
Both paths are idempotent — only one report is generated per day.

Data flow:
    trade_journal.get_trades_for_date(date)
            │
            ▼
    group_trades_by_trade_id(rows)  →  List[CombinedTrade]
            │
            ▼
    SessionReport  (aggregated metrics + trade list)
            │
        ┌───┼───────┐
        ▼   ▼       ▼
       DB  Log   Email
"""

import json
import logging
import os
import smtplib
from dataclasses import dataclass, field
from datetime import datetime
from email.mime.text import MIMEText
from typing import List, Optional

from trade_journal import (
    eod_report_exists,
    get_monthly_pnl,
    get_trade_count_for_date,
    get_trades_for_date,
    get_weekly_pnl,
    upsert_eod_report,
)

logger = logging.getLogger("autogex")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class CombinedTrade:
    """One logical trade as seen by the user: one entry, up to two exits (tranches)."""
    base_trade_id: str          # trade_id of the tranche="full" anchor row
    direction: str              # CALL or PUT
    strike: float
    entry_time: str             # ISO string
    entry_price: float
    total_qty: int              # contracts at entry
    conviction: int
    tranches: List[dict] = field(default_factory=list)  # raw DB rows for this trade
    total_pnl: float = 0.0
    capital_risked: float = 0.0
    initial_stop_price: float = 0.0
    exit_reason: str = "unknown"    # "ran", "stop_out", "hard_close", "closed"
    hold_seconds: int = 0
    avg_exit_price: Optional[float] = None


@dataclass
class SessionReport:
    """Aggregated summary of one trading session."""
    date: str
    trades: List[CombinedTrade] = field(default_factory=list)
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    total_pnl: float = 0.0
    total_capital_risked: float = 0.0
    avg_r: Optional[float] = None
    best_r: Optional[float] = None
    worst_r: Optional[float] = None
    weekly_pnl: float = 0.0
    monthly_pnl: float = 0.0


# ---------------------------------------------------------------------------
# Pure functions (unit-testable, no DB / side effects)
# ---------------------------------------------------------------------------

def safe_r_multiple(
    realized_pnl: Optional[float],
    capital_risked: Optional[float],
) -> Optional[float]:
    """Return R-multiple or None if it cannot be computed.

    R = realized_pnl / capital_risked
    Returns None when capital_risked is 0, None, or negative.
    """
    if realized_pnl is None or capital_risked is None:
        return None
    if capital_risked <= 0:
        return None
    return round(realized_pnl / capital_risked, 2)


def fmt_r(r: Optional[float]) -> str:
    """Format an R-multiple for display: '+1.2R', '-0.8R', or 'N/A'."""
    if r is None:
        return "N/A"
    sign = "+" if r >= 0 else ""
    return f"{sign}{r:.1f}R"


def _classify_exit_reason(tranches: List[dict]) -> str:
    """Derive a human-readable outcome from raw tranche rows.

    Priority: stop_out > hard_close > ran > closed
    """
    reasons = [str(t.get("exit_reason") or "") for t in tranches if t.get("exit_time")]
    for r in reasons:
        if "Stop hit" in r or "stop_out" in r.lower():
            return "stop_out"
    for r in reasons:
        if "End of day" in r or "hard_close" in r:
            return "hard_close"
    for r in reasons:
        if "target hit" in r.lower() or "tranche a" in r.lower():
            return "ran"
    if reasons:
        return "closed"
    return "open"


def group_trades_by_trade_id(rows: List[dict]) -> List[CombinedTrade]:
    """Group raw trades-table rows into logical CombinedTrade objects.

    A single logical trade may span two rows:
      - tranche="full"  → the entry row (updated with Tranche B exit)
      - tranche="A"     → the partial Tranche A exit row (has parent_trade_id)

    Grouping key: parent_trade_id column (set on tranche A rows).
    Orphaned tranche A rows (parent not found) are treated as standalone.
    """
    anchors: dict[str, dict] = {}   # trade_id → row  (tranche="full" rows)
    children: dict[str, list] = {}  # parent_trade_id → [tranche A rows]
    orphans: list[dict] = []

    for r in rows:
        if r.get("tranche") == "full":
            anchors[r["trade_id"]] = r
        elif r.get("tranche") == "A":
            parent_id = r.get("parent_trade_id")
            if parent_id:
                children.setdefault(parent_id, []).append(r)
            else:
                # Legacy row without parent_trade_id (pre-migration data) — standalone
                logger.warning(
                    "[EOD] Tranche A row %s has no parent_trade_id — treating as standalone",
                    r.get("trade_id", "?")[:8],
                )
                orphans.append(r)
        # Non-full, non-A rows are ignored (e.g. future tranche types)

    combined: List[CombinedTrade] = []

    # Any children whose parent_trade_id has no matching anchor → orphan
    for parent_id, child_list in children.items():
        if parent_id not in anchors:
            for r in child_list:
                logger.warning(
                    "[EOD] Tranche A row %s references missing parent %s — treating as standalone",
                    r.get("trade_id", "?")[:8],
                    parent_id[:8] if parent_id else "?",
                )
                orphans.append(r)

    for trade_id, anchor in anchors.items():
        child_rows = children.get(trade_id, [])
        tranche_rows = [anchor] + child_rows

        # P&L: sum closed exits only
        total_pnl = sum(
            float(r["realized_pnl"])
            for r in tranche_rows
            if r.get("realized_pnl") is not None and r.get("exit_time") is not None
        )

        # Capital risked from anchor (computed at entry using actual fill price)
        capital_risked = float(anchor.get("capital_risked") or 0.0)
        initial_stop_price = float(anchor.get("initial_stop_price") or 0.0)

        # Weighted average exit price across all closed tranches
        exit_pairs = [
            (float(r["exit_price"]), int(r["exit_qty"]))
            for r in tranche_rows
            if r.get("exit_price") is not None and r.get("exit_qty")
        ]
        if exit_pairs:
            total_exited = sum(q for _, q in exit_pairs)
            avg_exit: Optional[float] = (
                round(sum(p * q for p, q in exit_pairs) / total_exited, 2)
                if total_exited > 0 else None
            )
        else:
            avg_exit = None

        hold_secs = max(
            (int(r.get("hold_seconds") or 0) for r in tranche_rows),
            default=0,
        )

        combined.append(CombinedTrade(
            base_trade_id=trade_id,
            direction=anchor.get("direction", ""),
            strike=float(anchor.get("strike") or 0),
            entry_time=anchor.get("entry_time", ""),
            entry_price=float(anchor.get("entry_price") or 0),
            total_qty=int(anchor.get("entry_qty") or 0),
            conviction=int(anchor.get("conviction") or 0),
            tranches=tranche_rows,
            total_pnl=total_pnl,
            capital_risked=capital_risked,
            initial_stop_price=initial_stop_price,
            exit_reason=_classify_exit_reason(tranche_rows),
            hold_seconds=hold_secs,
            avg_exit_price=avg_exit,
        ))

    # Orphaned tranche A rows — add as standalone entries
    for r in orphans:
        pnl = float(r.get("realized_pnl") or 0.0) if r.get("exit_time") else 0.0
        combined.append(CombinedTrade(
            base_trade_id=r["trade_id"],
            direction=r.get("direction", ""),
            strike=float(r.get("strike") or 0),
            entry_time=r.get("entry_time", ""),
            entry_price=float(r.get("entry_price") or 0),
            total_qty=int(r.get("entry_qty") or 0),
            conviction=int(r.get("conviction") or 0),
            tranches=[r],
            total_pnl=pnl,
            capital_risked=float(r.get("capital_risked") or 0.0),
            initial_stop_price=float(r.get("initial_stop_price") or 0.0),
            exit_reason=_classify_exit_reason([r]),
            hold_seconds=int(r.get("hold_seconds") or 0),
            avg_exit_price=float(r["exit_price"]) if r.get("exit_price") else None,
        ))

    combined.sort(key=lambda t: t.entry_time)
    return combined


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_session_report(date_str: str) -> SessionReport:
    """Build a SessionReport from trades for date_str."""
    rows = get_trades_for_date(date_str)
    trades = group_trades_by_trade_id(rows)

    winners = [t for t in trades if t.total_pnl > 0]
    losers = [t for t in trades if t.total_pnl <= 0]

    total_pnl = sum(t.total_pnl for t in trades)
    total_capital_risked = sum(t.capital_risked for t in trades)

    r_vals = [
        safe_r_multiple(t.total_pnl, t.capital_risked)
        for t in trades
    ]
    valid_rs = [r for r in r_vals if r is not None]

    return SessionReport(
        date=date_str,
        trades=trades,
        trade_count=len(trades),
        win_count=len(winners),
        loss_count=len(losers),
        total_pnl=total_pnl,
        total_capital_risked=total_capital_risked,
        avg_r=round(sum(valid_rs) / len(valid_rs), 2) if valid_rs else None,
        best_r=max(valid_rs) if valid_rs else None,
        worst_r=min(valid_rs) if valid_rs else None,
        weekly_pnl=get_weekly_pnl(date_str),
        monthly_pnl=get_monthly_pnl(date_str),
    )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def store_report(report: SessionReport) -> None:
    """Serialize and cache the report in eod_reports table.

    This acts as the idempotency lock — subsequent engine restarts will see
    this row and skip re-generation. Falls back gracefully if DB is locked.
    The trades table is always the source of truth; this is just a cache.
    """
    try:
        report_dict = {
            "date": report.date,
            "trade_count": report.trade_count,
            "win_count": report.win_count,
            "loss_count": report.loss_count,
            "total_pnl": report.total_pnl,
            "total_capital_risked": report.total_capital_risked,
            "avg_r": report.avg_r,
            "best_r": report.best_r,
            "worst_r": report.worst_r,
            "weekly_pnl": report.weekly_pnl,
            "monthly_pnl": report.monthly_pnl,
            "trades": [
                {
                    "base_trade_id": t.base_trade_id,
                    "direction": t.direction,
                    "strike": t.strike,
                    "entry_time": t.entry_time,
                    "entry_price": t.entry_price,
                    "total_qty": t.total_qty,
                    "conviction": t.conviction,
                    "total_pnl": t.total_pnl,
                    "capital_risked": t.capital_risked,
                    "initial_stop_price": t.initial_stop_price,
                    "exit_reason": t.exit_reason,
                    "hold_seconds": t.hold_seconds,
                    "avg_exit_price": t.avg_exit_price,
                    "r_multiple": safe_r_multiple(t.total_pnl, t.capital_risked),
                }
                for t in report.trades
            ],
        }
        upsert_eod_report(report.date, json.dumps(report_dict))
    except Exception as exc:
        logger.warning(
            "[EOD] Failed to cache report in DB: %s — report still queryable from trades table",
            exc,
        )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_pnl(val: float) -> str:
    if val >= 0:
        return f"+${val:,.2f}"
    return f"-${abs(val):,.2f}"


def format_as_text(report: SessionReport) -> str:
    """Render a SessionReport as a plain-text summary block."""
    win_rate_str = (
        f"{report.win_count / report.trade_count * 100:.0f}%"
        if report.trade_count else "—"
    )
    lines = [
        "",
        "=" * 62,
        f"  AutoGEX EOD Summary — {report.date}",
        "=" * 62,
        f"  Trades: {report.trade_count}  |  Winners: {report.win_count}"
        f"  |  Losers: {report.loss_count}  |  Win rate: {win_rate_str}",
        f"  Daily P&L:      {_fmt_pnl(report.total_pnl)}",
        f"  Capital risked: ${report.total_capital_risked:,.2f}",
        f"  Average R:      {fmt_r(report.avg_r)}",
        "",
        f"  {'TIME':<7} {'DIR':<6} {'STRIKE':<8} {'ENTRY':>6} {'EXIT':>7}"
        f" {'P&L':>9} {'R':>6} {'RISKED':>8}  OUTCOME",
        f"  {'-'*74}",
    ]

    for t in report.trades:
        r_val = safe_r_multiple(t.total_pnl, t.capital_risked)
        try:
            entry_dt = datetime.fromisoformat(t.entry_time)
            time_str = entry_dt.strftime("%H:%M")
        except Exception:
            time_str = "—"

        exit_str = f"${t.avg_exit_price:.2f}" if t.avg_exit_price else "—"
        if t.exit_reason == "ran":
            outcome = "ran"
        elif t.exit_reason == "stop_out":
            outcome = "stopped"
        elif t.exit_reason == "hard_close":
            outcome = "EOD close"
        else:
            outcome = t.exit_reason

        risked_str = f"${t.capital_risked:,.0f}" if t.capital_risked > 0 else "N/A"

        lines.append(
            f"  {time_str:<7} {t.direction:<6} {t.strike:<8.0f}"
            f" ${t.entry_price:>5.2f}  {exit_str:>7}"
            f" {_fmt_pnl(t.total_pnl):>9} {fmt_r(r_val):>6} {risked_str:>8}  {outcome}"
        )

    lines += [
        f"  {'-'*74}",
        f"  Weekly P&L:  {_fmt_pnl(report.weekly_pnl)}",
        f"  Monthly P&L: {_fmt_pnl(report.monthly_pnl)}",
        "=" * 62,
        "",
    ]
    return "\n".join(lines)


def log_report(report: SessionReport) -> None:
    """Write the formatted report block to the autogex logger."""
    for line in format_as_text(report).splitlines():
        logger.info(line)


# ---------------------------------------------------------------------------
# Email notification
# ---------------------------------------------------------------------------

def notify_email(report: SessionReport) -> None:
    """Send EOD summary via SMTP email. Skips silently if env vars not configured.

    Required env vars (in .env, never in autogex_config.json):
        EOD_EMAIL_TO, EOD_EMAIL_FROM, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS
    """
    to_addr = os.environ.get("EOD_EMAIL_TO")
    from_addr = os.environ.get("EOD_EMAIL_FROM")
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")

    if not all([to_addr, from_addr, host, user, password]):
        logger.info("[EOD] Email notification skipped — SMTP env vars not fully configured.")
        return

    port = int(os.environ.get("SMTP_PORT", "587"))
    pnl_sign = "+" if report.total_pnl >= 0 else ""
    subject = (
        f"AutoGEX EOD — {report.date} | P&L: {pnl_sign}${report.total_pnl:,.2f}"
    )
    body = format_as_text(report)

    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    try:
        with smtplib.SMTP(host, port) as server:
            server.ehlo()
            server.starttls()
            server.login(user, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())
        logger.info("[EOD] Email sent to %s", to_addr)
    except Exception as exc:
        logger.warning("[EOD] Email notification failed: %s", exc)


# ---------------------------------------------------------------------------
# Standalone entry point (called by launchd at 4:00 PM ET Mon–Fri)
# ---------------------------------------------------------------------------

def _run_eod_report(date_str: str) -> None:
    """Generate, store, log, and email the EOD report for *date_str*.

    Idempotent — safe to call multiple times; skips if report already stored
    or if no trades were placed on that date.
    """
    if eod_report_exists(date_str):
        logger.info("[EOD] Report already exists for %s — skipping.", date_str)
        return

    trade_count = get_trade_count_for_date(date_str)
    if trade_count == 0:
        logger.info("[EOD] No trades on %s — skipping.", date_str)
        return

    logger.info("[EOD] Generating report for %s (%d trades)...", date_str, trade_count)
    report = generate_session_report(date_str)
    store_report(report)
    log_report(report)
    notify_email(report)
    logger.info("[EOD] Done — P&L: %s | trades: %d | avg R: %s",
                _fmt_pnl(report.total_pnl), report.trade_count, fmt_r(report.avg_r))


if __name__ == "__main__":
    import logging as _logging
    from datetime import date as _date
    from pathlib import Path as _Path

    try:
        from dotenv import load_dotenv as _load_dotenv
        _load_dotenv(_Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    _run_eod_report(_date.today().isoformat())
