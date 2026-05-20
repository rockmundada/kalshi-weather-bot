-- =============================================================================
-- Kalshi Bot Performance Analysis Queries
-- Run against kalshi_analytics.db (SQLite)
-- =============================================================================

-- 1. Overall accuracy and P&L summary
SELECT
    COUNT(*) as total_trades,
    SUM(CASE WHEN prediction_correct = '1' THEN 1 ELSE 0 END) as correct,
    ROUND(100.0 * SUM(CASE WHEN prediction_correct = '1' THEN 1 ELSE 0 END) / COUNT(*), 1) as accuracy_pct,
    SUM(CAST(pnl_cents AS INTEGER)) as total_pnl_cents,
    ROUND(SUM(CAST(pnl_cents AS INTEGER)) / 100.0, 2) as total_pnl_dollars,
    ROUND(AVG(CAST(pnl_cents AS INTEGER)), 1) as avg_pnl_per_trade_cents
FROM predictions
WHERE is_actionable = '1' AND pnl_cents != '';


-- 2. Accuracy by city
SELECT
    city,
    COUNT(*) as trades,
    SUM(CASE WHEN prediction_correct = '1' THEN 1 ELSE 0 END) as correct,
    ROUND(100.0 * SUM(CASE WHEN prediction_correct = '1' THEN 1 ELSE 0 END) / COUNT(*), 1) as accuracy_pct,
    SUM(CAST(pnl_cents AS INTEGER)) as pnl_cents,
    ROUND(AVG(CAST(pnl_cents AS INTEGER)), 1) as avg_pnl_cents
FROM predictions
WHERE is_actionable = '1' AND pnl_cents != ''
GROUP BY city
ORDER BY accuracy_pct DESC;


-- 3. Accuracy by buy side (YES vs NO)
SELECT
    buy_side,
    COUNT(*) as trades,
    SUM(CASE WHEN prediction_correct = '1' THEN 1 ELSE 0 END) as correct,
    ROUND(100.0 * SUM(CASE WHEN prediction_correct = '1' THEN 1 ELSE 0 END) / COUNT(*), 1) as accuracy_pct,
    SUM(CAST(pnl_cents AS INTEGER)) as pnl_cents
FROM predictions
WHERE is_actionable = '1' AND pnl_cents != ''
GROUP BY buy_side;


-- 4. Accuracy by market type
SELECT
    market_type,
    COUNT(*) as trades,
    SUM(CASE WHEN prediction_correct = '1' THEN 1 ELSE 0 END) as correct,
    ROUND(100.0 * SUM(CASE WHEN prediction_correct = '1' THEN 1 ELSE 0 END) / COUNT(*), 1) as accuracy_pct,
    SUM(CAST(pnl_cents AS INTEGER)) as pnl_cents
FROM predictions
WHERE is_actionable = '1' AND pnl_cents != ''
GROUP BY market_type;


-- 5. Calibration: fair_prob buckets vs actual hit rate
SELECT
    CASE
        WHEN CAST(fair_prob AS REAL) < 0.1 THEN '0-10%'
        WHEN CAST(fair_prob AS REAL) < 0.2 THEN '10-20%'
        WHEN CAST(fair_prob AS REAL) < 0.3 THEN '20-30%'
        WHEN CAST(fair_prob AS REAL) < 0.4 THEN '30-40%'
        WHEN CAST(fair_prob AS REAL) < 0.5 THEN '40-50%'
        WHEN CAST(fair_prob AS REAL) < 0.6 THEN '50-60%'
        WHEN CAST(fair_prob AS REAL) < 0.7 THEN '60-70%'
        WHEN CAST(fair_prob AS REAL) < 0.8 THEN '70-80%'
        WHEN CAST(fair_prob AS REAL) < 0.9 THEN '80-90%'
        ELSE '90-100%'
    END as prob_bucket,
    COUNT(*) as n,
    ROUND(AVG(CAST(fair_prob AS REAL)), 3) as avg_model_prob,
    ROUND(100.0 * SUM(CASE WHEN actual_outcome = 'YES' THEN 1 ELSE 0 END) / COUNT(*), 1) as actual_yes_pct,
    SUM(CAST(pnl_cents AS INTEGER)) as pnl_cents
FROM predictions
WHERE is_actionable = '1' AND pnl_cents != ''
GROUP BY prob_bucket
ORDER BY avg_model_prob;


-- 6. Edge analysis: edge_cents buckets vs win rate
SELECT
    CASE
        WHEN CAST(edge_cents AS REAL) < 5 THEN '0-5¢'
        WHEN CAST(edge_cents AS REAL) < 10 THEN '5-10¢'
        WHEN CAST(edge_cents AS REAL) < 20 THEN '10-20¢'
        WHEN CAST(edge_cents AS REAL) < 30 THEN '20-30¢'
        WHEN CAST(edge_cents AS REAL) < 50 THEN '30-50¢'
        ELSE '50+¢'
    END as edge_bucket,
    COUNT(*) as trades,
    ROUND(100.0 * SUM(CASE WHEN prediction_correct = '1' THEN 1 ELSE 0 END) / COUNT(*), 1) as win_rate,
    SUM(CAST(pnl_cents AS INTEGER)) as pnl_cents,
    ROUND(AVG(CAST(pnl_cents AS INTEGER)), 1) as avg_pnl
FROM predictions
WHERE is_actionable = '1' AND pnl_cents != ''
GROUP BY edge_bucket
ORDER BY MIN(CAST(edge_cents AS REAL));


-- 7. Kelly fraction analysis: did higher-conviction bets perform better?
SELECT
    CASE
        WHEN CAST(kelly_fraction AS REAL) < 0.01 THEN '<1%'
        WHEN CAST(kelly_fraction AS REAL) < 0.05 THEN '1-5%'
        WHEN CAST(kelly_fraction AS REAL) < 0.10 THEN '5-10%'
        WHEN CAST(kelly_fraction AS REAL) < 0.20 THEN '10-20%'
        ELSE '20%+'
    END as kelly_bucket,
    COUNT(*) as trades,
    ROUND(100.0 * SUM(CASE WHEN prediction_correct = '1' THEN 1 ELSE 0 END) / COUNT(*), 1) as win_rate,
    SUM(CAST(pnl_cents AS INTEGER)) as pnl_cents
FROM predictions
WHERE is_actionable = '1' AND pnl_cents != ''
GROUP BY kelly_bucket
ORDER BY MIN(CAST(kelly_fraction AS REAL));


-- 8. Forecast error analysis
SELECT
    city,
    contract_date,
    CAST(actual_high_f AS REAL) as actual,
    CAST(forecast_high_f AS REAL) as forecast,
    ROUND(CAST(actual_high_f AS REAL) - CAST(forecast_high_f AS REAL), 1) as error_f,
    ROUND(ABS(CAST(actual_high_f AS REAL) - CAST(forecast_high_f AS REAL)), 1) as abs_error
FROM actual_weather aw
JOIN (
    SELECT DISTINCT city, contract_date, actual_high_f, forecast_high_f
    FROM predictions
    WHERE market_type = 'high_temp' AND actual_high_f != ''
) p ON aw.city = p.city AND aw.contract_date = p.contract_date
ORDER BY city, contract_date;


-- 9. Signal filter breakdown (why were contracts not traded?)
SELECT
    CASE
        WHEN signal LIKE 'BUY%' THEN 'ACTIONABLE (BUY)'
        WHEN signal = 'HOLD' THEN 'HOLD'
        WHEN signal LIKE 'NO TRADE - edge%' THEN 'FILTERED: edge too small'
        WHEN signal LIKE 'NO TRADE - insufficient%' THEN 'FILTERED: source disagreement'
        WHEN signal LIKE 'NO TRADE - trust%' THEN 'FILTERED: trust gate'
        WHEN signal LIKE 'NO TRADE - fair%' THEN 'FILTERED: low probability'
        WHEN signal = 'NO TRADE' THEN 'NO TRADE (generic)'
        ELSE 'OTHER: ' || signal
    END as category,
    COUNT(*) as count,
    ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM predictions), 1) as pct
FROM predictions
GROUP BY category
ORDER BY count DESC;


-- 10. P&L by contract date
SELECT
    contract_date,
    COUNT(*) as trades,
    SUM(CASE WHEN prediction_correct = '1' THEN 1 ELSE 0 END) as correct,
    ROUND(100.0 * SUM(CASE WHEN prediction_correct = '1' THEN 1 ELSE 0 END) / COUNT(*), 1) as accuracy_pct,
    SUM(CAST(pnl_cents AS INTEGER)) as pnl_cents
FROM predictions
WHERE is_actionable = '1' AND pnl_cents != ''
GROUP BY contract_date;
