"""MVP 验收：Si T1 端到端 smoke test。

真调 Claude API（消耗 ANTHROPIC_API_KEY）+ 真跑 GPU QE（podman + NVHPC 容器），
耗时 5–15 分钟，单次 token 成本 ~$0.5–2。默认跳过；按需手动跑：

  pytest harness/tests/test_si_t1_smoke.py -v --run-slow

前置：
  - 已激活安装了 solver 依赖的 Python 环境
  - ANTHROPIC_API_KEY env 已 export
  - QE 可执行文件已在 PATH 中，或设置 `QE_GPU_WRAPPER_DIR`
  - solver repo clone 到 `~/workspace/repos/dft-agent-bench-solver/`
"""
import json
import os
import subprocess
from pathlib import Path

import pytest


BENCH_ROOT = Path(__file__).parent.parent.parent
SOLVER_PATH = Path.home() / "workspace" / "repos" / "dft-agent-bench-solver" / "agent" / "solve.py"


@pytest.mark.slow
def test_si_t1_end_to_end(tmp_path):
    assert os.environ.get("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY env not set"
    assert SOLVER_PATH.exists(), f"solver not found: {SOLVER_PATH}"

    runs_dir = tmp_path / "runs"
    cmd = [
        "python",
        str(BENCH_ROOT / "harness" / "run.py"),
        "--task-id", "M1_T1_Si_vc_relax",
        "--solver", str(SOLVER_PATH),
        "--solver-args", "--model claude-sonnet-4-6 --max-turns 50",
        "--runs-dir", str(runs_dir),
        "--timeout", "1800",
    ]
    proc = subprocess.run(cmd, cwd=BENCH_ROOT, capture_output=True, text=True)
    print("STDOUT:", proc.stdout)
    print("STDERR:", proc.stderr)
    assert proc.returncode == 0, f"harness failed: {proc.stderr}"

    run_dirs = list(runs_dir.glob("*_M1_T1_Si_vc_relax"))
    assert len(run_dirs) == 1
    report = json.loads((run_dirs[0] / "report.json").read_text())

    assert report["solver_exit_code"] == 0
    assert report["total_score"] >= 0.85, f"score too low: {report['total_score']}"
    for flag, val in report["audit_flags"].items():
        assert val is True, f"audit flag {flag} is {val}"

    ef = report["extracted_fields"]
    # Si primitive rhombohedral a ≈ 3.849 Å；放宽到 2% 给 MVP buffer
    assert abs(ef["a"] - 3.849) / 3.849 <= 0.02
    assert abs(ef["b"] - 3.849) / 3.849 <= 0.02
    assert abs(ef["c"] - 3.849) / 3.849 <= 0.02
    assert ef["space_group_number"] == 227
