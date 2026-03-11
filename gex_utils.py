"""Shared utilities for gamma exposure analysis."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pytz

from main import GammaExposureScheduler
from gamma_analysis import calculate_gamma_exposure, get_per_strike_details


def fetch_options_and_gex(
    client,
    option_symbol: str,
    strike_count: int,
    previous_gamma: Optional[Dict[float, float]] = None,
    client_module=None,
) -> Tuple[Optional[Tuple[Dict, float, Dict[float, float], Dict[float, Dict], float, date, float, bool, Dict[float, float]]], Optional[str]]:
    """Fetch option chain, compute GEX.
    Returns (result_tuple, error_message). Result is None on failure."""
    eastern = pytz.timezone("US/Eastern")
    now = datetime.now(eastern)

    if now.weekday() == 4 and now.hour >= 16:
        from_date = (now + timedelta(days=3)).date()
    else:
        from_date = now.date() + timedelta(days=1 if now.hour >= 16 else 0)
    # Equity options expire weekly (Fri) and monthly; indices like SPX can have daily exp.
    # Widen range so we get the next expiration for equities when "today" has none.
    to_date = from_date + timedelta(days=21)

    options_source = getattr(client, "get_option_chain", None)
    if not options_source:
        return None, "Client has no get_option_chain method"

    contract_type_all = None
    if client_module:
        try:
            options_source = getattr(client_module, "Options", None) or getattr(client, "Options", None)
            if options_source is not None:
                contract_type_all = getattr(options_source, "ContractType", None)
                if contract_type_all is not None:
                    contract_type_all = getattr(contract_type_all, "ALL", contract_type_all)
        except Exception:
            pass

    kwargs = {
        "symbol": option_symbol,
        "from_date": from_date,
        "to_date": to_date,
        "strike_count": strike_count,
    }
    if contract_type_all is not None:
        kwargs["contract_type"] = contract_type_all

    def _do_request():
        return client.get_option_chain(**kwargs)

    try:
        r = _do_request()
    except Exception as e:
        return None, str(e)

    # On 401, try one refresh and retry (handles stale cached client).
    if r.status_code == 401:
        try:
            GammaExposureScheduler._proactive_schwab_token_refresh(client)
            r = _do_request()
        except Exception:
            pass

    if r.status_code != 200:
        body = r.text
        try:
            err = r.json()
            body = err.get("message", err.get("error", body))
        except Exception:
            pass
        return None, f"API error {r.status_code}: {body}"

    data = r.json()

    # Use only the nearest expiration (equities often have no exp on Wed; indices may have daily)
    def _parse_exp_date(key: str) -> Optional[date]:
        try:
            part = (key.split(":")[0] if ":" in key else key)[:10]
            if len(part) >= 10 and part[4] == "-" and part[7] == "-":
                return datetime.strptime(part[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            pass
        return None

    today = now.date()
    call_map = data.get("callExpDateMap", {})
    put_map = data.get("putExpDateMap", {})
    all_keys = list(call_map.keys()) + list(put_map.keys())
    exp_dates = sorted({d for k in all_keys if (d := _parse_exp_date(k)) is not None and d >= today})
    use_date = exp_dates[0] if exp_dates else from_date

    # Filter to nearest expiration only (cleaner single-expiration GEX)
    def _filter_to_exp(m: dict, exp: date) -> dict:
        exp_str = exp.strftime("%Y-%m-%d")
        return {k: v for k, v in m.items() if k.startswith(exp_str)}
    data_filtered = dict(data)
    data_filtered["callExpDateMap"] = _filter_to_exp(call_map, use_date) if exp_dates else call_map
    data_filtered["putExpDateMap"] = _filter_to_exp(put_map, use_date) if exp_dates else put_map
    if not data_filtered["callExpDateMap"] and not data_filtered["putExpDateMap"]:
        data_filtered = data  # fallback: use all expirations if filter left nothing

    (
        total_gex,
        per_strike_gex,
        change_in_gamma,
        _largest_changes,
        spot_price,
    ) = calculate_gamma_exposure(data_filtered, previous_gamma or {})
    gamma_delta = sum(change_in_gamma.values()) if change_in_gamma else 0.0
    is_first_fetch = not bool(previous_gamma or {})
    details = get_per_strike_details(data_filtered)
    return (data_filtered, total_gex, per_strike_gex, details, spot_price, use_date, gamma_delta, is_first_fetch, change_in_gamma), None


@dataclass
class SymbolGexData:
    """Processed GEX data for one symbol."""
    symbol: str
    label: str
    spot_price: float
    total_gex: float
    exp_date: date
    per_strike_gex: Dict[float, float]
    strike_details: Dict[float, Dict]
    strikes: List[float]
    king_strike: Optional[float]
    king_gex: float
    downside_defense: List[float]
    upside_resistance: List[float]
    gatekeeper_strikes: set
    gamma_flip_strike: Optional[float]
    dist_to_king: Optional[float]
    nearest_gk_below: Optional[float]
    nearest_gk_above: Optional[float]
    dist_to_flip_pct: Optional[float] = None
    points_to_flip: Optional[float] = None
    gamma_delta: Optional[float] = None
    is_first_fetch: bool = False
    top_strike_velocity_strike: Optional[float] = None
    top_strike_velocity_value: Optional[float] = None
    prev_gamma_velocity: Optional[float] = None
    prev_fetch_timestamp: Optional[datetime] = None


def _effective_strike_range(strike_range: int, spot_price: float) -> int:
    """Scale strike window for lower-priced underlyings (SPY, QQQ).
    Use 50% of spot for SPY/QQQ so ~±300 for SPY at 600, giving ~$1 strikes
    full visibility. Previously 20% gave only ±120 which was too narrow."""
    if spot_price >= 1000:
        return strike_range
    return min(strike_range, max(100, int(spot_price * 0.5)))


def process_symbol_gex(
    result: Tuple,
    strike_range: int,
    gex_min_threshold: float,
) -> Optional[SymbolGexData]:
    """Process fetch result into SymbolGexData. Returns None if no usable strikes."""
    _data, total_gex, per_strike_gex, strike_details, spot_price, exp_date, gamma_delta, is_first_fetch, change_in_gamma = result
    all_strikes = sorted(per_strike_gex.keys(), reverse=True)
    if not all_strikes:
        return None

    eff_range = _effective_strike_range(strike_range, spot_price)
    strikes_in_window = [s for s in all_strikes if abs(s - spot_price) <= eff_range]
    if not strikes_in_window:
        strikes_in_window = all_strikes
    # Noise floor: never show $0.00B GEX strikes (common on SPY/QQQ with many low-OI strikes)
    NOISE_FLOOR = 0.01
    strikes = [
        s for s in strikes_in_window
        if abs(per_strike_gex[s]) >= max(gex_min_threshold, NOISE_FLOOR)
    ]
    if not strikes:
        strikes = [s for s in strikes_in_window if abs(per_strike_gex[s]) >= NOISE_FLOOR] or strikes_in_window
    # If GEX threshold filters down to very few strikes, show more range but still exclude zeros
    if len(strikes) < 15 and len(strikes_in_window) > len(strikes):
        expanded = [s for s in strikes_in_window if abs(per_strike_gex[s]) >= NOISE_FLOOR]
        if expanded:
            strikes = expanded

    sorted_by_abs = sorted(
        [(s, per_strike_gex[s]) for s in strikes],
        key=lambda x: abs(x[1]),
        reverse=True,
    )
    king_strike = sorted_by_abs[0][0] if sorted_by_abs else None
    king_gex = per_strike_gex.get(king_strike, 0) if king_strike else 0

    downside_defense = [
        s for s, _ in sorted(
            [(s, per_strike_gex[s]) for s in strikes if s < spot_price and per_strike_gex[s] > 0],
            key=lambda x: x[1],
            reverse=True,
        )[:3]
    ]
    upside_resistance = [
        s for s, _ in sorted(
            [(s, per_strike_gex[s]) for s in strikes if s > spot_price and per_strike_gex[s] < 0],
            key=lambda x: x[1],
        )[:3]
    ]
    top_positive = [s for s, _ in sorted(per_strike_gex.items(), key=lambda x: x[1], reverse=True) if s in strikes][:3]
    top_negative = [s for s, _ in sorted(per_strike_gex.items(), key=lambda x: x[1]) if s in strikes][:3]
    gatekeeper_strikes = set(downside_defense + upside_resistance + top_positive + top_negative)

    dist_to_king = abs(spot_price - king_strike) if king_strike else None
    nearest_gk_below = min((s for s in gatekeeper_strikes if s < spot_price), key=lambda x: spot_price - x, default=None)
    nearest_gk_above = min((s for s in gatekeeper_strikes if s > spot_price), key=lambda x: x - spot_price, default=None)

    # Gamma flip: use full chain (all_strikes) so cumulative can cross zero.
    # Strikes_in_window may be too narrow—zero-crossing often occurs at far OTM strikes.
    all_strikes_asc = sorted(all_strikes)
    prev_cum, cum = 0, 0
    gamma_flip_strike = None
    for s in all_strikes_asc:
        prev_cum = cum
        cum += per_strike_gex[s]
        if prev_cum != 0 and (prev_cum > 0) != (cum > 0):
            gamma_flip_strike = s
            break

    dist_to_flip_pct = (
        abs(spot_price - gamma_flip_strike) / spot_price * 100
        if gamma_flip_strike is not None and spot_price > 0
        else None
    )
    points_to_flip = (
        abs(spot_price - gamma_flip_strike)
        if gamma_flip_strike is not None
        else None
    )
    # Strike gaining gamma the fastest (largest positive change)
    top_strike_velocity_strike = None
    top_strike_velocity_value = None
    if change_in_gamma:
        positive_changes = [(s, v) for s, v in change_in_gamma.items() if v > 0]
        if positive_changes:
            top_strike_velocity_strike, top_strike_velocity_value = max(positive_changes, key=lambda x: x[1])
        else:
            # Fallback: strike with least negative change (biggest draw)
            top_strike_velocity_strike, top_strike_velocity_value = max(change_in_gamma.items(), key=lambda x: x[1])

    return SymbolGexData(
        symbol="",
        label="",
        spot_price=spot_price,
        total_gex=total_gex,
        exp_date=exp_date,
        per_strike_gex=per_strike_gex,
        strike_details=strike_details,
        strikes=strikes,
        king_strike=king_strike,
        king_gex=king_gex,
        downside_defense=downside_defense,
        upside_resistance=upside_resistance,
        gatekeeper_strikes=gatekeeper_strikes,
        gamma_flip_strike=gamma_flip_strike,
        dist_to_king=dist_to_king,
        nearest_gk_below=nearest_gk_below,
        nearest_gk_above=nearest_gk_above,
        dist_to_flip_pct=dist_to_flip_pct,
        points_to_flip=points_to_flip,
        gamma_delta=gamma_delta,
        is_first_fetch=is_first_fetch,
        top_strike_velocity_strike=top_strike_velocity_strike,
        top_strike_velocity_value=top_strike_velocity_value,
    )
