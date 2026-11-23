"""Raw transcript.json → flat TranscriptTurn (prototype 期望的 shape)。

solver agent/loop.py 写出顶层 JSON list，每元素 `{stop_reason, content[], usage}`；
prototype run.jsx 行 92-100 期待 flat shape (turn/role/text_blocks/tool_calls/tool_results/usage)。
adapter 做 raw → flat 的 lift 是宪法 2 的核心实现：前端零 drift，backend 适配 raw。

实测要点（spike 0502）：
- 单 turn `content[]` 可含多个 `tool_use` block（4.5/4.6 系列默认 parallel tool use）
- `tool_use.input` 可选字段会缺失（schema default 不被 LLM fill），消费端必须 .get(key, default)
- bash.log `cwd=` 字段 harness 跑（绝对路径）vs 单跑（相对路径）会不同
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.schemas.run import TranscriptTurn


DEFAULT_BASH_TIMEOUT_S = 900


def _normalize_tool_input(tool_name: str, raw_input: dict) -> dict:
    """对齐 solver tool input → prototype 期望字段。
    - bash: cmd → command；timeout_s 缺失补 default 900（schema 有 default 但 LLM 不 fill）
    其它 tool 直通。
    """
    if tool_name == "bash":
        return {
            "command": raw_input.get("cmd", ""),
            "timeout_s": raw_input.get("timeout_s", DEFAULT_BASH_TIMEOUT_S),
        }
    return dict(raw_input)


_HEADER_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+\[cwd=(?P<cwd>[^\]]*)\]\s+\[timeout=(?P<timeout>\d+)s\]\s+"
    r"\[wall=(?P<wall>[\d.]+)s\]\s+\[exit=(?P<exit>-?\d+)\]"
)
# BLOCKED 行：guard 拦截 cmd 没真跑（譬如 `realpath` 不在白名单）。
# 缺 timeout/wall/exit 字段，但仍对应 transcript 里一条 bash 调用——必须算 block，
# 否则与 transcript bash 顺序错位，导致 wall_offset / tool_results 全部偏移。
_HEADER_BLOCKED_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+\[cwd=(?P<cwd>[^\]]*)\]\s+\[BLOCKED"
)


def parse_bash_log_results(bash_log_path: Path) -> list[dict]:
    """扫 workdir/bash.log → list[dict]，按 header [ts] [cwd=..] [timeout=Ns] [wall=Ns] [exit=N] 分块。

    格式（spike1 §2.4 + spike2 §2.3）：
        [ts] [cwd=...] [timeout=900s] [wall=0.05s] [exit=0]
        $ <cmd>
        --- stdout ---
        <stdout block, 可能空缺整段>
        --- stderr ---
        <stderr block, 同上>

    cwd 字段 harness 跑（绝对）vs 单跑（相对）会不同；调用端如需比对路径用 `Path(cwd).resolve()`。
    """
    if not bash_log_path.exists():
        return []
    text = bash_log_path.read_text(errors="replace")
    blocks: list[dict] = []
    cur: Optional[dict] = None
    cur_section: Optional[str] = None
    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            if cur is not None:
                blocks.append(cur)
            cur = {
                "ts": m.group("ts"),
                "cwd": m.group("cwd"),
                "timeout_s": int(m.group("timeout")),
                "wall_s": float(m.group("wall")),
                "exit_code": int(m.group("exit")),
                "cmd": "",
                "stdout": "",
                "stderr": "",
                "blocked": False,
            }
            cur_section = None
            continue
        mb = _HEADER_BLOCKED_RE.match(line)
        if mb:
            if cur is not None:
                blocks.append(cur)
            cur = {
                "ts": mb.group("ts"),
                "cwd": mb.group("cwd"),
                "timeout_s": 0,
                "wall_s": 0.0,
                "exit_code": 0,
                "cmd": "",
                "stdout": "",
                "stderr": "",
                "blocked": True,
            }
            cur_section = None
            continue
        if cur is None:
            continue
        if line.startswith("$ "):
            cur["cmd"] = line[2:]
            cur_section = "cmd"
        elif line == "--- stdout ---":
            cur_section = "stdout"
        elif line == "--- stderr ---":
            cur_section = "stderr"
        elif cur_section in ("stdout", "stderr"):
            cur[cur_section] += line + "\n"
    if cur is not None:
        blocks.append(cur)
    return blocks


def _parse_log_ts(ts_str: str) -> Optional[float]:
    """bash.log header 的 'YYYY-MM-DD HH:MM:SS' → epoch sec（local tz；与 run.started_at 同基。"""
    try:
        return datetime.strptime(ts_str.strip(), "%Y-%m-%d %H:%M:%S").timestamp()
    except (ValueError, TypeError):
        return None


def _parse_log_ts_utc(ts_str: str) -> Optional[float]:
    """同 _parse_log_ts 但显式按 UTC 解析（生产环境 server tz=UTC，bash 工具 datetime.now()
    写出的 naive ts 实际是 UTC）。专门用于和 qe_calls.jsonl 的 'YYYY-MM-DDTHH:MM:SSZ' 时间
    戳互相比对——两边都按 UTC 解析才能匹配窗口。"""
    try:
        return datetime.strptime(ts_str.strip(), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except (ValueError, TypeError):
        return None


def _parse_iso_utc_strict(iso: str) -> Optional[float]:
    """qe_calls.jsonl 里 ts 是 'YYYY-MM-DDTHH:MM:SS[.ffffff]Z'。"""
    try:
        return datetime.fromisoformat(iso.rstrip("Z")).replace(
            tzinfo=timezone.utc
        ).timestamp()
    except (ValueError, TypeError):
        return None


def _load_qe_trace_entries(workdir: Path) -> list[dict]:
    """读 workdir/qe_calls.jsonl → list of {ts_epoch, raw}（按 ts 升序）。
    文件不存在 / 为空 / 全部 corrupt → 返 []。"""
    f = workdir / "qe_calls.jsonl"
    if not f.exists():
        return []
    entries: list[dict] = []
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_epoch = _parse_iso_utc_strict(r.get("ts", ""))
        if ts_epoch is None:
            continue
        entries.append({"ts_epoch": ts_epoch, "raw": r})
    entries.sort(key=lambda e: e["ts_epoch"])
    return entries


def _qe_calls_in_window(
    trace: list[dict], block_start: float, block_wall: float,
) -> int:
    """trace 中 ts ∈ [block_start, block_start + block_wall + slack] 的条数。
    slack = 1s，覆盖 trace 写入相对 subprocess.run 退出有微小延迟（先 close output
    handle 再调 _trace_qe_call）。"""
    SLACK = 1.0
    end = block_start + block_wall + SLACK
    return sum(1 for e in trace if block_start <= e["ts_epoch"] <= end)


def adapt_raw_transcript(
    raw: list[dict], bash_log_path: Path,
    started_at: Optional[str] = None, finished_at: Optional[str] = None,
) -> list[TranscriptTurn]:
    """raw[i] = {stop_reason, content, usage} → flat TranscriptTurn。
    bash tool_use 与 bash.log 顺序按 idx 1:1 对齐重建 tool_results。
    `started_at` (ISO Z): 给每 turn 算 wall_offset_sec；缺失则全 None。
    `finished_at` (ISO Z): 给末尾被外层 wall 超时 kill 的 cmd 估 cmd_wall_sec
    （bash.log 没记 metadata 的 cmd），用 (finished_at - last_done_end) / N_killed 均分。

    qe_call_count（v2 only）：每 turn 内通过 qe_tools 实际启动的 QE binary 数。
    匹配方式：扫 workdir/qe_calls.jsonl trace（每次 run_pw/run_dos/run_bands/run_pp 写一行），
    每个 trace ts 落到哪个 bash block 的 [start, start+wall+slack] 窗口里 → 该 bash 所属
    的 turn 计数 +1。trace 文件不存在（v1 / 老 row）→ 所有 turn 的 qe_call_count = None。
    """
    workdir = bash_log_path.parent
    bash_results = parse_bash_log_results(bash_log_path)
    qe_trace = _load_qe_trace_entries(workdir)
    has_trace = bool(qe_trace) or (workdir / "qe_calls.jsonl").exists()
    # 预计算每个 bash block 在窗口里包含多少 trace 条目（按 block index 排）
    qe_count_per_block: list[int] = []
    for br in bash_results:
        block_start = _parse_log_ts_utc(br.get("ts", ""))
        if block_start is None or br.get("blocked"):
            qe_count_per_block.append(0)
            continue
        qe_count_per_block.append(_qe_calls_in_window(qe_trace, block_start, float(br.get("wall_s") or 0.0)))
    bash_call_idx = 0

    started_epoch: Optional[float] = None
    if started_at:
        try:
            # ISO 'YYYY-MM-DDTHH:MM:SS.sssZ' → UTC epoch
            s = started_at.rstrip("Z")
            started_epoch = datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()
        except (ValueError, TypeError):
            started_epoch = None

    last_offset: Optional[float] = None
    flat: list[TranscriptTurn] = []
    # 记录每 turn 有多少 bash tool_use 没匹配到 bash_results（被 wall 超时 kill 等）；
    # 末尾遍历完后统一估算 wall 并加回该 turn 的 cmd_wall_sec。
    killed_per_turn: list[int] = []
    for i, r in enumerate(raw):
        content = r.get("content", []) or []
        text_blocks = [b.get("text", "") for b in content if b.get("type") == "text"]
        tool_calls: list[dict] = []
        tool_results: list[dict] = []
        first_bash_ts: Optional[str] = None
        cmd_wall_sum: float = 0.0
        has_bash = False
        killed_in_turn = 0
        qe_count_in_turn = 0
        for b in content:
            if b.get("type") != "tool_use":
                continue
            tool_calls.append({
                "tool_id": b.get("id", ""),
                "tool_name": b.get("name", ""),
                "input": _normalize_tool_input(b.get("name", ""), b.get("input", {}) or {}),
            })
            if b.get("name") == "bash":
                if bash_call_idx < len(bash_results):
                    br = bash_results[bash_call_idx]
                    if first_bash_ts is None:
                        first_bash_ts = br.get("ts")
                    cmd_wall_sum += float(br.get("wall_s") or 0.0)
                    has_bash = True
                    stdout = br.get("stdout", "") or ""
                    tool_results.append({
                        "tool_id": b.get("id", ""),
                        "stdout_tail": stdout[-500:],
                        "has_content": bool(stdout.strip()),
                    })
                    if bash_call_idx < len(qe_count_per_block):
                        qe_count_in_turn += qe_count_per_block[bash_call_idx]
                    bash_call_idx += 1
                else:
                    # 这条 bash tool_use 没在 bash.log 里——被 wall 超时 kill
                    killed_in_turn += 1
                    has_bash = True

        offset_sec: Optional[float] = None
        if started_epoch is not None and first_bash_ts:
            ts_epoch = _parse_log_ts(first_bash_ts)
            if ts_epoch is not None:
                offset_sec = max(0.0, ts_epoch - started_epoch)
                last_offset = offset_sec
        if offset_sec is None and last_offset is not None:
            offset_sec = last_offset

        killed_per_turn.append(killed_in_turn)
        flat.append(TranscriptTurn(
            turn=i + 1,
            role="assistant",
            stop_reason=r.get("stop_reason"),
            text_blocks=text_blocks,
            tool_calls=tool_calls,
            tool_results=tool_results,
            usage=r.get("usage"),
            wall_offset_sec=offset_sec,
            cmd_wall_sec=cmd_wall_sum if has_bash else None,
            qe_call_count=(qe_count_in_turn if has_trace else None),
        ))

    # killed cmd wall 估值：bash.log 最后一条结束 → finished_at 的窗口，按 killed cmd 数均分
    total_killed = sum(killed_per_turn)
    if total_killed > 0 and finished_at and bash_results:
        finished_epoch = _parse_iso_utc(finished_at)
        last_done = bash_results[-1]
        last_done_ts = _parse_log_ts(last_done.get("ts", ""))
        if finished_epoch is not None and last_done_ts is not None:
            last_done_end = last_done_ts + float(last_done.get("wall_s") or 0.0)
            killed_window = max(0.0, finished_epoch - last_done_end)
            per_killed = killed_window / total_killed
            for idx, n_killed in enumerate(killed_per_turn):
                if n_killed > 0:
                    base = flat[idx].cmd_wall_sec or 0.0
                    flat[idx] = flat[idx].model_copy(
                        update={"cmd_wall_sec": base + per_killed * n_killed}
                    )
    return flat


def _parse_iso_utc(iso: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(iso.rstrip("Z")).replace(
            tzinfo=timezone.utc
        ).timestamp()
    except (ValueError, TypeError):
        return None
