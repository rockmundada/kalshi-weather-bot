# Kalshi Weather Bot — Performance Analytics Dashboard

Post-deployment performance analysis of an automated weather derivatives trading system on Kalshi. Built to answer the question: **where did the model have real edge, and where did it break down?**

![Dashboard Preview](preview.png)

## What This Is

After building and running a weather derivatives trading bot that evaluated 1,360 contracts across 7 U.S. cities, I wanted to understand how it actually performed. This project takes the bot's raw prediction logs, enriches them with verified historical weather outcomes, and produces an interactive analytics dashboard with calibration curves, P&L attribution, forecast error analysis, and actionable findings.

## Key Findings

- **339 trades executed**, 48.1% win rate, -$4.61 net P&L (essentially break-even)
- **Critical asymmetry:** BUY NO signals won 61.3% vs. BUY YES at only 24.6% — a structural model bias
- **Calibration gaps:** Model was well-calibrated at probability extremes but significantly off in the 20-80% range
- **Forecast error varied by city:** LA forecasts were near-perfect (-0.1 deg F error), Denver was off by -2.7 deg F
- **Edge paradox:** Trades with the largest perceived edge (20-30 cents) had the worst win rate (12.5%)
- **Rain contracts outperformed:** 62.5% win rate, +$5.35 P&L (best market type)

Full analysis in [INSIGHT_MEMO.md](INSIGHT_MEMO.md).

## Project Structure

```
dashboard/
├── app.py                    # Streamlit dashboard (8 interactive charts)
├── enrich_predictions.py     # Data pipeline: fetch outcomes, compute P&L
├── analysis_queries.sql      # 10 documented SQL queries against the data
├── kalshi_analytics.db       # SQLite database with enriched data
├── enriched_predictions.csv  # Enriched dataset with outcomes
├── INSIGHT_MEMO.md           # One-page analysis memo with recommendations
└── README.md                 # This file
```

## Data Pipeline

1. **Raw data:** 1,360 rows from the bot's `predictions.csv` (40 columns: city, market type, signal, fair probability, market price, edge, Kelly fraction, forecast temps, station data, volume, etc.)

2. **Outcome enrichment:** Pulled actual high temperatures from Iowa Environmental Mesonet (ASOS/METAR) for all 7 cities on Feb 10-11, 2026. Determined contract resolution (YES/NO) by comparing actual temps against contract strike prices.

3. **P&L computation:** For each of the 339 actionable BUY signals, calculated profit/loss assuming $1/contract based on contract resolution.

4. **SQLite load:** All enriched data loaded into `kalshi_analytics.db` for SQL analysis.

## Dashboard Visualizations

| Chart | What It Shows |
|-------|---------------|
| Accuracy & P&L by City | Win rate and dollar P&L across 7 cities |
| BUY YES vs BUY NO | The critical performance asymmetry |
| Calibration Curve | Model probability vs. actual outcome rate |
| Edge Size vs. Win Rate | Whether larger perceived edge = better trades |
| Forecast Error by City | Temperature forecast accuracy by geography |
| Signal Funnel | How the bot filtered 1,360 evaluations to 339 trades |
| P&L Waterfall | Dollar attribution by city |
| Kelly Fraction vs. Win Rate | Whether higher-conviction bets outperformed |
| City x Date Heatmap | Performance breakdown by city and date |

## How to Run

```bash
# Install dependencies
pip install streamlit plotly pandas

# Rebuild the enriched dataset and SQLite DB (optional — already included)
python enrich_predictions.py

# Launch the dashboard
streamlit run app.py
```

Then open http://localhost:8501 in your browser.

## Tech Stack

- **Python** — data pipeline and analysis
- **SQLite** — structured data storage and queries
- **Pandas** — data manipulation
- **Plotly** — interactive visualizations
- **Streamlit** — dashboard framework
- **Iowa Environmental Mesonet** — historical weather data source (ASOS/METAR)

## What I'd Do Differently

Based on this analysis, the three highest-impact changes would be:

1. **Disable BUY YES signals** (or recalibrate the probability model) — BUY NO alone would have been profitable
2. **Add city-specific forecast bias corrections** — Denver and Austin need warm-bias adjustments
3. **Cap maximum edge** at ~20 cents — the market was usually right when disagreement was large
