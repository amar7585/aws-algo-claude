-- 2026-11-19 - remove bias system-wide (Amar: "we are making this big change remove bias also").
--
-- Neon project nameless-mountain-15353651 ("AI Trader APP"), database Algo, schema algo.
--
-- bias (bullish | bearish | range-bound) was the market-classifier layer's third
-- answer, from a +-5 score of SMA alignment, RSI, the swing direction and VWAP;
-- score, max_score and confidence were built on the same score. No decision used
-- any of them by 2026-11-19: the sweep playbook dropped bias 2026-11-14, the trend
-- playbook routes on day_trend, and strategy-manager's regime|bias registry
-- dispatched nothing. The layer (version 4), every writer and every reader stopped
-- using them in the same change.
--
-- BACKED UP FIRST: every row's security_id, instrument_type, key, bias, score,
-- max_score and confidence (1056 intraday rows, 45 daily rows) is in
-- pattern-recorder/replay/out/backup-2026-11-19/bias_columns.json (git-ignored,
-- local). No view or constraint other than their own NOT NULLs depends on them.
--
-- ORDER: run this BEFORE deploying the new code (the columns are NOT NULL, so the
-- new code's inserts would fail against them) and outside market hours (the old
-- code still writes them until it is replaced). Applied 2026-11-19 after the close.

ALTER TABLE algo.intraday_sentiments
    DROP COLUMN bias,
    DROP COLUMN score,
    DROP COLUMN max_score,
    DROP COLUMN confidence;

ALTER TABLE algo.daily_market_sentiment
    DROP COLUMN bias,
    DROP COLUMN score,
    DROP COLUMN max_score,
    DROP COLUMN confidence;
