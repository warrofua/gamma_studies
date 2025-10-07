"""Local configuration for the Charles Schwab API integration.

This module exposes the attributes expected by ``main.py`` when the
``BROKER`` environment variable is set to ``"schwab"`` (or when Schwab is the
first available provider).  The defaults pull values from environment
variables so that credentials never need to be committed to source control.

Update the environment variables referenced below—or override the module level
attributes directly in a local copy—to match your Schwab developer
application.
"""

from __future__ import annotations

import os
from pathlib import Path


def _optional_env(var_name: str) -> str | None:
    """Return the value of ``var_name`` if set to a non-empty string."""

    value = os.environ.get(var_name)
    if value:
        return value
    return None


# OAuth client identifier provided by the Schwab developer portal.  Schwab
# uses the ``{client_id}@AMER.OAUTHAP`` format for live applications.
api_key = os.environ.get("SCHWAB_API_KEY", "YOUR_SCHWAB_CLIENT_ID@AMER.OAUTHAP")

# Location on disk where the Schwab token JSON file should be stored.  This is
# created automatically during the first authentication flow.
token_path = os.environ.get(
    "SCHWAB_TOKEN_PATH", str(Path.home() / "schwab_token.json")
)

# Schwab requires a secure loopback redirect.  The application expects this
# value to default to ``https://127.0.0.1`` when not overridden.
redirect_uri = os.environ.get("SCHWAB_REDIRECT_URI", "https://127.0.0.1")

# Optional certificate bundle used when Schwab returns PKCS #12 material for
# the OAuth login.
cert_file = _optional_env("SCHWAB_CERT_FILE")

# Optional symmetric key used by ``schwab-py`` to encrypt refresh tokens.
token_encryption_key = _optional_env("SCHWAB_TOKEN_ENCRYPTION_KEY")

# Application specific defaults for plotting.
option_symbol = os.environ.get("SCHWAB_OPTION_SYMBOL", "$SPX.X")
strike_count = int(os.environ.get("SCHWAB_STRIKE_COUNT", "50"))


__all__ = [
    "api_key",
    "token_path",
    "redirect_uri",
    "cert_file",
    "token_encryption_key",
    "option_symbol",
    "strike_count",
]
