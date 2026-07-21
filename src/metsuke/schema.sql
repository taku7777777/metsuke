-- ledger.db — facts only. Derived money lives in views (see views.sql).
-- Rebuildable from the archive at any time; treat as disposable cache of truth.

CREATE TABLE IF NOT EXISTS request (
    request_id TEXT PRIMARY KEY,
    message_id TEXT,
    session_id TEXT,
    agent_id   TEXT,             -- NULL for main thread
    lineage_id TEXT,             -- session_id or session_id/agent_id
    prompt_id  TEXT,
    ts         REAL,             -- epoch seconds
    model      TEXT,
    input_tok      INTEGER,
    output_tok     INTEGER,      -- NULL when interrupted (placeholder value untrusted)
    cache_read_tok INTEGER,
    cache_w5m_tok  INTEGER,
    cache_w1h_tok  INTEGER,
    server_tool_use TEXT,        -- JSON as observed
    service_tier   TEXT,
    speed          TEXT,
    geo            TEXT,
    stop_reason    TEXT,
    is_synthetic   INTEGER DEFAULT 0,
    is_interrupted INTEGER DEFAULT 0,
    on_main_path   INTEGER DEFAULT 1,
    source         TEXT DEFAULT 'transcript',
    parser_version INTEGER,
    raw_path       TEXT,          -- source transcript rel path
    query_source TEXT,
    effort TEXT,
    cost_usd_sdk REAL,
    end_ts REAL,
    api_duration_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_request_ts ON request(ts);
CREATE INDEX IF NOT EXISTS idx_request_prompt ON request(prompt_id);
CREATE INDEX IF NOT EXISTS idx_request_lineage ON request(lineage_id, ts);
CREATE INDEX IF NOT EXISTS idx_request_session_ts ON request(session_id, ts);

CREATE TABLE IF NOT EXISTS prompt (
    prompt_id TEXT PRIMARY KEY,
    session_id TEXT,
    ts REAL,
    text TEXT,
    interrupted_message_id TEXT,
    task_label TEXT
);

CREATE TABLE IF NOT EXISTS tool_call (
    tool_use_id TEXT PRIMARY KEY,
    request_id TEXT,
    session_id TEXT,
    agent_id TEXT,
    prompt_id TEXT,
    name TEXT,
    ts REAL,
    is_error INTEGER DEFAULT 0,
    result_bytes INTEGER,
    file_path TEXT,
    lines_changed INTEGER,
    result_ts REAL,
    workflow_run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_prompt ON tool_call(prompt_id);

CREATE TABLE IF NOT EXISTS agent (
    agent_id TEXT PRIMARY KEY,
    session_id TEXT,
    agent_type TEXT,
    parent_tool_use_id TEXT,
    spawn_depth INTEGER,
    resolved_model TEXT,
    workflow_run_id TEXT
);

CREATE TABLE IF NOT EXISTS session (
    session_id TEXT PRIMARY KEY,
    project TEXT,
    slug TEXT,
    git_branch TEXT,
    cc_version TEXT,
    first_ts REAL,
    last_ts REAL
);

CREATE TABLE IF NOT EXISTS regime_event (
    ts REAL,
    kind TEXT,                   -- cc_version | model_new | price_change | config_change
    detail TEXT,
    UNIQUE(kind, detail)
);

CREATE TABLE IF NOT EXISTS quarantine (
    ts REAL,
    src TEXT,
    reason TEXT,
    raw TEXT
);

CREATE TABLE IF NOT EXISTS ingest_log (
    ts REAL,
    manifest_pos INTEGER,        -- lines consumed from archive manifest
    segments INTEGER,
    records INTEGER,
    quarantined INTEGER,
    parser_version INTEGER
);

-- price: SCD2, seeded from prices/prices.json (git). Facts join at read time.
CREATE TABLE IF NOT EXISTS price (
    model TEXT,
    valid_from TEXT,             -- ISO date
    valid_to   TEXT,             -- NULL = current
    in_usd  REAL,                -- per 1M tokens
    out_usd REAL,
    cache_read_x REAL,
    cache_w5m_x  REAL,
    cache_w1h_x  REAL,
    batch_x REAL,
    fast_x  REAL,
    geo_us_x REAL,
    source_url TEXT,
    PRIMARY KEY (model, valid_from)
);

-- Non-token charges reported in message.usage.server_tool_use. Rates are per
-- observed unit (for example, one web_search_requests unit = one search).
CREATE TABLE IF NOT EXISTS price_server_tool (
    tool TEXT,
    valid_from TEXT,
    valid_to TEXT,
    usd_per_unit REAL NOT NULL,
    source_url TEXT,
    PRIMARY KEY (tool, valid_from)
);

-- prompt attribution state: assistant records carry no promptId (verified on real
-- data); we track the current prompt per lineage from user records instead.
CREATE TABLE IF NOT EXISTS lineage_state (
    lineage_id TEXT PRIMARY KEY,
    prompt_id TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS hook_event (
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    session_id TEXT,
    prompt_id TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hook_event_session_ts ON hook_event(session_id, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hook_event_raw ON hook_event(payload_json);

CREATE TABLE IF NOT EXISTS nudge (
    rule TEXT NOT NULL,
    fired_ts REAL NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    detail_json TEXT,
    followed INTEGER,
    decided_ts REAL,
    outcome TEXT,
    outcome_reason TEXT,
    observed_json TEXT,
    experiment_group TEXT NOT NULL DEFAULT 'treatment',
    PRIMARY KEY (rule, fired_ts, session_id)
);

CREATE TABLE IF NOT EXISTS marker (
    marker_id TEXT PRIMARY KEY,
    ts_start REAL NOT NULL,
    ts_end REAL,
    category TEXT,
    hypothesis TEXT,
    expected_effect TEXT,
    verdict TEXT,
    verdict_ts REAL,
    decided_by TEXT,
    saving_usd REAL,
    saving_low_usd REAL,
    saving_high_usd REAL,
    saving_basis TEXT,
    verdict_note TEXT
);

CREATE TABLE IF NOT EXISTS outcome (
    prompt_id TEXT NOT NULL,
    ts REAL NOT NULL,
    label TEXT NOT NULL,
    lines_added INTEGER,
    lines_removed INTEGER,
    commits INTEGER,
    source TEXT NOT NULL,
    UNIQUE (prompt_id, ts, source)
);

CREATE TABLE IF NOT EXISTS work_task (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal TEXT,
    category TEXT NOT NULL,
    project TEXT,
    ts_start REAL NOT NULL,
    ts_end REAL,
    status TEXT NOT NULL DEFAULT 'active',
    outcome TEXT,
    quality_score INTEGER,
    rework_minutes REAL,
    note TEXT,
    created_by TEXT NOT NULL DEFAULT 'human'
);

CREATE TABLE IF NOT EXISTS task_prompt (
    task_id TEXT NOT NULL,
    prompt_id TEXT NOT NULL UNIQUE,
    attached_ts REAL NOT NULL,
    source TEXT NOT NULL,
    confidence REAL,
    PRIMARY KEY (task_id,prompt_id)
);

CREATE TABLE IF NOT EXISTS roi_cost (
    cost_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    minutes REAL,
    usd REAL,
    note TEXT,
    source TEXT NOT NULL DEFAULT 'human'
);

CREATE TABLE IF NOT EXISTS commit_event (
    sha TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    repo TEXT,
    repo_path TEXT,
    branch TEXT,
    subject TEXT,
    insertions INTEGER,
    deletions INTEGER,
    files_json TEXT,
    prompt_id TEXT
);

CREATE TABLE IF NOT EXISTS invoice (
    month TEXT PRIMARY KEY,
    billed_usd REAL NOT NULL,
    note TEXT,
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS otel_event (
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    session_id TEXT,
    request_id TEXT,
    prompt_id TEXT,
    model TEXT,
    effort TEXT,
    query_source TEXT,
    speed TEXT,
    input_tok INTEGER,
    output_tok INTEGER,
    cache_read_tok INTEGER,
    cache_creation_tok INTEGER,
    cost_usd_sdk REAL,
    duration_ms REAL,
    error TEXT,
    status_code TEXT,
    dedup_key TEXT NOT NULL UNIQUE,
    raw_json TEXT NOT NULL
);
