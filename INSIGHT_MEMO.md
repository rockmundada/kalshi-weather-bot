# What I Learned From 339 Automated Weather Trades

**Rock Mundada | May 2026**

---

I built a bot to trade weather derivatives on Kalshi — contracts that pay out based on whether a city's high temperature lands in a specific range. The bot pulls data from 5 sources (NWS forecasts, METAR observations, ASOS station data, ensemble weather models, and Claude AI), calculates where it thinks the market is mispriced, and generates trade signals.

After 339 trade signals across 20 settled markets in 7 cities, I went back and scored every one against actual outcomes. Here's what the data showed.

## The big finding: BUY NO crushed BUY YES

BUY NO trades (betting the temperature WON'T land in a specific range) won 61.3% of the time. BUY YES trades won 24.6%. That's not a small gap — it's the difference between a profitable strategy and a losing one.

Why? Kalshi's temperature markets use narrow 2-degree brackets (e.g., "74-75°F"). NWS forecasts have roughly a 2°F average error. Betting YES on any single 2°F window is basically a coin flip, but the market often prices these brackets at 30-50 cents as if there's real signal. Betting NO against overpriced brackets is where the edge actually lives.

## Larger perceived edge was a contra-indicator

This one surprised me. Trades where my model disagreed with the market by more than 20 cents had a 12.5% win rate. Trades with a small edge (under 10 cents) won about 65% of the time.

The explanation is straightforward: when my model says a contract is worth 80 cents and the market says 50 cents, the market is almost always right. Professional weather traders have access to the same NWS data. A large disagreement means my model is wrong, not that I found a hidden opportunity.

## The multi-bracket trap

Early on, the bot would sometimes buy YES on two adjacent brackets — say, 74-75°F and 76-77°F. This is mathematically guaranteed to lose money. You pay for two contracts and at most one can pay out. The 339-trade dataset made this obvious. I added a rule: one bet per city per day, period.

## What I changed

Based on this analysis, I rewrote the bot's strategy:

- **Never buy YES on narrow 2°F brackets** — only allow YES on threshold contracts (e.g., "above 76°F") which are more forgiving
- **Cap edge at 20 cents** — reject trades where model-market disagreement is too large
- **Require 2+ data sources to agree** before any trade
- **Block trades when forecast uncertainty exceeds 5°F** — if the models can't agree, don't bet

The dashboard at **rocksbot.streamlit.app** shows all of this — the BUY YES vs NO split, the edge-size paradox, calibration curves, and per-city forecast error. I'm now running a 4-week dry run on the new rules to validate before going live.

## Bottom line

The model's first instinct — buy YES on the bracket it thinks is most likely — was wrong. The real edge is in fading overpriced brackets and only taking YES positions on threshold contracts where the outcome is nearly locked. Sometimes the best trade is the one you don't make.
