"""Unit tests for position_manager.py — gatekeeper breakeven logic.

Run with:  python -m pytest test_position_manager.py -v

Covers the specific bug where gatekeeper_cleared was set to True unconditionally
before checking whether the breakeven stop was safe to apply, causing an immediate
stop-out when the option price hadn't risen above the breakeven level yet.
"""

import uuid
from datetime import datetime, time, date
from unittest.mock import patch

import pytz
import pytest

from config import AutoGexConfig
from position_manager import OpenPosition, PositionManager

_ET = pytz.timezone("US/Eastern")
_MID_DAY = time(10, 30)  # well clear of hard_close_time (15:55)


def _make_cfg(**overrides) -> AutoGexConfig:
    """Return a minimal config with predictable values."""
    defaults = dict(
        initial_stop_pct=0.50,
        breakeven_buffer=0.05,
        hard_close_time="15:55",
        trail_pct_am=0.30,
        trail_pct_afternoon=0.15,
        trail_pct_final=0.10,
        tranche_a_target_pct=0.35,
        tranche_a_pct=0.60,
        cooldown_after_stop=300,
        cooldown_after_entry=300,
        max_trades_per_day=10,
        max_concurrent_positions=3,
        daily_loss_limit=2000.0,
        min_block=4,
        max_block=12,
        max_risk_per_trade=2000.0,
        am_window_start="09:35",
        am_window_end="11:30",
        no_new_entries_after="14:30",
    )
    defaults.update(overrides)
    return AutoGexConfig(**defaults)


def _make_put_position(
    entry_price: float = 2.16,
    stop: float = 1.08,
    gk_below: float = 668.0,
    high_water_mark: float = None,
    gatekeeper_cleared: bool = False,
    tranche_a_closed: bool = False,
) -> OpenPosition:
    """Build a PUT OpenPosition with the given parameters."""
    qty = 8
    return OpenPosition(
        trade_id=str(uuid.uuid4()),
        symbol="SPY260313P00667000",
        direction="PUT",
        strike=667.0,
        expiration="2026-03-13",
        entry_price=entry_price,
        entry_time=datetime.now(_ET),
        total_qty=qty,
        remaining_qty=qty,
        tranche_a_qty=4,
        tranche_b_qty=4,
        tranche_a_closed=tranche_a_closed,
        high_water_mark=high_water_mark if high_water_mark is not None else entry_price,
        current_stop=stop,
        entry_conviction=6,
        gatekeeper_cleared=gatekeeper_cleared,
        nearest_gk_in_direction=gk_below,
    )


# ---------------------------------------------------------------------------
# Gatekeeper: defers breakeven stop when option price is below new_stop
# ---------------------------------------------------------------------------

def test_gatekeeper_defers_when_option_below_breakeven():
    """
    Scenario: spot crosses gatekeeper but option is at $2.19 — below breakeven
    stop of $2.21 ($2.16 + $0.05). The fix should leave gatekeeper_cleared=False
    and not update the stop, allowing a retry next tick.

    This is the exact scenario from the 2026-03-13 10:45 trade.
    """
    cfg = _make_cfg()
    pm = PositionManager(cfg)
    pos = _make_put_position(entry_price=2.16, stop=1.08, gk_below=668.0)

    # new_stop = 2.16 + 0.05 = 2.21; option is at 2.19 — below new_stop
    with patch.object(pm, "_time_et", return_value=_MID_DAY):
        actions = pm.evaluate_position(pos, current_price=2.19, spot_price=667.5)

    assert not pos.gatekeeper_cleared, "gatekeeper_cleared must stay False when stop cannot be applied"
    assert pos.current_stop == 1.08, "stop must not be updated when option is below breakeven level"
    assert not any(a.action == "update_stop" for a in actions), "no update_stop action should be emitted"
    assert not any(a.action == "stop_out" for a in actions), "must not stop out immediately"


def test_gatekeeper_applies_when_option_above_breakeven():
    """
    Happy path: option price ($2.25) is above the breakeven stop ($2.21).
    Gatekeeper should fire, cleared=True, stop updated.
    """
    cfg = _make_cfg()
    pm = PositionManager(cfg)
    pos = _make_put_position(entry_price=2.16, stop=1.08, gk_below=668.0, high_water_mark=2.25)

    # new_stop = 2.16 + 0.05 = 2.21; option is at 2.25 — safely above new_stop
    with patch.object(pm, "_time_et", return_value=_MID_DAY):
        actions = pm.evaluate_position(pos, current_price=2.25, spot_price=667.5)

    assert pos.gatekeeper_cleared, "gatekeeper_cleared must be set True when stop is safely applied"
    assert pos.current_stop == pytest.approx(2.21), "stop must be updated to entry + buffer"
    update_actions = [a for a in actions if a.action == "update_stop"]
    assert len(update_actions) == 1
    assert update_actions[0].new_stop == pytest.approx(2.21)


def test_gatekeeper_retries_after_deferral():
    """
    After a deferral tick (option below breakeven), the gatekeeper should retry
    on the next tick when the option has risen above the breakeven stop.
    """
    cfg = _make_cfg()
    pm = PositionManager(cfg)
    pos = _make_put_position(entry_price=2.16, stop=1.08, gk_below=668.0)

    with patch.object(pm, "_time_et", return_value=_MID_DAY):
        # Tick 1: option below breakeven — should defer
        pm.evaluate_position(pos, current_price=2.19, spot_price=667.5)
        assert not pos.gatekeeper_cleared

        # Tick 2: option now above breakeven — should apply
        pos.high_water_mark = 2.25
        pm.evaluate_position(pos, current_price=2.25, spot_price=667.5)

    assert pos.gatekeeper_cleared
    assert pos.current_stop == pytest.approx(2.21)


def test_gatekeeper_skips_when_already_cleared():
    """Once cleared, the gatekeeper block is skipped entirely (no double-trigger)."""
    cfg = _make_cfg()
    pm = PositionManager(cfg)
    # Start with gatekeeper already cleared and stop at 2.21
    pos = _make_put_position(
        entry_price=2.16, stop=2.21, gk_below=668.0, gatekeeper_cleared=True
    )

    with patch.object(pm, "_time_et", return_value=_MID_DAY):
        actions = pm.evaluate_position(pos, current_price=2.30, spot_price=667.5)

    # Stop should not change (no re-trigger of gatekeeper)
    assert pos.current_stop == pytest.approx(2.21)
    assert not any(a.action == "update_stop" and "gatekeeper" in a.reason.lower() for a in actions)


# ---------------------------------------------------------------------------
# Stop-out log message format
# ---------------------------------------------------------------------------

def test_stop_out_reason_includes_both_prices():
    """
    The stop-out reason string must include both the option price and the stop
    level so the log is unambiguous. Regression for 'Stop hit at 2.19' confusion.
    """
    cfg = _make_cfg()
    pm = PositionManager(cfg)
    # Set stop at 2.21 so current_price 2.19 triggers it
    pos = _make_put_position(entry_price=2.16, stop=2.21, gk_below=668.0, gatekeeper_cleared=True)

    with patch.object(pm, "_time_et", return_value=_MID_DAY):
        actions = pm.evaluate_position(pos, current_price=2.19, spot_price=670.0)

    assert len(actions) == 1
    assert actions[0].action == "stop_out"
    reason = actions[0].reason
    assert "2.19" in reason, "option price must appear in stop-out reason"
    assert "2.21" in reason, "stop level must appear in stop-out reason"


# ---------------------------------------------------------------------------
# Same-strike concurrent cap
# ---------------------------------------------------------------------------

_MID_DAY_DT = _ET.localize(datetime.combine(date.today(), _MID_DAY))


def _open_position_at_strike(pm, strike, entry_price=2.0):
    """Helper: inject a pre-built position at the given strike into pm.positions."""
    pos = _make_put_position(entry_price=entry_price, stop=1.0)
    pos.strike = strike
    pm.positions[pos.trade_id] = pos
    return pos


def _pm_with_time(cfg):
    """Return a PositionManager with time frozen to mid-day (well inside entry window)."""
    pm = PositionManager(cfg)
    pm._time_et = lambda: _MID_DAY
    pm._now_et = lambda: _MID_DAY_DT
    return pm


def test_same_strike_cap_blocks_third_position():
    """Two positions at 667 already open — a third at 667 must be blocked."""
    pm = _pm_with_time(_make_cfg(max_concurrent_positions=5))
    _open_position_at_strike(pm, 667.0)
    _open_position_at_strike(pm, 667.0)

    ok, reason = pm.can_enter(strike=667.0)

    assert not ok
    assert "667" in reason
    assert "same-strike" in reason.lower() or "cap" in reason.lower()


def test_same_strike_cap_allows_second_position():
    """One position at 667 — a second at 667 should be allowed."""
    pm = _pm_with_time(_make_cfg(max_concurrent_positions=5))
    _open_position_at_strike(pm, 667.0)

    ok, _ = pm.can_enter(strike=667.0)

    assert ok


def test_same_strike_cap_allows_different_strike():
    """Two positions at 667 already open — a new position at 665 must be allowed."""
    pm = _pm_with_time(_make_cfg(max_concurrent_positions=5))
    _open_position_at_strike(pm, 667.0)
    _open_position_at_strike(pm, 667.0)

    ok, _ = pm.can_enter(strike=665.0)

    assert ok


def test_can_enter_without_strike_skips_strike_checks():
    """can_enter(strike=None) must not fail on strike-specific checks (backward compat)."""
    pm = _pm_with_time(_make_cfg(max_concurrent_positions=5))
    _open_position_at_strike(pm, 667.0)
    _open_position_at_strike(pm, 667.0)

    ok, _ = pm.can_enter()  # no strike arg

    assert ok  # global limits not hit, no strike check performed


# ---------------------------------------------------------------------------
# Strike ban after consecutive pure stops
# ---------------------------------------------------------------------------

def _close_as_stop(pm, trade_id, pnl=-100.0):
    """Helper: close a position with a pure stop-out reason."""
    pm.close_position(trade_id, pnl, "Stop hit: option=1.50 <= stop=1.55")


def _close_as_tranche_a_then_stop(pm, trade_id, pnl=50.0):
    """Helper: close a position that had Tranche A taken before stopping out."""
    pos = pm.positions.get(trade_id)
    if pos:
        pos.tranche_a_closed = True
    pm.close_position(trade_id, pnl, "Stop hit: option=1.50 <= stop=1.55")


def test_strike_banned_after_two_consecutive_pure_stops():
    """Two pure stops at 663 with no Tranche A → strike must be banned."""
    pm = _pm_with_time(_make_cfg(max_concurrent_positions=5))

    pos1 = _open_position_at_strike(pm, 663.0)
    _close_as_stop(pm, pos1.trade_id)

    pos2 = _open_position_at_strike(pm, 663.0)
    _close_as_stop(pm, pos2.trade_id)

    ok, reason = pm.can_enter(strike=663.0)
    assert not ok
    assert "663" in reason
    assert "banned" in reason.lower()


def test_strike_not_banned_after_one_pure_stop():
    """One pure stop at 663 should not ban the strike."""
    pm = _pm_with_time(_make_cfg(max_concurrent_positions=5))

    pos1 = _open_position_at_strike(pm, 663.0)
    _close_as_stop(pm, pos1.trade_id)

    ok, _ = pm.can_enter(strike=663.0)
    assert ok


def test_tranche_a_exit_resets_stop_streak():
    """
    One pure stop at 663, then a position where Tranche A hits (partial win),
    then another stop — streak resets at the Tranche A exit, so ban must NOT fire.
    """
    pm = _pm_with_time(_make_cfg(max_concurrent_positions=5))

    pos1 = _open_position_at_strike(pm, 663.0)
    _close_as_stop(pm, pos1.trade_id)  # streak = 1

    pos2 = _open_position_at_strike(pm, 663.0)
    _close_as_tranche_a_then_stop(pm, pos2.trade_id)  # Tranche A taken → resets streak to 0

    pos3 = _open_position_at_strike(pm, 663.0)
    _close_as_stop(pm, pos3.trade_id)  # streak = 1 again (not 2)

    ok, _ = pm.can_enter(strike=663.0)
    assert ok, "streak was reset by Tranche A exit — ban must not fire after only one subsequent stop"


def test_strike_ban_resets_on_new_day():
    """reset_daily() must clear the strike ban."""
    pm = _pm_with_time(_make_cfg(max_concurrent_positions=5))

    pos1 = _open_position_at_strike(pm, 663.0)
    _close_as_stop(pm, pos1.trade_id)
    pos2 = _open_position_at_strike(pm, 663.0)
    _close_as_stop(pm, pos2.trade_id)

    assert not pm.can_enter(strike=663.0)[0], "sanity: banned before reset"

    pm.reset_daily()

    ok, _ = pm.can_enter(strike=663.0)
    assert ok, "ban must clear after reset_daily()"


def test_strike_ban_does_not_affect_other_strikes():
    """Banning 663 must not prevent entries at 665."""
    pm = _pm_with_time(_make_cfg(max_concurrent_positions=5))

    pos1 = _open_position_at_strike(pm, 663.0)
    _close_as_stop(pm, pos1.trade_id)
    pos2 = _open_position_at_strike(pm, 663.0)
    _close_as_stop(pm, pos2.trade_id)

    ok, _ = pm.can_enter(strike=665.0)
    assert ok
