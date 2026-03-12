"""Position manager for the AutoGEX trading system.

Manages the full lifecycle of a trading position: sizing, stop management,
partial exits, time rules, and the daily circuit breaker. Does NOT place
orders — returns instructions for the trading engine to act on.
"""

import uuid
from datetime import datetime, time, date, timedelta
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple

import pytz

from config import AutoGexConfig, load_config
from signal_engine import SignalResult


@dataclass
class OpenPosition:
    trade_id: str                    # UUID string
    symbol: str                      # option contract symbol (OCC format)
    direction: str                   # 'CALL' or 'PUT'
    strike: float
    expiration: str                  # YYYY-MM-DD
    entry_price: float               # per-contract price in dollars
    entry_time: datetime
    total_qty: int                   # total contracts bought
    remaining_qty: int               # contracts still open
    tranche_a_qty: int               # = total_qty // 2
    tranche_b_qty: int               # = total_qty - tranche_a_qty
    tranche_a_closed: bool = False
    high_water_mark: float = 0.0     # highest option price seen since entry
    current_stop: float = 0.0        # current stop price
    entry_conviction: int = 0
    entry_signals: Dict = field(default_factory=dict)
    gatekeeper_cleared: bool = False  # True once spot clears next gatekeeper
    nearest_gk_in_direction: Optional[float] = None  # the gatekeeper to watch for breakeven trigger


@dataclass
class PositionAction:
    trade_id: str
    action: str          # 'hold', 'sell_tranche_a', 'stop_out', 'hard_close', 'update_stop'
    qty: int             # contracts to sell (0 if action is 'hold' or 'update_stop')
    reason: str          # human-readable reason
    new_stop: Optional[float] = None   # set when action is 'update_stop' or alongside a sell


class PositionManager:
    """Manages position lifecycle for the AutoGEX trading system."""

    def __init__(self, cfg: AutoGexConfig = None):
        self.cfg = cfg or load_config()
        self.positions: Dict[str, OpenPosition] = {}  # keyed by trade_id
        self.trades_today: int = 0
        self.daily_realized_pnl: float = 0.0
        self.circuit_breaker_active: bool = False
        self.cooldown_until: Optional[datetime] = None  # single cooldown tracker
        self._eastern = pytz.timezone("US/Eastern")

    def _now_et(self) -> datetime:
        """Return current datetime in US/Eastern."""
        return datetime.now(self._eastern)

    def _time_et(self) -> time:
        """Return current time component in US/Eastern."""
        return self._now_et().time()

    def can_enter(self) -> Tuple[bool, str]:
        """Return (True, '') if a new trade can be opened, (False, reason) otherwise."""
        if self.circuit_breaker_active:
            return (False, "Circuit breaker active")

        if self.trades_today >= self.cfg.max_trades_per_day:
            return (False, "Max trades reached")

        cutoff_h, cutoff_m = map(int, self.cfg.no_new_entries_after.split(":"))
        cutoff = time(cutoff_h, cutoff_m)
        if self._time_et() >= cutoff:
            return (False, "Past entry cutoff")

        if self.cooldown_until is not None and self._now_et() < self.cooldown_until:
            return (False, f"Cooldown until {self.cooldown_until.strftime('%H:%M:%S')}")

        return (True, "")

    def compute_block_size(self, conviction: int, option_price: float) -> int:
        """Compute the number of contracts to buy based on conviction and option price."""
        scale = (min(conviction, 9) - 3) / 6   # 0.0 at conviction=3, 1.0 at conviction=9
        raw = self.cfg.min_block + scale * (self.cfg.max_block - self.cfg.min_block)
        max_by_risk = self.cfg.max_risk_per_trade / (option_price * 100)
        size = int(min(raw, max_by_risk))
        size = size if size % 2 == 0 else size - 1
        return max(2, size)

    def effective_conviction(self, conviction: int) -> int:
        """Return conviction adjusted for AM window bonus."""
        t = self._time_et()
        start_h, start_m = map(int, self.cfg.am_window_start.split(":"))
        end_h, end_m = map(int, self.cfg.am_window_end.split(":"))
        am_start = time(start_h, start_m)
        am_end = time(end_h, end_m)
        if am_start <= t < am_end:
            return conviction + self.cfg.am_conviction_bonus
        return conviction

    def open_position(
        self,
        signal: SignalResult,
        symbol: str,
        strike: float,
        expiration: str,
        entry_price: float,
        nearest_gk: Optional[float] = None,
    ) -> OpenPosition:
        """Create and register a new OpenPosition. Raises RuntimeError if entry not allowed."""
        ok, reason = self.can_enter()
        if not ok:
            raise RuntimeError(f"Cannot enter position: {reason}")

        effective_conv = self.effective_conviction(signal.conviction)
        qty = self.compute_block_size(effective_conv, entry_price)

        trade_id = str(uuid.uuid4())
        tranche_a_qty = qty // 2
        tranche_b_qty = qty - tranche_a_qty

        position = OpenPosition(
            trade_id=trade_id,
            symbol=symbol,
            direction=signal.direction,
            strike=strike,
            expiration=expiration,
            entry_price=entry_price,
            entry_time=self._now_et(),
            total_qty=qty,
            remaining_qty=qty,
            tranche_a_qty=tranche_a_qty,
            tranche_b_qty=tranche_b_qty,
            high_water_mark=entry_price,
            current_stop=round(entry_price * (1 - self.cfg.initial_stop_pct), 2),
            entry_conviction=effective_conv,
            entry_signals=signal.signal_scores,
            nearest_gk_in_direction=nearest_gk,
        )

        self.positions[trade_id] = position
        self.trades_today += 1
        self.cooldown_until = self._now_et() + timedelta(seconds=self.cfg.cooldown_after_entry)

        return position

    def evaluate_position(
        self,
        pos: OpenPosition,
        current_price: float,
        spot_price: float,
    ) -> List[PositionAction]:
        """Per-tick evaluation of a position. Returns list of actions to execute."""
        actions: List[PositionAction] = []

        # 1. Update high-water mark
        if current_price > pos.high_water_mark:
            pos.high_water_mark = current_price

        # 2. Hard close check
        hc_h, hc_m = map(int, self.cfg.hard_close_time.split(":"))
        hard_close = time(hc_h, hc_m)
        past_hard_close = self._time_et() >= hard_close

        # 3. Stop check (evaluated before hard close so stop protection always fires)
        if current_price <= pos.current_stop:
            self.cooldown_until = self._now_et() + timedelta(seconds=self.cfg.cooldown_after_stop)
            return [PositionAction(
                pos.trade_id,
                'stop_out',
                pos.remaining_qty,
                f'Stop hit at {current_price:.2f}',
            )]

        if past_hard_close:
            return [PositionAction(pos.trade_id, 'hard_close', pos.remaining_qty, 'End of day')]

        # 4. Gatekeeper breakeven trigger (only if not already triggered)
        if not pos.gatekeeper_cleared and pos.nearest_gk_in_direction is not None:
            triggered = False
            if pos.direction == 'CALL' and spot_price >= pos.nearest_gk_in_direction:
                triggered = True
            elif pos.direction == 'PUT' and spot_price <= pos.nearest_gk_in_direction:
                triggered = True

            if triggered:
                pos.gatekeeper_cleared = True
                new_stop = round(pos.entry_price + self.cfg.breakeven_buffer, 2)
                if new_stop > pos.current_stop:
                    pos.current_stop = new_stop
                    actions.append(PositionAction(
                        pos.trade_id,
                        'update_stop',
                        0,
                        'Breakeven: gatekeeper cleared',
                        new_stop=new_stop,
                    ))

        # 5. Tranche A exit (only if not already closed)
        if not pos.tranche_a_closed:
            target_price = round(pos.entry_price * (1 + self.cfg.tranche_a_target_pct), 2)
            if current_price >= target_price:
                pos.tranche_a_closed = True
                actions.append(PositionAction(
                    pos.trade_id,
                    'sell_tranche_a',
                    pos.tranche_a_qty,
                    f'Tranche A target hit at {current_price:.2f}',
                ))

        # 6. Trailing stop update for Tranche B (only after Tranche A is closed)
        if pos.tranche_a_closed:
            t = self._time_et()
            final_h, final_m = map(int, self.cfg.hard_close_time.split(":"))

            if t >= time(15, 0):
                trail_pct = self.cfg.trail_pct_final
            elif t >= time(13, 30):
                trail_pct = self.cfg.trail_pct_afternoon
            else:
                trail_pct = self.cfg.trail_pct_am

            trail_stop = round(pos.high_water_mark * (1 - trail_pct), 2)
            if trail_stop > pos.current_stop:
                pos.current_stop = trail_stop
                actions.append(PositionAction(
                    pos.trade_id,
                    'update_stop',
                    0,
                    f'Trail stop updated to {trail_stop:.2f}',
                    new_stop=trail_stop,
                ))

        return actions

    def close_position(self, trade_id: str, realized_pnl: float, reason: str) -> None:
        """Record realized P&L, remove position, and check circuit breaker."""
        self.daily_realized_pnl += realized_pnl
        self.positions.pop(trade_id, None)
        if self.daily_realized_pnl <= -self.cfg.daily_loss_limit:
            self.circuit_breaker_active = True
            print(f"[PositionManager] Circuit breaker triggered. Daily P&L: {self.daily_realized_pnl:.2f}")

    def get_nearest_gatekeeper(
        self,
        signal: SignalResult,
        gex_state,
        direction: str,
    ) -> Optional[float]:
        """Return the gatekeeper level the position should watch for breakeven trigger."""
        if direction == 'CALL':
            return gex_state.nearest_gk_above
        elif direction == 'PUT':
            return gex_state.nearest_gk_below
        return None

    def reset_daily(self) -> None:
        """Reset daily counters for a new trading day. Does not clear open positions."""
        self.trades_today = 0
        self.daily_realized_pnl = 0.0
        self.circuit_breaker_active = False
