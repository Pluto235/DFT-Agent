from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite


SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class Database:
    """单连接 aiosqlite 包装。aiosqlite 内部串行化写，对 demo 并发量足够。"""

    def __init__(self, path: Path):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._init_schema()

    async def _init_schema(self) -> None:
        assert self._conn is not None
        sql = SCHEMA_PATH.read_text()
        await self._conn.executescript(sql)
        # 增量 migration：SQLite 不支持 IF NOT EXISTS 列；逐条 ALTER + 容错 duplicate
        for ddl in [
            "ALTER TABLE runs ADD COLUMN solver_timeout_sec INTEGER NOT NULL DEFAULT 1800",
            "ALTER TABLE runs ADD COLUMN input_tokens_total INTEGER",
            "ALTER TABLE runs ADD COLUMN output_tokens_total INTEGER",
            "ALTER TABLE runs ADD COLUMN cost_usd REAL",
            # owner 列；老库没此列时存量行通过 DEFAULT 'localhost' 兼容 _LOCALHOST_USER
            "ALTER TABLE runs ADD COLUMN owner TEXT NOT NULL DEFAULT 'localhost'",
            "CREATE INDEX IF NOT EXISTS idx_runs_owner ON runs(owner)",
            # priority 列；存量行默认 'normal'。CHECK 在新建表时生效；ALTER 加列时
            # SQLite 不会回填校验存量值，但因为 DEFAULT='normal' 是合法值，所以无忧。
            "ALTER TABLE runs ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'",
            # agent_id 列：哪个 agent 跑的；存量行 NULL（UI 回退到 cfg.agents[0]）。
            "ALTER TABLE runs ADD COLUMN agent_id TEXT",
            # qe_call_count / qe_wall_sec：QE 子进程统计；存量行 NULL（UI 显 -）
            "ALTER TABLE runs ADD COLUMN qe_call_count INTEGER",
            "ALTER TABLE runs ADD COLUMN qe_wall_sec REAL",
            # QE wall 双信号：let UI 在两路明显不一致时（agent 重跑覆盖 .out / 后台启动）
            # 在主显后 paren 提示。详见 services/qe_stats.py compute_qe_stats_detailed
            "ALTER TABLE runs ADD COLUMN qe_wall_self_reported_sec REAL",
            "ALTER TABLE runs ADD COLUMN qe_wall_bash_cmd_sec REAL",
            # llm_wall_total_sec：sum(transcript[*].llm_wall_ms) / 1000，主循环层 time.monotonic
            # 测；与 9 provider adapter 解耦。NULL = 旧 v1 solver 跑的 run 没记此字段。
            # baseline-audit-rerun 用此字段做"qe_time ≈ wall − llm − bash" 间接推。
            "ALTER TABLE runs ADD COLUMN llm_wall_total_sec REAL",
            # prompt cache 拆分：deepseek API 暴露 prompt_cache_hit_tokens / prompt_cache_miss_tokens；
            # cost = hit × hit_rate + miss × miss_rate + output × out_rate（hit 折扣 ~120x，致 cost 大降）。
            # NULL = 旧 transcript 没此字段（v1 solver 跑的旧 run），cost 计算 fall back 到全 miss 假设。
            "ALTER TABLE runs ADD COLUMN input_cache_hit_tokens_total INTEGER",
            "ALTER TABLE runs ADD COLUMN input_cache_miss_tokens_total INTEGER",
            # qe_backend：每个 run 跑在 GPU (默认，podman wrapper) 还是 CPU (native CPU 7.4 build)。
            # ALTER ADD COLUMN NOT NULL DEFAULT 自动回填存量行为 'gpu'。
            "ALTER TABLE runs ADD COLUMN qe_backend TEXT NOT NULL DEFAULT 'gpu'",
        ]:
            try:
                await self._conn.execute(ddl)
            except aiosqlite.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected; call connect() first")
        return self._conn

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        async with self.conn.execute(sql, params):
            pass
        await self.conn.commit()

    async def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())


async def init_db(path: Path) -> Database:
    db = Database(path)
    await db.connect()
    return db
