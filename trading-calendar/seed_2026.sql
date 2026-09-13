-- algo.trading_holiday - NSE trading holidays 2026
--
-- THE OFFICIAL LIST, transcribed from NSE's annual holiday circular. This
-- supersedes anything generate_seed.py produces: the circular is what the
-- exchange actually observes, and it carries the names that exchange_calendars
-- does not publish.
--
-- Independently cross-checked against generate_seed.py (exchange_calendars
-- XBOM 4.13.2) on 2026-09-13: the same 16 dates, no additions, no omissions.
-- Every weekday shown here was verified against the date itself, and none
-- falls at a weekend.
--
-- Re-runnable: the upsert refreshes description, source and updated_at.
-- updated_at is stamped at apply time rather than frozen into the file, so a
-- re-apply records when it actually happened.

INSERT INTO algo.trading_holiday (trade_date, description, source, updated_at)
VALUES
    (DATE '2026-01-15', 'Municipal Corporation Election - Maharashtra', 'NSE trading holiday list 2026', EXTRACT(EPOCH FROM now())::bigint),  -- Thu
    (DATE '2026-01-26', 'Republic Day',                                 'NSE trading holiday list 2026', EXTRACT(EPOCH FROM now())::bigint),  -- Mon
    (DATE '2026-03-03', 'Holi',                                         'NSE trading holiday list 2026', EXTRACT(EPOCH FROM now())::bigint),  -- Tue
    (DATE '2026-03-26', 'Shri Ram Navami',                              'NSE trading holiday list 2026', EXTRACT(EPOCH FROM now())::bigint),  -- Thu
    (DATE '2026-03-31', 'Shri Mahavir Jayanti',                         'NSE trading holiday list 2026', EXTRACT(EPOCH FROM now())::bigint),  -- Tue
    (DATE '2026-04-03', 'Good Friday',                                  'NSE trading holiday list 2026', EXTRACT(EPOCH FROM now())::bigint),  -- Fri
    (DATE '2026-04-14', 'Dr. Baba Saheb Ambedkar Jayanti',              'NSE trading holiday list 2026', EXTRACT(EPOCH FROM now())::bigint),  -- Tue
    (DATE '2026-05-01', 'Maharashtra Day',                              'NSE trading holiday list 2026', EXTRACT(EPOCH FROM now())::bigint),  -- Fri
    (DATE '2026-05-28', 'Bakri Id',                                     'NSE trading holiday list 2026', EXTRACT(EPOCH FROM now())::bigint),  -- Thu
    (DATE '2026-06-26', 'Muharram',                                     'NSE trading holiday list 2026', EXTRACT(EPOCH FROM now())::bigint),  -- Fri
    (DATE '2026-09-14', 'Ganesh Chaturthi',                             'NSE trading holiday list 2026', EXTRACT(EPOCH FROM now())::bigint),  -- Mon
    (DATE '2026-10-02', 'Mahatma Gandhi Jayanti',                       'NSE trading holiday list 2026', EXTRACT(EPOCH FROM now())::bigint),  -- Fri
    (DATE '2026-10-20', 'Dussehra',                                     'NSE trading holiday list 2026', EXTRACT(EPOCH FROM now())::bigint),  -- Tue
    (DATE '2026-11-10', 'Diwali-Balipratipada',                         'NSE trading holiday list 2026', EXTRACT(EPOCH FROM now())::bigint),  -- Tue
    (DATE '2026-11-24', 'Prakash Gurpurb Sri Guru Nanak Dev',           'NSE trading holiday list 2026', EXTRACT(EPOCH FROM now())::bigint),  -- Tue
    (DATE '2026-12-25', 'Christmas',                                    'NSE trading holiday list 2026', EXTRACT(EPOCH FROM now())::bigint)   -- Fri
ON CONFLICT (trade_date) DO UPDATE SET
    description = EXCLUDED.description,
    source      = EXCLUDED.source,
    updated_at  = EXCLUDED.updated_at;
