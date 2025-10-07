"""Utilities for calculating gamma exposure metrics."""

import datetime
from typing import Dict, Tuple, List, Optional


GammaCalculationResult = Tuple[
    float,
    Dict[float, float],
    Dict[float, float],
    List[Tuple[float, float, str]],
    float,
]


def calculate_gamma_exposure(
    data: Dict,
    previous_gamma_exposure: Optional[Dict[float, float]] = None,
) -> GammaCalculationResult:
    """Compute gamma exposure statistics for the provided option chain data."""

    previous_gamma_exposure = previous_gamma_exposure or {}
    per_strike_gamma_exposure: Dict[float, float] = {}
    change_in_gamma_per_strike: Dict[float, float] = {}
    time_of_change_per_strike: Dict[float, datetime.datetime] = {}
    contract_size = 100
    spot_price = data.get("underlyingPrice", 0)
    calculation_time = datetime.datetime.now()

    def add_gamma_exposure(option_type: str, strike: str, gamma: float, volume: float) -> None:
        try:
            multiplier = 1 if option_type == "call" else -1
            gamma_exposure = (
                multiplier
                * spot_price
                * gamma
                * volume
                * contract_size
                * spot_price
                * 0.01
                / 1000000000
            )
            strike_value = float(strike)

            per_strike_gamma_exposure[strike_value] = (
                per_strike_gamma_exposure.get(strike_value, 0.0) + gamma_exposure
            )

            if strike_value in previous_gamma_exposure and previous_gamma_exposure[strike_value] != 0:
                previous_exposure = previous_gamma_exposure[strike_value]
                change = per_strike_gamma_exposure[strike_value] - previous_exposure
                change_in_gamma_per_strike[strike_value] = change
                time_of_change_per_strike[strike_value] = calculation_time

        except Exception as exc:  # pragma: no cover - defensive logging
            print(f"Strike {strike} included incompatible data: {exc}")

    for _, strikes in data.get("callExpDateMap", {}).items():
        for strike, options in strikes.items():
            for option in options:
                add_gamma_exposure("call", strike, option["gamma"], option["totalVolume"])

    for _, strikes in data.get("putExpDateMap", {}).items():
        for strike, options in strikes.items():
            for option in options:
                add_gamma_exposure("put", strike, option["gamma"], option["totalVolume"])

    total_gamma_exposure = sum(per_strike_gamma_exposure.values())
    print(total_gamma_exposure)
    largest_changes_with_time = sorted(
        change_in_gamma_per_strike.items(), key=lambda item: abs(item[1]), reverse=True
    )[:5]
    largest_changes = [
        (
            strike,
            change,
            time_of_change_per_strike[strike].strftime("%Y-%m-%d %H:%M:%S"),
        )
        for strike, change in largest_changes_with_time
    ]
    print(largest_changes)

    return (
        total_gamma_exposure,
        per_strike_gamma_exposure,
        change_in_gamma_per_strike,
        largest_changes,
        spot_price,
    )
