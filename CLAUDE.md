# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Real-time gamma exposure plotter for options trading. Fetches live option chain data from Charles Schwab or TD Ameritrade, calculates per-strike gamma exposure, and visualizes it via matplotlib (live plots) or Streamlit (web dashboard with AI-generated interpretation).

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run live plotter (matplotlib, market hours only)
python main.py

# Run web dashboard (Streamlit)
streamlit run dashboard.py

# Validate Schwab credentials/config
python check_schwab_config.py
```

No test suite exists. Validate manually by running the above commands.

## Architecture

### Core Data Flow

```
Broker API (Schwab/TDA) → main.py → gamma_analysis.py → plotter.py
                                  → db_storage.py (PostgreSQL, optional)
dashboard.py (separate entry point, independent of main.py)
```

### Key Files

- **`main.py`** — `GammaExposureScheduler` orchestrates the poll loop (every 4s, market hours 9:30–4:15 ET). Handles broker auth: Schwab uses manual OAuth (user copies callback URL); TDA uses automated Selenium login. Broker is auto-detected or forced via `BROKER` env var.

- **`gamma_analysis.py`** — `calculate_gamma_exposure()` computes per-strike GEX as `multiplier × spot × gamma × volume × contract_size × spot × 0.01 / 1B`. Tracks deltas vs previous snapshot. `get_per_strike_details()` aggregates call/put OI and volume by strike.

- **`plotter.py`** — `RealTimeGammaPlotter` manages a 3-panel matplotlib figure updated in-place each poll:
  - Panel 1: Per-strike GEX histogram
  - Panel 2: Change in GEX per strike (red = largest changes)
  - Panel 3: Total GEX (blue, left axis) + spot price (green, right axis); red/green dots for top-5 strikes with largest positive/negative changes; rolling mean±StdDev bands from deques of 100 updates

- **`dashboard.py`** — Standalone Streamlit app. Interactive symbol selector, Plotly heatmap of GEX by strike/expiration, Gemini LLM interpretation of market structure, "King Node" / support / resistance identification.

- **`db_storage.py`** — Optional PostgreSQL raw snapshot storage. Fails silently if DB unavailable. Needs table: `spx_options_data(id SERIAL PRIMARY KEY, data JSONB, fetched_at TIMESTAMP)`.

- **`secretsSchwab.py`** — Credentials module (gitignored). Template included. Credentials come from `.env`; do not hardcode values here.

### Configuration

All secrets via `.env` (gitignored):

| Variable | Purpose |
|---|---|
| `SCHWAB_API_KEY` / `SCHWAB_APP_SECRET` | OAuth credentials |
| `SCHWAB_REDIRECT_URI` | Default: `https://127.0.0.1` |
| `SCHWAB_TOKEN_PATH` | Token file location |
| `SCHWAB_OPTION_SYMBOL` | Default: `$SPX` |
| `SCHWAB_STRIKE_COUNT` | Number of strikes to fetch |
| `GEMINI_API_KEY` | For dashboard LLM interpretation |
| `DB_STORE_ENABLED` | Set to `0` to disable PostgreSQL |
| `BROKER` | Force `schwab` or `tda` |

`secretsSchwab.py` and `secretsTDA.py` are the credential modules loaded by `main.py` — keep them local, never commit them.

### Broker Support

- **Schwab** (primary): `schwab-py` library, OAuth with 7-day token refresh logic
- **TDA** (fallback): `tda-api` library, Selenium-automated login

## gstack

Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

Available gstack skills:
- `/browse` — fast headless Chromium browsing (~100ms/command after first call)
- `/plan-ceo-review` — CEO/founder-mode plan review
- `/plan-eng-review` — Eng manager-mode plan review
- `/review` — pre-landing PR review
- `/ship` — merge, test, version bump, changelog, PR creation
- `/retro` — weekly engineering retrospective
