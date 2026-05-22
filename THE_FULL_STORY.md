# The Full Story: Building a Kalshi Weather Trading Bot From Scratch

**Rock Mundada — Texas A&M Applied Math + Stats, Class of 2025**

---

## How it started

I found Kalshi sometime in college — it's the only CFTC-regulated event contract exchange in the US. They have markets on everything: elections, economics, weather. The weather markets caught my eye because they felt solvable. Unlike politics or the stock market, weather is governed by physics. There are government forecasts with known error rates. There's an entire infrastructure of weather stations reporting temperatures every 5 minutes. It felt like a math problem, not a gambling problem.

The basic idea: Kalshi lists contracts like "Will NYC's high temperature be 74-75°F tomorrow?" and the market prices them. If I can build a better estimate of what the temperature will actually be than what the market implies, I have edge. Buy when the market underprices, sell when it overprices.

Simple in theory. I was very wrong about how simple it would be in practice.

---

## The manual phase

Before I built anything, I traded manually. In January 2026, Dallas had a snowstorm forecast — 2+ inches over the weekend. The Kalshi contract looked underpriced, so I bet about $500 on it. The snow came. I doubled to $1,000.

Then I got overconfident. That night I spread the money across temperature contracts in multiple cities based on what looked like obvious mispricings. While I slept, weather shifted overnight. I lost most of it.

That was the moment I realized I needed a system instead of gut calls. And that's when I started building.

---

## The first version: just get it working

I started building in Python. The first challenge was just getting data. There's no single "weather API" that gives you what you need for Kalshi. You need:

- **What the forecast says** — but the NWS has like 4 different forecast products (daily, hourly, gridpoint, tabular) and they don't always agree
- **What's been observed so far today** — METAR reports from airports, but the raw temperature in a METAR is rounded to whole degrees Celsius, and there's a hidden "T-group" in the remarks section with 0.1°C precision that most people don't parse
- **What the settlement source actually is** — this took me weeks to figure out. Kalshi doesn't settle on METAR data. They settle on the NWS CLI (Climate Data) product, which is a post-midnight summary. The CLI rounds differently than raw METAR. If you don't know this, you'll be right about the temperature and still lose the bet.

I got the Kalshi API working first. Then the NWS API. Then I realized the NWS API forecasts sometimes disagreed with what the NWS website showed, so I added website scraping to cross-validate. Then I added METAR parsing. Then I learned about T-groups and added precision temperature extraction.

The T-group discovery came from a real trade. On January 29, all three data sources (API, T-group, proxy station) agreed at 16°F for Chicago. I bought YES on the 16-17°F bracket at 75 cents. During the afternoon, the API display jumped to 18°F. Everyone panic-sold — my position crashed from 60 cents to 14 cents. But the T-group (the precise 0.1°C measurement in the METAR remarks) still showed 17°F. I held. The CLI report confirmed 17°F that night. The contract settled at $1. I won.

That single trade proved the entire data trust hierarchy: CLI > T-group > synoptic 6-hour max > API display. The API rounds and converts (F→C→F), introducing up to 1°F of error. The T-group is what the CLI actually uses. Most retail traders don't parse it.

Then I added the Open-Meteo ensemble API to blend 4 global weather models (GFS, ECMWF, ICON, GEM) using inverse-MAE weighting.

By the time the data pipeline was "done" I had 5 API integrations and the codebase was already several thousand lines.

---

## The AI layer

I'm an Applied Math major, not a meteorologist. So I added Claude AI (Anthropic's model) as a decision-making layer. The idea was: feed the model all the weather data, market prices, risk flags, and forecast context, and let it make holistic trade/no-trade decisions.

I built a prompt system that constructs a detailed weather briefing for each city — current observations, forecast high, ensemble model spread, uncertainty estimates, hours remaining until settlement, historical forecast error — and sends it all to Claude with extended thinking (10,000 token budget). The model analyzes everything and outputs structured trade recommendations.

This was the "LLM-first" profile. Let the AI decide everything. Remove the statistical gates. Trust the model.

It was an interesting approach. It was also not profitable.

---

## The gate-removal mistake

Before the first live run, the bot had about 15 safety gates that each independently blocked trades: "CLI not final" blocked all same-day trades, "model spread too wide" blocked cities where forecasts disagreed by more than 6°F, "same-day max hours remaining" only allowed trading in the last 2 hours, minimum book size requirements blocked thin markets, and so on.

The problem was that every single trade got blocked by at least one gate. The bot evaluated hundreds of contracts and recommended zero.

So I told Claude Code to remove everything. Convert all hard blocks to soft warnings. Set all minimums to zero. Open it up.

It worked — the bot started producing dozens of BUY recommendations. But many were the kind of "too good to be true" signals that should have stayed blocked: 2-cent contracts the model thought were worth 80 cents, brackets where the NWS forecast actually disagreed with the model's estimate, trades on contracts with zero liquidity.

The lesson was immediate and expensive: removing governance controls increased volume but killed reliability. This became one of the most important things I learned from the entire project — it's the same principle behind model risk management at a bank. You don't remove the controls just because they're annoying. You figure out which controls are wrong and fix those specifically.

The conservative profile I built later was the direct response to this mistake. Every rule in it traces back to a specific failure.

---

## First real data: February 10-11, 2026

I ran the bot live for the first time on February 10-11 across 7 cities. It evaluated 1,360 contracts across those two days (now 1,718 including continued dry runs through May 2026) and generated 339 actionable trade signals. I logged every single one — the signal, the market price, the fair probability, the edge, the Kelly sizing, everything.

Then the markets settled.

**Results: 48.1% win rate, -$4.61 total P&L.** Basically broke even, but on the wrong side.

The initial reaction was "the bot doesn't work." The second reaction, after actually looking at the data, was much more interesting.

---

## The analysis: finding 4 root causes

I didn't just look at the total number. I built an enrichment pipeline that automatically fetches actual weather outcomes from the Iowa Environmental Mesonet and scores every single prediction against reality. Then I built a dashboard to visualize all of it.

Here's what the data showed:

### Root Cause 1: BUY YES on narrow brackets is a losing game

Kalshi temperature markets have 6 contracts per city per day — 4 middle brackets that are 2°F wide (like "74-75°F") and 2 tail thresholds ("above 76°F" or "below 73°F").

BUY YES on the narrow brackets had a **24.6% win rate**. BUY NO had a **61.3% win rate**.

Why? NWS forecasts have roughly a 2°F mean absolute error. So the probability of the temperature landing in any specific 2°F window is already low — maybe 25-35% for the "most likely" bracket. But the market was pricing these at 30-50 cents, as if there was real confidence. The model was buying YES because it thought these brackets were underpriced. They weren't. The model was overconfident.

BUY NO worked because you're fading overpriced brackets. If a bracket is priced at 50 cents but the true probability is only 25%, buying NO at 50 cents gives you 75% expected value on a 50-cent contract.

### Root Cause 2: Large edge was a contra-indicator

This was the most surprising finding. I assumed bigger edge = better trade. The data said the opposite.

- Trades with 0-10 cents of perceived edge: ~65% win rate
- Trades with 10-20 cents: ~37% win rate
- Trades with 20-30 cents: **~10% win rate**

When my model said a contract was worth 80 cents and the market said 50 cents, the market was almost always right. Professional weather traders — some of whom have meteorology degrees and use models I don't have access to — are setting those prices. A 30-cent disagreement doesn't mean I found a hidden gem. It means my model is wrong.

I capped edge at 20 cents. Any trade where the model-market disagreement is larger than that gets auto-rejected.

### Root Cause 3: The multi-bracket trap

Early on, the bot would sometimes buy YES on two or three adjacent brackets. Like: buy YES on 74-75°F AND 76-77°F. The logic made sense on a contract-by-contract basis — each looked like it had positive expected value.

But together, it's a guaranteed loss. Only one bracket can resolve YES. If you buy two for 40 cents each, you spend 80 cents and at most win 100 cents back — and that's only if one of them hits. If neither hits, you lose 80 cents. And with a 2°F forecast error, "neither hits" is the most likely outcome.

I added a hard rule: one bet per city per day, period. The bot picks the single best opportunity and ignores everything else.

### Root Cause 4: Night-before uncertainty

Trades placed the evening before (for tomorrow's settlement) had the worst results. This makes sense — the forecast uncertainty is at its maximum 12-24 hours out. By the afternoon of the settlement day, you have actual observed temperatures, updated METAR data, and the forecast error is much smaller.

I added hard blocks: if forecast uncertainty exceeds 5°F or the ensemble models disagree by more than 6°F, the bot won't trade. I also set a preference for trades with fewer than 6 hours remaining.

---

## Building the dashboard

After doing this analysis, I realized I needed to be able to see it, not just calculate it. I built an analytics dashboard in Streamlit with Plotly charts:

**10 interactive visualizations:**
1. Accuracy and P&L by city — dual-axis chart showing which cities make/lose money
2. BUY YES vs BUY NO performance — the single most important chart. 24.6% vs 61.3% is the whole story in one image.
3. Calibration curve — model's predicted probability vs actual outcome rate. Shows the model is overconfident in the 30-60% range.
4. Edge size vs win rate — proves larger edge = worse outcomes. This chart changed how I think about the whole strategy.
5. Forecast error by city — Philadelphia runs 2.2°F hot, Denver runs 2.5°F cold. Every city has a systematic bias.
6. Signal funnel — how 1,718 evaluations filter down through all the gates to 339 trades
7. P&L waterfall — Miami and Denver were the biggest losers, Chicago was the biggest winner
8. Kelly fraction vs win rate — high-conviction bets had tiny sample sizes; moderate-conviction bets clustered around 53%
9. City-by-date performance heatmap — shows exactly where money was made and lost
10. Raw data table — every trade, every outcome, fully searchable

I deployed it to Streamlit Cloud at **rocksbot.streamlit.app** connected to the GitHub repo. The whole thing updates automatically when I push new data.

---

## The conservative profile

Based on all of this, I designed a new trading strategy from the ground up. Not based on theory or research papers — based on my own 339 data points.

**The rules:**
- Never buy YES on narrow 2°F brackets (24.6% win rate historically)
- Allow YES on threshold contracts (">= 76°F") which are more forgiving — you're right even if the temp is 78° or 82°
- Always allow BUY NO (61.3% win rate historically)
- Cap edge at 20 cents (trades above this had 12.5% win rate)
- One bet per city per day (kills the multi-bracket trap)
- Require 2+ data sources to agree before any trade
- Block when uncertainty exceeds 5°F or models spread exceeds 6°F
- Narrow brackets need 5 cents+ edge (vs 3 cents for thresholds) since they're inherently harder
- Fifth-Kelly position sizing (conservative, survive bad weeks)
- Maker-only orders (research shows maker returns beat taker by 22 percentage points)

I implemented this as a "conservative" trading profile in the config. The bot now supports 5 profiles — conservative, aggressive, margin_of_safety, safe, and llm_first — and defaults to conservative.

The best example of the new approach working: Denver, February 9. Every data source agreed — NWS forecast 70°F, all four ensemble models clustered 67-70°F, current METAR at 70°F, and the day was nearly over. The market was pricing "above 71°F" at 41% probability. I bought NO at 59 cents. The CLI confirmed 70°F. I won. It wasn't exciting — no 30x payout, no "hidden gem." Just a data-aligned, forgiving threshold bet where I had genuine edge. That's what the conservative profile is designed to find.

The first dry run under the new rules (May 21, 2026) analyzed 86 contracts across 7 cities and correctly rejected all of them. Every rejection had a clear reason: edge too large (market pricing at 50 cents across the board = placeholder/illiquid pricing), BUY YES on bracket blocked, model spread too wide, etc. The bot is being selective. That's the point.

---

## What I learned about markets

The biggest lesson wasn't about weather. It was about the difference between knowing the answer and having edge.

I could build a model that predicts NYC's high temperature within 2°F. Great. But the market is already pricing in that prediction, because every other trader has access to the same NWS data. Edge doesn't come from having a good model. It comes from:

1. **Knowing what the market is systematically wrong about** — like overpricing narrow brackets
2. **Having better data** — like T-group precision temps that most retail traders don't parse
3. **Having better discipline** — like not buying YES on adjacent brackets, which feels smart but is mathematically stupid
4. **Knowing when NOT to trade** — which is most of the time

The favorite-longshot bias is real. Contracts priced under 10 cents lose money systematically — they feel cheap but they almost never pay out. The maker-taker spread matters enormously — posting limit orders instead of taking asks improved expected returns by 22 percentage points according to the research.

One specific loss: I had a trade where the time-series data showed 54°F all afternoon, so I bet on the 54-55°F bracket. But the CLI report said 53°F. The NWS uses "round half up asymmetric" rounding (per FMH-1 §2.6.3), and the actual precise measurement was just below 53.5°F, which rounds down. I was right about the temperature within 1°F and still lost the bet. After that I read the actual federal handbook and verified the rounding rules against three independent sources.

And the most expensive lesson: when your model disagrees with the market by a lot, the market is usually right. Humility is a trading strategy.

---

## What I learned about building things

This project taught me more than any class I took.

**On data engineering:** There is no clean API for anything. The NWS API sometimes disagrees with the NWS website by 3-5 degrees. METAR body temperatures are rounded differently than T-group temperatures. The ASOS station data uses Celsius while Kalshi settles in Fahrenheit, and the rounding convention matters because it can flip a settlement. Every data source has quirks and you just have to handle them.

**On AI as a tool:** Claude Code helped me build this. I'm not hiding that. But AI didn't design the strategy — the data did. AI helped me write the code faster, debug API issues, build the dashboard, and analyze patterns. Using AI well is a skill. Knowing what to ask, knowing when the output is wrong, knowing how to architect a system that an AI can help you build — that's the real skill.

**On the gap between theory and practice:** I could have read 10 research papers on Kelly criterion and weather forecasting and still not known that BUY YES on narrow brackets has a 24.6% win rate in my specific system. You have to run the thing, collect the data, and look at it honestly.

**On shipping:** The bot is 10,800+ lines of Python. It integrates 5 APIs. It has a live dashboard with 10 interactive charts. It has an automated backtesting pipeline. It has a one-command script that runs the bot, enriches data, and pushes to GitHub. None of this was assigned. Nobody asked me to build it. I just wanted to see if I could make money trading weather.

---

## Current state (May 2026)

- 10,800+ lines of Python across ~20 files
- 5 API integrations (Kalshi, NWS, Open-Meteo, Iowa Mesonet, Anthropic Claude)
- 9 cities tracked, 3 market types
- 1,718 contract evaluations logged
- 339 trades scored against verified outcomes
- Live dashboard at rocksbot.streamlit.app
- Public GitHub at github.com/rockmundada/kalshi-weather-bot
- Conservative profile operational, running daily dry runs
- 4-week validation period before going live with real money

---

## What's next

Run the conservative profile in dry-run mode for 4 weeks. Collect data. Score it. If the rules hold up — if BUY NO keeps winning, if the edge cap keeps rejecting bad trades, if the uncertainty blocks prevent losses — then go live with small money.

The goal was never to get rich trading weather. It was $5-15 a day of fun money while learning everything I could about building real systems that touch real markets. The project did that and more.

The fact that I lost $4.61 and learned all of this is probably the best ROI of anything I've done in college.
