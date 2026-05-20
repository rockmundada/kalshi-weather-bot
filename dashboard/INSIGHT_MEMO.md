# Performance Analysis Memo: Kalshi Weather Derivatives Trading Bot

**Author:** Rock Mundada  
**Date:** February 2026  
**Dataset:** 1,360 contract evaluations | 339 executed trades | 7 cities | Feb 10-11, 2026

---

## Executive Summary

After deploying an automated weather derivatives trading bot on Kalshi, I conducted a post-deployment performance analysis to evaluate model accuracy, calibration quality, and risk factors. The bot evaluated 1,360 contracts across 7 U.S. cities and generated 339 actionable trade signals, resulting in a net P&L of -$4.61 (essentially break-even at -1.4 cents per trade). While the system demonstrated strong filtering discipline and pockets of real edge, the analysis revealed critical asymmetries and calibration gaps that would need to be addressed before scaling.

---

## Key Findings

### 1. The BUY YES / BUY NO Asymmetry Is the Biggest Problem

The single most important finding: **BUY NO trades won 61.3% of the time, while BUY YES trades won only 24.6%.** This isn't a marginal difference — it's a fundamental model bias. The bot was systematically overestimating the probability of weather events occurring (temperatures landing in specific ranges). When it bet against outcomes (BUY NO), it was usually right. When it bet for them (BUY YES), it was usually wrong.

**Implication:** The probability model likely has a structural bias toward assigning too-high probabilities to narrow temperature bands. A simple recalibration — or even just disabling BUY YES signals — would have turned the -$4.61 loss into a net profit.

### 2. Calibration Is Poor in the Middle, Reasonable at Extremes

The calibration curve shows the model was reasonably accurate at the tails (very low and very high probability predictions) but significantly miscalibrated in the 20-50% range. At the 70-80% confidence bucket, the model predicted events with ~76% probability but the actual hit rate was only 6.3% — a catastrophic miss. Meanwhile, the 80-90% bucket showed 90.9% actual outcomes against 83.3% predicted, which is quite good.

**Implication:** The model needs a nonlinear recalibration, especially in the middle probability range. The extremes work; the mid-range doesn't.

### 3. Forecast Error Varied Dramatically by City

The bot's underlying temperature forecasts were very accurate for some cities and poor for others:
- **LA:** Near-perfect (mean error -0.1 deg F)
- **Miami:** Good (+0.5 deg F bias)
- **Denver:** Significant cold bias (-2.7 deg F) — forecast consistently too warm
- **Austin:** Cold bias (-2.5 deg F)

Denver on Feb 11 was the single worst case: the bot's forecast was ~6 deg F too warm, leading to $3.39 in losses on that city-date alone — 74% of total losses.

**Implication:** The NWS forecast data the bot relies on has uneven accuracy across geographies. Mountain/plains cities (Denver) and southern cities (Austin) showed the largest errors, possibly due to frontal systems being harder to forecast in those regions.

### 4. Edge Size Is Not a Reliable Signal of Quality

Counterintuitively, trades with the largest perceived edge (20-30 cents) had the *worst* win rate (12.5%) and generated the largest losses (-$9.60). Meanwhile, small-edge trades (0-10 cents) had a 65.7% win rate and were net profitable.

**Implication:** Large perceived edge likely correlates with the model diverging significantly from the market — and when the model and market disagree by a lot, the market was usually right. This suggests the bot should *cap* maximum edge rather than seeking it out, or at minimum apply extra skepticism to high-edge signals.

### 5. The Signal Funnel Works Well

Only 24.9% of evaluated contracts became trades. The bot's filtering pipeline (trust gate, source agreement checks, edge thresholds, probability floors) correctly suppressed 75% of opportunities. This is good discipline — it means the bot isn't overtading.

### 6. Rain Contracts Were the Bright Spot

The 16 rain contract trades had a 62.5% win rate and generated +$5.35 — the best-performing market type by far. The rain model appears to have genuine edge, though the sample size is too small (n=16) to draw definitive conclusions.

---

## Recommendations for Model Improvement

1. **Disable or heavily discount BUY YES signals** until the probability model is recalibrated. BUY NO alone would have been profitable.

2. **Implement city-specific forecast bias corrections.** Denver and Austin need warm-bias adjustments based on historical NWS error patterns.

3. **Cap maximum edge at ~20 cents** or add an extra confirmation layer for high-edge trades. The market is usually more right than the model when they disagree by a large amount.

4. **Recalibrate the probability model** using isotonic regression or Platt scaling, particularly in the 20-80% range where miscalibration is worst.

5. **Expand the rain model** — it showed the strongest risk-adjusted performance and may represent a less efficient market segment.

6. **Collect more data.** Two days of trading is enough to identify structural issues but not enough to validate fixes with statistical confidence. A minimum of 30-60 days of live data would be needed before drawing production-grade conclusions.

---

## Methodology

- **Outcome data:** Actual high temperatures sourced from Iowa Environmental Mesonet (ASOS/METAR stations: KNYC, KMDW, KMIA, KAUS, KDEN, KLAX, KPHL). Precipitation data from the same source.
- **P&L calculation:** Assumes $1 per contract. BUY YES at X cents: profit = (100-X) if YES, loss = -X if NO. BUY NO at X cents: profit = (100-X) if NO, loss = -X if YES.
- **Calibration:** Model's `fair_prob` field bucketed into deciles and compared against actual YES outcome rate.
- **Tools:** Python, SQLite, Pandas, Plotly, Streamlit.
