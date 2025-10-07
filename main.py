"""Application entry point for streaming gamma exposure analytics.

This module has been updated to support both the legacy TD Ameritrade API and
the newer Charles Schwab API.  The active broker can be selected with the
``BROKER`` environment variable (``schwab`` or ``tda``) or automatically falls
back to the first provider whose configuration is available on the system.
"""

from datetime import datetime, timedelta
import inspect
import os
from typing import Optional, Tuple, Callable, Dict, Any

import pytz
import matplotlib.pyplot as plt
from selenium import webdriver
import time as time_module

from gamma_analysis import calculate_gamma_exposure
from plotter import RealTimeGammaPlotter
from db_storage import store_raw_options_data


class BrokerConfigurationError(RuntimeError):
    """Raised when a supported broker configuration cannot be located."""


def _load_broker_client(preferred_broker: Optional[str] = None) -> Tuple[str, object, object, object]:
    """Locate a supported broker configuration and client.

    Parameters
    ----------
    preferred_broker:
        Optional explicit broker identifier (``"schwab"`` or ``"tda"``).

    Returns
    -------
    tuple
        ``(broker_name, auth_module, client_module, secrets_module)``

    Raises
    ------
    BrokerConfigurationError
        If a usable broker configuration cannot be imported.
    """

    errors = []

    def try_import(broker_key: str):
        broker_key = broker_key.lower()
        if broker_key == "schwab":
            try:
                from schwab import auth as schwab_auth, client as schwab_client  # type: ignore
                import secretsSchwab  # type: ignore

                return "Schwab", schwab_auth, schwab_client, secretsSchwab
            except ModuleNotFoundError as exc:  # pragma: no cover - import guard
                errors.append(f"Schwab configuration unavailable: {exc}")
        elif broker_key == "tda":
            try:
                from tda import auth as tda_auth, client as tda_client  # type: ignore
                import secretsTDA  # type: ignore

                return "TD Ameritrade", tda_auth, tda_client, secretsTDA
            except ModuleNotFoundError as exc:  # pragma: no cover - import guard
                errors.append(f"TD Ameritrade configuration unavailable: {exc}")

        return None

    if preferred_broker:
        broker = try_import(preferred_broker)
        if broker:
            return broker

    # Automatic detection order: Schwab first (new platform), then TDA fallback
    for broker_name in ("schwab", "tda"):
        broker = try_import(broker_name)
        if broker:
            return broker

    raise BrokerConfigurationError(
        "Unable to locate a usable broker configuration.\n" + "\n".join(errors)
    )

class GammaExposureScheduler:
    # Initialize and create dictionaries for temporary data storage and analysis
    def __init__(self, preferred_broker: Optional[str] = None):
        self.current_gamma_exposure = {}
        self.previous_gamma_exposure = {}
        self.change_in_gamma_per_strike = {}
        self.client = None
        self.plotter = RealTimeGammaPlotter()
        broker_name, self.auth_module, self.client_module, self.secrets = _load_broker_client(
            preferred_broker or os.environ.get("BROKER")
        )
        self.broker_name = broker_name
        print(f"Using {self.broker_name} broker configuration.")

        # Broker specific defaults
        self.option_symbol = getattr(self.secrets, "option_symbol", "$SPX.X")
        self.strike_count = getattr(self.secrets, "strike_count", 50)

    @staticmethod
    def _filter_supported_kwargs(function: Callable[..., object], raw_kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Filter keyword arguments to those supported by ``function``."""

        signature = inspect.signature(function)
        return {key: value for key, value in raw_kwargs.items() if key in signature.parameters}

    #API auth
    def authenticate(self):
        try:
            auth_kwargs = {}
            if hasattr(self.secrets, "redirect_uri"):
                auth_kwargs["redirect_uri"] = self.secrets.redirect_uri
            if hasattr(self.secrets, "token_encryption_key"):
                auth_kwargs["encryption_key"] = getattr(self.secrets, "token_encryption_key")
            if hasattr(self.secrets, "cert_file"):
                auth_kwargs["cert_file"] = getattr(self.secrets, "cert_file")

            filtered_kwargs = self._filter_supported_kwargs(
                self.auth_module.client_from_token_file,
                auth_kwargs,
            )

            self.client = self.auth_module.client_from_token_file(
                self.secrets.token_path,
                self.secrets.api_key,
                **filtered_kwargs,
            )
        except FileNotFoundError:
            with webdriver.Chrome() as driver:
                login_kwargs = {
                    "driver": driver,
                    "api_key": self.secrets.api_key,
                    "redirect_uri": getattr(self.secrets, "redirect_uri", None),
                    "token_path": self.secrets.token_path,
                }

                # Optional Schwab parameters are passed only if present.
                for optional_attr in ("cert_file", "encryption_key", "token_encryption_key"):
                    if hasattr(self.secrets, optional_attr):
                        login_kwargs[optional_attr] = getattr(self.secrets, optional_attr)

                # Remove keys with None values to avoid unexpected keyword errors
                login_kwargs = {k: v for k, v in login_kwargs.items() if v is not None}

                filtered_login_kwargs = self._filter_supported_kwargs(
                    self.auth_module.client_from_login_flow,
                    login_kwargs,
                )

                self.client = self.auth_module.client_from_login_flow(**filtered_login_kwargs)

    def fetch_and_update_gamma_exposure(self):
        eastern = pytz.timezone('US/Eastern')
        now = datetime.now(eastern)

        # If it's Friday after 4 PM, set use_date to the next Monday
        if now.weekday() == 4 and now.hour >= 16:
            use_date = (now + timedelta(days=3)).date()
        # Otherwise, use the next day's date if it's after 4 PM, or today's date if it's before 4 PM
        else:
            use_date = now.date() + timedelta(days=1 if now.hour >= 16 else 0)

        try:
            if self.client:
                options_source = getattr(self.client_module, "Options", None) or getattr(self.client, "Options", None)
                contract_type_all = getattr(options_source, "ContractType", None) if options_source else None
                if contract_type_all is not None:
                    contract_type_all = getattr(contract_type_all, "ALL", contract_type_all)

                kwargs = {
                    "symbol": self.option_symbol,
                    "from_date": use_date,
                    "to_date": use_date,
                    "strike_count": self.strike_count,
                }

                if contract_type_all is not None:
                    kwargs["contract_type"] = contract_type_all

                r = self.client.get_option_chain(**kwargs)
                if r.status_code == 200:
                    data = r.json()
                    total_gamma_exposure, self.current_gamma_exposure, self.change_in_gamma_per_strike, largest_changes, spot_price = calculate_gamma_exposure(data, self.previous_gamma_exposure)
                    self.previous_gamma_exposure = self.current_gamma_exposure.copy()
                    current_timestamp = datetime.now(pytz.timezone('US/Eastern'))
                    self.plotter.update_plot_gamma(self.current_gamma_exposure)
                    self.plotter.update_plot_change_in_gamma(self.change_in_gamma_per_strike, largest_changes)
                    self.plotter.update_total_gamma_exposure_plot(current_timestamp, total_gamma_exposure, spot_price)
                    self.plotter.show_plots()
                    pause_duration = 5
                else:
                    print(f"Failed to fetch data: {r.status_code}")
                    pause_duration = 5  # Longer pause when fetch fails
        except Exception as e:
            print(f"An error occurred: {e}")
            pause_duration = 5  # Longer pause on error
        finally:
            # Always attempt to store data in the database, even if the fetch or plotting fails
            db_params = {
                "dbname": "spx_options_data",
                "user": "postgres",
                "password": "password",
                "host": "localhost"
            }
            # Make sure to handle the case where data might not be defined due to failed fetch
            if 'data' in locals():
                store_raw_options_data(db_params, data, now)
            else:
                print("No data to store in database.")

            plt.pause(pause_duration)  # Adjust pause based on operation outcome

    def run(self):
        self.authenticate()
        
        while True:
            eastern = pytz.timezone('US/Eastern')
            now = datetime.now(eastern)
            start_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
            end_time = now.replace(hour=16, minute=15, second=0, microsecond=0)

            # Check if current time is within the trading hours
            if start_time <= now <= end_time:
                self.fetch_and_update_gamma_exposure()
            else:
                print("Outside trading hours. Waiting to resume...")

            time_module.sleep(4)  # Sleep until time to run API request again

scheduler = GammaExposureScheduler()
scheduler.run()
