"""
execution.py — Alpaca API connectivity layer (Phase 1: read-only)

Provides AlpacaClient for account info, positions, option contracts,
quotes, and order queries. No order placement (Phase 3).

Credentials are read from environment variables (already loaded via dotenv):
    ALPACA_API_KEY
    ALPACA_SECRET_KEY
    ALPACA_PAPER   # "true" or "false", default "true"
"""

import os
import re
import requests as _requests

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest, GetOrdersRequest
from alpaca.trading.enums import ContractType, QueryOrderStatus
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest


PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"


def _is_paper() -> bool:
    """Returns True if ALPACA_PAPER env var is 'true' (case-insensitive) or not set."""
    val = os.environ.get("ALPACA_PAPER", "true")
    return val.strip().lower() == "true"


def _looks_like_option(symbol: str) -> bool:
    """Heuristic: option OCC symbols contain a date block + C/P + strike."""
    # Standard OCC format: SPY   240119C00500000
    return bool(re.search(r"\d{6}[CP]\d{8}", symbol))


class AlpacaClient:
    """Read-only Alpaca API client for Phase 1 connectivity and data retrieval."""

    def __init__(self):
        api_key = os.environ.get("ALPACA_API_KEY", "")
        secret_key = os.environ.get("ALPACA_SECRET_KEY", "")

        if not api_key or not secret_key:
            print("[Alpaca] WARNING: ALPACA_API_KEY and/or ALPACA_SECRET_KEY not set.")

        paper = _is_paper()
        self._base_url = PAPER_BASE_URL if paper else LIVE_BASE_URL
        self._paper = paper
        self._api_key = api_key
        self._secret_key = secret_key

        self._trading = TradingClient(
            api_key=api_key,
            secret_key=secret_key,
            paper=paper,
        )

        # Option market data client (does not require paper flag)
        self._option_data = OptionHistoricalDataClient(
            api_key=api_key,
            secret_key=secret_key,
        )

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_account(self) -> dict:
        """
        Returns account info as a dict with keys:
            buying_power, cash, portfolio_value, currency
        Returns an empty dict on failure.
        """
        try:
            acct = self._trading.get_account()
            return {
                "buying_power": float(acct.buying_power),
                "cash": float(acct.cash),
                "portfolio_value": float(acct.portfolio_value),
                "currency": str(acct.currency),
            }
        except Exception as e:
            print(f"[Alpaca] error in get_account: {e}")
            return {}

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_open_positions(self) -> list:
        """
        Returns list of open option positions as dicts with keys:
            symbol, qty, avg_entry_price, current_price, unrealized_pl, side

        Filters to positions where the asset class is 'us_option' or
        the symbol looks like an OCC option contract.
        Returns an empty list on failure.
        """
        try:
            all_positions = self._trading.get_all_positions()
            option_positions = []
            for pos in all_positions:
                asset_class = str(getattr(pos, "asset_class", "")).lower()
                symbol = str(pos.symbol)
                is_option = "option" in asset_class or _looks_like_option(symbol)
                if not is_option:
                    continue

                current_price = None
                try:
                    current_price = float(pos.current_price) if pos.current_price is not None else None
                except (TypeError, ValueError):
                    pass

                unrealized_pl = None
                try:
                    unrealized_pl = float(pos.unrealized_pl) if pos.unrealized_pl is not None else None
                except (TypeError, ValueError):
                    pass

                option_positions.append({
                    "symbol": symbol,
                    "qty": int(float(pos.qty)),
                    "avg_entry_price": float(pos.avg_entry_price),
                    "current_price": current_price,
                    "unrealized_pl": unrealized_pl,
                    "side": str(pos.side.value if hasattr(pos.side, "value") else pos.side),
                })
            return option_positions
        except Exception as e:
            print(f"[Alpaca] error in get_open_positions: {e}")
            return []

    # ------------------------------------------------------------------
    # Option contracts
    # ------------------------------------------------------------------

    def get_option_contracts(
        self,
        underlying: str,
        expiration_date: str,
        option_type: str,
        strike_price: float,
    ) -> list:
        """
        Fetch available option contracts matching the given parameters.

        Args:
            underlying:      e.g. "SPY"
            expiration_date: "YYYY-MM-DD"
            option_type:     "call" or "put"
            strike_price:    e.g. 582.0

        Returns list of dicts with keys:
            symbol, strike_price, expiration_date, option_type,
            open_interest, close_price
        Returns empty list on failure.
        """
        try:
            contract_type = ContractType.CALL if option_type.lower() == "call" else ContractType.PUT

            # Use a small window around the strike to capture the exact contract
            strike_str = str(strike_price)
            req = GetOptionContractsRequest(
                underlying_symbols=[underlying],
                expiration_date=expiration_date,
                type=contract_type,
                strike_price_gte=strike_str,
                strike_price_lte=strike_str,
            )
            response = self._trading.get_option_contracts(req)

            # Response may be a model with .option_contracts list or an iterable
            contracts_raw = []
            if hasattr(response, "option_contracts"):
                contracts_raw = response.option_contracts
            elif hasattr(response, "__iter__"):
                contracts_raw = list(response)

            results = []
            for c in contracts_raw:
                results.append({
                    "symbol": str(c.symbol),
                    "strike_price": float(c.strike_price) if c.strike_price is not None else None,
                    "expiration_date": str(c.expiration_date),
                    "option_type": str(c.type.value if hasattr(c.type, "value") else c.type),
                    "open_interest": int(c.open_interest) if c.open_interest is not None else None,
                    "close_price": float(c.close_price) if c.close_price is not None else None,
                })
            return results
        except Exception as e:
            print(f"[Alpaca] error in get_option_contracts: {e}")
            return []

    # ------------------------------------------------------------------
    # Option quotes
    # ------------------------------------------------------------------

    def get_latest_option_quote(self, symbol: str) -> dict | None:
        """
        Get the latest bid/ask for an option contract symbol.

        Returns dict with keys: bid, ask, mid
        Returns None on failure.
        """
        try:
            req = OptionLatestQuoteRequest(symbol_or_symbols=symbol)
            response = self._option_data.get_option_latest_quote(req)

            # response is a dict keyed by symbol
            quote = None
            if isinstance(response, dict):
                quote = response.get(symbol)
            elif hasattr(response, symbol):
                quote = getattr(response, symbol)

            if quote is None:
                return None

            bid = float(quote.bid_price) if hasattr(quote, "bid_price") else float(quote.bp)
            ask = float(quote.ask_price) if hasattr(quote, "ask_price") else float(quote.ap)
            return {
                "bid": bid,
                "ask": ask,
                "mid": round((bid + ask) / 2, 4),
            }
        except Exception as e:
            print(f"[Alpaca] error in get_latest_option_quote: {e}")
            # Fallback: REST API
            return self._get_latest_option_quote_rest(symbol)

    def _get_latest_option_quote_rest(self, symbol: str) -> dict | None:
        """Fallback REST call for option quote if SDK fails."""
        try:
            url = f"{DATA_BASE_URL}/v1beta1/options/quotes/latest"
            headers = {
                "APCA-API-KEY-ID": self._api_key,
                "APCA-API-SECRET-KEY": self._secret_key,
            }
            resp = _requests.get(url, params={"symbols": symbol}, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            quotes = data.get("quotes", {})
            q = quotes.get(symbol)
            if q is None:
                return None
            bid = float(q.get("bp", 0))
            ask = float(q.get("ap", 0))
            return {
                "bid": bid,
                "ask": ask,
                "mid": round((bid + ask) / 2, 4),
            }
        except Exception as e:
            print(f"[Alpaca] error in _get_latest_option_quote_rest: {e}")
            return None

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def cancel_all_orders(self) -> int:
        """
        Cancel all open orders.
        Returns the count of cancelled orders.
        """
        try:
            cancelled = self._trading.cancel_orders()
            # cancel_orders returns a list of CancelOrderResponse objects
            if isinstance(cancelled, list):
                return len(cancelled)
            return 0
        except Exception as e:
            print(f"[Alpaca] error in cancel_all_orders: {e}")
            return 0

    def get_orders(self, status: str = "open") -> list:
        """
        Returns list of orders as dicts with keys:
            id, symbol, side, qty, filled_qty, status,
            order_type, limit_price, filled_avg_price

        Args:
            status: "open", "closed", or "all"
        Returns empty list on failure.
        """
        try:
            status_map = {
                "open": QueryOrderStatus.OPEN,
                "closed": QueryOrderStatus.CLOSED,
                "all": QueryOrderStatus.ALL,
            }
            query_status = status_map.get(status.lower(), QueryOrderStatus.OPEN)
            req = GetOrdersRequest(status=query_status)
            orders_raw = self._trading.get_orders(req)

            results = []
            for o in orders_raw:
                limit_price = None
                try:
                    limit_price = float(o.limit_price) if o.limit_price is not None else None
                except (TypeError, ValueError):
                    pass

                filled_avg_price = None
                try:
                    filled_avg_price = float(o.filled_avg_price) if o.filled_avg_price is not None else None
                except (TypeError, ValueError):
                    pass

                results.append({
                    "id": str(o.id),
                    "symbol": str(o.symbol),
                    "side": str(o.side.value if hasattr(o.side, "value") else o.side),
                    "qty": float(o.qty) if o.qty is not None else None,
                    "filled_qty": float(o.filled_qty) if o.filled_qty is not None else None,
                    "status": str(o.status.value if hasattr(o.status, "value") else o.status),
                    "order_type": str(
                        o.order_type.value if hasattr(o.order_type, "value") else o.order_type
                    ),
                    "limit_price": limit_price,
                    "filled_avg_price": filled_avg_price,
                })
            return results
        except Exception as e:
            print(f"[Alpaca] error in get_orders: {e}")
            return []
