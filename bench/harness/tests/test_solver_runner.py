"""solver_runner.run_solver 的单测：主要验证 wall_s 跟踪行为。

不调用真实 LLM / QE，而是构造一个 sleep + 写 result.json 的假 solver 脚本。
"""
import json
from pathlib import Path

from harness.lib.solver_runner import run_solver


_BENCH_ROOT = Path(__file__).resolve().parent.parent.parent  # repos/dft-agent-bench/


def _make_fake_solver(tmp_path: Path, sleep_seconds: float) -> Path:
    """构造最简 solver 脚本：argparse → sleep → 写 minimal result.json → 退出。"""
    script = tmp_path / "fake_solver.py"
    script.write_text(
        "import argparse, json, time\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--prompt')\n"
        "p.add_argument('--workdir')\n"
        "p.add_argument('--out')\n"
        "args, _ = p.parse_known_args()\n"
        f"time.sleep({sleep_seconds})\n"
        "with open(args.out, 'w') as f:\n"
        "    json.dump({'task_outputs': {}}, f)\n"
    )
    return script


def _run(tmp_path: Path, fake_solver: Path, timeout_s: int = 10):
    workdir = tmp_path / "wd"
    workdir.mkdir()
    prompt = workdir / "task_prompt.txt"
    prompt.write_text("test")
    out = workdir / "result.json"
    return run_solver(
        solver_path=fake_solver,
        solver_args=[],
        workdir=workdir,
        prompt_path=prompt,
        out_path=out,
        timeout_s=timeout_s,
        solver_stdout_path=tmp_path / "stdout.log",
        solver_stderr_path=tmp_path / "stderr.log",
        bench_root=_BENCH_ROOT,
    )


def test_run_solver_returns_wall_s_for_normal_exit(tmp_path):
    """正常退出：返回 (exit_code, status, wall_s)；wall_s ≥ sleep 时长。"""
    fake_solver = _make_fake_solver(tmp_path, sleep_seconds=0.5)
    exit_code, status, wall_s = _run(tmp_path, fake_solver)
    assert status == "ok"
    assert exit_code == 0
    assert wall_s >= 0.5
    assert wall_s < 5.0  # sanity: subprocess 本身启动应远低于 sleep + 4.5s


def test_run_solver_returns_wall_s_on_timeout(tmp_path):
    """超时也要返回 wall_s（≈ timeout_s 而不是 sleep 全长）。"""
    fake_solver = _make_fake_solver(tmp_path, sleep_seconds=10.0)
    exit_code, status, wall_s = _run(tmp_path, fake_solver, timeout_s=1)
    assert status == "timeout"
    # timeout 后 wall_s 应大致等于 timeout_s（允许 ±0.5s 浮动）
    assert 0.5 <= wall_s < 3.0


def _make_env_probe_solver(tmp_path: Path) -> Path:
    """构造 solver 脚本：读 SOLVER_HARNESS_WALL_S env 写到 result.json。
    用于验证 harness 向 solver 子进程透传该 env。"""
    script = tmp_path / "env_probe_solver.py"
    script.write_text(
        "import argparse, json, os\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--prompt')\n"
        "p.add_argument('--workdir')\n"
        "p.add_argument('--out')\n"
        "args, _ = p.parse_known_args()\n"
        "with open(args.out, 'w') as f:\n"
        "    json.dump({\n"
        "        'task_outputs': {},\n"
        "        'solver_harness_wall_s': os.environ.get('SOLVER_HARNESS_WALL_S'),\n"
        "    }, f)\n"
    )
    return script


def test_run_solver_propagates_timeout_to_solver_env(tmp_path):
    """FIX-21 (2026-05-25): harness 必须把 --timeout 通过 SOLVER_HARNESS_WALL_S
    env 透传给 solver 子进程；否则 solver agent/tools/bash.py 的 _BASH_CAP_S
    永远停留在 14400×0.8=11520s，web UI 拖到 8h+ 失效。"""
    fake_solver = _make_env_probe_solver(tmp_path)
    exit_code, status, wall_s = _run(tmp_path, fake_solver, timeout_s=28800)
    assert status == "ok"
    assert exit_code == 0

    out_path = tmp_path / "wd" / "result.json"
    payload = json.loads(out_path.read_text())
    assert payload["solver_harness_wall_s"] == "28800", (
        f"expected SOLVER_HARNESS_WALL_S='28800' inside solver subprocess; "
        f"got {payload['solver_harness_wall_s']!r}"
    )


def test_run_solver_env_propagation_uses_actual_timeout(tmp_path):
    """不同的 timeout_s 必须各自透传，不能 hard-coded 一个值。"""
    fake_solver = _make_env_probe_solver(tmp_path)
    for ts in (3600, 14400, 43200):
        # 每次单独 wd / out（_run 内部会 mkdir wd，重复需要不同 tmp_path）
        sub = tmp_path / f"t{ts}"
        sub.mkdir()
        workdir = sub / "wd"
        workdir.mkdir()
        prompt = workdir / "task_prompt.txt"
        prompt.write_text("test")
        out = workdir / "result.json"
        run_solver(
            solver_path=fake_solver,
            solver_args=[],
            workdir=workdir,
            prompt_path=prompt,
            out_path=out,
            timeout_s=ts,
            solver_stdout_path=sub / "stdout.log",
            solver_stderr_path=sub / "stderr.log",
            bench_root=_BENCH_ROOT,
        )
        payload = json.loads(out.read_text())
        assert payload["solver_harness_wall_s"] == str(ts)
