"""subprocess stdout/stderr 异步逐行读 + 提取 phase markers。

harness markers (spike1 §3.1 实测，行首可能有 pymatgen DeprecationWarning，
不能 startswith；用 `in line` 子串匹配):
- "[harness] Phase 2 - workdir = <abs_path>"
- "[harness] Phase 3 - solver exit_code=N status=ok|timeout|error wall=Ns"
- "[harness] Phase 7 - report written: <path>"
- "[harness] FAILURE - <reason> (report: <path>)"
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


PHASE2_MARKER = "[harness] Phase 2 - workdir = "
PHASE3_MARKER = "[harness] Phase 3 - solver exit_code="
PHASE7_MARKER = "[harness] Phase 7 - report written: "
FAILURE_MARKER = "[harness] FAILURE - "

# 单个 log 文件最大字节数；超过后停写新内容（继续 drain pipe 避免子进程被 PIPE 阻塞），
# 写一行截断标记。防 LLM 死循环 echo / API retry 风暴打满磁盘。
MAX_LOG_BYTES = 100 * 1024 * 1024


_PHASE3_RE = re.compile(
    r"\[harness\] Phase 3 - solver exit_code=(?P<exit>-?\d+)\s+"
    r"status=(?P<status>\w+)\s+wall=(?P<wall>[\d.]+)s"
)


@dataclass
class HarnessProgress:
    """从 harness stdout 抓取的 phase 状态；wrapper 用其触发 evaluating / done 状态切换。"""
    workdir: Optional[Path] = None
    solver_exit_code: Optional[int] = None
    solver_status: Optional[str] = None
    solver_wall_s: Optional[float] = None
    report_path: Optional[Path] = None
    failure_reason: Optional[str] = None


async def wait_for_workdir_marker(
    proc: asyncio.subprocess.Process, *, timeout_s: float = 15.0,
) -> Optional[Path]:
    """从 proc.stdout 读直到匹配 Phase 2 marker；返回 workdir Path 或 None（超时 / EOF）。"""
    if proc.stdout is None:
        return None
    loop = asyncio.get_event_loop()
    end_time = loop.time() + timeout_s
    while True:
        remaining = end_time - loop.time()
        if remaining <= 0:
            return None
        try:
            line_bytes = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        except asyncio.TimeoutError:
            return None
        if not line_bytes:
            return None
        line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
        if PHASE2_MARKER in line:
            idx = line.index(PHASE2_MARKER) + len(PHASE2_MARKER)
            return Path(line[idx:].strip())


async def stream_harness_stdout(
    proc: asyncio.subprocess.Process, run_dir: Path, progress: HarnessProgress,
    *, max_bytes: int = MAX_LOG_BYTES,
) -> None:
    """Phase 2 之后继续读 stdout 抓 Phase 3/7 + FAILURE；写到 run_dir/harness.log。

    harness 自己已经把 solver subprocess 的 stdout/stderr 重定向到 solver_stdout.log /
    solver_stderr.log；harness 自己的 stdout（含 phase markers）单独存到 harness.log。

    超 max_bytes 后停写新内容但继续读 pipe（防子进程因 PIPE 满阻塞）；phase marker
    解析仍然进行——markers 极少（每 run 4-5 条），不会触发截断。
    """
    if proc.stdout is None:
        return
    log_path = run_dir / "harness.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    truncated = False
    with log_path.open("a", buffering=1) as fh:
        while True:
            line_bytes = await proc.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
            if not truncated:
                payload = line + "\n"
                if written + len(payload) > max_bytes:
                    fh.write(
                        f"\n[log truncated at {written / 1024 / 1024:.1f} MB; "
                        f"continuing output discarded]\n"
                    )
                    truncated = True
                else:
                    fh.write(payload)
                    written += len(payload)
            _parse_phase_markers(line, progress)


async def stream_subprocess_stream(
    stream: Optional[asyncio.StreamReader], log_path: Path,
    *, max_bytes: int = MAX_LOG_BYTES,
) -> None:
    """通用 stdout/stderr 流到 log；solver 自己的 stdout 一般是空（spike2 §2.4），
    但 stream 落盘是为了 cancel / debug。

    超 max_bytes 后继续 drain pipe 但不再写盘，避免 LLM 死循环 echo / API retry
    风暴撑爆磁盘。
    """
    if stream is None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    truncated = False
    with log_path.open("a", buffering=1) as fh:
        while True:
            line_bytes = await stream.readline()
            if not line_bytes:
                break
            if truncated:
                continue
            if written + len(line_bytes) > max_bytes:
                fh.write(
                    f"\n[log truncated at {written / 1024 / 1024:.1f} MB; "
                    f"continuing output discarded]\n"
                )
                truncated = True
                continue
            fh.write(line_bytes.decode("utf-8", errors="replace"))
            written += len(line_bytes)


def _parse_phase_markers(line: str, progress: HarnessProgress) -> None:
    if PHASE3_MARKER in line:
        m = _PHASE3_RE.search(line)
        if m:
            progress.solver_exit_code = int(m.group("exit"))
            progress.solver_status = m.group("status")
            progress.solver_wall_s = float(m.group("wall"))
        return
    if PHASE7_MARKER in line:
        idx = line.index(PHASE7_MARKER) + len(PHASE7_MARKER)
        progress.report_path = Path(line[idx:].strip())
        return
    if FAILURE_MARKER in line:
        idx = line.index(FAILURE_MARKER) + len(FAILURE_MARKER)
        # 截到 "(report: " 之前
        tail = line[idx:]
        progress.failure_reason = tail.split("(report:")[0].strip()
