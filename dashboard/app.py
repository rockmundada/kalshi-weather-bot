import sqlite3
import os
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
import numpy as np

st.set_page_config(
    page_title="Kalshi Bot Performance Analytics",
    page_icon="📊",
    layout="wide",
)

DB_PATH = os.path.join(os.path.dirname(__file__), "kalshi_analytics.db")


@st.cache_data(ttl=300)  # refresh every 5 minutes
def load_data():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql("SELECT * FROM predictions", conn)
    weather = pd.read_sql("SELECT * FROM actual_weather", conn)
    conn.close()

    for col in ["fair_prob", "market_price", "edge_cents", "kelly_fraction",
                 "forecast_high_f", "actual_high_f", "hours_remaining",
                 "volume", "open_interest", "spread_cents"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["pnl_cents"] = pd.to_numeric(df["pnl_cents"], errors="coerce")
    df["limit_price_cents"] = pd.to_numeric(df["limit_price_cents"], errors="coerce")
    df["is_actionable"] = df["is_actionable"] == "1"
    df["prediction_correct"] = df["prediction_correct"] == "1"

    return df, weather


df, weather = load_data()
trades = df[df["is_actionable"] & df["pnl_cents"].notna()].copy()

# ===========================================================================
# HEADER
# ===========================================================================
st.title("Kalshi Weather Bot — Performance Analytics")
_dates = sorted(df["contract_date"].dropna().unique())
_date_range = f"{_dates[0]} to {_dates[-1]}" if len(_dates) > 1 else (_dates[0] if _dates else "N/A")
_n_cities = df["city"].nunique()
st.markdown(
    f"Post-deployment analysis of **{len(df):,} contract evaluations** and "
    f"**{len(trades)} executed trades** across {_n_cities} cities ({_date_range})."
)

# ===========================================================================
# TOP-LEVEL KPIs
# ===========================================================================
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Total Trades", f"{len(trades)}")
k2.metric("Win Rate", f"{trades['prediction_correct'].mean():.1%}")
k3.metric("Total P&L", f"${trades['pnl_cents'].sum()/100:.2f}")
k4.metric("Avg P&L/Trade", f"{trades['pnl_cents'].mean():.1f}¢")
k5.metric("Contracts Evaluated", f"{len(df):,}")

st.divider()

# ===========================================================================
# ROW 1: Accuracy by City + BUY YES vs NO
# ===========================================================================
col1, col2 = st.columns(2)

with col1:
    st.subheader("Accuracy & P&L by City")
    city_stats = trades.groupby("city").agg(
        trades=("pnl_cents", "count"),
        wins=("prediction_correct", "sum"),
        pnl=("pnl_cents", "sum"),
    ).reset_index()
    city_stats["accuracy"] = city_stats["wins"] / city_stats["trades"]
    city_stats["pnl_dollars"] = city_stats["pnl"] / 100
    city_stats = city_stats.sort_values("accuracy", ascending=True)

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(
        go.Bar(
            x=city_stats["city"], y=city_stats["accuracy"] * 100,
            name="Win Rate %", marker_color="#636EFA",
        ), secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(
            x=city_stats["city"], y=city_stats["pnl_dollars"],
            name="P&L ($)", mode="markers+lines",
            marker=dict(size=10, color="#EF553B"),
        ), secondary_y=True,
    )
    fig.update_layout(height=380, margin=dict(t=10))
    fig.update_yaxes(title_text="Win Rate (%)", secondary_y=False)
    fig.update_yaxes(title_text="P&L ($)", secondary_y=True)
    st.plotly_chart(fig, use_container_width=True)

with col2:
    st.subheader("BUY YES vs BUY NO Performance")
    side_stats = trades.groupby("buy_side").agg(
        trades=("pnl_cents", "count"),
        wins=("prediction_correct", "sum"),
        pnl=("pnl_cents", "sum"),
    ).reset_index()
    side_stats["accuracy"] = side_stats["wins"] / side_stats["trades"]
    side_stats["pnl_dollars"] = side_stats["pnl"] / 100

    fig2 = go.Figure()
    fig2.add_trace(go.Bar(
        x=["BUY YES", "BUY NO"],
        y=[side_stats[side_stats.buy_side == "YES"]["accuracy"].values[0] * 100,
           side_stats[side_stats.buy_side == "NO"]["accuracy"].values[0] * 100],
        name="Win Rate %",
        marker_color=["#EF553B", "#636EFA"],
        text=[f"{side_stats[side_stats.buy_side == 'YES']['accuracy'].values[0]*100:.1f}%",
              f"{side_stats[side_stats.buy_side == 'NO']['accuracy'].values[0]*100:.1f}%"],
        textposition="outside",
    ))
    fig2.update_layout(height=380, margin=dict(t=10), yaxis_title="Win Rate (%)")
    st.plotly_chart(fig2, use_container_width=True)

    yes_stats = side_stats[side_stats.buy_side == "YES"].iloc[0]
    no_stats = side_stats[side_stats.buy_side == "NO"].iloc[0]
    c1, c2 = st.columns(2)
    c1.metric("BUY YES", f"{int(yes_stats.trades)} trades", f"${yes_stats.pnl_dollars:.2f}")
    c2.metric("BUY NO", f"{int(no_stats.trades)} trades", f"${no_stats.pnl_dollars:.2f}")

st.divider()

# ===========================================================================
# ROW 2: Calibration Curve + Edge Analysis
# ===========================================================================
col3, col4 = st.columns(2)

with col3:
    st.subheader("Calibration Curve")
    st.caption("Model's predicted probability vs. actual outcome rate")

    cal_data = trades.copy()
    cal_data["prob_bucket"] = pd.cut(
        cal_data["fair_prob"],
        bins=[0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
        labels=["5%", "15%", "25%", "35%", "45%", "55%", "65%", "75%", "85%", "95%"],
    )
    cal_agg = cal_data.groupby("prob_bucket", observed=True).agg(
        n=("pnl_cents", "count"),
        avg_model=("fair_prob", "mean"),
        actual_yes=("actual_outcome", lambda x: (x == "YES").mean()),
    ).reset_index()
    cal_agg = cal_agg[cal_agg["n"] >= 3]

    fig3 = go.Figure()
    fig3.add_trace(go.Scatter(
        x=[0, 1], y=[0, 1], mode="lines", name="Perfect Calibration",
        line=dict(dash="dash", color="gray"),
    ))
    fig3.add_trace(go.Scatter(
        x=cal_agg["avg_model"], y=cal_agg["actual_yes"],
        mode="markers+lines", name="Model",
        marker=dict(size=cal_agg["n"].clip(upper=30), color="#636EFA"),
        text=[f"n={n}" for n in cal_agg["n"]],
    ))
    fig3.update_layout(
        height=400, margin=dict(t=10),
        xaxis_title="Model Predicted Probability",
        yaxis_title="Actual Outcome Rate",
        xaxis=dict(range=[0, 1]), yaxis=dict(range=[0, 1]),
    )
    st.plotly_chart(fig3, use_container_width=True)

with col4:
    st.subheader("Edge Size vs. Win Rate")
    st.caption("Larger perceived edge doesn't always mean better outcomes")

    trades_edge = trades.copy()
    trades_edge["edge_bucket"] = pd.cut(
        trades_edge["edge_cents"],
        bins=[0, 10, 20, 30, 50, 100],
        labels=["0-10¢", "10-20¢", "20-30¢", "30-50¢", "50+¢"],
    )
    edge_agg = trades_edge.groupby("edge_bucket", observed=True).agg(
        n=("pnl_cents", "count"),
        win_rate=("prediction_correct", "mean"),
        total_pnl=("pnl_cents", "sum"),
    ).reset_index()

    fig4 = make_subplots(specs=[[{"secondary_y": True}]])
    fig4.add_trace(
        go.Bar(
            x=edge_agg["edge_bucket"].astype(str), y=edge_agg["win_rate"] * 100,
            name="Win Rate %", marker_color="#636EFA",
            text=[f"n={n}" for n in edge_agg["n"]], textposition="outside",
        ), secondary_y=False,
    )
    fig4.add_trace(
        go.Scatter(
            x=edge_agg["edge_bucket"].astype(str), y=edge_agg["total_pnl"] / 100,
            name="Total P&L ($)", mode="markers+lines",
            marker=dict(size=10, color="#EF553B"),
        ), secondary_y=True,
    )
    fig4.update_layout(height=400, margin=dict(t=10))
    fig4.update_yaxes(title_text="Win Rate (%)", secondary_y=False)
    fig4.update_yaxes(title_text="P&L ($)", secondary_y=True)
    st.plotly_chart(fig4, use_container_width=True)

st.divider()

# ===========================================================================
# ROW 3: Forecast Error + Signal Funnel
# ===========================================================================
col5, col6 = st.columns(2)

with col5:
    st.subheader("Forecast Error by City")
    st.caption("Actual high temp minus bot's forecast (°F)")

    temp_trades = df[df["market_type"] == "high_temp"].drop_duplicates(
        subset=["city", "contract_date", "forecast_high_f"]
    ).copy()
    temp_trades["error"] = temp_trades["actual_high_f"] - temp_trades["forecast_high_f"]
    temp_trades = temp_trades.dropna(subset=["error"])

    error_by_city = temp_trades.groupby("city").agg(
        mean_error=("error", "mean"),
        mae=("error", lambda x: x.abs().mean()),
    ).reset_index().sort_values("mae")

    fig5 = go.Figure()
    fig5.add_trace(go.Bar(
        x=error_by_city["city"], y=error_by_city["mean_error"],
        name="Mean Error (°F)", marker_color=["#EF553B" if e < 0 else "#636EFA" for e in error_by_city["mean_error"]],
        text=[f"{e:+.1f}°F" for e in error_by_city["mean_error"]], textposition="outside",
    ))
    fig5.add_hline(y=0, line_dash="dash", line_color="gray")
    fig5.update_layout(height=400, margin=dict(t=10), yaxis_title="Mean Forecast Error (°F)")
    st.plotly_chart(fig5, use_container_width=True)

with col6:
    st.subheader("Signal Funnel")
    st.caption(f"How the bot filtered {len(df):,} contract evaluations")

    def categorize(sig):
        if sig.startswith("BUY"):
            return "Traded (BUY)"
        if sig == "HOLD":
            return "HOLD"
        if "edge" in sig:
            return "Edge too small"
        if "insufficient" in sig:
            return "Source disagreement"
        if "trust" in sig:
            return "Trust gate"
        if "fair prob" in sig:
            return "Low probability"
        return "No trade"

    df["signal_cat"] = df["signal"].apply(categorize)
    funnel = df["signal_cat"].value_counts().reset_index()
    funnel.columns = ["category", "count"]
    order = ["No trade", "Edge too small", "HOLD", "Source disagreement",
             "Trust gate", "Low probability", "Traded (BUY)"]
    funnel["category"] = pd.Categorical(funnel["category"], categories=order, ordered=True)
    funnel = funnel.sort_values("category")

    fig6 = go.Figure(go.Funnel(
        y=funnel["category"], x=funnel["count"],
        textinfo="value+percent initial",
        marker=dict(color=["#FECB52", "#FFA15A", "#B6E880", "#FF6692",
                           "#19D3F3", "#AB63FA", "#636EFA"][:len(funnel)]),
    ))
    fig6.update_layout(height=400, margin=dict(t=10))
    st.plotly_chart(fig6, use_container_width=True)

st.divider()

# ===========================================================================
# ROW 4: P&L Waterfall + Kelly Analysis
# ===========================================================================
col7, col8 = st.columns(2)

with col7:
    st.subheader("P&L Waterfall by City")
    city_pnl = trades.groupby("city")["pnl_cents"].sum().sort_values() / 100
    colors = ["#EF553B" if v < 0 else "#00CC96" for v in city_pnl.values]

    fig7 = go.Figure(go.Waterfall(
        x=list(city_pnl.index) + ["Total"],
        y=list(city_pnl.values) + [city_pnl.sum()],
        measure=["relative"] * len(city_pnl) + ["total"],
        connector={"line": {"color": "gray"}},
        decreasing={"marker": {"color": "#EF553B"}},
        increasing={"marker": {"color": "#00CC96"}},
        totals={"marker": {"color": "#636EFA"}},
        text=[f"${v:.2f}" for v in city_pnl.values] + [f"${city_pnl.sum():.2f}"],
        textposition="outside",
    ))
    fig7.update_layout(height=400, margin=dict(t=10), yaxis_title="P&L ($)")
    st.plotly_chart(fig7, use_container_width=True)

with col8:
    st.subheader("Kelly Fraction vs. Win Rate")
    st.caption("Higher-conviction bets (Kelly %) didn't reliably outperform")

    kelly_data = trades.copy()
    kelly_data["kelly_bucket"] = pd.cut(
        kelly_data["kelly_fraction"],
        bins=[0, 0.01, 0.05, 0.10, 0.20, 1.0],
        labels=["<1%", "1-5%", "5-10%", "10-20%", "20%+"],
    )
    kelly_agg = kelly_data.groupby("kelly_bucket", observed=True).agg(
        n=("pnl_cents", "count"),
        win_rate=("prediction_correct", "mean"),
        pnl=("pnl_cents", "sum"),
    ).reset_index()

    fig8 = go.Figure()
    fig8.add_trace(go.Bar(
        x=kelly_agg["kelly_bucket"].astype(str), y=kelly_agg["win_rate"] * 100,
        marker_color="#636EFA",
        text=[f"n={n}<br>{w:.0f}%" for n, w in zip(kelly_agg["n"], kelly_agg["win_rate"] * 100)],
        textposition="outside",
    ))
    fig8.add_hline(y=50, line_dash="dash", line_color="gray", annotation_text="50% breakeven")
    fig8.update_layout(height=400, margin=dict(t=10), yaxis_title="Win Rate (%)",
                       xaxis_title="Kelly Fraction Bucket")
    st.plotly_chart(fig8, use_container_width=True)

st.divider()

# ===========================================================================
# ROW 5: Detailed city heatmap
# ===========================================================================
st.subheader("City × Date Performance Heatmap")

heatmap_data = trades.groupby(["city", "contract_date"]).agg(
    trades=("pnl_cents", "count"),
    accuracy=("prediction_correct", "mean"),
    pnl=("pnl_cents", "sum"),
).reset_index()
heatmap_pivot = heatmap_data.pivot(index="city", columns="contract_date", values="pnl")
heatmap_acc = heatmap_data.pivot(index="city", columns="contract_date", values="accuracy")

fig9 = go.Figure(go.Heatmap(
    z=heatmap_pivot.values,
    x=heatmap_pivot.columns.tolist(),
    y=heatmap_pivot.index.tolist(),
    text=[[f"P&L: {v/100:.2f}$<br>Win: {heatmap_acc.values[i][j]*100:.0f}%"
           for j, v in enumerate(row)]
          for i, row in enumerate(heatmap_pivot.values)],
    texttemplate="%{text}",
    colorscale="RdYlGn",
    zmid=0,
))
fig9.update_layout(height=350, margin=dict(t=10))
st.plotly_chart(fig9, use_container_width=True)

st.divider()

# ===========================================================================
# RAW DATA TABLE
# ===========================================================================
with st.expander("📋 View All Trades (raw data)"):
    display_cols = [
        "city", "contract_date", "contract_subtitle", "signal", "buy_side",
        "limit_price_cents", "fair_prob", "market_price", "edge_cents",
        "actual_high_f", "actual_outcome", "prediction_correct", "pnl_cents",
    ]
    st.dataframe(
        trades[display_cols].sort_values(["city", "contract_date"]),
        use_container_width=True,
        height=500,
    )

# ===========================================================================
# FOOTER
# ===========================================================================
st.divider()
st.caption(
    f"Data: {len(df):,} contract evaluations from an automated Kalshi weather derivatives trading bot. "
    f"Actual weather outcomes sourced from Iowa Environmental Mesonet (ASOS/METAR). "
    f"Analysis covers {_date_range} across {', '.join(sorted(df['city'].unique()))}."
)
