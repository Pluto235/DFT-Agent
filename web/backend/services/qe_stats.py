"""QE 调用次数 + 累计 wall-time 统计。

call_count（agent 发起的 QE 调用数）数据源：
- 主：`<run_dir>/workdir/bash.log` —— 每条 bash cmd 先打 metadata header
  `[ts] [cwd=...] [timeout=Ns] [wall=X.XXs] [exit=N]`，下一行 `$ <cmd>`。
- Fallback：transcript.json —— 若 transcript 比 bash.log 多 N 条（被外层 wall 超时 kill 没落
  metadata），末尾 N 条 1:1 对应到 transcript 末尾。

wall_sec（QE 实际 GPU 计算耗时）数据源：取以下两路 **max**：
- A) 扫 `<run_dir>/workdir/**/*.out` 里 QE binary 末尾自报的 summary 行
  `^\\s+(PWSCF|BANDS|DOS|PROJWFC|PH|PP)\\s+:\\s+\\S+\\s+CPU\\s+<wall>\\s+WALL\\s*$`
  （不带 `(N calls)` 后缀）。pw.x 等正常退出写这一行，前台/后台无关。
- B) 扫 bash.log，每个 QE cmd 的 [wall=...] 求和 + transcript 估 killed 窗口的旧算法。

为什么取 max：
- 后台启动场景（`pw.x &` 立刻返回）：bash wall ≈ 0，但 .out WALL 是真实计算时间 → A 大；
- agent 重跑覆盖 .out 场景：bash wall = 历次 cmd 总和，.out WALL 仅最后一次（因 `>` 覆盖）
  → B 大；
- 普通单次 run：两者基本一致（A ≈ pure compute；B ≈ A + 微量 mpirun/容器启动开销）。
取 max 在两端 case 都不会少算（旧的"优先 .out"分支会漏 agent 重跑；纯 bash 算法漏后台启动）。

QE 判定（call_count / 旧 fallback wall 都用）：
- 拆段：把 cmd 用 `;` / `&&` / `||` / `|` / `&` / `\\n` 拆开，每段独立判
- 每段提取主命令 token（跳过 env var `FOO=bar` 和前导词 `time`/`nohup`/`exec`/`sudo`）
- 主命令 basename ∈ QE_BINS：
  - args 含 `--version`/`--help`/`-version`/`-help`/`-V`/`-h` → 不算（查询）
  - 否则 → 算
- 主命令 ∈ MPI_LAUNCHERS（mpirun/mpiexec/srun）且 args 中有 token basename ∈ QE_BINS：
  - 若 QE binary 后续 args 含查询 flag → 不算
  - 否则 → 算

排除案例：`which pw.x` / `cat $(which pw.x)` / `cd ... && cat pw.in` / `pw.x --version`。

输出 (call_count, wall_sec)；文件不存在 / 解析失败 → (0, 0.0)。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple


_QE_BINS = ("pw.x", "dos.x", "pp.x", "projwfc.x", "bands.x", "ph.x")
_MPI_LAUNCHERS = ("mpirun", "mpiexec", "srun")
_PRE_TOKENS = ("time", "nohup", "exec", "sudo")
# 查询性 flag：QE binary 见到这些就只是打印 version/help，不是实际计算
_QUERY_FLAGS = ("--version", "--help", "-version", "-help", "-V", "-h")
_HEADER_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+\[cwd=[^\]]*\]\s+\[timeout=\d+s\]\s+"
    r"\[wall=(?P<wall>[\d.]+)s\]\s+\[exit=-?\d+\]"
)
# BLOCKED 行：guard 拦截 cmd 没真跑（譬如 `realpath` 不在白名单）。
# 格式：`[ts] [cwd=...] [BLOCKED by guard]` —— 缺 timeout/wall/exit。
# 此条仍占 transcript 中一条 bash 调用位置，必须算成 block 否则会与 transcript 顺序错位。
_HEADER_BLOCKED_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+\[cwd=[^\]]*\]\s+\[BLOCKED"
)
_SEG_SPLIT_RE = re.compile(r"&&|\|\||;|\||&|\n")

# QE binary 末尾 summary 行：`     PWSCF        :     24.99s CPU     24.76s WALL`
# 关键约束：行尾必须是 `WALL`（不带 `(N calls)`），过滤掉 init_run / electrons 等子项。
# 顶层 binary 名白名单（QE 7.5）：pw.x→PWSCF / bands.x→BANDS / dos.x→DOS / projwfc.x→PROJWFC
# / ph.x→PH / pp.x→PP（也叫 PP_POSTPROC，实际 grep 用前缀匹配更稳）。
_QE_WALL_RE = re.compile(
    r"^\s+(?:PWSCF|BANDS|DOS|PROJWFC|PH|PP)\s+:\s+\S+\s+CPU\s+(.+?)\s+WALL\s*$"
)
# 时间字符串解析：`24.76s` / `2m 3.68s` / `1m41.74s` / `8m 8.14s` / `3h 5m 2.5s`
_TIME_PARTS_RE = re.compile(
    r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*([\d.]+)\s*s"
)


def _segment_invokes_qe(seg: str) -> bool:
    """段（cmd 用 shell 操作符拆开后的 piece）的主命令是否实际启动 QE binary。"""
    tokens = seg.strip().split()
    main = None
    args = []
    for tok in tokens:
        # 前导 env var FOO=bar / FOO=$(...)，主命令未定时跳过
        if main is None and "=" in tok and not tok.startswith("-"):
            head = tok.split("=", 1)[0]
            if head and head[0].isalpha() and all(c.isalnum() or c == "_" for c in head):
                continue
        if main is None and tok in _PRE_TOKENS:
            continue
        if main is None:
            main = tok
        else:
            args.append(tok)
    if main is None:
        return False
    main_base = main.rsplit("/", 1)[-1]
    if main_base in _QE_BINS:
        if any(a in _QUERY_FLAGS for a in args):
            return False
        return True
    if main_base in _MPI_LAUNCHERS:
        for i, a in enumerate(args):
            if a.rsplit("/", 1)[-1] in _QE_BINS:
                qe_args = args[i + 1:]
                if any(qa in _QUERY_FLAGS for qa in qe_args):
                    return False
                return True
    return False


def _is_qe_invocation(cmd: str) -> bool:
    return any(_segment_invokes_qe(seg) for seg in _SEG_SPLIT_RE.split(cmd))


def _parse_log_ts_utc(ts_str: str) -> Optional[float]:
    """bash.log header `YYYY-MM-DD HH:MM:SS` → UTC epoch sec（服务器 tz 假设 UTC）。"""
    try:
        return datetime.strptime(ts_str.strip(), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except (ValueError, TypeError):
        return None


def _parse_iso_utc(iso: str) -> Optional[float]:
    """ISO `YYYY-MM-DDTHH:MM:SS.sssZ` → UTC epoch sec。"""
    try:
        return datetime.fromisoformat(iso.rstrip("Z")).replace(
            tzinfo=timezone.utc
        ).timestamp()
    except (ValueError, TypeError):
        return None


def _parse_bash_log(bash_log_path: Path) -> list[dict]:
    """扫 bash.log → list[{ts, wall_s, cmd, blocked}]，按 metadata header + 下一行 `$ cmd` 配对。

    BLOCKED 行（`[ts] [cwd=...] [BLOCKED by guard]`）也算一个 block（wall_s=0, blocked=True）：
    guard 拒掉的 cmd 仍在 transcript 里占一条，不算 block 会导致后续 1:1 对齐错位。
    """
    if not bash_log_path.exists():
        return []
    try:
        text = bash_log_path.read_text(errors="replace")
    except OSError:
        return []
    blocks: list[dict] = []
    pending: Optional[dict] = None
    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            pending = {"ts": m.group("ts"), "wall_s": float(m.group("wall")),
                       "cmd": "", "blocked": False}
            continue
        mb = _HEADER_BLOCKED_RE.match(line)
        if mb:
            pending = {"ts": mb.group("ts"), "wall_s": 0.0, "cmd": "", "blocked": True}
            continue
        if line.startswith("$ ") and pending is not None:
            pending["cmd"] = line[2:]
            blocks.append(pending)
            pending = None
    return blocks


def _transcript_bash_cmds(transcript_path: Path) -> list[str]:
    """从 transcript.json 拿全部 bash tool_use 的 cmd 串（按发起顺序）。"""
    if not transcript_path.exists():
        return []
    try:
        raw = json.loads(transcript_path.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    cmds: list[str] = []
    for r in raw if isinstance(raw, list) else []:
        for b in r.get("content", []) or []:
            if b.get("type") == "tool_use" and b.get("name") == "bash":
                cmd = (b.get("input") or {}).get("cmd", "") or ""
                cmds.append(cmd)
    return cmds


def _parse_qe_time(s: str) -> Optional[float]:
    """`24.76s` / `2m 3.68s` / `1m41.74s` / `3h 5m 2.5s` → seconds (float)。"""
    m = _TIME_PARTS_RE.search(s.strip())
    if not m:
        return None
    h = int(m.group(1)) if m.group(1) else 0
    minutes = int(m.group(2)) if m.group(2) else 0
    seconds = float(m.group(3)) if m.group(3) else 0.0
    return h * 3600 + minutes * 60 + seconds


def _scan_qe_self_wall(workdir: Path) -> Tuple[int, float]:
    """递归扫 workdir 下 *.out 文件，提取 QE binary 末尾 summary 行的 WALL 时间求和。

    返 (matched_files_count, wall_total_sec)：
    - matched_files_count = 至少出现一条顶层 summary 的 .out 文件数（用于诊断 fallback）
    - wall_total_sec = 所有 summary WALL 时间求和

    若没有任何 .out 命中（agent 没用 QE / agent 中途 kill / 全是 query flag）→ (0, 0.0)，
    上层 fallback 到 bash-cmd-wall 旧算法。
    """
    if not workdir.exists():
        return 0, 0.0
    matched = 0
    total = 0.0
    try:
        out_files = list(workdir.rglob("*.out"))
    except OSError:
        return 0, 0.0
    for p in out_files:
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        # 一个 .out 通常只有 1 个顶层 summary（pw.x 退出时写）；保险起见 sum all matches，
        # 兼容 agent 用 `>>` append 多次写同一个 .out 的极端情况。
        file_total = 0.0
        for line in text.splitlines():
            m = _QE_WALL_RE.match(line)
            if not m:
                continue
            t = _parse_qe_time(m.group(1))
            if t is not None:
                file_total += t
        if file_total > 0:
            matched += 1
            total += file_total
    return matched, total


def parse_qe_stats(bash_log_path: Path) -> Tuple[int, float]:
    """旧接口（仅 bash.log）：保留给单测 + 不需要 transcript fallback 的场景。
    wall 仍走旧算法（bash cmd wall 求和）—— 这个 API 没 workdir 做 self-report 扫描。"""
    blocks = _parse_bash_log(bash_log_path)
    count = 0
    wall_total = 0.0
    for b in blocks:
        if b.get("blocked"):
            continue  # guard 拦截没真跑，不算 QE 调用
        if _is_qe_invocation(b["cmd"]):
            count += 1
            wall_total += b["wall_s"]
    return count, wall_total


def compute_qe_stats(
    run_dir: Path, *, finished_at: Optional[str] = None,
) -> Tuple[int, float]:
    """主接口：run_dir 是 outer dir，bash.log / transcript.json / *.out 都在 workdir/。

    call_count：bash.log 里识别出的 QE 调用数；若给 `finished_at` 启用 transcript fallback
    （末尾被 wall 超时 kill 没落 metadata 的那些）。

    wall_sec：取 max(self_wall, bash_wall)。
    - self_wall = 扫 workdir/**/*.out 的 PWSCF/BANDS/... summary WALL 之和（每个 .out 1 条 ≈
      最近一次该 binary 的计算时间；agent 用 `>` 重定向覆盖会丢失历次记录）
    - bash_wall = bash.log 里所有 QE cmd 的 [wall=...] 之和（含 mpirun/容器启动开销，
      但完整覆盖每次调用）+ transcript 估的 killed 尾部窗口
    取 max 让两端 case 都不漏（后台启动 → self_wall 大；agent 重跑 → bash_wall 大；
    普通单跑 → 两者接近，max 取略大那个 ≈ 真值上界）。
    """
    workdir = run_dir / "workdir"
    blocks = _parse_bash_log(workdir / "bash.log")

    # ---- 1) 先算 call_count（bash.log + 可选 transcript fallback） ----
    count = 0
    bash_wall = 0.0
    for b in blocks:
        if b.get("blocked"):
            continue
        if _is_qe_invocation(b["cmd"]):
            count += 1
            bash_wall += b["wall_s"]

    # transcript fallback：仅记 count，不污染 wall —— killed-window 估算只在
    # 后面"完全没 QE 信号"时才用作兜底（self_wall=0 且 bash_wall=0）。
    killed_window_estimate = 0.0
    qe_killed = 0
    if finished_at:
        transcript_cmds = _transcript_bash_cmds(workdir / "transcript.json")
        n_done = len(blocks)
        if len(transcript_cmds) > n_done:
            killed_cmds = transcript_cmds[n_done:]
            qe_killed = sum(1 for cmd in killed_cmds if _is_qe_invocation(cmd))
            if qe_killed:
                count += qe_killed
                last_done_end_epoch: Optional[float] = None
                if blocks:
                    ts_epoch = _parse_log_ts_utc(blocks[-1]["ts"])
                    if ts_epoch is not None:
                        last_done_end_epoch = ts_epoch + blocks[-1]["wall_s"]
                finished_epoch = _parse_iso_utc(finished_at)
                if last_done_end_epoch is not None and finished_epoch is not None:
                    killed_window = max(0.0, finished_epoch - last_done_end_epoch)
                    per_killed = killed_window / len(killed_cmds)
                    killed_window_estimate = per_killed * qe_killed

    # ---- 2) self-reported WALL（每个 .out 1 条 summary） ----
    _matched_files, self_wall = _scan_qe_self_wall(workdir)

    # ---- 3) 决策：max(self_wall, bash_wall) 修正"完成的 cmd"双面盲点，再加 killed 估算 ----
    # max() 选择：
    #   bash_wall > self_wall (agent 重跑覆盖 .out)  → bash_wall 准
    #   bash_wall < self_wall (后台启动 bash ≈ 0s) → self_wall 准
    #   两者接近 → 哪个都行，max 取上界
    # 单独把 killed_window_estimate 累加：它估的是"被 kill 的 cmd 实际跑了多久"，与
    # 完成 cmd 的 wall 互不重叠，所以加而不取 max。
    return count, max(self_wall, bash_wall) + killed_window_estimate


def _read_qe_trace(workdir: Path) -> Optional[dict]:
    """Path 0 (v2 solver): qe_tools.py emits one JSON line per QE binary call
    to <workdir>/qe_calls.jsonl. When present, this is source-of-truth — beats
    bash.log inference (can't see calls inside `python script.py`) and .out
    summary scans (lose calls when agent overwrites .out via re-run).

    Returns the same shape as compute_qe_stats_detailed, or None if file
    absent (v1 solver) or empty / fully corrupt (treat as no signal, fall
    back to old logic so we don't return spurious zeros).

    self_wall_sec is filled from .out summary scan as in v1 path so the
    frontend's self-vs-trace comparison still works (trace_wall ≥ self_wall
    typically by wrapper overhead seconds; large gap signals re-run).
    bash_wall_sec / killed_estimate_sec are 0.0 — not meaningful in trace
    mode (the trace IS the truth)."""
    f = workdir / "qe_calls.jsonl"
    if not f.exists():
        return None
    calls: list[dict] = []
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                calls.append(json.loads(line))
            except json.JSONDecodeError:
                # Corrupt line (e.g. partial write at SIGKILL). Skip; still
                # use whatever valid lines we got.
                continue
    except OSError:
        return None
    if not calls:
        return None
    trace_wall = sum(float(c.get("wall_sec", 0.0)) for c in calls)
    _matched_files, self_wall = _scan_qe_self_wall(workdir)
    return {
        "call_count": len(calls),
        "wall_sec": trace_wall,
        "self_wall_sec": self_wall,
        "bash_wall_sec": 0.0,
        "killed_estimate_sec": 0.0,
    }


def compute_qe_stats_detailed(
    run_dir: Path, *, finished_at: Optional[str] = None,
) -> dict:
    """同 compute_qe_stats，但返回所有原始信号供前端在两边明显不一致时显示补充信息：

    {
      "call_count": int,
      "wall_sec":            float,  # 主显（同 compute_qe_stats 第二项）
      "self_wall_sec":       float,  # .out 末尾 PWSCF/BANDS/... 自报 WALL 求和
      "bash_wall_sec":       float,  # bash.log 里 QE cmd [wall=...] 求和
      "killed_estimate_sec": float,  # transcript 估的 killed 尾部窗口
    }

    用于 monitor 落 SQLite + frontend 比较 self vs bash：差距 > 20% 时在主显后括号
    提示"(self-reported X / bash sum Y)"，让用户察觉 agent 重跑覆盖 .out 或后台启动等情况。

    Path 0 (v2 solver): 若 workdir/qe_calls.jsonl 存在则直接读 trace（qe_tools.py
    每次 run_pw/run_dos/run_bands/run_pp 都 append 一行）—— 比 bash.log 推断
    （看不到 python script 内部调用）和 .out summary 扫描（被 agent 重跑覆盖
    会少算）都准确。trace 文件不存在 / 空 → 走 v1 path A+B fallback。
    """
    workdir = run_dir / "workdir"
    trace_result = _read_qe_trace(workdir)
    if trace_result is not None:
        return trace_result

    blocks = _parse_bash_log(workdir / "bash.log")

    count = 0
    bash_wall = 0.0
    for b in blocks:
        if b.get("blocked"):
            continue
        if _is_qe_invocation(b["cmd"]):
            count += 1
            bash_wall += b["wall_s"]

    killed_window_estimate = 0.0
    if finished_at:
        transcript_cmds = _transcript_bash_cmds(workdir / "transcript.json")
        n_done = len(blocks)
        if len(transcript_cmds) > n_done:
            killed_cmds = transcript_cmds[n_done:]
            qe_killed = sum(1 for cmd in killed_cmds if _is_qe_invocation(cmd))
            if qe_killed:
                count += qe_killed
                last_done_end_epoch: Optional[float] = None
                if blocks:
                    ts_epoch = _parse_log_ts_utc(blocks[-1]["ts"])
                    if ts_epoch is not None:
                        last_done_end_epoch = ts_epoch + blocks[-1]["wall_s"]
                finished_epoch = _parse_iso_utc(finished_at)
                if last_done_end_epoch is not None and finished_epoch is not None:
                    killed_window = max(0.0, finished_epoch - last_done_end_epoch)
                    per_killed = killed_window / len(killed_cmds)
                    killed_window_estimate = per_killed * qe_killed

    _matched_files, self_wall = _scan_qe_self_wall(workdir)
    primary = max(self_wall, bash_wall) + killed_window_estimate
    return {
        "call_count": count,
        "wall_sec": primary,
        "self_wall_sec": self_wall,
        "bash_wall_sec": bash_wall,
        "killed_estimate_sec": killed_window_estimate,
    }
