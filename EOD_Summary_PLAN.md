# EOD Summary Feature — Implementation Plan

## Status
> IMPLEMENTED. See git history for changes.

---

## What We Built

An end-of-day trading summary that fires **once per day, after market close, if and only if at least 1 trade was executed that day**. The trigger is time-based and idempotent.

### Key user-facing outputs
1. **New "📊 Reports" tab** in `autogex_dashboard.py` with:
   - Live session view during market hours (open + closed trades)
   - EOD summary after close
   - Weekly and Monthly P&L sub-tabs
   - CSV export button
   - Tranche detail expanders (combined trade view)
2. **EOD email notification** sent after hard close (SMTP, credentials in `.env`)
3. **Per-trade capital risk context**: every trade shows dollars risked and R-multiple

---

## Trigger Logic

```
After market close (>= hard_close_time "15:55" ET):
  IF eod_report already exists for today → skip (idempotent)
  IF trade_count_today == 0 → skip (no trades)
  ELSE → generate report, store in DB, send email, log to autogex.log
```

The check runs at the END of `trading_engine.py` `main()`, after the loop exits.
NOT tied to engine shutdown — only fires if after market close.
Idempotent: engine can be restarted 20 times; `eod_reports` table acts as the lock.

---

## Eng Review Refinements (applied)

1. **`parent_trade_id` column** added to `trades` schema — tranche A rows store `pos.trade_id` as parent reference. Grouping uses this instead of fragile composite key.
2. **`capital_risked` computed inline** at `insert_trade()` time in `_execute_entry()` — NOT on `OpenPosition`. This ensures live fill price is used, not the $1.50 placeholder.
3. **`position_manager.py` unchanged** — no new fields needed on `OpenPosition`.
4. **DB query functions** (`get_weekly_pnl`, `get_monthly_pnl`, `get_trade_count_for_date`, `eod_report_exists`) live in `trade_journal.py` only.
5. **`test_eod_report.py`** added — unit tests for pure functions.
6. **Orphaned tranche A rows** handled gracefully in `group_trades_by_trade_id()`.
7. **Malformed `eod_reports` JSON** handled with try/except fallback in dashboard.

---

## Files Changed

| File | Type | What |
|---|---|---|
| `eod_report.py` | NEW | Report logic, email notification, pure functions |
| `trade_journal.py` | Modified | Schema migration (3 cols + 1 table + index), 6 new query functions |
| `trading_engine.py` | Modified | `insert_trade()` gets new fields; EOD trigger added |
| `autogex_dashboard.py` | Modified | Reports tab in `render_autogex_view()` |
| `test_eod_report.py` | NEW | Unit tests for pure functions |

`position_manager.py` — **NOT modified**.

---

## New `.env` Variables

```
# EOD Email Notification (all optional — feature skipped if not set)
EOD_EMAIL_TO=
EOD_EMAIL_FROM=
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
```

---

## TODOS (deferred)

1. **Intraday equity curve chart** (P2, S) — Plotly line of cumulative P&L by exit_time. Needs live data history.
2. **Signal performance breakdown** (P2, S) — Win rate and avg R by conviction score. Needs ~30 trades.
3. **DRY weekly query** (P3, XS) — Replace the Mon→today date loop in `_render_performance_metrics()` with `get_weekly_pnl()`.
