"""Spawn solver subprocess with fully-configured env."""
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def run_solver(
    *,
    solver_path: Path,
    solver_args: list[str],
    workdir: Path,
    prompt_path: Path,
    out_path: Path,
    timeout_s: int,
    solver_stdout_path: Path,
    solver_stderr_path: Path,
    bench_root: Path,
) -> tuple[int, str, float]:
    """启动 solver subprocess。返回 (exit_code, status_str, wall_s)。

    status_str ∈ {"ok", "timeout", "error"}；
    wall_s = subprocess 实际墙钟耗时（秒），超时/异常时也已经计入实际等待时间，
    供 audit 计算 QE 真实耗时占 solver 总耗时的 ratio（防作弊）。"""
    env = _build_env(bench_root, timeout_s)

    # 让 solver subprocess 能 `from agent.xxx import ...`
    # （`python /abs/path/agent/solve.py` 时 sys.path[0] 是 agent/，找不到 agent 包本身）
    solver_repo_root = Path(solver_path).resolve().parent.parent
    existing_pypath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{solver_repo_root}:{existing_pypath}" if existing_pypath else str(solver_repo_root)

    cmd = [
        sys.executable, str(solver_path),
        "--prompt", str(prompt_path),
        "--workdir", str(workdir),
        "--out", str(out_path),
    ] + solver_args

    t0 = time.monotonic()
    with open(solver_stdout_path, "w") as so, open(solver_stderr_path, "w") as se:
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                cwd=str(workdir),
                timeout=timeout_s,
                stdout=so,
                stderr=se,
            )
            return proc.returncode, "ok", time.monotonic() - t0
        except subprocess.TimeoutExpired:
            return -1, "timeout", time.monotonic() - t0
        except Exception:
            return -2, "error", time.monotonic() - t0


def _build_env(bench_root: Path, timeout_s: int) -> dict[str, str]:
    """构造 solver 子进程的环境变量:
      - 保留当前 Python 环境和 PATH
      - 若设置 QE_GPU_WRAPPER_DIR，则将其加入 PATH
      - 设 PSEUDO_DIR / QE_GPU_NP / PYTHONNOUSERSITE
      - 设 SOLVER_HARNESS_WALL_S = subprocess timeout（秒），让 solver 端
        bash watchdog cap (agent/tools/bash.py:_BASH_CAP_S) 能跟着 harness
        --timeout 一起伸缩。不传时 solver 默认 14400s → bash cap 11520s，
        web UI 拖到 8h+ 也没用（2026-05-24 Ti3O5 失败 root cause #1）。
    """
    env = os.environ.copy()

    # QE 可执行文件可直接来自当前 PATH，也可由用户显式指定目录。
    qe_wrapper_env = env.get("QE_GPU_WRAPPER_DIR")
    if qe_wrapper_env:
        qe_wrapper = Path(qe_wrapper_env).expanduser()
        if not qe_wrapper.is_dir():
            raise FileNotFoundError(
                f"QE_GPU_WRAPPER_DIR is not a directory: {qe_wrapper}"
            )
        old_path = env.get("PATH", "")
        env["PATH"] = os.pathsep.join(filter(None, (str(qe_wrapper), old_path)))

    env["PYTHONNOUSERSITE"] = "1"
    env["QE_GPU_NP"] = env.get("QE_GPU_NP", "1")

    # Pseudopotential dir（agent 可通过 $PSEUDO_DIR 或 ./pseudos/ 访问）
    # 这里给绝对路径；workdir 里也有 pseudos/ symlink
    pp_dir = bench_root / "pseudos" / "GBRV_USPP_PBE_v1.5"
    env["PSEUDO_DIR"] = str(pp_dir.resolve())

    # 把 harness 实际 wall budget 透出给 solver，让 solver 的 bash watchdog
    # (agent/tools/bash.py:_HARNESS_WALL_S) 跟着 --timeout 一起伸缩。
    env["SOLVER_HARNESS_WALL_S"] = str(timeout_s)

    return env
