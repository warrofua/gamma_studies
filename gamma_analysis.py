"""Utilities for calculating gamma exposure metrics."""

import datetime
from typing import Dict, Tuple, List, Optional


GammaCalculationResult = Tuple[
    float,
    Dict[float, float],
    Dict[float, float],
    List[Tuple[float, float, datetime.datetime]],
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
    largest_changes_with_time = sorted(
        change_in_gamma_per_strike.items(), key=lambda item: abs(item[1]), reverse=True
    )[:5]
    largest_changes = [
        (
            strike,
            change,
            time_of_change_per_strike[strike],
        )
        for strike, change in largest_changes_with_time
    ]

    return (
        total_gamma_exposure,
        per_strike_gamma_exposure,
        change_in_gamma_per_strike,
        largest_changes,
        spot_price,
    )


def get_per_strike_details(data: Dict) -> Dict[float, Dict]:
    """Extract per-strike OI, gamma contribution, and volume for tooltips.

    Returns a dict mapping strike -> {oi, gamma_sum, volume, call_oi, put_oi}.
    """
    details: Dict[float, Dict] = {}

    def add(strike: str, option_type: str, gamma: float, volume: float, oi: float) -> None:
        try:
            k = float(strike)
            if k not in details:
                details[k] = {
                    "oi": 0,
                    "gamma_sum": 0.0,
                    "volume": 0,
                    "call_oi": 0,
                    "put_oi": 0,
                }
            details[k]["oi"] += oi
            details[k]["volume"] += volume
            details[k]["gamma_sum"] += gamma * volume
            if option_type == "call":
                details[k]["call_oi"] += oi
            else:
                details[k]["put_oi"] += oi
        except (ValueError, TypeError):
            pass

    for _, strikes in data.get("callExpDateMap", {}).items():
        for strike, options in strikes.items():
            for opt in options:
                vol = opt.get("totalVolume", 0) or 0
                oi = opt.get("openInterest", vol) or vol
                add(strike, "call", opt.get("gamma", 0) or 0, vol, oi)

    for _, strikes in data.get("putExpDateMap", {}).items():
        for strike, options in strikes.items():
            for opt in options:
                vol = opt.get("totalVolume", 0) or 0
                oi = opt.get("openInterest", vol) or vol
                add(strike, "put", opt.get("gamma", 0) or 0, vol, oi)

    return details
