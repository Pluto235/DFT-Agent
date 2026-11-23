"""QE 实时进度探测：从 run_dir/workdir/ 下递归扫 `.out` 文件 → 提取 binary
名 / SCF iter 计数 / per-iter 时间 → 估算 ETA。

设计目标（user 0508 决策）：web-fresh 自包含——不依赖任何 solver-side trace
（v1 没有 qe_calls.jsonl，v2/strong 有也不强求）；唯一信号源是 QE 自身写到磁盘
的 `.out` 文件。这意味着 v1 / v2 / strong / 任何遵循 agent 协议的 solver 都生效。

API：`get_qe_progress(run_dir)` 返 dict（in_flight=False → 不渲染；in_flight=True
→ 前端拼"⏳ QE pw.x [scf] · iter 12/50 · elapsed 3:42 · ETA ~5:30"）。

路径不固定（agent 自由命名 .out 位置，本道 audit run 是 workdir/runs/scf/scf.out，
其他可能是 workdir/calcs/*.out），所以用 rglob 而不是固定子目录扫。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional


# 30s 内有写认为活跃；阈值与 transcript polling cadence + container 启动延迟权衡：
# - 容器启动慢（5-10s 可能 .out 还没写第一行）→ 阈值需大于此
# - 跑完 JOB DONE 后用户切到下一题 → 阈值不能太大（避免显"前一题进度"残留）
_FRESH_THRESHOLD_SEC = 30.0

# .out 文件 tail 读取上限：50KB 足够覆盖 100 行 SCF 输出，避免大文件全读
_TAIL_MAX_BYTES = 50_000

# 默认 electron_maxstep（若 .in 没显式声明）：QE 默认值是 100
_DEFAULT_ELECTRON_MAXSTEP = 100

# ETA warmup 跳过数：QE 第 1-2 次 SCF 迭代包含 init / 第一次密度构造，比稳态慢；
# 用稳态 mean 外推更准
_ETA_WARMUP_SKIP = 2


def get_qe_progress(run_dir: Path) -> dict:
    """探测 run 当前 QE 调用进度。

    Returns:
        dict with keys:
        - in_flight: bool — 是否有活跃 QE 调用（30s 内 .out 有写 + 没 JOB DONE）
        - reason: str — in_flight=False 时的原因码（no_out_files / stale / job_done）
        - binary: str — pw.x / dos.x / bands.x / pp.x / unknown
        - calc_name: str — .out basename 不带 ext，用于前端文案
        - out_path: str — 相对 workdir 的 .out 路径，便于 debug
        - iter_n / iter_max: int|None — SCF iter 进度（仅 pw.x 有）
        - elapsed_sec: float — .out 创建到现在的秒数（近似 = QE 启动到现在）
        - eta_sec: float|None — 剩余预估秒数（pw.x + ≥3 iter 才有）
        - last_activity_sec: float — .out 上次写到现在
        - phase: str — scf / vc_relax / dos / bands / pp / unknown
    """
    workdir = run_dir / "workdir"
    if not workdir.exists():
        return {"in_flight": False, "reason": "no_out_files"}

    out_files = list(workdir.rglob("*.out"))
    if not out_files:
        return {"in_flight": False, "reason": "no_out_files"}

    # mtime 最新即为"当前"那个 QE call；多并发场景未来可扩展为返列表
    latest = max(out_files, key=lambda p: p.stat().st_mtime)
    age_sec = time.time() - latest.stat().st_mtime

    if age_sec > _FRESH_THRESHOLD_SEC:
        return {"in_flight": False, "reason": "stale", "last_activity_sec": age_sec}

    head = _read_first_n_lines(latest, 50)
    tail = _read_last_n_lines(latest, 100)

    # JOB DONE 检测：看 tail 末 30 行（QE 写 JOB DONE 后还有几行尾部输出）
    if any("JOB DONE" in line for line in tail[-30:]):
        return {
            "in_flight": False,
            "reason": "job_done",
            "last_activity_sec": age_sec,
        }

    binary = identify_binary(head)

    iter_n: Optional[int] = None
    iter_max: Optional[int] = None
    eta_sec: Optional[float] = None
    phase = binary.replace(".x", "") if binary != "unknown" else "unknown"

    if binary == "pw.x":
        iter_n, per_iter = parse_iterations(tail)
        in_file = latest.with_suffix(".in")
        iter_max = parse_electron_maxstep(in_file)
        # vc-relax 跨 BFGS step 的 ETA 难精算（剩多少 BFGS step 不知道），MVP 不做；
        # SCF iter 部分仍正常显示。phase 都标 'scf'，前端不区分。
        phase = "scf"
        if iter_n and iter_max:
            remaining = max(iter_max - iter_n, 0)
            eta_sec = estimate_eta(per_iter, remaining)

    # ctime 在 Linux 是 inode change time，但文件创建后短期内 ≈ 创建时间；用作"QE 启动到
    # 现在"的近似（误差 < 1s 可接受，本来 ETA 也是估值）
    elapsed_sec = time.time() - latest.stat().st_ctime

    try:
        out_path_rel = str(latest.relative_to(workdir))
    except ValueError:
        out_path_rel = latest.name

    return {
        "in_flight": True,
        "binary": binary,
        "calc_name": latest.stem,
        "out_path": out_path_rel,
        "iter_n": iter_n,
        "iter_max": iter_max,
        "elapsed_sec": elapsed_sec,
        "eta_sec": eta_sec,
        "last_activity_sec": age_sec,
        "phase": phase,
    }


def identify_binary(head_lines: list[str]) -> str:
    """从 .out 前 50 行识别 QE binary。

    QE 自报格式：'Program PWSCF v.7.5 starts on ...' / 'Program DOS v.7.5 ...' 等。
    NVHPC SDK banner 在更前面（'== NVIDIA HPC SDK =='），不冲突。
    """
    text = "\n".join(head_lines)
    if "Program PWSCF" in text:
        return "pw.x"
    if "Program DOS" in text:
        return "dos.x"
    if "Program BANDS" in text:
        return "bands.x"
    if "Program POST" in text:
        return "pp.x"
    return "unknown"


# 'iteration #  1' / 'iteration # 12' 等（# 后空格数可变）
_ITER_RE = re.compile(r"iteration #\s*\d+")
# 'total cpu time spent up to now is        2.2 secs' → cumulative seconds
_CPUTIME_RE = re.compile(
    r"total cpu time spent up to now is\s+([\d.]+)\s+secs",
)


def parse_iterations(tail_lines: list[str]) -> tuple[int, list[float]]:
    """从 tail 数 'iteration #N' 行 + 提相邻 'total cpu time spent' 累计时间。

    Returns:
        (iter_count, per_iter_secs)
        per_iter_secs 是相邻累计时间的差（每次 iter 实际耗时）。
        cumulative 列表长度 N 时 per_iter 长度 N-1。
    """
    iter_count = sum(1 for line in tail_lines if _ITER_RE.search(line))
    cum_times: list[float] = []
    for line in tail_lines:
        m = _CPUTIME_RE.search(line)
        if m:
            cum_times.append(float(m.group(1)))
    per_iter = [
        cum_times[i] - cum_times[i - 1]
        for i in range(1, len(cum_times))
    ]
    return iter_count, per_iter


_ELECTRON_MAXSTEP_RE = re.compile(r"electron_maxstep\s*=\s*(\d+)")


def parse_electron_maxstep(in_file: Path) -> int:
    """从 .in 文件 grep `electron_maxstep = N`（&ELECTRONS namelist）；缺则返 100。"""
    if not in_file.exists():
        return _DEFAULT_ELECTRON_MAXSTEP
    try:
        text = in_file.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return _DEFAULT_ELECTRON_MAXSTEP
    m = _ELECTRON_MAXSTEP_RE.search(text)
    return int(m.group(1)) if m else _DEFAULT_ELECTRON_MAXSTEP


def estimate_eta(
    per_iter_secs: list[float], remaining_iters: int,
) -> Optional[float]:
    """ETA = mean(per-iter, 跳过前 2 warmup) × remaining_iters。

    < 3 个 per-iter 样本（对应 ≤ 3 cumulative time）信噪比不够，返 None；
    前端不显 ETA 字段。
    """
    if len(per_iter_secs) < 3:
        return None
    stable = per_iter_secs[_ETA_WARMUP_SKIP:]
    if not stable:
        return None
    mean = sum(stable) / len(stable)
    return mean * max(remaining_iters, 0)


def _read_first_n_lines(path: Path, n: int) -> list[str]:
    """读首 n 行；最多读 50KB（前 50 行 banner 不会超）避免病态文件。"""
    try:
        with path.open("rb") as f:
            chunk = f.read(_TAIL_MAX_BYTES)
    except OSError:
        return []
    return chunk.decode("utf-8", errors="ignore").splitlines()[:n]


def _read_last_n_lines(path: Path, n: int) -> list[str]:
    """反向读最后 _TAIL_MAX_BYTES 字节，split 行；避免大 .out 全文加载。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(-min(_TAIL_MAX_BYTES, size), 2)
            chunk = f.read()
    except OSError:
        return []
    return chunk.decode("utf-8", errors="ignore").splitlines()[-n:]
