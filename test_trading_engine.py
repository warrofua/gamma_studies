"""Unit tests for trading_engine.py — caffeinate sleep-prevention helpers
and EOD report shutdown trigger.

Run with:  python -m pytest test_trading_engine.py -v
"""

import platform
import subprocess
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import pytz

import trading_engine


# ---------------------------------------------------------------------------
# _start_caffeinate / _stop_caffeinate
# ---------------------------------------------------------------------------

def setup_function():
    """Reset module-level caffeinate state before each test."""
    trading_engine._caffeinate_proc = None


def test_start_caffeinate_spawns_on_macos():
    """On macOS, caffeinate should be spawned and _caffeinate_proc set."""
    mock_proc = MagicMock()
    mock_proc.pid = 12345

    with patch.object(platform, "system", return_value="Darwin"), \
         patch.object(subprocess, "Popen", return_value=mock_proc) as mock_popen:
        trading_engine._start_caffeinate()

    assert trading_engine._caffeinate_proc is mock_proc
    mock_popen.assert_called_once_with(
        ["caffeinate", "-dims"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_start_caffeinate_noop_on_non_macos():
    """On non-macOS, _start_caffeinate should be a no-op."""
    with patch.object(platform, "system", return_value="Linux"), \
         patch.object(subprocess, "Popen") as mock_popen:
        trading_engine._start_caffeinate()

    assert trading_engine._caffeinate_proc is None
    mock_popen.assert_not_called()


def test_start_caffeinate_handles_missing_binary(caplog):
    """If caffeinate binary is not found, log a warning and don't crash."""
    with patch.object(platform, "system", return_value="Darwin"), \
         patch.object(subprocess, "Popen", side_effect=FileNotFoundError):
        trading_engine._start_caffeinate()

    assert trading_engine._caffeinate_proc is None
    assert "not found" in caplog.text.lower() or "unavailable" in caplog.text.lower()


def test_stop_caffeinate_terminates_process():
    """_stop_caffeinate should terminate the process and clear _caffeinate_proc."""
    mock_proc = MagicMock()
    mock_proc.pid = 12345
    trading_engine._caffeinate_proc = mock_proc

    trading_engine._stop_caffeinate()

    mock_proc.terminate.assert_called_once()
    mock_proc.wait.assert_called_once_with(timeout=3)
    assert trading_engine._caffeinate_proc is None


def test_stop_caffeinate_noop_when_not_running():
    """_stop_caffeinate should be a no-op if caffeinate was never started."""
    trading_engine._caffeinate_proc = None
    trading_engine._stop_caffeinate()  # must not raise


def test_stop_caffeinate_force_kills_on_timeout():
    """If .wait() times out, _stop_caffeinate should fall back to .kill()."""
    mock_proc = MagicMock()
    mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="caffeinate", timeout=3)
    trading_engine._caffeinate_proc = mock_proc

    trading_engine._stop_caffeinate()

    mock_proc.kill.assert_called_once()
    assert trading_engine._caffeinate_proc is None


# ---------------------------------------------------------------------------
# _maybe_generate_eod_report — belt-and-suspenders shutdown trigger
# ---------------------------------------------------------------------------

def _make_cfg(hard_close_time="15:55"):
    cfg = MagicMock()
    cfg.hard_close_time = hard_close_time
    return cfg


_ET = pytz.timezone("US/Eastern")


@patch("trading_engine.get_trade_count_for_date", return_value=2)
@patch("trading_engine.eod_report_exists", return_value=False)
@patch("trading_engine._eod_report")
def test_maybe_eod_report_skips_before_hard_close(mock_eod, mock_exists, mock_count):
    """Engine exits before hard_close_time → skip (launchd is primary trigger)."""
    early = _ET.localize(datetime(2026, 3, 13, 10, 0, 0))  # 10 AM ET
    with patch("trading_engine.datetime") as mock_dt:
        mock_dt.now.return_value = early
        trading_engine._maybe_generate_eod_report(MagicMock(), _make_cfg("15:55"))

    mock_eod.generate_session_report.assert_not_called()


@patch("trading_engine.get_trade_count_for_date", return_value=2)
@patch("trading_engine.eod_report_exists", return_value=False)
@patch("trading_engine._eod_report")
def test_maybe_eod_report_fires_after_hard_close(mock_eod, mock_exists, mock_count):
    """Engine exits at or after hard_close_time with trades → report generated."""
    after_close = _ET.localize(datetime(2026, 3, 13, 16, 5, 0))  # 4:05 PM ET
    mock_report = MagicMock()
    mock_eod.generate_session_report.return_value = mock_report

    with patch("trading_engine.datetime") as mock_dt:
        mock_dt.now.return_value = after_close
        trading_engine._maybe_generate_eod_report(MagicMock(), _make_cfg("15:55"))

    mock_eod.generate_session_report.assert_called_once()
    mock_eod.notify_email.assert_called_once_with(mock_report)
