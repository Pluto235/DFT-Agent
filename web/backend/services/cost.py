"""模型 token 成本计算。

价格来源 settings.ModelInfo (input_price_per_mtok / output_price_per_mtok / price_currency)；
非 USD 模型按固定汇率 CNY_PER_USD 换算到 USD 统一展示。

Prompt cache 三段计价（2026-05-10 加，对账 deepseek 账单确认 v1 agent input ~97% cache hit）：
- 若 transcript usage 含 cache_hit_tokens / cache_miss_tokens（hit + miss == input_tokens 校验过）
  且 ModelInfo 配 cache_hit_price_per_mtok / cache_miss_price_per_mtok（目前仅 deepseek 系列）→
  cost = hit × hit_rate + miss × miss_rate + output × out_rate
- 否则 fall back 2 段：cost = input × input_rate + output × out_rate（旧逻辑，用于 anthropic /
  其他 provider 或老 transcript 缺 cache 字段的 run）

汇率写死，不做动态查询。变了改这里一行。

F2 cost preview（pre-launch 估算，2026-05-08）：
- estimate_cost: 历史均值（≥3 done runs 同 task/agent/model 组合）→ source='history'，
  否则 per-model HEURISTIC_TOKENS_PER_TURN × max_turns → source='heuristic'。
- 历史阈值 3 是可调常量；< 3 时样本太小、噪声大于信号，宁可走启发式而不报误导值。
- 启发式 token 数是粗估而非精算：input 主要由累积 conversation history 主导，turn 数 N
  时大致是 system_prompt + N × tool_outputs（每 turn ~1-3kB），output 由模型 reasoning
  长度决定（~200-1000 tok）。这里给的是 per-turn 平均值（不是单次 LLM call 输入）。
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any, Optional


# 1 USD ≈ 6.83 CNY（来源 Google Finance 0501）。CNY 浮动区间 6.7-7.3，按此粗估对成本展示足够。
# 偏差超 5% 时改这里一行；不做动态查询（避免 demo 依赖外部 API）。
CNY_PER_USD: float = 6.83


def _find_model(settings: Any, model_id: str) -> Optional[Any]:
    for m in getattr(settings, "available_models", []):
        if m.id == model_id:
            return m
    return None


def compute_cost_usd(
    settings: Any, model_id: str, input_tokens: int, output_tokens: int,
    *,
    cache_hit_tokens: Optional[int] = None,
    cache_miss_tokens: Optional[int] = None,
) -> Optional[float]:
    """返 USD 金额。model 未注册 / 价格未配 → None（前端显示 "—"）。

    单位：input_tokens / output_tokens 为绝对 token 数；price 按 per 1M tokens 计。

    Prompt cache 三段计价启用条件（同时满足）：
      1) cache_hit_tokens 与 cache_miss_tokens 都不是 None
      2) hit + miss == input_tokens（deepseek 风格 — 两者拆 input 总和；
         anthropic 风格的 cache_creation/read 是 input 之外的 extra tokens，hit+miss != input → 不启用）
      3) ModelInfo 配 cache_hit_price_per_mtok 且 cache_miss_price_per_mtok（None = 不区分）

    都满足 → cost = hit × hit_rate + miss × miss_rate + output × out_rate
    任一缺失 → 旧 2 段：cost = input × input_rate + output × out_rate（保 backward compat）
    """
    m = _find_model(settings, model_id)
    if m is None:
        return None
    rate_in = float(getattr(m, "input_price_per_mtok", 0.0) or 0.0)
    rate_out = float(getattr(m, "output_price_per_mtok", 0.0) or 0.0)
    rate_cache_hit = getattr(m, "cache_hit_price_per_mtok", None)
    rate_cache_miss = getattr(m, "cache_miss_price_per_mtok", None)
    if rate_in == 0.0 and rate_out == 0.0:
        return None
    cur = str(getattr(m, "price_currency", "USD") or "USD").upper()

    use_cache_pricing = (
        cache_hit_tokens is not None
        and cache_miss_tokens is not None
        and rate_cache_hit is not None
        and rate_cache_miss is not None
        and (cache_hit_tokens + cache_miss_tokens) == input_tokens
    )
    if use_cache_pricing:
        cost_native = (
            (cache_hit_tokens / 1_000_000.0) * float(rate_cache_hit)
            + (cache_miss_tokens / 1_000_000.0) * float(rate_cache_miss)
            + (output_tokens / 1_000_000.0) * rate_out
        )
    else:
        cost_native = (input_tokens / 1_000_000.0) * rate_in + (output_tokens / 1_000_000.0) * rate_out
    if cur == "USD":
        return cost_native
    if cur == "CNY":
        return cost_native / CNY_PER_USD
    # 未知货币：按 USD 处理（保守不放大）
    return cost_native


def sum_transcript_llm_wall_sec(transcript_path: Path) -> Optional[float]:
    """读 transcript.json + sum 各 turn `llm_wall_ms` 字段 / 1000 = 该 run 总 LLM wall（秒）。

    Solver loop.py 主循环层在每 turn `llm.call()` 前后用 time.monotonic 测,与 9 provider
    解耦,一处加全部受益。Mark_* 终态时一次性调,落 `runs.llm_wall_total_sec`。

    None 语义（区分"缺失" vs 0）:
    - transcript 文件不存在 → None
    - transcript 损坏 / JSON parse 失败 → None
    - transcript 存在但所有 turn 都没 `llm_wall_ms` 字段（譬如旧 v1 solver 跑的 run）→ None
    - 含部分 turn 有 + 部分没 → 仅 sum 非 null turn,返非 None
    """
    if not transcript_path.exists():
        return None
    try:
        ts = json.loads(transcript_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    total_ms = 0.0
    seen_any = False
    for turn in ts or []:
        v = turn.get("llm_wall_ms")
        if v is not None:
            total_ms += float(v)
            seen_any = True
    return total_ms / 1000.0 if seen_any else None


def sum_transcript_tokens(
    transcript: list[dict],
) -> tuple[int, int, Optional[int], Optional[int]]:
    """transcript 是 raw list[turn]；每 turn usage = {input_tokens, output_tokens, [cache_hit_tokens], [cache_miss_tokens]}。

    Anthropic / OpenAI 都是 per-call 计费（每次把整 history 当 input 发回去），
    所以 sum 各 turn 的 input_tokens 就是用户真实付费的 input total。

    返 (total_in, total_out, total_cache_hit, total_cache_miss)：
    - cache_hit/miss 字段在新 solver（2026-05-10+）写入；老 transcript 没有 → 整个 sum 返 None
      （区分「全 0」与「没数据」，让 cost.py 知道是否走 cache 三段计价）
    - 部分 turn 有 + 部分没 → 仅 sum 有的 turn，作 None 处理（避免双价格混算误差）
    """
    total_in = 0
    total_out = 0
    total_hit = 0
    total_miss = 0
    seen_cache = False
    all_have_cache = True
    for turn in transcript or []:
        u = turn.get("usage") or {}
        total_in += int(u.get("input_tokens") or 0)
        total_out += int(u.get("output_tokens") or 0)
        h = u.get("cache_hit_tokens")
        m = u.get("cache_miss_tokens")
        if h is not None and m is not None:
            total_hit += int(h)
            total_miss += int(m)
            seen_cache = True
        else:
            all_have_cache = False
    if seen_cache and all_have_cache:
        return (total_in, total_out, total_hit, total_miss)
    return (total_in, total_out, None, None)


# ============ F2 cost preview ============

# 每 turn 平均 token 估算（input/output 是绝对 token 数，不是 1M 单位）。
# 量级合理性：v1 agent 一个 turn 通常发回完整 conversation history 给 LLM，
# 累积下 input 趋势：N turn 末尾的 input ≈ system_prompt(2-3k) + N × (tool_call + result, 平均 1-2k)。
# 60 turn 量级时单次 input 约 60k-120k——除以 N 得 per-turn 均值 ~5-12k。
# 本表给"前沿模型"留 12k input、稀疏模型 4-6k input；output 200-800 token 是 reasoning + tool args 长度。
# 模型不在表里 → fall through 到 DEFAULT_HEURISTIC（覆盖 OpenRouter / Ollama / xAI / Mistral）。
HEURISTIC_TOKENS_PER_TURN: dict[str, dict[str, int]] = {
    # Anthropic
    "claude-opus-4-7":      {"input": 12000, "output": 800},
    "claude-sonnet-4-6":    {"input":  8000, "output": 600},
    "claude-haiku-4-5":     {"input":  5000, "output": 400},
    # DeepSeek
    "deepseek-v4-pro":      {"input":  6000, "output": 600},
    "deepseek-v4-flash":    {"input":  5000, "output": 800},
    # Moonshot Kimi
    "kimi-k2-turbo-preview": {"input":  6000, "output": 600},
    "kimi-k2-0905-preview":  {"input":  6000, "output": 600},
    "kimi-k2.5":             {"input":  6000, "output": 600},
    # OpenAI
    "gpt-4o":               {"input":  6000, "output": 500},
    "gpt-4o-mini":          {"input":  4000, "output": 400},
    "o1":                   {"input":  6000, "output": 600},
    # Google
    "gemini-2.5-pro":       {"input":  6000, "output": 600},
    "gemini-2.0-flash":     {"input":  4000, "output": 400},
    # OpenRouter / Ollama / xAI / Mistral fall through to DEFAULT_HEURISTIC
}
DEFAULT_HEURISTIC = {"input": 6000, "output": 500}

# 触发"使用历史均值"所需的最少 done runs 数；< 3 时走启发式。
_HISTORY_MIN_SAMPLES = 3
# 取最近 N 条 done runs 算均值（防早期一次离群点永久污染估算）；
# limit 10 是经验值——比平均值漂移与新模型版本的影响窗口短。
_HISTORY_RECENT_LIMIT = 10


async def estimate_cost(
    settings: Any, db: Any, *,
    task_id: str,
    agent_id: str,
    model_id: str,
    max_turns: int,
) -> dict:
    """返回 {est_usd, source: "history"|"heuristic", n_samples}。

    history 路径：≥3 条 status='done' 同 (task_id, agent_id, model_id) 组合
    且 cost_usd 非 NULL → 取最近 _HISTORY_RECENT_LIMIT 条算 mean。
    heuristic 路径：HEURISTIC_TOKENS_PER_TURN[model_id] × max_turns × per-MTok 价。
    """
    rows = await db.fetchall(
        """
        SELECT cost_usd FROM runs
        WHERE task_id = ? AND agent_id = ? AND model = ?
              AND status = 'done' AND cost_usd IS NOT NULL
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (task_id, agent_id, model_id, _HISTORY_RECENT_LIMIT),
    )
    hits = [float(r["cost_usd"]) for r in rows if r["cost_usd"] is not None]
    if len(hits) >= _HISTORY_MIN_SAMPLES:
        return {
            "est_usd": float(statistics.fmean(hits)),
            "source": "history",
            "n_samples": len(hits),
        }
    # heuristic：未知 model 走 DEFAULT；compute_cost_usd 已含 USD/CNY 换算与未知模型 None
    h = HEURISTIC_TOKENS_PER_TURN.get(model_id, DEFAULT_HEURISTIC)
    in_tok = int(h["input"]) * int(max_turns)
    out_tok = int(h["output"]) * int(max_turns)
    cost = compute_cost_usd(settings, model_id, in_tok, out_tok)
    # compute_cost_usd 返 None（model 未注册 / 价格未配）→ 仍返 0.0 以免前端 "—"
    # 误导 user 以为系统坏了；具体语义是 "无法定价" 不等于 "免费"，但展示层 0.0 比 NaN 友好。
    return {
        "est_usd": float(cost) if cost is not None else 0.0,
        "source": "heuristic",
        "n_samples": 0,
    }


def compute_run_token_cost(
    transcript_path: Path, settings: Any, model_id: str,
) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int], Optional[float]]:
    """读 workdir/transcript.json + sum tokens + 算 cost。
    monitor 在终态分支（done/failed/cancelled）一次性调，传给 mark_*。

    返 (input_tokens, output_tokens, cache_hit_tokens, cache_miss_tokens, cost_usd)：
    - cache_hit/miss 为 None：transcript 没记此字段（旧 solver 跑的 run），cost 走 2 段。
    - 都有：cost 走 3 段（cache 折扣价）。

    transcript 不存在 / 损坏 → (None, None, None, None, None)，dao 列保持 NULL（前端显示 "—"）。
    transcript 存在但全 0 token（极端 edge case） → (0, 0, None, None, 0.0)。
    """
    if not transcript_path.exists():
        return (None, None, None, None, None)
    try:
        ts = json.loads(transcript_path.read_text())
    except (OSError, json.JSONDecodeError):
        return (None, None, None, None, None)
    in_total, out_total, hit_total, miss_total = sum_transcript_tokens(ts)
    if in_total == 0 and out_total == 0:
        # transcript 存在但全 0 token（401 / 立即崩 / 0 turn）= 真没花钱，应显示 $0.00 而非 "—"
        return (0, 0, None, None, 0.0)
    cost = compute_cost_usd(
        settings, model_id, in_total, out_total,
        cache_hit_tokens=hit_total, cache_miss_tokens=miss_total,
    )
    return (in_total, out_total, hit_total, miss_total, cost)
