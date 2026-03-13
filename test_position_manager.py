"""Unit tests for position_manager.py — gatekeeper breakeven logic.

Run with:  python -m pytest test_position_manager.py -v

Covers the specific bug where gatekeeper_cleared was set to True unconditionally
before checking whether the breakeven stop was safe to apply, causing an immediate
stop-out when the option price hadn't risen above the breakeven level yet.
"""

import uuid
from datetime import datetime, time
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
