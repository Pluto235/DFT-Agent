"""Sample GPU telemetry while a target subprocess runs.

Spawned by run_subset42_batch.py per task. Loops nvidia-smi, then on shutdown
computes p95/peak and a bottleneck classification matching baseline-2026-05-10.html
§1.1's "GPU 闲置 / GPU 算力" labels.

Usage:
    python gpu_sampler.py --pid <pid> --out gpu_metrics.json [--gpu-index 0] [--interval 2]

The sampler exits when the target pid exits OR when SIGTERM/SIGINT is received.
"""
from __future__ import annotations
import argparse
import json
import os
import signal
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

NVIDIA_SMI_QUERY = "utilization.gpu,memory.used,power.draw,temperature.gpu"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def sample_once(gpu_index: int) -> tuple[float, float, float, float] | None:
    """Returns (util%, mem_mib, power_w, temp_c) or None on parse failure."""
    try:
        r = subprocess.run(
            ["nvidia-smi", f"--id={gpu_index}",
             f"--query-gpu={NVIDIA_SMI_QUERY}",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        line = r.stdout.strip().splitlines()[0]
        u, m, p, t = [x.strip() for x in line.split(",")]
        return float(u), float(m), float(p), float(t)
    except Exception:
        return None


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def classify_bottleneck(util_p95: float, mem_p95_frac: float, temp_p95: float, mem_total_mib: float) -> str:
    """Match baseline-2026-05-10.html §1.1 method: normalize 4 dims, max wins.

    - GPU compute: util / 100
    - mem capacity: used / total
    - temp: temp / 90 (H100 throttle threshold)
    Memory bandwidth would need a 4th source (DCGM); we approximate with util only.
    If max < 0.30 (lazy threshold) → "GPU 闲置".
    Else if compute is the max → "GPU 算力".
    Else if mem dominates → "GPU 显存".
    Else if temp dominates → "GPU 温度".
    """
    compute = util_p95 / 100.0
    mem = mem_p95_frac
    temp = temp_p95 / 90.0
    items = [("compute", compute), ("mem", mem), ("temp", temp)]
    name, val = max(items, key=lambda kv: kv[1])
    if val < 0.30:
        return "GPU 闲置"
    return {
        "compute": "GPU 算力",
        "mem":     "GPU 显存",
        "temp":    "GPU 温度",
    }[name]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True, help="Target pid; sampler exits when it does")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--gpu-index", type=int, default=0, help="Which GPU to sample (0..3 for H100x4)")
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args()

    # Discover total mem capacity (used in mem-fraction bottleneck calc)
    try:
        r = subprocess.run(
            ["nvidia-smi", f"--id={args.gpu_index}",
             "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        mem_total_mib = float(r.stdout.strip().splitlines()[0])
    except Exception:
        mem_total_mib = 81559.0  # H100 80GB fallback

    utils: list[float] = []
    mems:  list[float] = []
    pows:  list[float] = []
    temps: list[float] = []

    stop_flag = {"stop": False}
    def _handler(signum, frame):
        stop_flag["stop"] = True
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)

    t0 = time.time()
    while not stop_flag["stop"] and pid_alive(args.pid):
        s = sample_once(args.gpu_index)
        if s is not None:
            utils.append(s[0]); mems.append(s[1]); pows.append(s[2]); temps.append(s[3])
        time.sleep(args.interval)
    t1 = time.time()

    util_p95 = percentile(utils, 95)
    mem_p95  = percentile(mems, 95)
    pow_p95  = percentile(pows, 95)
    temp_p95 = percentile(temps, 95)

    bot = classify_bottleneck(util_p95, mem_p95 / mem_total_mib if mem_total_mib else 0.0,
                              temp_p95, mem_total_mib)

    out = {
        "started_at": datetime.fromtimestamp(t0, tz=timezone.utc).isoformat(),
        "ended_at":   datetime.fromtimestamp(t1, tz=timezone.utc).isoformat(),
        "duration_sec": round(t1 - t0, 2),
        "samples": len(utils),
        "interval_sec": args.interval,
        "gpu_index": args.gpu_index,
        "mem_total_mib": mem_total_mib,
        "gpu_util_p95":  round(util_p95, 1),
        "gpu_util_max":  round(max(utils, default=0.0), 1),
        "gpu_util_mean": round(statistics.mean(utils), 1) if utils else 0.0,
        "mem_mib_p95":  round(mem_p95, 1),
        "mem_mib_max":  round(max(mems, default=0.0), 1),
        "power_w_p95":  round(pow_p95, 1),
        "power_w_max":  round(max(pows, default=0.0), 1),
        "temp_c_p95":   round(temp_p95, 1),
        "temp_c_max":   round(max(temps, default=0.0), 1),
        "bottleneck":   bot,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"[gpu_sampler] wrote {args.out} (samples={len(utils)}, p95_util={util_p95:.1f}%, bottleneck={bot})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
