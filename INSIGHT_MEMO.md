# Performance Analysis: Kalshi Weather Trading Bot

**Rock Mundada | May 2026**
**Dataset:** 1,718 contract evaluations | 339 executed trades | 7 cities | Feb 10-11 + May 20, 2026

---

## Summary

After deploying an automated weather derivatives trading bot on Kalshi, I scored every prediction against verified weather outcomes from Iowa Environmental Mesonet. The bot evaluated 1,718 contracts across 7 US cities and generated 339 actionable trade signals, resulting in a net P&L of -$4.61 — essentially break-even at -1.4 cents per trade. The analysis revealed structural patterns that led to a complete strategy redesign.

---

## Key Findings

### 1. BUY NO crushed BUY YES

**BUY NO trades won 61.3% of the time. BUY YES trades won 24.6%.** The bot was overestimating the probability of temperatures landing in specific 2°F brackets. NWS forecasts have roughly 2°F mean absolute error — betting YES on any single 2°F window is close to a coin flip. Fading overpriced brackets (BUY NO) is where edge actually lives.

### 2. Larger edge was a contra-indicator

Trades with 0-10¢ edge won ~65%. Trades with 20-30¢ edge won 12.5%. When the model and market disagree by a lot, the market — set by professional weather traders — was almost always right. Large perceived edge means the model is wrong, not that the market is mispriced.

### 3. Calibration was poor in the middle range

The model was accurate at extremes (very high and very low probability) but badly miscalibrated in the 20-80% range. At the 70-80% confidence bucket, the model predicted ~76% but the actual hit rate was 6.3%.

### 4. Forecast error varied by city

- LA: near-perfect (-0.1°F mean error)
- Miami: good (+0.5°F)
- Denver: significant cold bias (-2.7°F) — $3.39 in losses on Feb 11 alone
- Austin: cold bias (-2.5°F)

### 5. The multi-bracket trap

Buying YES on adjacent brackets (e.g., 74-75°F AND 76-77°F) is mathematically guaranteed to lose. You pay for two contracts; at most one pays out.

### 6. Rain contracts outperformed

16 rain trades had a 62.5% win rate and +$5.35 P&L — the best market type, though sample size is small.

---

## What I Changed

Based on these findings, I redesigned the strategy:

1. **Block BUY YES on narrow 2°F brackets** — only allow YES on threshold contracts (e.g., "above 76°F") which are more forgiving
2. **Cap edge at 20¢** — reject trades where model-market disagreement is too large
3. **One bet per city per day** — eliminates the multi-bracket trap
4. **Require 2+ data sources to agree** before any trade
5. **Hard-block on uncertainty >5°F or model spread >6°F**
6. **Maker-only orders** — research shows a 22 percentage point return advantage over taker orders

These rules are now implemented as the "conservative" trading profile. Currently running daily dry runs to validate before going live.

---

## Methodology

- **Outcome data:** Actual high temperatures from Iowa Environmental Mesonet (ASOS/METAR stations: KNYC, KMDW, KMIA, KAUS, KDEN, KLAX, KPHL)
- **P&L calculation:** $1/contract. BUY YES at X¢: profit = (100-X) if YES, loss = -X if NO. BUY NO: inverse.
- **Calibration:** fair_prob bucketed into deciles, compared against actual YES outcome rate
- **Limitation:** Outcome data covers 3 trading days (Feb 10-11, May 20). Conclusions are directional, not statistically definitive. Ongoing dry runs will expand the dataset.
