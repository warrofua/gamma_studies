"""Unit tests for eod_report.py pure functions.

Run with:  python -m pytest test_eod_report.py -v
"""

import pytest
from unittest.mock import MagicMock, patch
from eod_report import (
    _classify_exit_reason,
    _run_eod_report,
    fmt_r,
    group_trades_by_trade_id,
    safe_r_multiple,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_row(
    trade_id="uuid-default",
    tranche="full",
    direction="PUT",
    strike=580.0,
    entry_time="2026-03-12T09:42:00",
    entry_price=1.50,
    entry_qty=10,
    exit_time="2026-03-12T10:15:00",
    exit_price=2.10,
    exit_qty=10,
    exit_reason="trailing stop hit at 2.10",
    realized_pnl=600.0,
    capital_risked=750.0,
    initial_stop_price=0.75,
    parent_trade_id=None,
    conviction=7,
    hold_seconds=1980,
):
    return {
        "trade_id": trade_id,
        "tranche": tranche,
        "direction": direction,
        "strike": strike,
        "entry_time": entry_time,
        "entry_price": entry_price,
        "entry_qty": entry_qty,
        "exit_time": exit_time,
        "exit_price": exit_price,
        "exit_qty": exit_qty,
        "exit_reason": exit_reason,
        "realized_pnl": realized_pnl,
        "capital_risked": capital_risked,
        "initial_stop_price": initial_stop_price,
        "parent_trade_id": parent_trade_id,
        "conviction": conviction,
        "hold_seconds": hold_seconds,
    }


# ---------------------------------------------------------------------------
# safe_r_multiple
# ---------------------------------------------------------------------------

def test_safe_r_multiple_normal():
    assert safe_r_multiple(300.0, 200.0) == 1.5


def test_safe_r_multiple_loss():
    assert safe_r_multiple(-200.0, 200.0) == -1.0


def test_safe_r_multiple_zero_capital():
    assert safe_r_multiple(300.0, 0.0) is None


def test_safe_r_multiple_negative_capital():
    assert safe_r_multiple(300.0, -100.0) is None


def test_safe_r_multiple_none_pnl():
    assert safe_r_multiple(None, 200.0) is None


def test_safe_r_multiple_none_capital():
    assert safe_r_multiple(300.0, None) is None


def test_safe_r_multiple_both_none():
    assert safe_r_multiple(None, None) is None


# ---------------------------------------------------------------------------
# fmt_r
# ---------------------------------------------------------------------------

def test_fmt_r_positive():
    assert fmt_r(1.5) == "+1.5R"


def test_fmt_r_negative():
    assert fmt_r(-1.0) == "-1.0R"


def test_fmt_r_none():
    assert fmt_r(None) == "N/A"


def test_fmt_r_zero():
    assert fmt_r(0.0) == "+0.0R"


# ---------------------------------------------------------------------------
# _classify_exit_reason
# ---------------------------------------------------------------------------

def test_classify_stop_out():
    tranches = [_make_row(exit_reason="Stop hit at 0.75")]
    assert _classify_exit_reason(tranches) == "stop_out"


def test_classify_hard_close():
    tranches = [_make_row(exit_reason="End of day")]
    assert _classify_exit_reason(tranches) == "hard_close"


def test_classify_ran():
    tranches = [_make_row(exit_reason="Tranche A target hit at 2.03")]
    assert _classify_exit_reason(tranches) == "ran"


def test_classify_stop_takes_priority_over_ran():
    # Stop fires on tranche B after tranche A ran — should still be stop_out
    tranches = [
        _make_row(tranche="A", exit_reason="Tranche A target hit at 2.03"),
        _make_row(tranche="full", exit_reason="Stop hit at 1.60"),
    ]
    assert _classify_exit_reason(tranches) == "stop_out"


def test_classify_empty():
    assert _classify_exit_reason([]) == "open"


def test_classify_open_trade_no_exit():
    tranches = [_make_row(exit_time=None, exit_reason=None)]
    assert _classify_exit_reason(tranches) == "open"


# ---------------------------------------------------------------------------
# group_trades_by_trade_id
# ---------------------------------------------------------------------------

def test_group_empty():
    assert group_trades_by_trade_id([]) == []


def test_group_single_trade_no_tranches():
    rows = [_make_row(trade_id="uuid-a", realized_pnl=300.0, exit_qty=10)]
    result = group_trades_by_trade_id(rows)
    assert len(result) == 1
    assert result[0].total_pnl == 300.0
    assert result[0].base_trade_id == "uuid-a"


def test_group_two_tranches_combined():
    """One entry split into tranche A (partial) + tranche B (final)."""
    parent_id = "uuid-a"
    rows = [
        _make_row(
            trade_id=parent_id,
            tranche="full",
            entry_qty=10,
            exit_qty=4,
            realized_pnl=200.0,
            capital_risked=750.0,
        ),
        _make_row(
            trade_id="uuid-b",
            tranche="A",
            parent_trade_id=parent_id,
            entry_qty=6,
            exit_qty=6,
            realized_pnl=312.0,
            capital_risked=750.0,
        ),
    ]
    result = group_trades_by_trade_id(rows)
    assert len(result) == 1
    assert result[0].base_trade_id == parent_id
    assert result[0].total_pnl == pytest.approx(512.0)
    assert result[0].capital_risked == 750.0
    assert len(result[0].tranches) == 2


def test_group_orphaned_tranche_a_becomes_standalone():
    """Tranche A row with no matching parent is treated as a standalone trade."""
    rows = [
        _make_row(
            trade_id="uuid-orphan",
            tranche="A",
            parent_trade_id="uuid-missing",
            realized_pnl=150.0,
        )
    ]
    result = group_trades_by_trade_id(rows)
    assert len(result) == 1
    assert result[0].base_trade_id == "uuid-orphan"
    assert result[0].total_pnl == 150.0


def test_group_two_independent_trades():
    rows = [
        _make_row(trade_id="uuid-a", strike=580.0, realized_pnl=300.0),
        _make_row(trade_id="uuid-b", strike=585.0, realized_pnl=-200.0),
    ]
    result = group_trades_by_trade_id(rows)
    assert len(result) == 2
    pnls = {t.strike: t.total_pnl for t in result}
    assert pnls[580.0] == 300.0
    assert pnls[585.0] == -200.0


def test_group_sorted_by_entry_time():
    rows = [
        _make_row(trade_id="uuid-later", entry_time="2026-03-12T13:30:00"),
        _make_row(trade_id="uuid-earlier", entry_time="2026-03-12T09:42:00"),
    ]
    result = group_trades_by_trade_id(rows)
    assert result[0].base_trade_id == "uuid-earlier"
    assert result[1].base_trade_id == "uuid-later"


def test_group_weighted_avg_exit_price():
    """Avg exit price weighted by qty: 6ct@$2.10 + 4ct@$2.60 = $2.30."""
    parent_id = "uuid-a"
    rows = [
        _make_row(
            trade_id=parent_id,
            tranche="full",
            exit_price=2.60,
            exit_qty=4,
            realized_pnl=440.0,
        ),
        _make_row(
            trade_id="uuid-b",
            tranche="A",
            parent_trade_id=parent_id,
            exit_price=2.10,
            exit_qty=6,
            realized_pnl=360.0,
        ),
    ]
    result = group_trades_by_trade_id(rows)
    assert result[0].avg_exit_price == pytest.approx(2.30)


# ---------------------------------------------------------------------------
# _run_eod_report (standalone entry point — primary launchd trigger)
# ---------------------------------------------------------------------------

@patch("eod_report.notify_email")
@patch("eod_report.log_report")
@patch("eod_report.store_report")
@patch("eod_report.generate_session_report")
@patch("eod_report.get_trade_count_for_date", return_value=0)
@patch("eod_report.eod_report_exists", return_value=False)
def test_run_eod_report_no_trades_skips(mock_exists, mock_count, mock_gen, mock_store, mock_log, mock_email):
    """Zero trades today → report is skipped, nothing generated or emailed."""
    _run_eod_report("2026-03-13")
    mock_gen.assert_not_called()
    mock_email.assert_not_called()


@patch("eod_report.notify_email")
@patch("eod_report.log_report")
@patch("eod_report.store_report")
@patch("eod_report.generate_session_report")
@patch("eod_report.get_trade_count_for_date", return_value=3)
@patch("eod_report.eod_report_exists", return_value=False)
def test_run_eod_report_with_trades_generates_and_emails(mock_exists, mock_count, mock_gen, mock_store, mock_log, mock_email):
    """Trades exist and no report yet → full pipeline runs."""
    mock_report = MagicMock()
    mock_report.trade_count = 3
    mock_report.total_pnl = 450.0
    mock_report.avg_r = 1.5
    mock_gen.return_value = mock_report

    _run_eod_report("2026-03-13")

    mock_gen.assert_called_once_with("2026-03-13")
    mock_store.assert_called_once_with(mock_report)
    mock_log.assert_called_once_with(mock_report)
    mock_email.assert_called_once_with(mock_report)


@patch("eod_report.notify_email")
@patch("eod_report.generate_session_report")
@patch("eod_report.eod_report_exists", return_value=True)
def test_run_eod_report_idempotent_skips_if_already_stored(mock_exists, mock_gen, mock_email):
    """Report already stored for today → skip entirely (idempotent)."""
    _run_eod_report("2026-03-13")
    mock_gen.assert_not_called()
    mock_email.assert_not_called()
