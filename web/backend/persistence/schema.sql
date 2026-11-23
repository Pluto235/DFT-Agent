-- runs: master table，每个 run 一行；与 metadata.json 双 source-of-truth
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    mode                TEXT NOT NULL CHECK (mode IN ('bench','custom')),
    task_id             TEXT,
    custom_task_summary TEXT,
    custom_spec_json    TEXT,
    model               TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN
        ('queued','running','evaluating','done','failed','cancelled')),
    -- solver 退出状态：bench 从 report.json 读回写；custom 由 monitor 在终态用 exit_code+timeout 推断写入
    solver_status       TEXT CHECK (solver_status IN ('ok','timeout','error')),
    source              TEXT NOT NULL DEFAULT 'demo' CHECK (source IN ('demo','bench')),
    created_at          TEXT NOT NULL,
    started_at          TEXT,
    finished_at         TEXT,
    wall_time_sec       REAL,
    total_score         REAL,
    current_turn        INTEGER NOT NULL DEFAULT 0,
    max_turns           INTEGER NOT NULL DEFAULT 80,
    -- bench 模式 harness `--timeout` 值；custom 暂未接线。0429 audit：39 条 vc-relax fail 全是
    -- 无差别 1800s timeout，原因是这字段只藏在 harness CLI default，前端无法 override。
    solver_timeout_sec  INTEGER NOT NULL DEFAULT 1800,
    exit_code           INTEGER,
    error               TEXT,
    run_dir             TEXT NOT NULL,
    pid                 INTEGER,
    cancel_requested_at TEXT,
    cancel_reason       TEXT,
    -- 终态时一次性写：sum(transcript[*].usage.input_tokens / output_tokens) + 按 ModelInfo 价格 + 汇率算 USD
    input_tokens_total  INTEGER,
    output_tokens_total INTEGER,
    -- prompt cache 拆分（deepseek API 直暴露 prompt_cache_hit_tokens / _miss_tokens）。
    -- 三段计价：hit × cache_hit_rate + miss × cache_miss_rate + output × out_rate（hit 折扣 ~120x）。
    -- NULL = 旧 v1 solver transcript 没记此字段；cost 计算回退「全 miss」假设。
    input_cache_hit_tokens_total  INTEGER,
    input_cache_miss_tokens_total INTEGER,
    cost_usd            REAL,
    -- 多租户：owner = 提交者 user.owner（disabled mode 用 'localhost'）。dao.runs_list 不按 owner 过滤
    -- (阿里云 cluster 逻辑，all see all)，仅 endpoint 层在 cancel/delete 时校验只能操作自己的；dev 通吃
    owner               TEXT NOT NULL DEFAULT 'localhost',
    -- 调度优先级：normal/high/urgent。queued 排序 = priority desc → enqueued_at asc。
    -- urgent 仅 dev role 可设；start_run / start_batch 校验。存量行默认 'normal'。
    priority            TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('normal','high','urgent'))
);

-- queue: queued runs 的有序入队记录（runs.status='queued' ⇔ queue_entries.run_id 存在）
CREATE TABLE IF NOT EXISTS queue_entries (
    run_id          TEXT PRIMARY KEY,
    queue_position  INTEGER NOT NULL,
    enqueued_at     TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

-- config: 运行时可改的 KV
CREATE TABLE IF NOT EXISTS config_kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_queue_position ON queue_entries(queue_position);
CREATE INDEX IF NOT EXISTS idx_runs_status     ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_mode       ON runs(mode);
CREATE INDEX IF NOT EXISTS idx_runs_task_id    ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at);
-- 注：idx_runs_owner 不在这里建——存量 DB 走 ALTER 加列才有 owner，必须放到
-- db.py 的 migration 列表里在 ADD COLUMN 之后执行。

PRAGMA user_version = 1;
