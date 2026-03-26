# Kalshi Weather Contract Trading Bot

Automated trading system for weather derivative contracts on [Kalshi](https://kalshi.com), built to identify mispriced contracts by comparing real-time meteorological data against market-implied probabilities.

## What It Does

The bot ingests weather data from 5 sources, builds probability distributions for temperature and precipitation outcomes across multiple US cities, and identifies contracts where the market price diverges significantly from the model's fair value. When edge exceeds configurable thresholds, it sizes positions using Kelly criterion and executes trades via Kalshi's API.

## Architecture

```
data_sources/          # Real-time weather data ingestion
├── nws.py             # National Weather Service API (forecasts, alerts, 5-min obs)
├── metar.py           # Aviation weather reports (METAR/TAF from AWC)
├── iem.py             # Iowa Environmental Mesonet (CLI reports, historical)
├── ensemble.py        # Ensemble model forecasts (spread analysis)
└── wethr.py           # Wethr API (high/low forecasts, precip, NWS data)

analysis/              # Signal generation and validation
├── edge.py            # Probability distributions, edge calculation, Kelly sizing
├── trust_gate.py      # Hard/soft gate filters for signal quality
├── claude_ai.py       # Claude API integration for qualitative analysis
├── openai_ai.py       # OpenAI API integration (alternative LLM)
├── llm_ai.py          # LLM orchestration layer
└── validation.py      # Calibration tracking, NWS bias computation

trading/
└── kalshi_api.py      # Kalshi REST API client (RSA-PSS auth, order placement)

alerts/
└── telegram.py        # Real-time trade alerts and daily summaries

main.py                # Orchestrator — coordinates data, analysis, and execution
config.py              # All configuration, thresholds, and API settings
backtest.py            # Historical backtesting framework
rules_catalog.py       # Market-specific trading rules
```

## Key Technical Details

- **Probability modeling:** Builds temperature distributions using forecast point estimates + calibrated uncertainty bands, accounting for NWS forecast bias, time-of-day effects, and model spread
- **Position sizing:** Full Kelly criterion implementation with configurable fractional Kelly and maximum position limits
- **Trust gates:** Two-tier filtering system — hard gates (never override) and soft gates (LLM can override with justification) — to prevent trading on low-confidence signals
- **Multi-model validation:** Cross-references NWS, METAR observations, ensemble spreads, and historical bias to reduce false signals
- **1,300+ logged predictions** in `data/predictions.csv` for ongoing calibration analysis

## Markets Covered

- Daily high temperature (threshold and bracket contracts)
- Daily rainfall (yes/no)
- Monthly cumulative precipitation

## Built With

Python 3.12 · Kalshi API · NWS API · AWC METAR · Claude API · OpenAI API · Telegram Bot API

## Setup

1. Clone the repo
2. `pip install -r requirements.txt`
3. Set environment variables for API keys (see `config.py` for required variables)
4. `python run_bot.py`

> **Note:** API keys are not included. You'll need your own Kalshi, Anthropic, and OpenAI credentials.
