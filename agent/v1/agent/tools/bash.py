"""bash tool — stateless subprocess with cwd tracking and log append."""
import re
import subprocess
import time
from pathlib import Path
from typing import Any, TextIO

from agent.tools.bash_guard import check_command


def run_bash(
    cmd: str,
    cwd: Path,
    timeout_s: int,
    log_file: TextIO,
) -> tuple[dict[str, Any], Path]:
    """执行 bash 命令。stateless subprocess；识别形如 `cd X` 的纯 cd 更新 cwd。

    Returns: (tool_result_dict, new_cwd)
    """
    # pre-exec guard：白名单 + path 限制 + python 内容扫描，违反直接 reject
    block_reason = check_command(cmd, cwd)
    if block_reason is not None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_file.write(
            f"[{timestamp}] [cwd={cwd}] [BLOCKED by guard]\n"
            f"$ {cmd}\n"
            f"--- blocked ---\n{block_reason}\n\n"
        )
        log_file.flush()
        return (
            {
                "stdout": "",
                "stderr": (
                    f"BLOCKED by solver bash guard: {block_reason}\n"
                    f"Allowed programs / path constraints / python content rules "
                    f"are enforced. If genuinely needed, rephrase the command to "
                    f"comply (e.g. write inside the workdir, use literal paths "
                    f"instead of $VAR), or set env SOLVER_BASH_GUARD=off."
                ),
                "exit_code": 126,        # 'command found but not executable' 语义最近
                "wall_s": 0.0,
                "cwd_after": str(cwd),
                "blocked": True,
            },
            cwd,
        )

    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(cwd),
            timeout=timeout_s,
            capture_output=True,
            text=True,
            errors="replace",  # 命令输出可能含非 UTF-8 字节（如误 cat 二进制）；
                               # 严格解码会抛 UnicodeDecodeError 拖垮整个 agent loop。
                               # 与下面 TimeoutExpired 分支的 errors="replace" 保持一致。
        )
        stdout = proc.stdout
        stderr = proc.stderr
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as e:
        stdout = (e.stdout.decode("utf-8", errors="replace")
                  if isinstance(e.stdout, bytes) else (e.stdout or ""))
        stderr_tail = (e.stderr.decode("utf-8", errors="replace")
                       if isinstance(e.stderr, bytes) else (e.stderr or ""))
        stderr = f"TIMEOUT after {timeout_s}s\n{stderr_tail}"
        exit_code = -1
    wall_s = time.perf_counter() - t0

    stdout_display = _truncate(stdout)
    stderr_display = _truncate(stderr)
    new_cwd = _maybe_update_cwd(cmd, cwd)

    # bash.log 格式
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_file.write(
        f"[{timestamp}] [cwd={cwd}] [timeout={timeout_s}s] "
        f"[wall={wall_s:.2f}s] [exit={exit_code}]\n"
    )
    log_file.write(f"$ {cmd}\n")
    if stdout:
        log_file.write(f"--- stdout ---\n{stdout_display}\n")
    if stderr:
        log_file.write(f"--- stderr ---\n{stderr_display}\n")
    log_file.write("\n")
    log_file.flush()

    return (
        {
            "stdout": stdout_display,
            "stderr": stderr_display,
            "exit_code": exit_code,
            "wall_s": round(wall_s, 2),
            "cwd_after": str(new_cwd),
        },
        new_cwd,
    )


def _truncate(s: str, head_lines: int = 100, tail_lines: int = 100) -> str:
    """把太长的输出截成 head + tail + 总量标记，防炸 LLM context。"""
    if not s:
        return s
    lines = s.splitlines()
    if len(lines) <= head_lines + tail_lines:
        return s
    omitted = len(lines) - head_lines - tail_lines
    return "\n".join(
        lines[:head_lines]
        + [f"... [{omitted} lines omitted, {len(s)} bytes total] ..."]
        + lines[-tail_lines:]
    )


_CD_RE = re.compile(r"^\s*cd\s+(\S+)\s*;?\s*$")


def _maybe_update_cwd(cmd: str, cwd: Path) -> Path:
    """识别纯 `cd X`（可带尾分号），其他形式不动 cwd。"""
    m = _CD_RE.fullmatch(cmd)
    if not m:
        return cwd
    target_raw = m.group(1).strip("'\"")
    target = Path(target_raw)
    new = target.resolve() if target.is_absolute() else (cwd / target).resolve()
    return new if new.is_dir() else cwd
