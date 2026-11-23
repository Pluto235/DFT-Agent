from __future__ import annotations

import asyncio
import hmac
import logging
import logging.handlers
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from backend.api import all_routers
from backend.auth.tokens import TokenRegistry, User, load_token_registry
from backend.persistence import dao
from backend.persistence.db import init_db
from backend.runners.harness_wrapper import spawn_bench_run
from backend.runners.solver_wrapper import spawn_custom_run
from backend.schemas.material import MaterialSpec
from backend.schemas.run import BenchRunStartRequest, CustomRunStartRequest
from backend.services import run_registry as _run_registry  # noqa: F401  (touch import for clarity)
from backend.services.reconciler import reconcile_runs
from backend.services.scheduler import RunHandle, Scheduler
from backend.services.scheduler_lifespan import scheduler_lifespan
from backend.settings import load_settings


log = logging.getLogger("backend")
# console handler（uvicorn nohup 抓 stdout/stderr 到 /tmp/dft-agent-bench-backend.log，
# 那个路径由 start.sh 决定，没有轮转 → 仍依赖外部 logrotate）
# 同时另写一份带轮转的副本：repo 根 `logs/backend.log`，单文件 10MB × 5 备份 = 50MB 上限，
# 防 LLM API retry 风暴 / unhandled exception 风暴打满 disk。
_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_file_handler = logging.handlers.RotatingFileHandler(
    _LOG_DIR / "backend.log", maxBytes=10 * 1024 * 1024, backupCount=5,
)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
logging.basicConfig(level=logging.INFO, handlers=[_console_handler, _file_handler])


class _BenchRunner:
    """RunnerProtocol：scheduler.promote queue → 重建 BenchRunStartRequest → 调真 spawn_bench_run。

    audit P1-6：原 v1 endpoint 直接 spawn，本 shim 抛 NotImplementedError；现在补齐
    queue-promote 路径，将来 endpoint 接入 queue 时无缝工作。
    """

    def __init__(self, settings, registry: dict[str, RunHandle], gpu_allocator=None) -> None:
        self.settings = settings
        self.registry = registry
        self.gpu_allocator = gpu_allocator

    async def spawn(self, db, run_id: str) -> RunHandle:
        # run_id 此处是 endpoint 端生成的 placeholder（status=queued）；spawn_bench_run
        # 内部 wait Phase 2 marker 反推 real run_id，并在 create_run real_id 之前
        # 先 dao.run_delete(placeholder_id) → UI 任意时刻只见一个对应行。
        row = await dao.run_get(db, run_id)
        if row is None:
            raise RuntimeError(f"queued run {run_id} not in runs table")
        if not row.get("task_id"):
            raise RuntimeError(f"bench run {run_id} missing task_id")
        # agent_id 从 DB row 读，反查 settings.agents 拿 solver_path——否则 spawn_bench_run
        # 走 settings.agent_solver_path fallback（=v1）造成 agent_id 与实际 solver 不一致。
        agent_id = row.get("agent_id") or self.settings.agents[0].id
        agent = next((a for a in self.settings.agents if a.id == agent_id), None)
        solver_path = agent.solver_path if agent else None
        req = BenchRunStartRequest(
            custom=False,
            task_id=row["task_id"],
            model=row["model"],
            max_turns=int(row.get("max_turns") or self.settings.max_turns_default),
            solver_timeout_sec=int(row.get("solver_timeout_sec") or 1800),
            agent_id=agent_id,
            solver_path=solver_path,
            qe_backend=row.get("qe_backend") or "gpu",
        )
        return await spawn_bench_run(
            settings=self.settings, db=db, registry=self.registry, request=req,
            placeholder_id=run_id, gpu_allocator=self.gpu_allocator,
        )


class _CustomRunner:
    """RunnerProtocol：scheduler.promote queue → 反序列化 custom_spec → 调真 spawn_custom_run。"""

    def __init__(self, settings, registry: dict[str, RunHandle], gpu_allocator=None) -> None:
        self.settings = settings
        self.registry = registry
        self.gpu_allocator = gpu_allocator

    async def spawn(self, db, run_id: str) -> RunHandle:
        # placeholder → real_id 的 delete 由 spawn_custom_run 内部完成。
        row = await dao.run_get(db, run_id)
        if row is None:
            raise RuntimeError(f"queued run {run_id} not in runs table")
        spec = row.get("custom_spec")
        if not spec:
            raise RuntimeError(f"custom run {run_id} missing custom_spec")
        # 同 _BenchRunner.spawn：从 row.agent_id 反查 solver_path，否则走 settings fallback (=v1)。
        agent_id = row.get("agent_id") or self.settings.agents[0].id
        agent = next((a for a in self.settings.agents if a.id == agent_id), None)
        solver_path = agent.solver_path if agent else None
        req = CustomRunStartRequest(
            custom=True,
            prompt=spec["prompt"],
            material=MaterialSpec(**spec["material"]),
            task_type=spec["task_type"],
            model=spec.get("model") or row["model"],
            max_turns=int(spec.get("max_turns") or self.settings.custom_run_max_turns_default),
            max_output_tokens=int(spec.get("max_output_tokens") or 8192),
            solver_timeout_sec=int(row.get("solver_timeout_sec") or 1800),
            agent_id=agent_id,
            solver_path=solver_path,
            qe_backend=row.get("qe_backend") or "gpu",
        )
        return await spawn_custom_run(
            settings=self.settings, db=db, registry=self.registry, request=req,
            placeholder_id=run_id, gpu_allocator=self.gpu_allocator,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = load_settings()
    app.state.settings = s

    # token registry：tokens.yaml > AUTH_TOKEN env > empty（auth disabled）
    app.state.token_registry = load_token_registry(
        tokens_yaml=s.tokens_yaml,
        env_auth_token=os.environ.get("AUTH_TOKEN") or None,
    )

    if not app.state.token_registry.has_tokens:
        raise RuntimeError("Set AUTH_TOKEN before starting the web application.")

    db = await init_db(s.demo_runs_dir / "state.db")
    app.state.db = db

    # 内存 RunHandle 字典：endpoint spawn 后写入；monitor 写终态后 pop
    app.state.run_registry = {}

    # 容量门：endpoint spawn 之前先获取 lock 再 check live count，
    # 防多并发提交瞬间突破 max_concurrent_runs。lock 序列化 spawn check + 真 spawn。
    app.state.spawn_lock = asyncio.Lock()

    # GPU 分配池：spawn 时按 active-run-count 最少挑卡注入 CUDA_VISIBLE_DEVICES。
    # nvidia-smi 探测失败 → 空池（CPU-only 部署 OK）。
    from backend.services.config_service import get_effective_gpu_policy
    from backend.services.gpu_allocator import GpuAllocator, detect_gpus
    gpu_ids = detect_gpus()
    app.state.gpu_detected_total = len(gpu_ids)
    # admin policy 持久化在 config_kv（_KV_GPU_POLICY）：dev POST /api/admin/gpu_policy
    # 立即写 KV → 重启从 KV 恢复。从未设过 → fallback 到全 enabled（detect_gpus 全集）
    app.state.gpu_policy = await get_effective_gpu_policy(db, set(gpu_ids))
    app.state.gpu_allocator = GpuAllocator(
        gpu_ids,
        enabled_resolver=lambda: app.state.gpu_policy,
    )
    log.info("gpu_allocator initialized with %s, policy=%s",
             gpu_ids if gpu_ids else "no GPUs (CPU-only mode)",
             sorted(app.state.gpu_policy))

    # QE GPU 排队模式：config_kv 持久值覆盖 settings 默认。runners 读 settings.qe_gpu_queueing_mode，
    # 所以直接 mutate s 让运行时态生效（无需重启）。admin POST /api/admin/qe_gpu_mode 同步两边。
    from backend.services.config_service import get_effective_qe_gpu_mode
    s.qe_gpu_queueing_mode = await get_effective_qe_gpu_mode(s, db)  # type: ignore[assignment]
    log.info("qe_gpu_queueing_mode = %s", s.qe_gpu_queueing_mode)

    # startup PID 校验 + 孤儿目录 warn（不依赖 metadata.json）
    await reconcile_runs(db, s.demo_runs_dir, settings=s)

    # scheduler 仅作为 queue promote loop（v1 暂时空跑；endpoint 直接 spawn）
    scheduler = Scheduler(
        db=db, settings=s,
        runners={
            "bench":  _BenchRunner(s, app.state.run_registry, app.state.gpu_allocator),
            "custom": _CustomRunner(s, app.state.run_registry, app.state.gpu_allocator),
        },
        registry=app.state.run_registry,
        tick_interval=1.0,
    )
    app.state.scheduler = scheduler

    log.info("backend started: bench_dir=%s, demo_runs_dir=%s", s.bench_dir, s.demo_runs_dir)

    # 后台预热 task cache：避免首个用户访问 /api/tasks 时阻塞 ~27s（172 题
    # render_prompt + derive_initial_files + pymatgen SpaceGroup）。
    # 用 to_thread 跑在 thread pool，否则 _load_raw_tasks 是 CPU-bound sync 会
    # 阻塞 event loop 28s（导致 startup 慢、其他请求排队）。
    async def _warm_task_cache() -> None:
        """task_loader.load_all_tasks 已 thread-pool + asyncio.Lock 保护，
        warm 和 list_tasks 共享同一把锁 → 第二个 caller 直接拿 cache，不重复 28s。"""
        try:
            from backend.services.task_loader import load_all_tasks
            t0 = time.perf_counter()
            items = await load_all_tasks(s, db)
            log.info("task cache warmed in %.1fs (%d tasks)",
                     time.perf_counter() - t0, len(items))
        except Exception:
            log.exception("task cache warm failed (will retry on first request)")
    asyncio.create_task(_warm_task_cache())

    async with scheduler_lifespan(scheduler):
        try:
            yield
        finally:
            await db.close()
            log.info("backend stopped")


app = FastAPI(
    title="DFT-Agent Bench Web",
    description="Backend adapter for CD iter-4 prototype frontend",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])


# Only the login status/check endpoints are public API routes.
_AUTH_PUBLIC_API = {"/api/auth/status", "/api/auth/check"}


def _bearer_token(request: Request) -> str:
    hdr = request.headers.get("authorization", "")
    return hdr[7:] if hdr.startswith("Bearer ") else ""


def _resolve_user(request: Request) -> Optional[User]:
    return request.app.state.token_registry.lookup(_bearer_token(request))


# ============ rate limit ============
# 防止 token 泄露后被刷 LLM API budget / 暴力枚举 token。
# default 300/min per IP：单用户 frontend polling 高峰 ~150 r/min（health 5s + scheduler 3s
# + runs 1s + transcript 1s），余量 2×；多用户独立计数（每个 cf-connecting-ip 独立桶）。
# /api/auth/check 单独 5/min：暴力枚举 32-byte AUTH_TOKEN 不可行。
limiter = Limiter(key_func=get_remote_address, default_limits=["300/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    registry: TokenRegistry = request.app.state.token_registry
    path = request.url.path

    # 静态文件 / health 公开
    if not path.startswith("/api/") or path == "/health":
        return await call_next(request)
    # 登录所需端点公开（splash 登录前没 token）
    if path in _AUTH_PUBLIC_API:
        return await call_next(request)

    user = _resolve_user(request)
    if user is None:
        return JSONResponse(
            status_code=401,
            content={"error_code": "unauthorized", "message": "missing or invalid bearer token"},
        )
    # F3 Step 2：原 middleware-level admin gate（path.startswith('/api/admin/')
    # + user.role!='dev'）已移除。改由 per-endpoint `Depends(require_capability(
    # ADMIN_*))` 守门，让"normal user 拿到 admin.view_usage 单 cap 也能调对应
    # endpoint"成为可能（middleware 级 prefix gate 会先一刀切挡掉，没有这个空间）。
    request.state.user = user
    return await call_next(request)


def _user_payload(user: User) -> dict:
    """`/api/auth/status` & `/api/auth/check` 用户字段 payload。

    F3 Step 2 形态变更（breaking）：去掉 `role: "dev"|"normal"`；新增
    `is_dev_role: bool` + `capabilities: list[str]`。前端 Step 4 适配。
    """
    return {
        "owner": user.owner,
        "is_dev_role": user.is_dev_role,
        "capabilities": list(user.capabilities),
        "display_name": user.display_name,
    }


@app.get("/api/auth/status")
async def auth_status(request: Request):
    registry: TokenRegistry = request.app.state.token_registry
    bearer = _bearer_token(request)
    if not bearer:
        return {"enabled": True, "authenticated": False, "user": None}
    user = registry.lookup(bearer)
    if user is None:
        return {"enabled": True, "authenticated": False, "user": None}
    return {"enabled": True, "authenticated": True, "user": _user_payload(user)}


@app.post("/api/auth/check")
@limiter.limit("5/minute")
async def auth_check(request: Request, payload: dict = Body(...)):
    """splash 登录表单校验 token；200 表 ok（前端存 localStorage 并刷新），
    401 表 token 错。返用户信息让 frontend 立即装上 UserPill 不用再 round-trip。
    body: {"token": "..."}"""
    registry: TokenRegistry = request.app.state.token_registry
    token = payload.get("token", "") if isinstance(payload, dict) else ""
    user = registry.lookup(token)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return {"ok": True, "user": _user_payload(user)}


@app.middleware("http")
async def log_requests(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    log.info("%s %s -> %s (%.1f ms)",
             request.method, request.url.path, response.status_code, elapsed_ms)
    return response


@app.exception_handler(Exception)
async def unknown_exception_handler(request: Request, exc: Exception):
    """unhandled exception → 500，response 不含 stack/path/exc message。
    详情仅入服务端 log（含完整 traceback），客户端只看到通用错误信息。"""
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error_code": "internal", "message": "internal server error"},
    )


@app.get("/health")
async def health(request: Request):
    """frontend 用作动态状态指示：
      - harness_ok / solver_ok / bench_questions_ok：3 个关键路径文件存在性（一次磁盘 stat）
      - tasks_loaded：task_loader cache 题数（首次 ~28s 之前是 0）
    前端 TopBar LED + Statusbar 'harness ready' 据此显示绿/黄/红。
    """
    if _resolve_user(request) is None:
        return {"ok": True}
    s = request.app.state.settings
    db = request.app.state.db
    harness_path = s.bench_dir / "harness" / "run.py"
    solver_path = Path(s.agent_solver_path) if s.agent_solver_path else None
    questions_dir = s.bench_questions_dir if hasattr(s, "bench_questions_dir") else None
    tasks_loaded = 0
    try:
        from backend.services import task_loader as _tl
        tasks_loaded = len(_tl._CACHE_RAW) if _tl._CACHE_RAW else 0
    except Exception:
        pass
    return {
        "ok": True,
        "harness_ok": harness_path.exists(),
        "harness_path": str(harness_path),
        "solver_ok": bool(solver_path and solver_path.exists()),
        "solver_path": str(solver_path) if solver_path else None,
        "bench_questions_ok": bool(questions_dir and Path(questions_dir).exists()),
        "bench_questions_dir": str(questions_dir) if questions_dir else None,
        "tasks_loaded": tasks_loaded,
        "anthropic_api_key_present": bool(s.anthropic_api_key),
    }


for router in all_routers():
    app.include_router(router)


# 同源 frontend：把 fresh repo 根目录（含 index.html / *.jsx / data.js / styles.css）
# 挂到 `/`，单端口部署（VS Code Remote 只转发一个端口即可访问 demo）。
# 必须在所有 API router 之后挂载，否则 catch-all 会拦截 /api/*。
# subclass 加 no-cache header — VS Code Simple Browser 经常激进 cache CSS/JS，
# demo 改动看不到。Cache-Control: no-store 强制每次重读。
class _NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        if isinstance(resp, Response):
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
        return resp


_FRONTEND_ROOT = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", _NoCacheStaticFiles(directory=str(_FRONTEND_ROOT), html=True), name="frontend")
