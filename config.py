import json
import os
from pathlib import Path
from dataclasses import dataclass, asdict

CONFIG_PATH = Path(__file__).resolve().parent / "autogex_config.json"

@dataclass
class AutoGexConfig:
    # Signal thresholds
    min_conviction: int = 3           # minimum conviction score to enter a trade
    am_conviction_bonus: int = 1      # conviction threshold reduced by this during AM window
    velocity_strike_threshold: float = 0.05   # $B — minimum |top_velocity_value| to score
    total_delta_threshold: float = 0.10       # $B — minimum |gamma_delta| to score
    flip_veto_pct: float = 0.3        # dist_to_flip_pct below this → veto
    flip_penalty_pct: float = 0.5     # dist_to_flip_pct below this → -2 penalty

    # Position sizing
    max_risk_per_trade: float = 2000.0   # dollars
    min_block: int = 4                   # minimum contracts (always even)
    max_block: int = 12                  # maximum contracts (always even)

    # Stop loss
    initial_stop_pct: float = 0.50       # 50% below entry price
    breakeven_buffer: float = 0.05       # $0.05 above entry when breakeven triggered
    trail_pct_am: float = 0.30           # trailing stop % before 1:30 PM ET
    trail_pct_afternoon: float = 0.15    # trailing stop % after 1:30 PM ET
    trail_pct_final: float = 0.10        # trailing stop % after 3:00 PM ET

    # Partial exits
    tranche_a_target_pct: float = 0.30   # sell Tranche A at +30% gain
    tranche_a_pct: float = 0.60          # fraction of position to sell at Tranche A target

    # Time rules (24h ET, as "HH:MM" strings)
    am_window_start: str = "09:35"
    am_window_end: str = "11:30"
    no_new_entries_after: str = "14:30"
    hard_close_time: str = "15:55"

    # Daily limits
    max_trades_per_day: int = 5
    max_concurrent_positions: int = 3    # hard cap on simultaneously open positions
    daily_loss_limit: float = 2000.0     # circuit breaker threshold (dollars, positive)

    # Cooldowns (seconds)
    cooldown_after_entry: int = 300
    cooldown_after_stop: int = 300
    cooldown_after_flip: int = 60

    # Engine
    poll_interval_seconds: int = 7
    spy_underlying: str = "SPY"
    use_0dte: bool = True               # prefer 0DTE; falls back to 1DTE if unavailable
    dry_run: bool = True                # if True, log signals but place no real orders
    strike_count: int = 50              # number of strikes to fetch per side


def load_config() -> AutoGexConfig:
    """Load config from autogex_config.json if it exists, else return defaults."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
            # Only apply keys that exist in the dataclass
            valid = {k: v for k, v in data.items() if k in AutoGexConfig.__dataclass_fields__}
            return AutoGexConfig(**valid)
        except Exception as e:
            print(f"[config] Failed to load {CONFIG_PATH}: {e}. Using defaults.")
    return AutoGexConfig()


def save_config(cfg: AutoGexConfig) -> None:
    """Persist config to autogex_config.json."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(asdict(cfg), f, indent=2)
