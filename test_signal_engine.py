"""Unit tests for signal_engine.py — dead-signal veto and core scoring.

Run with:  python -m pytest test_signal_engine.py -v
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from signal_engine import evaluate, GexSignalState, build_signal_state


def _make_state(**overrides) -> GexSignalState:
    """Build a GexSignalState. Defaults: spot near positive king, no gatekeeper, no velocity."""
    defaults = dict(
        timestamp=datetime(2026, 3, 13, 10, 30, 0),
        spot_price=560.5,
        total_gex=5.0,
        king_strike=561.0,
        king_gex=1.0,           # positive king → CALL bias
        gamma_flip_strike=550.0,
        dist_to_flip_pct=1.5,   # far from flip → no veto/penalty
        nearest_gk_below=540.0, # far → gatekeeper scores 0
        nearest_gk_above=580.0, # far → gatekeeper scores 0
        top_velocity_strike=None,
        top_velocity_value=None,
        gamma_delta=None,
        per_strike_gex={561.0: 1.0, 580.0: -0.5, 540.0: 0.3},
        regime='positive_stable',
        spot_vs_king=-0.5,
    )
    defaults.update(overrides)
    return GexSignalState(**defaults)


def _evaluate_with_state(state: GexSignalState):
    """Call evaluate() using a mock SymbolGexData, patching build_signal_state to return state."""
    mock_data = MagicMock()
    with patch('signal_engine.build_signal_state', return_value=state):
        return evaluate(mock_data)


# ---------------------------------------------------------------------------
# Dead-signal veto: gatekeeper=0 AND velocity=0
# ---------------------------------------------------------------------------

def test_dead_signal_veto_fires_when_gk_and_vel_both_zero():
    """
    Mirrors the 3/13 opening CALL: king proximity scores but gatekeeper=0
    and velocity=0 (no momentum). Must be vetoed — king alone is insufficient.
    """
    state = _make_state(
        nearest_gk_above=580.0,   # far → gatekeeper=0
        nearest_gk_below=540.0,
        top_velocity_value=None,  # no velocity
        gamma_delta=None,
    )
    result = _evaluate_with_state(state)

    assert result.direction == 'NONE', "dead-signal veto must suppress direction"
    assert result.conviction == 0
    assert result.vetoed is True
    assert result.signal_scores['gatekeeper'] == 0
    assert result.signal_scores['velocity'] == 0


def test_dead_signal_veto_does_not_fire_when_velocity_present():
    """
    gatekeeper=0 but velocity=2 — matches profitable 3/12 pattern.
    Veto must NOT fire; velocity alone is sufficient confirmation.
    """
    state = _make_state(
        nearest_gk_above=580.0,          # gatekeeper=0
        nearest_gk_below=540.0,
        top_velocity_strike=565.0,
        top_velocity_value=0.15,          # above threshold → velocity scores
        gamma_delta=0.20,
    )
    result = _evaluate_with_state(state)

    assert result.signal_scores['velocity'] > 0, "velocity should score"
    assert result.vetoed is False


def test_dead_signal_veto_does_not_fire_when_gatekeeper_present():
    """
    gatekeeper=1 but velocity=0 — structure present, veto must NOT fire.
    """
    state = _make_state(
        spot_price=558.1,
        nearest_gk_below=558.0,          # close → gatekeeper may score
        nearest_gk_above=580.0,
        per_strike_gex={558.0: 0.5, 561.0: 1.0},  # gk_below positive → CALL
        top_velocity_value=None,
        gamma_delta=None,
    )
    result = _evaluate_with_state(state)

    # Veto must not have fired if gatekeeper scored
    if result.signal_scores['gatekeeper'] > 0:
        assert result.vetoed is False


def test_flip_veto_still_takes_precedence():
    """
    Existing flip-proximity veto must fire before the dead-signal veto —
    it is checked first in evaluate().
    """
    state = _make_state(dist_to_flip_pct=0.1)  # inside 0.3 threshold → flip veto
    result = _evaluate_with_state(state)

    assert result.direction == 'NONE'
    assert result.conviction == 0
    assert result.vetoed is True


def test_normal_signal_with_all_components_passes():
    """
    Full signal (king + gatekeeper + velocity) must produce non-zero conviction
    and not be vetoed.
    """
    state = _make_state(
        spot_price=558.1,
        nearest_gk_below=558.0,
        per_strike_gex={558.0: 0.5, 561.0: 1.0},
        top_velocity_strike=565.0,
        top_velocity_value=0.15,
        gamma_delta=0.20,
    )
    result = _evaluate_with_state(state)

    assert result.vetoed is False
    assert result.conviction > 0
