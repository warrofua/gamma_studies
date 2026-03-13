"""GEX-based signal engine for SPY 0DTE options trading."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Tuple

from gex_utils import SymbolGexData


@dataclass
class GexSignalState:
    timestamp: datetime
    spot_price: float
    total_gex: float
    king_strike: Optional[float]
    king_gex: float
    gamma_flip_strike: Optional[float]
    dist_to_flip_pct: Optional[float]
    nearest_gk_below: Optional[float]
    nearest_gk_above: Optional[float]
    top_velocity_strike: Optional[float]
    top_velocity_value: Optional[float]
    gamma_delta: Optional[float]
    per_strike_gex: Dict[float, float]
    regime: str  # 'positive_stable', 'positive_transition', 'negative_trending', 'negative_transition', 'unknown'
    spot_vs_king: Optional[float]  # spot - king_strike (signed), None if no king


@dataclass
class SignalResult:
    direction: str          # 'CALL', 'PUT', or 'NONE'
    conviction: int         # 0–9+
    signal_scores: Dict[str, int]  # {'king_node': 2, 'gatekeeper': 1, 'velocity': 1, 'regime': 1, 'flip_penalty': 0}
    vetoed: bool            # True if flip proximity veto fired
    regime: str
    timestamp: datetime


def classify_regime(data: SymbolGexData) -> str:
    """Classify the current GEX regime."""
    if data.total_gex == 0:
        return 'unknown'

    if data.dist_to_flip_pct is None:
        # Fall back to sign of total_gex
        if data.total_gex > 0:
            return 'positive_stable'
        else:
            return 'negative_trending'

    if data.total_gex > 0:
        if data.dist_to_flip_pct > 1.0:
            return 'positive_stable'
        else:
            return 'positive_transition'
    else:  # total_gex < 0
        if data.dist_to_flip_pct > 1.0:
            return 'negative_trending'
        else:
            return 'negative_transition'


def build_signal_state(data: SymbolGexData) -> GexSignalState:
    """Construct a GexSignalState from a SymbolGexData."""
    regime = classify_regime(data)
    spot_vs_king = (data.spot_price - data.king_strike) if data.king_strike is not None else None
    timestamp = data.prev_fetch_timestamp if data.prev_fetch_timestamp is not None else datetime.now()

    return GexSignalState(
        timestamp=timestamp,
        spot_price=data.spot_price,
        total_gex=data.total_gex,
        king_strike=data.king_strike,
        king_gex=data.king_gex,
        gamma_flip_strike=data.gamma_flip_strike,
        dist_to_flip_pct=data.dist_to_flip_pct,
        nearest_gk_below=data.nearest_gk_below,
        nearest_gk_above=data.nearest_gk_above,
        top_velocity_strike=data.top_strike_velocity_strike,
        top_velocity_value=data.top_strike_velocity_value,
        gamma_delta=data.gamma_delta,
        per_strike_gex=data.per_strike_gex,
        regime=regime,
        spot_vs_king=spot_vs_king,
    )


def _signal_king_node(state: GexSignalState) -> Tuple[int, str]:
    """Evaluate king node signal."""
    if state.king_strike is None:
        return (0, 'NONE')

    spot = state.spot_price
    king = state.king_strike
    threshold = spot * 0.01  # 1% of spot

    if state.king_gex > 0:
        # Positive King = support → CALL bias
        king_above_spot = king - spot
        spot_above_king = spot - king

        if 0 < king_above_spot <= threshold * 0.5:
            return (3, 'CALL')  # at support
        elif threshold * 0.5 < king_above_spot <= threshold:
            return (2, 'CALL')  # approaching support (spot below king by 0.5–1%)
        elif 0 < spot_above_king <= threshold * 0.75:
            return (1, 'CALL')  # just bounced above king
        else:
            return (0, 'NONE')

    elif state.king_gex < 0:
        # Negative King = resistance → PUT bias
        spot_above_king = spot - king
        king_above_spot = king - spot

        if 0 < spot_above_king <= threshold * 0.5:
            return (3, 'PUT')  # at resistance
        elif threshold * 0.5 < spot_above_king <= threshold:
            return (2, 'PUT')  # approaching resistance
        elif 0 < king_above_spot <= threshold * 0.75:
            return (1, 'PUT')  # just rejected below king
        else:
            return (0, 'NONE')

    return (0, 'NONE')


def _signal_gatekeeper(state: GexSignalState) -> Tuple[int, str]:
    """Evaluate gatekeeper signal (proximity only)."""
    spot = state.spot_price
    proximity_threshold = spot * 0.003  # 0.3% of spot

    gk_below = state.nearest_gk_below
    gk_above = state.nearest_gk_above

    if (
        gk_below is not None
        and state.per_strike_gex.get(gk_below, 0) > 0
        and (spot - gk_below) <= proximity_threshold
    ):
        return (1, 'CALL')

    if (
        gk_above is not None
        and state.per_strike_gex.get(gk_above, 0) < 0
        and (gk_above - spot) <= proximity_threshold
    ):
        return (1, 'PUT')

    return (0, 'NONE')


def _signal_velocity(state: GexSignalState) -> Tuple[int, str]:
    """Evaluate velocity signal."""
    VELOCITY_STRIKE_THRESHOLD = 0.05  # $B
    TOTAL_DELTA_THRESHOLD = 0.10      # $B

    score = 0
    direction_votes: Dict[str, int] = {'CALL': 0, 'PUT': 0}

    # Strike velocity component
    if state.top_velocity_value is not None and abs(state.top_velocity_value) >= VELOCITY_STRIKE_THRESHOLD:
        vel_strike = state.top_velocity_strike
        vel_value = state.top_velocity_value
        spot = state.spot_price

        if vel_strike is not None:
            if vel_strike > spot and vel_value > 0:
                direction_votes['CALL'] += 1  # positive gamma building above = magnetic pull up
            elif vel_strike > spot and vel_value < 0:
                direction_votes['PUT'] += 1   # negative gamma building above = resistance
            elif vel_strike < spot and vel_value > 0:
                direction_votes['PUT'] += 1   # support building below, spot may test it
            elif vel_strike < spot and vel_value < 0:
                direction_votes['CALL'] += 1  # negative gamma shed below = less resistance below

    # Total delta component
    if state.gamma_delta is not None and abs(state.gamma_delta) >= TOTAL_DELTA_THRESHOLD:
        if state.gamma_delta > 0:
            # Favors mean reversion
            if state.king_strike is not None and state.spot_price < state.king_strike:
                direction_votes['CALL'] += 1
            else:
                direction_votes['PUT'] += 1
        else:
            # Favors momentum: vote in direction of current king_node signal
            if state.king_gex > 0 and state.king_strike is not None and state.spot_price < state.king_strike:
                direction_votes['CALL'] += 1
            else:
                direction_votes['PUT'] += 1

    # Determine direction and score
    score = direction_votes['CALL'] + direction_votes['PUT']
    if direction_votes['CALL'] > direction_votes['PUT']:
        direction = 'CALL'
    elif direction_votes['PUT'] > direction_votes['CALL']:
        direction = 'PUT'
    else:
        direction = 'NONE'

    return (min(score, 2), direction)


def _signal_regime_alignment(state: GexSignalState, primary_direction: str) -> Tuple[int, str]:
    """Evaluate regime alignment signal."""
    if primary_direction == 'CALL' and state.regime in ('positive_stable',):
        return (1, 'CALL')
    elif primary_direction == 'PUT' and state.regime in ('negative_trending',):
        return (1, 'PUT')
    else:
        return (0, 'NONE')


def _signal_flip_proximity(state: GexSignalState) -> Tuple[int, bool]:
    """Evaluate flip proximity — returns (score, vetoed). Score may be negative."""
    if state.dist_to_flip_pct is None:
        return (0, False)  # no info, no penalty

    if state.dist_to_flip_pct < 0.3:
        return (0, True)   # VETO

    if state.dist_to_flip_pct < 0.5:
        return (-2, False)  # penalty

    return (0, False)


def evaluate(data: SymbolGexData) -> SignalResult:
    """Main entry point. Orchestrates the five signals."""
    state = build_signal_state(data)

    # Flip veto check first
    flip_score, vetoed = _signal_flip_proximity(state)
    if vetoed:
        return SignalResult(
            direction='NONE',
            conviction=0,
            signal_scores={'king_node': 0, 'gatekeeper': 0, 'velocity': 0, 'regime': 0, 'flip_penalty': 0},
            vetoed=True,
            regime=state.regime,
            timestamp=state.timestamp,
        )

    # Evaluate directional signals
    king_score, king_dir = _signal_king_node(state)
    gk_score, gk_dir = _signal_gatekeeper(state)
    vel_score, vel_dir = _signal_velocity(state)

    # Dead-signal veto: no structure (gatekeeper=0) AND no momentum (velocity=0).
    # King proximity alone is insufficient — price may be near the strike by chance.
    if gk_score == 0 and vel_score == 0:
        return SignalResult(
            direction='NONE',
            conviction=0,
            signal_scores={'king_node': king_score, 'gatekeeper': 0, 'velocity': 0, 'regime': 0, 'flip_penalty': flip_score},
            vetoed=True,
            regime=state.regime,
            timestamp=state.timestamp,
        )

    # Determine primary direction by weighted vote
    call_score = sum(s for s, d in [(king_score, king_dir), (gk_score, gk_dir), (vel_score, vel_dir)] if d == 'CALL')
    put_score  = sum(s for s, d in [(king_score, king_dir), (gk_score, gk_dir), (vel_score, vel_dir)] if d == 'PUT')

    if call_score == put_score or (call_score == 0 and put_score == 0):
        primary_direction = 'NONE'
    elif call_score > put_score:
        primary_direction = 'CALL'
    else:
        primary_direction = 'PUT'

    # Regime alignment (uses primary direction)
    regime_score, _ = _signal_regime_alignment(state, primary_direction)

    # Net conviction
    directional_score = max(call_score, put_score)
    raw_conviction = directional_score + regime_score + flip_score  # flip_score is 0 or negative
    conviction = max(0, raw_conviction)

    return SignalResult(
        direction=primary_direction if conviction >= 3 else 'NONE',
        conviction=conviction,
        signal_scores={
            'king_node': king_score,
            'gatekeeper': gk_score,
            'velocity': vel_score,
            'regime': regime_score,
            'flip_penalty': flip_score,
        },
        vetoed=False,
        regime=state.regime,
        timestamp=state.timestamp,
    )
