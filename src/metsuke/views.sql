-- Derived layer (L2 as code). Never persisted: change a definition here and the
-- entire history is consistently re-derived at the next query.

DROP VIEW IF EXISTS v_request_cost;
CREATE VIEW v_request_cost AS
WITH priced AS (
    SELECT
        r.rowid AS request_rowid,
        p.in_usd, p.out_usd, p.cache_read_x, p.cache_w5m_x, p.cache_w1h_x,
        (CASE WHEN r.service_tier = 'batch' THEN p.batch_x ELSE 1.0 END)
        * (CASE WHEN r.speed = 'fast' THEN p.fast_x ELSE 1.0 END)
        * (CASE WHEN r.geo LIKE 'us%' THEN p.geo_us_x ELSE 1.0 END) AS price_factor
    FROM request r
    LEFT JOIN price p ON p.rowid = (
        SELECT candidate.rowid FROM price candidate
        WHERE candidate.model = r.model
          AND date(r.ts, 'unixepoch') >= candidate.valid_from
          AND (candidate.valid_to IS NULL OR date(r.ts, 'unixepoch') < candidate.valid_to)
        ORDER BY candidate.valid_from DESC LIMIT 1
    )
    WHERE r.is_synthetic = 0
), server_cost AS (
    SELECT r.rowid AS request_rowid,
           SUM(CAST(j.value AS REAL) * p.usd_per_unit) AS server_tool_usd
    FROM request r
    JOIN json_each(
        CASE WHEN json_valid(r.server_tool_use) THEN r.server_tool_use ELSE '{}' END
    ) j
    JOIN price_server_tool p ON p.rowid = (
        SELECT candidate.rowid FROM price_server_tool candidate
        WHERE candidate.tool = j.key
          AND date(r.ts, 'unixepoch') >= candidate.valid_from
          AND (candidate.valid_to IS NULL OR date(r.ts, 'unixepoch') < candidate.valid_to)
        ORDER BY candidate.valid_from DESC LIMIT 1
    )
    GROUP BY r.rowid
)
SELECT
    r.*,
    p.in_usd, p.out_usd, p.cache_read_x, p.cache_w5m_x, p.cache_w1h_x,
    p.price_factor,
    (
        (
            COALESCE(r.input_tok, 0) * p.in_usd
            + COALESCE(r.cache_read_tok, 0) * p.in_usd * p.cache_read_x
            + COALESCE(r.cache_w5m_tok, 0) * p.in_usd * p.cache_w5m_x
            + COALESCE(r.cache_w1h_tok, 0) * p.in_usd * p.cache_w1h_x
            + COALESCE(r.output_tok, 0) * p.out_usd
        ) / 1000000.0
    )
    * p.price_factor AS token_cost_usd,
    COALESCE(s.server_tool_usd, 0) AS server_tool_usd,
    (
        (
            COALESCE(r.input_tok, 0) * p.in_usd
            + COALESCE(r.cache_read_tok, 0) * p.in_usd * p.cache_read_x
            + COALESCE(r.cache_w5m_tok, 0) * p.in_usd * p.cache_w5m_x
            + COALESCE(r.cache_w1h_tok, 0) * p.in_usd * p.cache_w1h_x
            + COALESCE(r.output_tok, 0) * p.out_usd
        ) / 1000000.0
    ) * p.price_factor + COALESCE(s.server_tool_usd, 0) AS cost_usd,
    (
        COALESCE(r.cache_w5m_tok, 0) * p.in_usd * p.cache_w5m_x
        + COALESCE(r.cache_w1h_tok, 0) * p.in_usd * p.cache_w1h_x
    ) / 1000000.0 * p.price_factor AS cache_write_usd
FROM request r
JOIN priced p ON p.request_rowid = r.rowid
LEFT JOIN server_cost s ON s.request_rowid = r.rowid;

DROP VIEW IF EXISTS v_prompt_cost;
CREATE VIEW v_prompt_cost AS
SELECT
    r.prompt_id,
    MIN(r.ts) AS ts,
    COUNT(*) AS n_requests,
    SUM(CASE WHEN r.agent_id IS NULL THEN 0 ELSE 1 END) AS n_agent_requests,
    COUNT(DISTINCT r.agent_id) AS n_agents,
    SUM(r.input_tok) AS input_tok,
    SUM(COALESCE(r.output_tok, 0)) AS output_tok,
    SUM(r.cache_read_tok) AS cache_read_tok,
    SUM(r.cache_w5m_tok + r.cache_w1h_tok) AS cache_creation_tok,
    SUM(r.cost_usd) AS cost_usd,
    MAX(r.is_interrupted) AS interrupted
FROM v_request_cost r
WHERE r.prompt_id IS NOT NULL
GROUP BY r.prompt_id;

DROP VIEW IF EXISTS v_daily;
CREATE VIEW v_daily AS
SELECT
    date(ts, 'unixepoch', 'localtime') AS day,   -- day boundary = local time (METRICS.md)
    COUNT(*) AS n_requests,
    SUM(cost_usd) AS cost_usd,
    SUM(cache_read_tok) AS cache_read_tok,
    SUM(cache_w5m_tok + cache_w1h_tok) AS cache_creation_tok,
    SUM(input_tok) AS input_tok,
    SUM(COALESCE(output_tok, 0)) AS output_tok
FROM v_request_cost
GROUP BY day;

-- Cache-lineage identity: cache_read(n+1) = cache_read(n) + cache_creation(n).
-- A break is a signal (TTL expiry / rewrite / model switch), classified from
-- intrinsic evidence (Stage 1); hook-based evidence refines it in Stage 2.
DROP VIEW IF EXISTS v_cache_identity;
CREATE VIEW v_cache_identity AS
WITH seq AS (
    SELECT
        lineage_id, session_id, request_id, ts, model, is_interrupted,
        cache_read_tok,
        cache_w5m_tok + cache_w1h_tok AS cc,
        input_tok,
        LAG(cache_read_tok) OVER w AS prev_cr,
        LAG(cache_w5m_tok + cache_w1h_tok) OVER w AS prev_cc,
        LAG(cache_w5m_tok) OVER w AS prev_w5m,
        LAG(cache_w1h_tok) OVER w AS prev_w1h,
        LAG(input_tok) OVER w AS prev_input,
        LAG(model) OVER w AS prev_model,
        LAG(ts) OVER w AS prev_ts,
        LAG(end_ts) OVER w AS prev_end,
        LAG(is_interrupted) OVER w AS prev_interrupted
    FROM request
    WHERE is_synthetic = 0 AND source = 'transcript'
    WINDOW w AS (PARTITION BY lineage_id ORDER BY ts)
)
SELECT
    lineage_id, session_id, request_id, ts,
    cache_read_tok - (prev_cr + prev_cc) AS gap,
    CASE
        WHEN prev_interrupted = 1 THEN 'interruption'
        WHEN EXISTS (SELECT 1 FROM hook_event h WHERE h.session_id=seq.session_id AND h.kind IN ('PreCompact','PostCompact') AND h.ts BETWEEN seq.prev_ts-600 AND seq.ts+600) THEN 'compaction'
        WHEN model != prev_model THEN 'model_switch'
        WHEN EXISTS (SELECT 1 FROM hook_event h WHERE h.session_id=seq.session_id AND h.kind='SessionStart' AND h.ts BETWEEN seq.prev_ts AND seq.ts) THEN 'config_change'
        WHEN ts - prev_ts > 3600
             OR (
               -- prev_w1h=0 は間隔ちょうど3600秒の境界を守る（1h規則は>3600、生存判定は<3600のため）
               prev_w5m > 0 AND prev_w1h = 0
               AND prev_end IS NOT NULL AND ts - prev_end > 300
               AND cache_read_tok < (prev_cr + prev_cc) * 0.1
               AND NOT EXISTS (
                 SELECT 1 FROM request q
                 WHERE q.lineage_id = seq.lineage_id AND q.ts < seq.ts
                   AND q.cache_w1h_tok > 0 AND seq.ts - q.ts < 3600
                   AND q.is_synthetic = 0 AND q.source = 'transcript'
               )
             ) THEN 'ttl_expiry'
        ELSE 'unknown'
    END AS cause,
    ts - prev_ts AS gap_seconds
FROM seq
WHERE prev_cr IS NOT NULL
  AND ABS(cache_read_tok - (prev_cr + prev_cc)) > COALESCE(prev_input, 0) + 16;

-- Fixed context overhead: what every session pays up front (system prompt +
-- tool definitions + CLAUDE.md/memory). The cheapest lever in the whole system.
DROP VIEW IF EXISTS v_context_overhead;
CREATE VIEW v_context_overhead AS
SELECT
    s.session_id, s.project, s.first_ts,
    date(s.first_ts, 'unixepoch', 'localtime') AS day,
    r.model,
    r.input_tok + r.cache_read_tok + r.cache_w5m_tok + r.cache_w1h_tok AS startup_context_tok
FROM session s
JOIN request r ON r.request_id = (
    SELECT request_id FROM request
    WHERE session_id = s.session_id AND agent_id IS NULL AND is_synthetic = 0
    ORDER BY ts LIMIT 1
);

DROP VIEW IF EXISTS v_label_coverage;
CREATE VIEW v_label_coverage AS
SELECT
    strftime('%G-W%V', p.ts, 'unixepoch', 'localtime') AS iso_week,
    COUNT(DISTINCT p.prompt_id) AS prompts,
    COUNT(DISTINCT CASE WHEN p.task_label IS NOT NULL THEN p.prompt_id END) AS labeled_prompts,
    100.0 * COUNT(DISTINCT CASE WHEN p.task_label IS NOT NULL THEN p.prompt_id END)
        / NULLIF(COUNT(DISTINCT p.prompt_id), 0) AS coverage_pct,
    COUNT(DISTINCT CASE WHEN EXISTS (SELECT 1 FROM outcome o WHERE o.prompt_id=p.prompt_id)
                   THEN p.prompt_id END) AS outcome_prompts,
    100.0 * COUNT(DISTINCT CASE WHEN EXISTS (
            SELECT 1 FROM outcome o WHERE o.prompt_id=p.prompt_id) THEN p.prompt_id END)
        / NULLIF(COUNT(DISTINCT p.prompt_id), 0) AS outcome_coverage_pct,
    SUM(CASE WHEN p.task_label IS NOT NULL THEN COALESCE(v.cost_usd,0) ELSE 0 END)
        AS labeled_cost_usd,
    SUM(COALESCE(v.cost_usd,0)) AS total_cost_usd,
    100.0 * SUM(CASE WHEN p.task_label IS NOT NULL THEN COALESCE(v.cost_usd,0) ELSE 0 END)
        / NULLIF(SUM(COALESCE(v.cost_usd,0)), 0) AS cost_coverage_pct
FROM prompt p
LEFT JOIN v_prompt_cost v ON v.prompt_id=p.prompt_id
GROUP BY iso_week;

DROP VIEW IF EXISTS v_unaccounted;
CREATE VIEW v_unaccounted AS
WITH lost AS (
    SELECT p.prompt_id,p.session_id,p.ts,
           (SELECT r.request_id FROM request r
            WHERE r.session_id=p.session_id AND r.ts<=p.ts AND r.is_synthetic=0
            ORDER BY r.ts DESC LIMIT 1) AS prev_request_id
    FROM prompt p
    WHERE p.interrupted_message_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM request missing WHERE missing.message_id=p.interrupted_message_id)
)
SELECT date(l.ts,'unixepoch','localtime') AS day,
       strftime('%Y-%m',l.ts,'unixepoch') AS month_utc,
       COUNT(*) AS n_lost_interruptions,
       SUM(COALESCE((COALESCE(r.cache_read_tok,0)*r.in_usd*r.cache_read_x
            + COALESCE(r.input_tok,0)*r.in_usd)/1000000.0,0)) AS input_side_lower_usd
FROM lost l
LEFT JOIN v_request_cost r ON r.request_id=l.prev_request_id
GROUP BY day;

DROP VIEW IF EXISTS v_counter;
CREATE VIEW v_counter AS
WITH request_week AS (
    SELECT strftime('%G-W%V',ts,'unixepoch','localtime') AS week,
           100.0*SUM(is_interrupted)/NULLIF(COUNT(*),0) AS interrupted_rate_pct
    FROM request WHERE is_synthetic=0 GROUP BY week
), outcome_week AS (
    SELECT strftime('%G-W%V',ts,'unixepoch','localtime') AS week,
           COUNT(DISTINCT CASE WHEN label='reverted' THEN prompt_id END) AS reverted_n,
           COUNT(DISTINCT CASE WHEN label='completed' THEN prompt_id END) AS completed_n
    FROM outcome WHERE source='auto' GROUP BY week
), weeks AS (
    SELECT week FROM request_week UNION SELECT week FROM outcome_week
)
SELECT w.week,r.interrupted_rate_pct,
       100.0*COALESCE(o.reverted_n,0)/NULLIF(o.completed_n,0) AS revert_rate_pct,
       COALESCE(o.reverted_n,0) AS reverted_n,COALESCE(o.completed_n,0) AS completed_n
FROM weeks w
LEFT JOIN request_week r ON r.week=w.week
LEFT JOIN outcome_week o ON o.week=w.week;

DROP VIEW IF EXISTS v_health;
CREATE VIEW v_health AS
WITH gaps AS (
    SELECT session_id,COUNT(*) AS matched_requests,
           100.0*(SUM(cost_usd)-SUM(cost_usd_sdk))/SUM(cost_usd_sdk) AS gap_pct
    FROM v_request_cost
    WHERE ts>=CAST(strftime('%s','now') AS REAL)-172800
      AND cost_usd_sdk>0
    GROUP BY session_id
    HAVING SUM(cost_usd_sdk)>0
), ordered_gaps AS (
    SELECT gap_pct,ROW_NUMBER() OVER (ORDER BY gap_pct) AS rn,
           COUNT(*) OVER () AS n,SUM(matched_requests) OVER () AS total_requests
    FROM gaps
), median_gap AS (
    SELECT AVG(gap_pct) AS value,COALESCE(MAX(n),0) AS samples,
           COALESCE(MAX(total_requests),0) AS matched_requests
    FROM ordered_gaps
    WHERE rn IN ((n+1)/2,(n+2)/2)
), recent_label AS (
    SELECT SUM(CASE WHEN p.task_label IS NOT NULL THEN COALESCE(v.cost_usd,0) ELSE 0 END)
               AS labeled_cost,
           SUM(COALESCE(v.cost_usd,0)) AS total_cost
    FROM prompt p LEFT JOIN v_prompt_cost v ON v.prompt_id=p.prompt_id
    WHERE p.ts>=CAST(strftime('%s','now') AS REAL)-28*86400
), nudge_rate AS (
    SELECT COUNT(*) AS n,100.0*AVG(followed) AS rate
    FROM nudge WHERE fired_ts>=CAST(strftime('%s','now') AS REAL)-14*86400
      AND followed IS NOT NULL
), nudge_observability AS (
    SELECT COUNT(*) AS n,
           100.0*SUM(outcome IN ('followed','not_followed'))/NULLIF(COUNT(*),0) AS rate
    FROM nudge WHERE fired_ts>=CAST(strftime('%s','now') AS REAL)-14*86400
      AND outcome IS NOT NULL
), interrupted AS (
    SELECT COUNT(*) AS n,100.0*AVG(is_interrupted) AS rate FROM request
    WHERE is_synthetic=0 AND ts>=CAST(strftime('%s','now') AS REAL)-7*86400
), timeline_coverage AS (
    SELECT COUNT(*) AS requests,
           SUM(end_ts IS NOT NULL) AS request_ends,
           (SELECT COUNT(*) FROM tool_call WHERE ts>=CAST(strftime('%s','now') AS REAL)-7*86400)
               AS tools,
           (SELECT SUM(result_ts IS NOT NULL) FROM tool_call
            WHERE ts>=CAST(strftime('%s','now') AS REAL)-7*86400) AS tool_results
    FROM request WHERE ts>=CAST(strftime('%s','now') AS REAL)-7*86400
), price_overlaps AS (
    SELECT COUNT(*) AS n FROM (
        SELECT a.model
        FROM price a JOIN price b
          ON a.model=b.model AND a.valid_from<b.valid_from
         AND COALESCE(a.valid_to,'9999-12-31')>b.valid_from
        UNION ALL
        SELECT a.tool
        FROM price_server_tool a JOIN price_server_tool b
          ON a.tool=b.tool AND a.valid_from<b.valid_from
         AND COALESCE(a.valid_to,'9999-12-31')>b.valid_from
    )
), unpriced_server_tools AS (
    SELECT COUNT(DISTINCT j.key) AS n,GROUP_CONCAT(DISTINCT j.key) AS tools
    FROM request r
    JOIN json_each(
        CASE WHEN json_valid(r.server_tool_use) THEN r.server_tool_use ELSE '{}' END
    ) j
    WHERE CAST(j.value AS REAL)>0
      AND NOT EXISTS (
        SELECT 1 FROM price_server_tool p
        WHERE p.tool=j.key
          AND date(r.ts,'unixepoch')>=p.valid_from
          AND (p.valid_to IS NULL OR date(r.ts,'unixepoch')<p.valid_to)
      )
), task_coverage AS (
    SELECT COUNT(*) AS n,
           SUM(outcome IS NOT NULL) AS outcomes,
           SUM(quality_score IS NOT NULL) AS quality
    FROM work_task
    WHERE ts_start>=CAST(strftime('%s','now') AS REAL)-28*86400
)
SELECT 'ledger_freshness' AS check_name,
       CASE WHEN age<1800 THEN 'ok' WHEN age<7200 THEN 'warn' ELSE 'fail' END AS status,
       CASE WHEN age IS NULL THEN 'missing' ELSE printf('%.0f',age) END AS value,
       'seconds since latest request' AS detail
FROM (SELECT CAST(strftime('%s','now') AS REAL)-MAX(ts) age FROM request)
UNION ALL
SELECT 'hook_freshness',CASE WHEN age<1800 THEN 'ok' WHEN age<7200 THEN 'warn' ELSE 'fail' END,
       CASE WHEN age IS NULL THEN 'missing' ELSE printf('%.0f',age) END,'seconds since latest hook event'
FROM (SELECT CAST(strftime('%s','now') AS REAL)-MAX(ts) age FROM hook_event)
UNION ALL
SELECT 'ingest_recent',CASE WHEN age<900 THEN 'ok' WHEN age<3600 THEN 'warn' ELSE 'fail' END,
       CASE WHEN age IS NULL THEN 'missing' ELSE printf('%.0f',age) END,'seconds since latest ingest'
FROM (SELECT CAST(strftime('%s','now') AS REAL)-MAX(ts) age FROM ingest_log)
UNION ALL
SELECT 'quarantine_7d',CASE WHEN n=0 THEN 'ok' ELSE 'fail' END,CAST(n AS TEXT),
       COALESCE((SELECT reason FROM quarantine
                 WHERE ts>=CAST(strftime('%s','now') AS REAL)-7*86400
                 ORDER BY ts DESC LIMIT 1),'no quarantine rows')
FROM (SELECT COUNT(*) n FROM quarantine
      WHERE ts>=CAST(strftime('%s','now') AS REAL)-7*86400)
UNION ALL
SELECT 'request_end_coverage_7d',CASE WHEN requests=0 THEN 'skip'
       WHEN 100.0*request_ends/requests>=98.0 THEN 'ok'
       WHEN 100.0*request_ends/requests>=85.0 THEN 'warn' ELSE 'fail' END,
       printf('%.1f',100.0*request_ends/NULLIF(requests,0)),'percent with end_ts'
FROM timeline_coverage
UNION ALL
SELECT 'tool_result_coverage_7d',CASE WHEN tools=0 THEN 'skip'
       WHEN 100.0*tool_results/tools>=98.0 THEN 'ok'
       WHEN 100.0*tool_results/tools>=85.0 THEN 'warn' ELSE 'fail' END,
       printf('%.1f',100.0*tool_results/NULLIF(tools,0)),'percent with result_ts'
FROM timeline_coverage
UNION ALL
SELECT 'unknown_models',CASE WHEN n=0 THEN 'ok' ELSE 'fail' END,CAST(n AS TEXT),COALESCE(models,'none')
FROM (SELECT COUNT(DISTINCT r.model) n,GROUP_CONCAT(DISTINCT r.model) models
      FROM request r
      WHERE r.is_synthetic=0 AND r.model IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM price p WHERE p.model=r.model
          AND date(r.ts,'unixepoch')>=p.valid_from
          AND (p.valid_to IS NULL OR date(r.ts,'unixepoch')<p.valid_to)))
UNION ALL
SELECT 'price_range_overlap',CASE WHEN n=0 THEN 'ok' ELSE 'fail' END,CAST(n AS TEXT),
       'overlapping SCD2 price periods'
FROM price_overlaps
UNION ALL
SELECT 'unpriced_server_tools',CASE WHEN n=0 THEN 'ok' ELSE 'warn' END,CAST(n AS TEXT),
       COALESCE(tools,'none')
FROM unpriced_server_tools
UNION ALL
SELECT 'task_outcome_coverage_28d',CASE WHEN n=0 THEN 'skip'
       WHEN 100.0*outcomes/n>=80 THEN 'ok' WHEN 100.0*outcomes/n>=40 THEN 'warn' ELSE 'fail' END,
       CASE WHEN n=0 THEN 'no tasks' ELSE printf('%.2f%%',100.0*outcomes/n) END,
       printf('%d tasks; %d with quality score',n,quality)
FROM task_coverage
UNION ALL
SELECT 'estimator_gap',CASE WHEN samples=0 THEN 'skip' WHEN ABS(value)<=10 THEN 'ok'
                            WHEN ABS(value)<=20 THEN 'warn' ELSE 'fail' END,
       CASE WHEN samples=0 THEN 'no samples' ELSE printf('%.2f%%',value) END,
       printf('%d sessions / %d requests; median ledger vs OTel SDK gap',samples,matched_requests)
FROM median_gap
UNION ALL
SELECT 'label_coverage_cost',CASE WHEN total_cost IS NULL OR total_cost=0 THEN 'skip'
                                  WHEN 100.0*labeled_cost/total_cost>=80 THEN 'ok'
                                  WHEN 100.0*labeled_cost/total_cost>=40 THEN 'warn' ELSE 'fail' END,
       CASE WHEN total_cost IS NULL OR total_cost=0 THEN 'no cost'
            ELSE printf('%.2f%%',100.0*labeled_cost/total_cost) END,'last 4 weeks, cost weighted'
FROM recent_label
UNION ALL
SELECT 'orphan_agents',CASE WHEN n=0 THEN 'ok' ELSE 'warn' END,CAST(n AS TEXT),'request agents absent from agent table'
FROM (SELECT COUNT(DISTINCT r.agent_id) n FROM request r LEFT JOIN agent a ON a.agent_id=r.agent_id
      WHERE r.agent_id IS NOT NULL AND a.agent_id IS NULL)
UNION ALL
SELECT 'nudge_conversion_14d',CASE WHEN n=0 THEN 'skip' WHEN rate>=10 THEN 'ok' ELSE 'warn' END,
       CASE WHEN n=0 THEN 'no decisions' ELSE printf('%.2f%%',rate) END,printf('%d decided nudges',n)
FROM nudge_rate
UNION ALL
SELECT 'nudge_observability_14d',CASE WHEN n=0 THEN 'skip' WHEN rate>=60 THEN 'ok'
                                      WHEN rate>=30 THEN 'warn' ELSE 'fail' END,
       CASE WHEN n=0 THEN 'no decisions' ELSE printf('%.2f%%',rate) END,
       printf('%d classified nudges; unknown is excluded from conversion',n)
FROM nudge_observability
UNION ALL
SELECT 'interrupted_share_7d',CASE WHEN n=0 THEN 'skip' WHEN rate<2 THEN 'ok'
                                   WHEN rate<5 THEN 'warn' ELSE 'fail' END,
       CASE WHEN n=0 THEN 'no requests' ELSE printf('%.2f%%',rate) END,printf('%d requests',n)
FROM interrupted
UNION ALL
SELECT 'otel_freshness',CASE WHEN n=0 THEN 'skip' WHEN age<7200 THEN 'ok'
                             WHEN age<86400 THEN 'warn' ELSE 'fail' END,
       CASE WHEN n=0 THEN 'no events' ELSE printf('%.0f',age) END,
       'seconds since latest native OTel event'
FROM (SELECT COUNT(*) n,CAST(strftime('%s','now') AS REAL)-MAX(ts) age FROM otel_event);

DROP VIEW IF EXISTS v_background;
CREATE VIEW v_background AS
SELECT date(ts,'unixepoch','localtime') AS day,query_source,COUNT(*) AS n,SUM(cost_usd) AS cost_usd
FROM v_request_cost
WHERE source='otel'
GROUP BY day,query_source;

DROP VIEW IF EXISTS v_task_efficiency;
CREATE VIEW v_task_efficiency AS
SELECT t.task_id,t.title,t.goal,t.category,t.project,t.ts_start,t.ts_end,t.status,
       t.outcome,t.quality_score,t.rework_minutes,t.note,
       COUNT(DISTINCT tp.prompt_id) AS n_prompts,
       COUNT(DISTINCT CASE WHEN r.agent_id IS NOT NULL THEN r.agent_id END) AS n_agents,
       COUNT(DISTINCT r.request_id) AS n_requests,
       COALESCE(SUM(r.cost_usd),0) AS cost_usd,
       CASE WHEN t.ts_end IS NULL THEN NULL ELSE (t.ts_end-t.ts_start)/60.0 END
         AS elapsed_minutes,
       COALESCE(SUM(r.cost_usd),0)/NULLIF(t.quality_score,0) AS cost_per_quality_point,
       COALESCE(SUM(r.cost_usd),0)/NULLIF(COUNT(DISTINCT tp.prompt_id),0)
         AS cost_per_prompt
FROM work_task t
LEFT JOIN task_prompt tp ON tp.task_id=t.task_id
LEFT JOIN v_request_cost r ON r.prompt_id=tp.prompt_id
GROUP BY t.task_id;
