# Kalshi Weather Trading Bot

Automated trading system for weather derivative contracts on [Kalshi](https://kalshi.com). Ingests real-time data from 5 sources, builds probability estimates for temperature and precipitation outcomes across 9 US cities, and identifies mispriced contracts using statistical edge calculation and AI-assisted decision-making.

**[Live Dashboard](https://rocksbot.streamlit.app)** | **[Insight Memo](INSIGHT_MEMO.md)**

---

## Results

After 1,718 contract evaluations and 339 executed trades across 20 settled markets, I analyzed every outcome against verified weather data and found:

| Finding | Detail |
|---------|--------|
| BUY NO win rate | **61.3%** — fading overpriced brackets works |
| BUY YES win rate | **24.6%** — model overestimates narrow 2°F brackets |
| Large edge (>20¢) win rate | **12.5%** — market is usually right when disagreement is large |
| Overall P&L | **-$4.61** — near break-even on first iteration |

These findings led to a complete strategy redesign. Full analysis in the [Insight Memo](INSIGHT_MEMO.md) and on the [live dashboard](https://rocksbot.streamlit.app).

## How It Works

```
Weather Data (5 sources)  →  Edge Calculation  →  Filter Rules  →  Trade / No Trade
├── NWS API (forecasts)        ├── Fair probability      ├── Edge cap (20¢)
├── METAR (observations)       ├── Market price           ├── Block YES on brackets
├── ASOS 5-min (precision)     ├── Edge in cents          ├── Uncertainty limits
├── Ensemble models (4x)       └── Kelly sizing           ├── Source agreement
└── Claude AI (decisions)                                 └── One bet per event
```

**Data pipeline:** METAR observations with T-group precision parsing (0.1°C) → NWS gridpoint/daily/hourly forecasts with website cross-validation → 5-minute ASOS station data → ensemble blending of GFS, ECMWF, ICON, GEM models using inverse-MAE weighting → Claude AI holistic analysis.

**Key technical detail:** Kalshi settles on the NWS CLI (Climate Data) product, not raw METAR. The CLI uses different rounding rules and measurement windows (DST-aware). Getting this wrong means you can predict the temperature correctly and still lose the bet.

## Analytics Dashboard

Live at **[rocksbot.streamlit.app](https://rocksbot.streamlit.app)** — 10 interactive visualizations built with Streamlit + Plotly + SQLite:

- Accuracy & P&L by city
- BUY YES vs BUY NO performance
- Calibration curve (predicted vs actual probability)
- Edge size vs win rate (larger edge ≠ better outcomes)
- Forecast error by city
- Signal funnel (1,718 evaluations → 339 trades)
- P&L waterfall
- Kelly fraction vs win rate
- City × date heatmap
- Full raw data table

## Architecture

```
main.py                 # Orchestrator — data collection → analysis → execution
config.py               # Configuration, 5 trading profiles, city configs
run_bot.py              # CLI: --dry-run, --loop, --profile=conservative

analysis/
├── edge.py             # Signal generation, edge calculation (~1,800 lines)
├── trust_gate.py       # Hard/soft gate filters
├── claude_ai.py        # Claude API integration
├── llm_ai.py           # LLM prompt construction
└── validation.py       # Calibration tracking

data_sources/
├── nws.py              # NWS API + tabular website scraping
├── metar.py            # METAR/ASOS parsing with T-group extraction
├── ensemble.py         # 4-model ensemble blending
└── iem.py              # Iowa Mesonet (CLI reports)

trading/
└── kalshi_api.py       # Kalshi REST API (RSA-PSS auth)

dashboard/
├── app.py              # Streamlit dashboard (10 charts)
└── enrich_predictions.py  # Auto-fetch actuals, compute P&L

alerts/
└── telegram.py         # Trade alerts
```

## Trading Profiles

| Profile | Description |
|---------|-------------|
| **conservative** (default) | Data-driven rules from 339-trade analysis. Blocks YES on brackets, caps edge, requires source agreement. |
| llm_first | All gates removed, Claude AI decides everything |
| margin_of_safety | Benjamin Graham approach, 4+ source agreement, tenth-Kelly |
| safe | Threshold-preferred, NWS-aligned |
| aggressive | Original loose settings |

## Markets

- **Daily high temperature** — 9 cities (NYC, Chicago, Miami, Austin, Denver, LA, Philadelphia, Houston, Seattle)
- **Daily rain** — yes/no precipitation
- **Monthly rain** — cumulative precipitation thresholds

## Setup

```bash
pip install -r requirements.txt

# Set API keys (see config.py)
export KALSHI_API_KEY="your_key"
export ANTHROPIC_API_KEY="your_key"

# Dry run (no real trades)
python run_bot.py --dry-run --both

# Enrich predictions with actual outcomes
python dashboard/enrich_predictions.py

# Launch dashboard locally
streamlit run dashboard/app.py
```

## Built With

Python 3.12 · Kalshi API · NWS Weather API · Open-Meteo · Iowa Mesonet · Anthropic Claude · Streamlit · Plotly · SQLite · Pandas · Telegram Bot API
