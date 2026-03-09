"""Application entry point for streaming gamma exposure analytics.

This module has been updated to support both the legacy TD Ameritrade API and
the newer Charles Schwab API.  The active broker can be selected with the
``BROKER`` environment variable (``schwab`` or ``tda``) or automatically falls
back to the first provider whose configuration is available on the system.
"""

from pathlib import Path
from dotenv import load_dotenv

# Load .env from the directory containing this script (works regardless of cwd)
load_dotenv(Path(__file__).resolve().parent / ".env")

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


# Charles Schwab uses a loopback HTTPS redirect during OAuth flows.  The
# application falls back to this value whenever the secrets module does not
# provide an explicit URI so that both the login and token refresh flows share a
# consistent default.
DEFAULT_REDIRECT_URI = "https://127.0.0.1"

# Schwab OAuth token endpoint (fallback when session metadata doesn't provide it).
SCHWAB_TOKEN_ENDPOINT = "https://api.schwabapi.com/v1/oauth/token"


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
                missing = getattr(exc, "name", "schwab component")
                errors.append(f"Schwab configuration unavailable (missing {missing})")
        elif broker_key == "tda":
            try:
                from tda import auth as tda_auth, client as tda_client  # type: ignore
                import secretsTDA  # type: ignore

                return "TD Ameritrade", tda_auth, tda_client, secretsTDA
            except ModuleNotFoundError as exc:  # pragma: no cover - import guard
                missing = getattr(exc, "name", "tda component")
                errors.append(f"TD Ameritrade configuration unavailable (missing {missing})")

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

    @staticmethod
    def _proactive_schwab_token_refresh(client: object) -> None:
        """Force a token refresh on startup to reset Schwab's 7-day inactivity clock.

        Schwab refresh tokens expire after ~7 days of *inactivity*—meaning the refresh
        token must be *used* at least every ~6 days. The access token expires in 30 min.
        Proactively refreshing on every launch ensures the refresh token is used and
        the clock resets, and gives us a fresh access token before any API calls.
        """
        session = getattr(client, "session", None)
        if session is None:
            return
        token = getattr(session, "token", None)
        if token is None or not token.get("refresh_token"):
            return
        # Try multiple sources for token_endpoint (authlib/schwab-py structure varies).
        metadata = getattr(session, "metadata", None) or {}
        token_endpoint = (
            metadata.get("token_endpoint")
            or getattr(session, "token_endpoint", None)
            or SCHWAB_TOKEN_ENDPOINT
        )
        if not token_endpoint:
            return
        # ensure_active_token does not update the token (authlib/schwab-py); use
        # refresh_token directly to get a new access token and reset the 7-day clock.
        try:
            refresh_fn = getattr(session, "refresh_token", None)
            if callable(refresh_fn):
                new_token = refresh_fn(token_endpoint, refresh_token=token.get("refresh_token"))
                if new_token:
                    token.update(new_token)
                    update_token = getattr(session, "update_token", None)
                    if callable(update_token):
                        update_token(new_token, refresh_token=new_token.get("refresh_token"))
            else:
                # Fallback: ensure_active_token (may not work with all authlib versions)
                token["expires_at"] = 0
                if callable(getattr(session, "ensure_active_token", None)):
                    try:
                        session.ensure_active_token(token)
                    except TypeError:
                        session.ensure_active_token()
        except Exception:
            raise

    #API auth
    def authenticate(self):
        try:
            auth_kwargs = {}
            redirect_uri = getattr(self.secrets, "redirect_uri", None) or DEFAULT_REDIRECT_URI
            if redirect_uri:
                auth_kwargs["redirect_uri"] = redirect_uri
            if hasattr(self.secrets, "app_secret"):
                auth_kwargs["app_secret"] = self.secrets.app_secret
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
            redirect_uri = getattr(self.secrets, "redirect_uri", None) or DEFAULT_REDIRECT_URI

            if self.broker_name == "Schwab" and hasattr(self.auth_module, "client_from_manual_flow"):
                # Schwab: manual flow (print URL, user pastes redirect back)
                if (self.secrets.api_key == "YOUR_SCHWAB_CLIENT_ID@AMER.OAUTHAP"
                        or self.secrets.app_secret == "YOUR_APP_SECRET_HERE"):
                    raise BrokerConfigurationError(
                        "Credentials not loaded. Ensure .env exists with SCHWAB_API_KEY and "
                        "SCHWAB_APP_SECRET, or that those environment variables are set."
                    )
                print(f"Callback URL: {redirect_uri}")
                print("  (Must EXACTLY match a URL in your Schwab app's Callback URL list)")
                print("  IMPORTANT: After logging in, copy the redirect URL immediately and paste within ~30 seconds (the code expires quickly).")
                # Sanitize pasted URLs: browsers often wrap long URLs with newlines when copying
                _orig_prompt = getattr(self.auth_module, "prompt", None)
                if _orig_prompt is not None:
                    def _sanitized_prompt(*args, **kwargs):
                        result = _orig_prompt(*args, **kwargs)
                        return result.replace("\n", "").replace("\r", "").strip()
                    self.auth_module.prompt = _sanitized_prompt
                try:
                    manual_kwargs = {
                        "api_key": self.secrets.api_key,
                        "app_secret": self.secrets.app_secret,
                        "callback_url": redirect_uri,
                        "token_path": self.secrets.token_path,
                    }
                    filtered_manual_kwargs = self._filter_supported_kwargs(
                        self.auth_module.client_from_manual_flow,
                        manual_kwargs,
                    )
                    _max_retries = 3
                    for _attempt in range(_max_retries):
                        try:
                            self.client = self.auth_module.client_from_manual_flow(**filtered_manual_kwargs)
                            break
                        except Exception as oauth_exc:
                            _msg = str(oauth_exc).lower()
                            if ("expired" in _msg or "authorizationcode" in _msg) and _attempt < _max_retries - 1:
                                print("\nAuthorization code expired. Visit the URL again and paste the NEW callback URL within 30 seconds.\n")
                                continue
                            raise
                finally:
                    if _orig_prompt is not None:
                        self.auth_module.prompt = _orig_prompt
            elif hasattr(self.auth_module, "client_from_login_flow"):
                # TDA: automated login via Selenium
                with webdriver.Chrome() as driver:
                    login_kwargs = {
                        "driver": driver,
                        "api_key": self.secrets.api_key,
                        "redirect_uri": redirect_uri,
                        "token_path": self.secrets.token_path,
                    }
                    for optional_attr in ("cert_file", "encryption_key", "token_encryption_key"):
                        if hasattr(self.secrets, optional_attr):
                            login_kwargs[optional_attr] = getattr(self.secrets, optional_attr)
                    login_kwargs = {k: v for k, v in login_kwargs.items() if v is not None}
                    filtered_login_kwargs = self._filter_supported_kwargs(
                        self.auth_module.client_from_login_flow,
                        login_kwargs,
                    )
                    self.client = self.auth_module.client_from_login_flow(**filtered_login_kwargs)
            else:
                raise BrokerConfigurationError(
                    f"No token file at {self.secrets.token_path} and no login flow "
                    "available for this broker."
                )

        if self.client and self.broker_name == "Schwab":
            try:
                self._proactive_schwab_token_refresh(self.client)
                # Warmup: make a lightweight API call to force authlib to refresh if token
                # expired (proactive refresh may have returned early). This ensures we have
                # a valid access token before the dashboard makes its first request.
                warmup = getattr(self.client, "get_quote", None) or getattr(
                    self.client, "get_price_history_every_day", None
                )
                if callable(warmup):
                    symbol = getattr(self.secrets, "option_symbol", "$SPX")
                    if symbol:
                        symbol = symbol.split(".")[0]  # $SPX.X -> $SPX
                    symbol = symbol or "$SPX"
                    try:
                        r = warmup(symbol)
                        if r.status_code == 401:
                            raise BrokerConfigurationError(
                                "Schwab token expired (401). Delete your token file and restart:\n\n"
                                f"  rm {self.secrets.token_path}\n\n"
                                "Then restart the app; you'll be prompted to complete the OAuth flow."
                            )
                    except BrokerConfigurationError:
                        raise
                    except Exception:
                        pass  # warmup best-effort; proceed if it fails for other reasons
            except BrokerConfigurationError:
                raise
            except Exception as exc:
                _msg = str(exc).lower()
                if "refresh" in _msg or "token" in _msg or "invalid_client" in _msg or "401" in _msg:
                    raise BrokerConfigurationError(
                        "Refresh token expired. Schwab refresh tokens expire after ~7 days of "
                        "inactivity. Delete your token file and restart to re-authenticate:\n\n"
                        f"  rm {self.secrets.token_path}\n\n"
                        "Then restart the app; you'll be prompted to complete the OAuth flow."
                    ) from exc
                raise

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
                    body = r.text
                    try:
                        err = r.json()
                        body = err.get("message", err.get("error", body))
                    except Exception:
                        pass
                    print(f"Failed to fetch data: {r.status_code} – {body}")
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

if __name__ == "__main__":
    scheduler = GammaExposureScheduler()
    scheduler.run()
