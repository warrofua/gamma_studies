"""Streamlit dashboard for gamma exposure analytics.

Replaces matplotlib plots with an interactive Plotly heatmap.
Run with: streamlit run dashboard.py
"""

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from typing import Dict, List, Optional, Tuple

import csv
import urllib.request

import numpy as np
import pytz
import streamlit as st
import plotly.graph_objects as go

SP500_CSV_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv"


def _format_time_ago(seconds: float) -> str:
    """Format elapsed seconds as human-readable 'X min ago' or 'Xh Ym ago'."""
    if seconds < 60:
        return "< 1 min ago"
    if seconds < 3600:
        mins = int(seconds / 60)
        return f"{mins} min ago"
    if seconds < 86400:
        hours = int(seconds / 3600)
        mins = int((seconds % 3600) / 60)
        if mins == 0:
            return f"{hours}h ago"
        return f"{hours}h {mins}m ago"
    days = int(seconds / 86400)
    return f"{days}d ago"


@st.cache_data(ttl=86400)
def get_sp500_symbols() -> List[str]:
    """Fetch S&P 500 constituent symbols. Returns sorted list of tickers."""
    try:
        with urllib.request.urlopen(SP500_CSV_URL) as resp:
            reader = csv.DictReader(resp.read().decode().splitlines())
            symbols = [row["Symbol"] for row in reader if row.get("Symbol")]
        return sorted(set(symbols))
    except Exception:
        return []


def display_to_api_symbol(label: str) -> str:
    """Map display label to API symbol (Schwab uses $SPX for the index)."""
    return "$SPX" if label == "SPX" else label


from main import (
    _load_broker_client,
    BrokerConfigurationError,
    GammaExposureScheduler,
)
from gamma_analysis import calculate_gamma_exposure, get_per_strike_details


# --- Session-state client (persist across reruns) ---
@st.cache_resource
def get_authenticated_client():
    """Load broker and authenticate once per session."""
    try:
        broker_name, auth_module, client_module, secrets = _load_broker_client(
            os.environ.get("BROKER")
        )
        scheduler = GammaExposureScheduler(os.environ.get("BROKER"))
        scheduler.authenticate()
        default_symbol = getattr(secrets, "option_symbol", "$SPX.X")
        return scheduler.client, broker_name, default_symbol, client_module
    except BrokerConfigurationError as e:
        st.error(str(e))
        return None, None, "$SPX.X", None


def generate_gex_interpretation(
    spot_price: float,
    total_gex: float,
    king_strike: Optional[float],
    downside_defense: List[float],
    upside_resistance: List[float],
    per_strike_gex: Dict[float, float],
) -> str:
    """Generate a dynamically tailored interpretation of the GEX dashboard."""
    sentences = []

    # Sentence 1: Overall context with magnitude nuance
    net = "positive" if total_gex > 0 else "negative"
    abs_total = abs(total_gex)
    mag = "elevated" if abs_total > 20 else "moderate" if abs_total > 5 else "modest"
    sentences.append(
        f"Spot at ${spot_price:.0f} with {net} total GEX of ${abs_total:.2f}B ({mag} dealer gamma); "
        f"dealers are {'long gamma—expect mean reversion and dampened moves' if total_gex > 0 else 'short gamma—momentum can extend as hedging amplifies direction'}."
    )

    # Sentence 2: King Node with relative dominance
    if king_strike is not None:
        king_gex = per_strike_gex.get(king_strike, 0)
        king_role = "support" if king_gex > 0 else "resistance"
        dist = spot_price - king_strike
        second = sorted(
            [(s, abs(per_strike_gex[s])) for s in per_strike_gex if s != king_strike],
            key=lambda x: x[1],
            reverse=True,
        )
        ratio = ""
        if second:
            r = abs(king_gex) / second[0][1] if second[0][1] else 0
            if r > 2:
                ratio = f"—{r:.1f}x the next level—"
        sentences.append(
            f"The King Node at ${king_strike:.0f} (${abs(king_gex):.2f}B {king_role}) {ratio} "
            f"dominates; spot is {abs(dist):.0f} pts {'above' if dist > 0 else 'below'}, so expect "
            f"{'support and bounce risk' if king_gex > 0 else 'resistance and rejection risk'} on approach."
        )
    else:
        sentences.append("No dominant King Node in the viewed range; gamma is distributed across multiple strikes.")

    # Sentence 3: Downside defense and upside resistance (actual roles, not generic gatekeepers)
    parts = []
    if downside_defense:
        dd_str = ", ".join(f"${s:.0f} (${per_strike_gex[s]:.2f}B)" for s in downside_defense[:2])
        parts.append(f"Downside defense at {dd_str}")
    if upside_resistance:
        ur_str = ", ".join(f"${s:.0f} (${abs(per_strike_gex[s]):.2f}B)" for s in upside_resistance[:2])
        parts.append(f"Upside resistance at {ur_str}")
    if parts:
        sentences.append(
            "Focal levels: " + " and ".join(parts) + ". "
            "These act as magnets or walls depending on dealer delta hedging as price approaches."
        )

    # Sentence 4: Nearest level and break implication (use actual distances)
    refs = [s for s in ([king_strike] if king_strike else []) + downside_defense + upside_resistance if s]
    if refs:
        nearest = min(refs, key=lambda s: abs(s - spot_price))
        pts = abs(spot_price - nearest)
        direction = "up" if spot_price < nearest else "down"
        g = per_strike_gex.get(nearest, 0)
        sentences.append(
            f"Nearest key level: ${nearest:.0f} ({pts:.0f} pts {direction}); "
            f"a break {'above' if direction == 'up' else 'below'} flips dealer hedging and may accelerate."
        )

    return " ".join(sentences)


def generate_trader_suggestions(
    spot_price: float,
    total_gex: float,
    king_strike: Optional[float],
    downside_defense: List[float],
    upside_resistance: List[float],
    per_strike_gex: Dict[float, float],
) -> str:
    """Generate data-driven trading suggestions from the GEX profile."""
    sentences = []

    refs = [s for s in ([king_strike] if king_strike else []) + downside_defense + upside_resistance if s]
    near_pct = 0.015  # within 1.5% of spot = "near"
    near_threshold = spot_price * near_pct

    # Suggestion 1: Concrete levels with GEX values
    if refs:
        levels = sorted(set(refs), reverse=True)
        levels_str = ", ".join(f"${s:.0f}" for s in levels[:5])
        sentences.append(
            f"Watch {levels_str} as decision points: "
            f"{'fade extensions toward positive-GEX strikes, trail stops through negative-GEX resistance' if total_gex != 0 else 'expect chop until a gamma level breaks'}."
        )
    else:
        sentences.append("Focus on the highest-OI strikes in the chain for likely support and resistance.")

    # Suggestion 2: Fade vs follow—tailored to magnitude
    if total_gex > 0:
        sentences.append(
            f"Positive GEX (${total_gex:.2f}B) favors fading extended wicks; "
            "consider selling premium or mean-reversion entries near gatekeeper support."
        )
    elif total_gex < 0:
        sentences.append(
            f"Negative GEX (${abs(total_gex):.2f}B) favors momentum—avoid fading breakouts; "
            "wait for confirmation before adding, and use gamma levels as invalidation."
        )

    # Suggestion 3: Spot-specific idea (scale-aware "near")
    if king_strike is not None:
        dist = spot_price - king_strike
        if abs(dist) <= near_threshold:
            sentences.append(
                f"Spot is within {abs(dist):.0f} pts of the King Node (${king_strike:.0f})—range likely; "
                "reduce size or widen stops until a break confirms direction."
            )
        elif downside_defense and spot_price > max(downside_defense):
            dd_near = min(downside_defense, key=lambda s: abs(s - spot_price))
            pts = spot_price - dd_near
            sentences.append(
                f"Downside defense at ${dd_near:.0f} ({pts:.0f} pts below)— "
                f"pullbacks may hold; break below ${dd_near:.0f} opens follow-through lower."
            )
        elif upside_resistance and spot_price < min(upside_resistance):
            ur_near = min(upside_resistance, key=lambda s: abs(s - spot_price))
            pts = ur_near - spot_price
            sentences.append(
                f"Upside resistance at ${ur_near:.0f} ({pts:.0f} pts above)— "
                f"rallies may stall; break above ${ur_near:.0f} flips hedging and can extend."
            )
        else:
            direction = "above" if dist > 0 else "below"
            sentences.append(
                f"King Node ${king_strike:.0f} is {abs(dist):.0f} pts {direction} spot; "
                f"trade in that direction with a stop {'below' if dist > 0 else 'above'} the level."
            )

    # Suggestion 4: Risk (short, data-aware)
    nearest_break = min(refs, key=lambda s: abs(s - spot_price)) if refs else None
    if nearest_break:
        sentences.append(f"Use ${nearest_break:.0f} as a key level for stops and targets.")
    else:
        sentences.append("Size appropriately and use gamma clusters as stop references.")

    return " ".join(sentences)


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


def build_heatmap_fig(
    data: SymbolGexData,
    symbol_label: str,
    exp_date_str: str,
    height: int = 550,
    y_domain_max: float = 50.0,
) -> go.Figure:
    """Build a Plotly heatmap figure for given SymbolGexData.
    y_domain_max normalizes the y-axis so SPX, SPY, QQQ render at the same visual size."""
    strikes = data.strikes
    per_strike_gex = data.per_strike_gex
    strike_details = data.strike_details
    king_strike = data.king_strike
    downside_defense = data.downside_defense
    upside_resistance = data.upside_resistance
    gatekeeper_strikes = data.gatekeeper_strikes
    gamma_flip_strike = data.gamma_flip_strike
    spot_price = data.spot_price

    gex_values = [per_strike_gex[s] for s in strikes]
    max_abs = max(abs(g) for g in gex_values) if gex_values else 1

    # Normalize y to [0, y_domain_max] so all heatmaps have same visual scale (SPX/SPY/QQQ)
    # Low strike prices at bottom, high strike prices at top (strikes are ordered high->low)
    n = len(strikes)
    y_positions = np.linspace(y_domain_max, 0, n) if n > 1 else np.array([y_domain_max / 2])
    strike_to_y = {s: y_positions[i] for i, s in enumerate(strikes)}

    z = np.array([[g] for g in gex_values])
    gex_colorscale = [
        [0.0, "#4B0082"], [0.25, "#8B0000"], [0.5, "#F5F5F5"],
        [0.75, "#228B22"], [1.0, "#006400"],
    ]
    customdata = np.zeros((len(strikes), 1, 5))
    cell_texts = []
    for i, s in enumerate(strikes):
        d = strike_details.get(s, {})
        call_oi, put_oi = d.get("call_oi", 0), d.get("put_oi", 0)
        customdata[i, 0, :] = [s, per_strike_gex[s], d.get("oi", 0), call_oi, put_oi]
        t = f"${gex_values[i]:.2f}B"
        if s == king_strike:
            t += "\n👑 KING NODE"
        elif s in downside_defense:
            t += "\n↓ Downside defense"
        elif s in upside_resistance:
            t += "\n↑ Upside resistance"
        elif s in gatekeeper_strikes:
            t += "\nGatekeeper"
        cell_texts.append([t])

    fig = go.Figure(
        data=go.Heatmap(
            z=z,
            x=["GEX"],
            y=y_positions,
            text=cell_texts,
            texttemplate="%{text}",
            textfont=dict(size=10),
            colorscale=gex_colorscale,
            zmid=0,
            zmin=-max_abs,
            zmax=max_abs,
            showscale=False,
            hoverongaps=False,
            customdata=customdata,
            hovertemplate="Strike: %{customdata[0]:.0f}<br>GEX: $%{z:.3f}B<br>OI: %{customdata[2]:,.0f}<br>Call OI: %{customdata[3]:,.0f} / Put OI: %{customdata[4]:,.0f}<extra></extra>",
        )
    )

    strong_threshold = max_abs * 0.25 if max_abs > 0 else 0
    i = 0
    while i < len(strikes):
        j = i
        while j < len(strikes) and abs(gex_values[j]) >= strong_threshold:
            j += 1
        if j - i >= 3:
            y_top, y_bot = y_positions[i], y_positions[j - 1]
            fig.add_shape(
                type="rect",
                x0=-0.55, x1=0.55,
                y0=y_top, y1=y_bot,
                line=dict(color="rgba(255,165,0,0.8)", width=2, dash="dot"),
                fillcolor="rgba(255,165,0,0.08)",
                xref="x", yref="y",
            )
        i = j if j > i else i + 1

    if gamma_flip_strike is not None:
        _y_gf = strike_to_y.get(gamma_flip_strike)
        if _y_gf is None:
            _idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - gamma_flip_strike))
            _y_gf = y_positions[_idx]
        fig.add_shape(
            type="line",
            x0=-0.5, x1=0.5,
            y0=_y_gf, y1=_y_gf,
            line=dict(color="limegreen", width=2, dash="dash"),
            xref="x", yref="y",
        )
        fig.add_annotation(
            x=-0.5, y=_y_gf,
            text=f"Zero-Gamma Flip ${gamma_flip_strike:.0f}",
            showarrow=False,
            font=dict(color="limegreen", size=9),
            xref="x", yref="y",
            xanchor="right",
        )

    _y_spot = strike_to_y.get(spot_price)
    if _y_spot is None:
        _idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot_price))
        _y_spot = y_positions[_idx]
    fig.add_shape(
        type="line",
        x0=-0.5, x1=0.5,
        y0=_y_spot, y1=_y_spot,
        line=dict(color="red", width=4, dash="dash"),
        xref="x", yref="y",
    )
    fig.add_annotation(
        x=-0.5, y=_y_spot,
        text=f"Current Spot ${spot_price:.1f}",
        showarrow=False,
        font=dict(color="red", size=11, family="Arial Black"),
        xref="x", yref="y",
        xanchor="right",
    )

    max_ticks = 35
    if n <= max_ticks:
        tickvals = list(y_positions)
        ticktext = [f"{s:.0f}" for s in strikes]
    else:
        step = max(1, (n - 1) // (max_ticks - 1))
        tick_indices = list(range(0, n, step))
        if tick_indices[-1] != n - 1:
            tick_indices.append(n - 1)
        tickvals = [y_positions[i] for i in tick_indices]
        ticktext = [f"{strikes[i]:.0f}" for i in tick_indices]
    fig.update_layout(
        title=f"{symbol_label} — Expiring {exp_date_str}",
        xaxis_title="GEX ($B)",
        yaxis_title="Strike Price",
        yaxis=dict(
            range=[-2, y_domain_max + 2],
            tickmode="array",
            tickvals=tickvals,
            ticktext=ticktext,
            tickfont=dict(size=10),
        ),
        height=height,
        margin=dict(l=80, r=100),
    )
    return fig


def generate_combined_interpretation(
    symbol_data: Dict[str, SymbolGexData],
    confluence_alerts: List[str],
) -> str:
    """Generate a cross-symbol interpretation synthesizing SPX, SPY, QQQ together.
    SPX, SPY, and QQQ are highly correlated (index/ETF proxies for broad market and tech)."""
    spx = symbol_data.get("SPX")
    spy = symbol_data.get("SPY")
    qqq = symbol_data.get("QQQ")
    available = [d for d in [spx, spy, qqq] if d is not None]
    if len(available) < 2:
        return (
            "SPX, SPY, and QQQ are highly correlated; viewing multiple allows cross-checking gamma signals "
            "and identifying regime confluence. Add more symbols for full combined interpretation."
        )

    sentences = []

    # Correlation context
    sentences.append(
        "**SPX, SPY, and QQQ are highly correlated**—SPX is the index, SPY tracks it (~1/10 scale), "
        "and QQQ leans tech-heavy. When gamma regimes align across all three, the signal is more robust; "
        "divergences can indicate sector-specific positioning."
    )

    # Regime alignment or divergence
    signs = [(d.label, 1 if d.total_gex > 0 else -1 if d.total_gex < 0 else 0) for d in available]
    gex_strs = [f"{d.label} ${d.total_gex:.2f}B" for d in available]
    all_pos = all(s[1] == 1 for s in signs)
    all_neg = all(s[1] == -1 for s in signs)
    if all_pos:
        sentences.append(
            f"All show **positive GEX** ({', '.join(gex_strs)})—"
            "dealer long gamma across the board; expect mean reversion and dampened volatility in both broad market and tech."
        )
    elif all_neg:
        sentences.append(
            f"All show **negative GEX** ({', '.join(gex_strs)})—"
            "dealers short gamma; momentum can extend in either direction."
        )
    else:
        regime_parts = [f"{s[0]} ({'long' if s[1] == 1 else 'short'} gamma)" for s in signs]
        sentences.append(
            f"**Mixed regimes** across symbols: {', '.join(regime_parts)}. "
            "Index and ETF gamma can diverge when institutional flows concentrate in one product; "
            "use the dominant regime (often SPX or SPY by volume) for broad market bias."
        )

    # Confluence summary
    if confluence_alerts:
        sentences.append(
            "**Confluence:** Aligned King Nodes across these correlated underlyings reinforce support/resistance."
        )
    else:
        sentences.append(
            "No strong King Node confluence across symbols; each has distinct gamma focal points. "
            "Check individual interpretations for per-symbol levels."
        )

    return " ".join(sentences)


def get_confluence_alerts(symbol_data: Dict[str, SymbolGexData]) -> List[str]:
    """Detect aligned King Nodes across symbols. SPX ≈ 10× SPY."""
    alerts = []
    spx = symbol_data.get("SPX")
    spy = symbol_data.get("SPY")
    qqq = symbol_data.get("QQQ")

    if spx and spy and spx.king_strike and spy.king_strike:
        spx_norm = spx.king_strike / 10
        if abs(spx_norm - spy.king_strike) / spy.king_strike < 0.02:
            alerts.append("🟢 **SPX/SPY King aligned → high prob zone** (index/ETF gamma confluence)")
    if spx and qqq and spx.king_strike and qqq.king_strike:
        spx_norm = spx.king_strike / 12  # rough SPX/QQQ ratio
        if abs(spx_norm - qqq.king_strike) / qqq.king_strike < 0.03:
            alerts.append("🟢 **SPX/QQQ King aligned → broad gamma support**")
    if spy and qqq and spy.king_strike and qqq.king_strike:
        if abs(spy.king_strike / 1.2 - qqq.king_strike) / qqq.king_strike < 0.03:
            alerts.append("🟢 **SPY/QQQ King aligned → ETF gamma confluence**")
    return alerts


def _build_gex_payload(symbol_data: Dict[str, SymbolGexData], include_extended: bool = False) -> str:
    """Build a structured JSON payload of gamma data for all symbols, for LLM consumption."""
    payload = {}
    for label in symbol_data.keys():
        d = symbol_data[label]
        if d is None:
            continue
        p = {
            "spot_price": d.spot_price,
            "total_gex_B": round(d.total_gex, 2),
            "king_node": {"strike": d.king_strike, "gex_B": round(d.king_gex, 2)} if d.king_strike else None,
            "downside_defense": [
                {"strike": s, "gex_B": round(d.per_strike_gex.get(s, 0), 2)} for s in d.downside_defense[:3]
            ] if d.downside_defense else [],
            "upside_resistance": [
                {"strike": s, "gex_B": round(d.per_strike_gex.get(s, 0), 2)} for s in d.upside_resistance[:3]
            ] if d.upside_resistance else [],
            "nearest_support_below": d.nearest_gk_below,
            "nearest_resistance_above": d.nearest_gk_above,
            "gamma_flip_strike": d.gamma_flip_strike,
            "exp_date": str(d.exp_date),
        }
        if include_extended:
            p["regime_gauge"] = {
                "dist_to_flip_pct": round(d.dist_to_flip_pct, 2) if d.dist_to_flip_pct is not None else None,
                "points_to_flip": round(d.points_to_flip, 2) if d.points_to_flip is not None else None,
                "regime": (
                    "Stable (scalp mean reversion)" if d.dist_to_flip_pct and d.dist_to_flip_pct > 1.0
                    else "Transition (caution)" if d.dist_to_flip_pct and d.dist_to_flip_pct >= 0.5
                    else "Volatile (prepare for trending)" if d.dist_to_flip_pct is not None else None
                ),
            }
            p["gamma_velocity"] = {
                "total_delta_B": round(d.gamma_delta, 3) if d.gamma_delta is not None else None,
                "top_strike_velocity": {
                    "strike": d.top_strike_velocity_strike,
                    "delta_B": round(d.top_strike_velocity_value, 3) if d.top_strike_velocity_value is not None else None,
                } if d.top_strike_velocity_strike is not None else None,
            }
        payload[label] = p
    return json.dumps(payload, indent=2)


def generate_llm_interpretation(
    symbol_data: Dict[str, SymbolGexData],
    confluence_alerts: List[str],
    is_single_ticker: bool = False,
) -> Optional[str]:
    """Call Gemini with actual gamma data to produce actionable buy/sell guidance.
    Returns None if API key missing or call fails (caller should fallback to generic interpretation)."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or api_key.strip() == "":
        return None

    payload = _build_gex_payload(symbol_data, include_extended=is_single_ticker)
    today = datetime.now(pytz.timezone("US/Eastern")).strftime("%A, %B %d, %Y")
    symbols_str = ", ".join(symbol_data.keys())

    if is_single_ticker:
        prompt = f"""You are an options market maker and gamma exposure expert. You are advising a trader in **real time** on a single underlying: {symbols_str}. This is a snapshot as of {today}—the user may regenerate this interpretation multiple times per day. Focus on a **near-term (next few hours)** outlook, not a full-day forecast. GEX levels and nodes shift throughout the day in response to market conditions; your interpretation reflects current conditions only. The user is likely trading 0DTE options, so every day is an expiry day—do not reference expiry in your output. The user has the following GEX data including regime and velocity metrics.

GEX DATA (JSON):
```
{payload}
```

CONFLUENCE ALERTS (if any): {confluence_alerts if confluence_alerts else "None"}

DEFINITIONS:
- **Spot** = current price
- **King Node** = strike with largest |GEX|; positive GEX = support (dealers buy as spot falls), negative = resistance (dealers sell as spot rises)
- **Downside defense** = support levels (positive GEX below spot)
- **Upside resistance** = resistance levels (negative GEX above spot)
- **Total GEX** > 0: dealers long gamma → mean reversion, dampened moves
- **Total GEX** < 0: dealers short gamma → momentum can extend
- **Gamma flip strike** = level where cumulative gamma flips sign; breaks above/below can accelerate dealer hedging
- **Regime Gauge (DtF)**: Distance-to-Flip = % distance from spot to zero-gamma flip. >1% = Stable (scalp mean reversion), 0.5–1% = Transition (caution), <0.5% = Volatile (prepare for trending). Points to Flip = distance in points.
- **Gamma Velocity**: Change in total GEX since last refresh. Positive = gamma being added (dealers accumulating), negative = gamma being shed.
- **Top Strike Velocity**: The strike gaining gamma fastest. Acts as "Magnetic North"—price may gravitate toward it as dealers hedge.

IMPORTANT: Some fields in the JSON may be null (e.g., regime_gauge, gamma_velocity, gamma_flip_strike). This is normal—those metrics are not always computable. Base your analysis and recommendations ONLY on the data that is present. Do not mention missing data, complain about it, or suggest the analysis is incomplete. Use what you have to give actionable guidance.

TASK: Write a concise, actionable interpretation (4–6 short paragraphs) that gives **clear near-term (next few hours) recommendations** for this setup. Include only sections for which you have data:

1. **Regime analysis** (if regime_gauge or gamma_flip_strike present): What does the DtF and regime tell you? Otherwise, infer regime from total GEX sign and King Node placement.
2. **Velocity analysis** (if gamma_velocity present): What does gamma velocity imply? Is Top Strike Velocity relevant? Skip if absent.
3. **Concrete recommendations**: Entry, stop, and target levels. Reference actual strikes and distances from available data.
4. **When to trade vs. when to stay away**: If the setup is poor (mixed signals, low conviction), say so. Do not force a trade when the data suggests caution.

Use plain language. Reference actual numbers (strikes, distances, percentages). Give clear directional bias when the data supports it; give clear "stay away" or "reduce size" advice when it does not."""
    else:
        prompt = f"""You are an options market maker and gamma exposure expert. You are advising a trader in **real time** based on the following gamma exposure (GEX) data for {symbols_str}. This is a snapshot as of {today}—the user may regenerate this interpretation multiple times per day. Focus on a **near-term (next few hours)** outlook, not a full-day forecast. GEX levels and nodes shift throughout the day in response to market conditions; your interpretation reflects current conditions only. The user is likely trading 0DTE options, so every day is an expiry day—do not reference expiry in your output. These underlyings are highly correlated: SPX is the S&P 500 index, SPY tracks it at ~1/10 scale, QQQ is tech-heavy.

GEX DATA (JSON):
```
{payload}
```

CONFLUENCE ALERTS (if any): {confluence_alerts if confluence_alerts else "None"}

DEFINITIONS:
- **Spot** = current price
- **King Node** = strike with largest |GEX|; positive GEX = support (dealers buy as spot falls), negative = resistance (dealers sell as spot rises)
- **Downside defense** = support levels (positive GEX below spot)
- **Upside resistance** = resistance levels (negative GEX above spot)
- **Total GEX** > 0: dealers long gamma → mean reversion, dampened moves
- **Total GEX** < 0: dealers short gamma → momentum can extend
- **Gamma flip strike** = level where cumulative gamma flips sign (may be null if not computable from the chain)

IMPORTANT: Some fields may be null (e.g., gamma_flip_strike, regime_gauge). This is normal. Base your analysis and recommendations ONLY on the data that is present. Do not mention missing data, complain about it, or suggest the analysis is incomplete. Evaluate market conditions and make recommendations using whatever data is available.

TASK: Write a concise, actionable interpretation (3–5 short paragraphs) that guides the user on **how to think about buying and selling** over the next few hours given current spot prices. Be specific:
1. For each symbol with data: Is spot near support or resistance? Should they lean long, short, or neutral?
2. What concrete levels should they watch for entries, stops, and targets?
3. What does confluence across symbols imply for conviction?
4. Any caveats (e.g., mixed signals)?

Use plain language. Reference actual numbers (strikes, distances). Do not hedge with disclaimers; give clear directional bias where the data supports it."""

    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        config = types.GenerateContentConfig(
            system_instruction="You are a gamma exposure expert giving actionable trading guidance.",
            max_output_tokens=4096,
            temperature=0.4,
            safety_settings=[
                types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_ONLY_HIGH"),
                types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_ONLY_HIGH"),
                types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_ONLY_HIGH"),
                types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
            ],
        )
        # Use streaming to avoid truncation issues with the non-streaming API
        chunks = []
        for chunk in client.models.generate_content_stream(
            model="gemini-2.5-flash",
            contents=prompt,
            config=config,
        ):
            if chunk.text:
                chunks.append(chunk.text)
        content = "".join(chunks).strip() if chunks else None
        return content if content else None
    except Exception:
        # Silently fall back to generic interpretation; caller will handle
        return None


# --- Page config and client init (before sidebar so we have default_symbol) ---
st.set_page_config(
    page_title="Gamma Exposure Dashboard",
    page_icon="📊",
    layout="wide",
)

client, broker_name, default_symbol, client_module = get_authenticated_client()
if client is None:
    st.stop()

with st.sidebar:
    st.title("Gamma Exposure")
    if not os.environ.get("GEMINI_API_KEY"):
        st.caption("💡 Set GEMINI_API_KEY in .env for AI interpretation")
    strike_count = st.slider("Strike count", min_value=10, max_value=100, value=50, key="strikes")
    refresh_interval = st.slider(
        "Refresh interval (seconds)",
        min_value=30,
        max_value=300,
        value=60,
        step=15,
        key="refresh",
    )
    strike_range = st.slider(
        "Strike window (± from spot)",
        min_value=200,
        max_value=1000,
        value=800,
        step=50,
        key="strike_range",
        help="Only show strikes within ± this many points from spot",
    )
    gex_min_threshold = st.slider(
        "GEX min threshold ($B)",
        min_value=0.0,
        max_value=10.0,
        value=0.1,
        step=0.1,
        key="gex_threshold",
        help="Hide strikes with |GEX| below this (0 = show all)",
    )
    if st.button("Refresh now"):
        st.rerun()

st.caption(f"Using {broker_name} API")

# --- Display mode (main pane top) ---
display_mode = st.radio(
    "Display mode",
    options=["Single Ticker", "Trinity Display"],
    index=0,
    key="display_mode",
    horizontal=True,
)
all_symbol_options = ["SPX", "SPY", "QQQ"] + get_sp500_symbols()
if display_mode == "Single Ticker":
    single_ticker = st.selectbox(
        "Symbol",
        options=all_symbol_options,
        index=1,  # SPY
        key="single_ticker",
    )
    selected_symbols = [single_ticker]
else:
    selected_symbols = ["SPX", "SPY", "QQQ"]

st.markdown("---")

# --- Fetch all symbols ---
if "previous_gex" not in st.session_state:
    st.session_state.previous_gex = {}
if "previous_gamma_velocity" not in st.session_state:
    st.session_state.previous_gamma_velocity = {}
if "previous_gamma_velocity_timestamp" not in st.session_state:
    st.session_state.previous_gamma_velocity_timestamp = {}

symbol_data: Dict[str, SymbolGexData] = {}
fetch_errors: List[str] = []

fetch_pairs = [(display_to_api_symbol(label), label) for label in selected_symbols]

with st.spinner(f"Fetching {', '.join(selected_symbols)}..."):
    for api_symbol, label in fetch_pairs:
        prev = st.session_state.previous_gex.get(api_symbol, {})
        result, err_msg = fetch_options_and_gex(
            client, api_symbol, strike_count, prev, client_module
        )
        if result is None:
            fetch_errors.append(f"{label}: {err_msg}")
            continue
        st.session_state.previous_gex[api_symbol] = dict(result[2])  # per_strike_gex
        gamma_delta = result[6]
        eastern = pytz.timezone("US/Eastern")
        fetch_now = datetime.now(eastern)
        prev_velocity = st.session_state.previous_gamma_velocity.get(api_symbol)
        prev_timestamp = st.session_state.previous_gamma_velocity_timestamp.get(api_symbol)
        st.session_state.previous_gamma_velocity[api_symbol] = gamma_delta
        st.session_state.previous_gamma_velocity_timestamp[api_symbol] = fetch_now
        processed = process_symbol_gex(result, strike_range, gex_min_threshold)
        if processed is None:
            fetch_errors.append(f"{label}: no strikes in window")
            continue
        processed.symbol = api_symbol
        processed.label = label
        processed.prev_gamma_velocity = prev_velocity
        processed.prev_fetch_timestamp = prev_timestamp
        symbol_data[label] = processed

if not symbol_data:
    all_refresh_errors = all("refresh_token" in (e or "").lower() for e in fetch_errors)
    all_401_errors = all("401" in (e or "") for e in fetch_errors)
    if (all_refresh_errors or all_401_errors) and fetch_errors:
        _tp = str(Path.home() / "schwab_token.json")
        try:
            import secretsSchwab
            _tp = str(getattr(secretsSchwab, "token_path", _tp))
        except Exception:
            pass
        st.error(
            "**Schwab token expired (401).** Refresh tokens expire after ~7 days of inactivity. "
            f"Delete your token file and restart to re-authenticate:\n\n"
            f"`rm {_tp}`\n\n"
            "Then restart the app; you'll be prompted to complete the OAuth flow."
        )
    else:
        st.error("Could not fetch any symbols. " + (" ".join(fetch_errors)))
    st.stop()
if fetch_errors:
    for e in fetch_errors:
        st.warning(e)

last_update = datetime.now(pytz.timezone("US/Eastern")).strftime("%b %d, %Y %H:%M:%S ET")
st.caption(f"Data as of {last_update}")

selected_data = {k: v for k, v in symbol_data.items() if k in selected_symbols}

def _render_symbol_column(data: SymbolGexData, show_extended_metrics: bool = False):
    """Render heatmap, inference, and interpretation for one symbol in a column."""
    exp_date_str = data.exp_date.strftime("%b %d, %Y")
    fig = build_heatmap_fig(data, data.label, exp_date_str)
    st.plotly_chart(fig, use_container_width=True, key=f"heatmap_{data.label}")

    eastern = pytz.timezone("US/Eastern")
    now = datetime.now(eastern)
    time_ago_str = None
    if data.prev_fetch_timestamp is not None:
        elapsed_sec = (now - data.prev_fetch_timestamp).total_seconds()
        time_ago_str = _format_time_ago(elapsed_sec)

    if show_extended_metrics:
        # Single-ticker: use 3-column layout for better use of horizontal space
        col_inf, col_regime, col_velocity = st.columns(3)
        with col_inf:
            st.markdown("#### Inference")
            dir_kn = "above" if (data.king_strike and data.spot_price > data.king_strike) else "below"
            st.metric("Distance to King Node", f"{data.dist_to_king:.0f} pts {dir_kn}" if data.dist_to_king else "—")
            st.metric("Gatekeeper below", f"${data.nearest_gk_below:.0f}" if data.nearest_gk_below else "—")
            st.metric("Gatekeeper above", f"${data.nearest_gk_above:.0f}" if data.nearest_gk_above else "—")
            if data.gamma_flip_strike:
                st.caption(f"Zero-gamma flip: ${data.gamma_flip_strike:.0f}")
        with col_regime:
            st.markdown("#### Regime Gauge (DtF)")
            if data.dist_to_flip_pct is not None and data.gamma_flip_strike is not None:
                if data.dist_to_flip_pct > 1.0:
                    color, regime = "#22c55e", "Stable / Scalp mean reversion"
                elif data.dist_to_flip_pct >= 0.5:
                    color, regime = "#eab308", "Transition / Caution"
                else:
                    color, regime = "#ef4444", "Volatile / Prepare for trending moves"
                st.markdown(
                    f'<p style="font-size: 1.8em; font-weight: bold; color: {color}; margin: 0;">{data.dist_to_flip_pct:.2f}%</p>'
                    f'<p style="font-size: 0.85em; color: #666; margin: 0;">{regime}</p>',
                    unsafe_allow_html=True,
                )
                st.metric("Points to Flip", f"{data.points_to_flip:.2f} pts")
            else:
                st.metric("Distance to Flip", "—")
                st.metric("Points to Flip", "—")
        with col_velocity:
            st.markdown("#### Pressure Gauge (Velocity)")
            if data.is_first_fetch:
                st.metric("Gamma Velocity", "N/A (first refresh)", delta=None)
            else:
                vel_delta = None
                if data.prev_gamma_velocity is not None and time_ago_str:
                    accel = (data.gamma_delta or 0) - data.prev_gamma_velocity
                    vel_delta = f"${accel:+.3f}B vs {time_ago_str}"
                st.metric("Gamma Velocity", f"${data.gamma_delta:+.3f}B", delta=vel_delta)
            if data.top_strike_velocity_strike is not None and data.top_strike_velocity_value is not None:
                st.metric(
                    "Top Strike Velocity",
                    f"${data.top_strike_velocity_strike:.0f}",
                    delta=f"${data.top_strike_velocity_value:+.3f}B",
                )
                caption = "Magnetic North — strike gaining gamma fastest"
                if time_ago_str and not data.is_first_fetch:
                    caption += f" (since {time_ago_str})"
                st.caption(caption)

        # Metrics row
        m1, m2, m3 = st.columns(3)
        with m1:
            st.metric("Total GEX", f"${data.total_gex:.3f}B")
        with m2:
            st.metric("Spot", f"${data.spot_price:.2f}")
        with m3:
            st.metric("King Node", f"${data.king_strike:.0f}" if data.king_strike else "—")

        # Alerts
        alerts = []
        if data.king_strike:
            dist = data.spot_price - data.king_strike
            if dist < 0 and data.king_gex > 0:
                alerts.append(("🟢", "Approaching strong support node"))
            elif dist < 0 and data.king_gex < 0:
                alerts.append(("🔴", "Approaching King Node resistance"))
            elif dist > 0 and data.king_gex < 0:
                alerts.append(("🔴", "Resistance overhead at King Node"))
            elif dist > 0 and data.king_gex > 0:
                alerts.append(("🟢", "Support below at King Node"))
        if data.nearest_gk_below and data.per_strike_gex.get(data.nearest_gk_below, 0) > 0:
            if (data.spot_price - data.nearest_gk_below) < data.spot_price * 0.02:
                alerts.append(("🟢", f"Near downside defense (${data.nearest_gk_below:.0f})"))
        if data.nearest_gk_above and data.per_strike_gex.get(data.nearest_gk_above, 0) < 0:
            if (data.nearest_gk_above - data.spot_price) < data.spot_price * 0.02:
                alerts.append(("🔴", f"Resistance overhead (${data.nearest_gk_above:.0f})"))
        if data.gamma_flip_strike and abs(data.spot_price - data.gamma_flip_strike) < data.spot_price * 0.015:
            alerts.append(("🟡", f"Near zero-gamma flip (${data.gamma_flip_strike:.0f})"))
        if alerts:
            st.markdown("#### Alerts")
            for icon, msg in alerts[:3]:
                st.markdown(f"{icon} {msg}")
        else:
            st.caption("No active alerts.")
    else:
        # Multi-ticker: vertical layout (narrow column)
        st.markdown("#### Inference")
        dir_kn = "above" if (data.king_strike and data.spot_price > data.king_strike) else "below"
        st.metric("Distance to King Node", f"{data.dist_to_king:.0f} pts {dir_kn}" if data.dist_to_king else "—")
        st.metric("Gatekeeper below", f"${data.nearest_gk_below:.0f}" if data.nearest_gk_below else "—")
        st.metric("Gatekeeper above", f"${data.nearest_gk_above:.0f}" if data.nearest_gk_above else "—")
        if data.gamma_flip_strike:
            st.caption(f"Zero-gamma flip: ${data.gamma_flip_strike:.0f}")

        alerts = []
        if data.king_strike:
            dist = data.spot_price - data.king_strike
            if dist < 0 and data.king_gex > 0:
                alerts.append(("🟢", "Approaching strong support node"))
            elif dist < 0 and data.king_gex < 0:
                alerts.append(("🔴", "Approaching King Node resistance"))
            elif dist > 0 and data.king_gex < 0:
                alerts.append(("🔴", "Resistance overhead at King Node"))
            elif dist > 0 and data.king_gex > 0:
                alerts.append(("🟢", "Support below at King Node"))
        if data.nearest_gk_below and data.per_strike_gex.get(data.nearest_gk_below, 0) > 0:
            if (data.spot_price - data.nearest_gk_below) < data.spot_price * 0.02:
                alerts.append(("🟢", f"Near downside defense (${data.nearest_gk_below:.0f})"))
        if data.nearest_gk_above and data.per_strike_gex.get(data.nearest_gk_above, 0) < 0:
            if (data.nearest_gk_above - data.spot_price) < data.spot_price * 0.02:
                alerts.append(("🔴", f"Resistance overhead (${data.nearest_gk_above:.0f})"))
        if data.gamma_flip_strike and abs(data.spot_price - data.gamma_flip_strike) < data.spot_price * 0.015:
            alerts.append(("🟡", f"Near zero-gamma flip (${data.gamma_flip_strike:.0f})"))
        for icon, msg in alerts[:3]:
            st.markdown(f"{icon} {msg}")
        if not alerts:
            st.caption("No active alerts.")

        st.metric("Total GEX", f"${data.total_gex:.3f}B")
        st.metric("Spot", f"${data.spot_price:.2f}")
        st.metric("King Node", f"${data.king_strike:.0f}" if data.king_strike else "—")

    interp = generate_gex_interpretation(
        data.spot_price, data.total_gex, data.king_strike,
        data.downside_defense, data.upside_resistance, data.per_strike_gex
    )
    sugg = generate_trader_suggestions(
        data.spot_price, data.total_gex, data.king_strike,
        data.downside_defense, data.upside_resistance, data.per_strike_gex
    )
    with st.expander("Interpretation & Suggestions", expanded=False):
        st.markdown(f"<p style='line-height: 1.5; font-size: 0.9em;'>{interp.replace('$', '&#36;')}</p>", unsafe_allow_html=True)
        st.markdown("**Suggestions**")
        st.markdown(f"<p style='line-height: 1.5; font-size: 0.9em;'>{sugg.replace('$', '&#36;')}</p>", unsafe_allow_html=True)


# --- King Node comparison and confluence (top) ---
st.markdown("### King Node Comparison")
rows = []
for label in selected_symbols:
    d = symbol_data.get(label)
    if d:
        rows.append({
            "Symbol": label,
            "Spot": f"${d.spot_price:.1f}",
            "King Node": f"${d.king_strike:.0f}" if d.king_strike else "—",
            "King GEX ($B)": f"{d.king_gex:.2f}" if d.king_strike else "—",
            "Total GEX ($B)": f"{d.total_gex:.2f}",
            "Zero-Gamma Flip": f"${d.gamma_flip_strike:.0f}" if d.gamma_flip_strike else "—",
        })
if rows:
    st.dataframe(rows, use_container_width=True, hide_index=True)

confluence = get_confluence_alerts(selected_data)
if confluence:
    st.markdown("### Confluence Alerts")
    for msg in confluence:
        st.markdown(msg)

# --- Side-by-side columns: SPX | SPY | QQQ ---
st.markdown("---")
st.markdown("### " + " | ".join(selected_symbols))
num_cols = len(selected_symbols)
cols = st.columns(num_cols) if num_cols > 0 else []
for j, label in enumerate(selected_symbols):
    with cols[j]:
        st.markdown(f"### {label}")
        if label in symbol_data:
            _render_symbol_column(symbol_data[label], show_extended_metrics=(len(selected_symbols) == 1))
        else:
            st.info(f"No data for {label}. Check fetch errors above.")

with st.expander("Legend", expanded=True):
    st.markdown("""
    - **Orange dotted box:** Strong gamma cluster — 3+ consecutive strikes with GEX ≥ 25% of max. Dealers concentrate hedging here.
    - **King Node:** Strike with largest absolute GEX; dominant level for support (positive) or resistance (negative).
    - **Gatekeeper:** Key gamma strike (support below spot or resistance above) that can act as a magnet or wall.
    - **Zero-gamma flip:** Level where cumulative gamma flips sign; breaks above/below can accelerate dealer hedging.
    """)

# --- Combined interpretation (selected charts) ---
st.markdown("---")
st.markdown("### Combined Interpretation (" + ", ".join(selected_symbols) + ")")

# Initialize session state for cached LLM output
if "llm_interpretation" not in st.session_state:
    st.session_state.llm_interpretation = None

# Generate button: LLM only runs when user clicks
if os.environ.get("GEMINI_API_KEY"):
    if st.button("Generate", key="gen_interpretation"):
        with st.spinner("Generating AI interpretation..."):
            st.session_state.llm_interpretation = generate_llm_interpretation(
                selected_data, confluence, is_single_ticker=(len(selected_symbols) == 1)
            )
    llm_text = st.session_state.llm_interpretation
else:
    llm_text = None

if llm_text:
    # Escape for safe HTML display: $ (LaTeX), < > & (HTML entities)
    def _escape_for_display(s: str) -> str:
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("$", "&#36;")
        )
    escaped = _escape_for_display(llm_text)
    st.markdown(
        "<div style='line-height: 1.6; font-size: 0.95em; white-space: pre-wrap; overflow-y: auto; max-height: 70vh;'>"
        + escaped
        + "</div>",
        unsafe_allow_html=True,
    )
else:
    if not os.environ.get("GEMINI_API_KEY"):
        st.caption("Set GEMINI_API_KEY in .env for AI-powered interpretation.")
    else:
        st.caption("Click **Generate** above for AI interpretation.")

# --- Auto-refresh ---
st.markdown("---")
st.caption(f"Auto-refreshing every {refresh_interval} seconds.")
time.sleep(refresh_interval)
st.rerun()
