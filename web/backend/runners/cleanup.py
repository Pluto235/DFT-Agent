"""run_dir 中间文件清理 + 残留进程清扫。

`cleanup_run_dir`：终态后调，删 QE 重型 tmp 子目录，保留 input/output/log/json。
实测一例 Ag vc_relax 252M：tmp/ + tmp2/ + tmp3/ 占 99% 空间。

`kill_run_dir_processes`：cwd-sweep 抓 detach 出 harness pgroup 的孤儿进程
（conmon / container 内 mpirun / pw.x）。被 cancel.py 与 scheduler reaper 共用。

cleanup_run_dir 保留：

  workdir/
    transcript.json    agent 推理路径（小）
    bash.log           agent 命令历史（小）
    task_prompt.txt    初始 prompt（小）
    *.in / *.out       QE 输入 / 输出（每个 ~30KB）
    result.json        custom mode 答案（bench 没此文件）
    pseudos/           符号链接，rmtree 不触底层文件

  <run_dir>/
    report.json        evaluator score（bench 关键产物）
    {harness,solver}_{stdout,stderr}.log
    harness.log

调用方：harness_wrapper / solver_wrapper monitor task 在 mark_done/failed/cancelled
之后判 settings + config_kv，拿 effective cleanup flag 决定是否调 cleanup_run_dir。
"""
from __future__ import annotations

import logging
import os
import signal
import shutil
from pathlib import Path
from typing import Optional

log = logging.getLogger("cleanup")


def kill_run_dir_processes(run_dir: Optional[Path], run_id: str) -> int:
    """SIGKILL 任何 cwd 在 run_dir 之下的进程。补刀 conmon / container 内 mpirun / pw.x
    （这些 detach 出 harness 的 pgroup，killpg 抓不到）。返回杀掉的 PID 数。

    安全：run_dir 必须是绝对路径且非空；resolve() 去 symlink；只匹配 startswith。
    cwd 唯一性靠 run_id 时间戳前缀保证 — 不会误杀其他 run 的进程。
    """
    if run_dir is None:
        return 0
    try:
        target = str(Path(run_dir).resolve())
    except OSError:
        return 0
    if not target or target == "/" or len(target) < 10:
        # paranoid：阻止路径退化成 "/" 之类引发全局扫杀
        log.error("run %s sweep aborted: run_dir resolves to suspicious target '%s'", run_id, target)
        return 0
    killed = 0
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return 0
    for pid_str in pids:
        try:
            cwd = os.readlink(f"/proc/{pid_str}/cwd")
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if cwd == target or cwd.startswith(target + os.sep):
            try:
                os.kill(int(pid_str), signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError):
                pass
    if killed:
        log.info("run %s cwd-sweep killed %d residual process(es) under %s", run_id, killed, target)
    return killed


def cleanup_run_dir(run_dir: Path) -> None:
    """rmtree workdir 下所有 'tmp*' 子目录。run_dir 不存在 / 没 workdir → no-op。"""
    if not run_dir or not run_dir.exists():
        return
    workdir = run_dir / "workdir"
    if not workdir.exists() or not workdir.is_dir():
        return
    removed = 0
    freed = 0
    for child in workdir.iterdir():
        # symlink 不算 tmp（pseudos 是 symlink，但名字不是 tmp，安全双保险）
        if child.is_symlink():
            continue
        if not child.is_dir():
            continue
        if not child.name.startswith("tmp"):
            continue
        try:
            size_before = _dir_size(child)
            shutil.rmtree(child)
            removed += 1
            freed += size_before
        except OSError as e:
            log.warning("cleanup %s: rmtree %s failed: %s", run_dir.name, child.name, e)
    if removed:
        log.info(
            "cleanup %s: removed %d tmp dirs, freed %.1f MB",
            run_dir.name, removed, freed / 1_048_576,
        )


def _dir_size(path: Path) -> int:
    """递归累加文件 size（不跟 symlink）。"""
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file() and not p.is_symlink():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total
