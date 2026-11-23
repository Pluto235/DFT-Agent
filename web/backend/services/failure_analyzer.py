"""调 claude-opus-4-7 分析 failed run 的根因。按钮触发，cache 到 run_dir/failure_analysis.md。

输入采样（保持 prompt 小、成本可控）：
- task_prompt.txt（任务 spec）
- DB row.error / report.json failure_reason
- bash.log 末尾 ~3KB（最后几条 cmd 输出/错误）
- transcript 末尾 5 turn 的 text + bash cmds
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional


log = logging.getLogger("failure_analyzer")

_MODEL = "claude-opus-4-7"
_TIMEOUT_S = 60.0
_BASH_LOG_TAIL_BYTES = 3000
_TRANSCRIPT_TAIL_TURNS = 5
_CACHE_FILENAME = "failure_analysis.md"

_SYSTEM_PROMPT = """你是 DFT 计算 + LLM agent 调试专家，分析一个 agent 在 Quantum ESPRESSO bench 上失败的 run。
基于给定材料（task_prompt + 末尾 bash.log + 末尾 transcript turns + failure_reason），给出三段中文分析。

输出格式（严格遵守，纯文本，不要 markdown 标题、不要 ```代码块```）：

【根本原因】
1-2 句话。直接说技术原因。

【关键证据】
2-4 行。每行一条具体证据：cmd / 错误信息 / turn 编号。

【改进建议】
2-3 条。每条一行，agent 应该怎样做才能成功（譬如 timeout_s 调到 X / pseudo path 应该怎么写 / 应该用 mpirun 哪种 flag）。

注意：
- 只分析失败原因，不要复述任务描述
- 如果失败是 wall 超时被 kill，要明确指出"agent 时间预估失败"
- 如果是 pseudo path 错，要给出正确路径写法
- 如果是 mpirun 参数错，要给出正确参数
- 中文输出
"""


def _format_user_input(
    *,
    task_prompt: str,
    failure_reason: Optional[str],
    error: Optional[str],
    bash_log_tail: str,
    transcript_tail: str,
    wall_time_sec: Optional[float],
    qe_call_count: Optional[int],
    current_turn: Optional[int],
    max_turns: Optional[int],
) -> str:
    parts = ["=== 任务 prompt ==="]
    parts.append(task_prompt[:2000] if task_prompt else "(empty)")
    parts.append("")
    parts.append("=== 终态状态 ===")
    parts.append(f"wall_time_sec: {wall_time_sec}")
    parts.append(f"qe_call_count: {qe_call_count}")
    parts.append(f"turn: {current_turn}/{max_turns}")
    parts.append(f"failure_reason: {failure_reason or '(none)'}")
    parts.append(f"error: {error or '(none)'}")
    parts.append("")
    parts.append("=== bash.log 末尾 ===")
    parts.append(bash_log_tail or "(empty)")
    parts.append("")
    parts.append("=== transcript 末尾 turns ===")
    parts.append(transcript_tail or "(empty)")
    return "\n".join(parts)


def _read_bash_log_tail(workdir: Path) -> str:
    p = workdir / "bash.log"
    if not p.exists():
        return ""
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > _BASH_LOG_TAIL_BYTES:
                f.seek(size - _BASH_LOG_TAIL_BYTES)
            data = f.read()
        text = data.decode(errors="replace")
        if size > _BASH_LOG_TAIL_BYTES:
            # 切掉可能不完整的首行
            nl = text.find("\n")
            if nl > 0:
                text = text[nl + 1:]
        return text
    except OSError:
        return ""


def _read_transcript_tail(workdir: Path) -> str:
    p = workdir / "transcript.json"
    if not p.exists():
        return ""
    try:
        raw = json.loads(p.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(raw, list):
        return ""
    tail = raw[-_TRANSCRIPT_TAIL_TURNS:]
    lines: list[str] = []
    base_idx = len(raw) - len(tail) + 1
    for offset, t in enumerate(tail):
        turn_n = base_idx + offset
        lines.append(f"--- turn {turn_n} (stop={t.get('stop_reason')}) ---")
        for c in t.get("content", []) or []:
            ctype = c.get("type")
            if ctype == "text":
                txt = (c.get("text") or "")[:500]
                lines.append(f"[text] {txt}")
            elif ctype == "tool_use":
                tn = c.get("name", "?")
                inp = c.get("input", {}) or {}
                if tn == "bash":
                    cmd = (inp.get("cmd") or "")[:300]
                    lines.append(f"[bash] {cmd}")
                elif tn == "write_file":
                    lines.append(f"[write_file] {inp.get('path', '?')}")
                else:
                    lines.append(f"[{tn}] {json.dumps(inp)[:200]}")
    return "\n".join(lines)


def _read_failure_reason(run_dir: Path) -> Optional[str]:
    """bench: report.json.failure_reason；custom: result.json.reason。"""
    rep = run_dir / "report.json"
    if rep.exists():
        try:
            d = json.loads(rep.read_text())
            return d.get("failure_reason") or d.get("notes")
        except (OSError, json.JSONDecodeError):
            pass
    res = run_dir / "workdir" / "result.json"
    if res.exists():
        try:
            d = json.loads(res.read_text())
            return d.get("reason")
        except (OSError, json.JSONDecodeError):
            pass
    return None


def get_cached_analysis(run_dir: Path) -> Optional[str]:
    p = run_dir / _CACHE_FILENAME
    if not p.exists():
        return None
    try:
        return p.read_text(errors="replace")
    except OSError:
        return None


async def analyze_failure(
    *,
    run_dir: Path,
    error: Optional[str],
    wall_time_sec: Optional[float],
    qe_call_count: Optional[int],
    current_turn: Optional[int],
    max_turns: Optional[int],
    api_key: str,
    force: bool = False,
) -> str:
    """分析 failed run；force=False 时优先返 cache。返 markdown / 纯文本（中文）。"""
    cache = get_cached_analysis(run_dir)
    if cache is not None and not force:
        return cache

    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    workdir = run_dir / "workdir"
    task_prompt = ""
    tp = workdir / "task_prompt.txt"
    if tp.exists():
        try:
            task_prompt = tp.read_text(errors="replace")
        except OSError:
            pass

    user_input = _format_user_input(
        task_prompt=task_prompt,
        failure_reason=_read_failure_reason(run_dir),
        error=error,
        bash_log_tail=_read_bash_log_tail(workdir),
        transcript_tail=_read_transcript_tail(workdir),
        wall_time_sec=wall_time_sec,
        qe_call_count=qe_call_count,
        current_turn=current_turn,
        max_turns=max_turns,
    )

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)
    msg = await asyncio.wait_for(
        client.messages.create(
            model=_MODEL,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_input}],
        ),
        timeout=_TIMEOUT_S,
    )
    analysis = "".join(
        getattr(b, "text", "") for b in msg.content
        if getattr(b, "type", None) == "text"
    )
    if not analysis.strip():
        raise RuntimeError("model returned empty analysis")

    # 写 cache（失败不抛错，只 log）
    try:
        (run_dir / _CACHE_FILENAME).write_text(analysis)
    except OSError:
        log.exception("write failure_analysis cache failed: %s", run_dir)

    return analysis
