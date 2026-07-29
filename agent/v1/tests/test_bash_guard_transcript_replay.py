"""Transcript replay：拿 dft-agent-bench-web-fresh runs_local/ 里历史 1100 条
LLM 输出的真实 bash 命令喂 guard，验证误报率（false reject）足够低。

跑过的合法 LLM 输出仍可能被拒（如 LLM 用了 `git status` 这种白名单外命令），
但目标是：99%+ pass，且失败列表里没有"应该让过的合法 LLM 行为"。

如果未来扩了白名单或调了 path 规则，这个 test 是最直接的回归验证。
"""
from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

import pytest

from agent.tools.bash_guard import check_command


_TRANSCRIPT_ROOT_STR = os.environ.get(
    "BENCH_WEB_RUNS_DIR",
    "./web/runs_local",
)
_TRANSCRIPT_ROOT = Path(_TRANSCRIPT_ROOT_STR)

pytestmark = pytest.mark.skipif(
    not _TRANSCRIPT_ROOT.exists(),
    reason=(
        f"transcript replay corpus not found at {_TRANSCRIPT_ROOT_STR}; "
        f"set BENCH_WEB_RUNS_DIR env to point at a fresh-style runs_local/"
    ),
)


def _collect_bash_commands() -> list[str]:
    """从所有 transcript.json 抽出 bash tool 的 cmd 字符串。"""
    cmds: list[str] = []
    if not _TRANSCRIPT_ROOT.exists():
        return cmds
    for tp in _TRANSCRIPT_ROOT.glob("*/workdir/transcript.json"):
        try:
            d = json.loads(tp.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(d, list):
            continue
        for msg in d:
            if not isinstance(msg, dict):
                continue
            for blk in msg.get("content", []) or []:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "tool_use" and blk.get("name") == "bash":
                    cmd = blk.get("input", {}).get("cmd", "") or ""
                    if cmd:
                        cmds.append(cmd)
    return cmds


@pytest.fixture(scope="module")
def historical_commands() -> list[str]:
    cmds = _collect_bash_commands()
    if not cmds:
        pytest.skip(
            "no transcripts found at "
            f"{_TRANSCRIPT_ROOT}; replay test requires populated runs_local/"
        )
    return cmds


def test_replay_pass_rate_99pct(historical_commands, tmp_path):
    """整体 pass 率 ≥ 99%。"""
    n = len(historical_commands)
    rejects = []
    for cmd in historical_commands:
        r = check_command(cmd, tmp_path)
        if r is not None:
            rejects.append((cmd, r))
    pass_rate = (n - len(rejects)) / n
    print(f"\nreplay total: {n}, pass: {n - len(rejects)}, reject: {len(rejects)}, "
          f"pass_rate: {pass_rate:.4%}")
    if rejects:
        print(f"\n=== top 20 reject reasons ===")
        reason_counter = Counter(r for _, r in rejects)
        for reason, count in reason_counter.most_common(20):
            print(f"  {count:4d}  {reason}")
        # 抽几个样本命令看
        print(f"\n=== sample rejected commands (10) ===")
        for cmd, reason in rejects[:10]:
            short = cmd if len(cmd) < 120 else cmd[:117] + "..."
            print(f"  REASON: {reason}")
            print(f"  CMD:    {short}")
    assert pass_rate >= 0.99, (
        f"replay pass rate {pass_rate:.2%} < 99%; see stdout for reject breakdown"
    )


def test_replay_no_critical_false_rejects(historical_commands, tmp_path):
    """更严约束：被拒的命令里不应该有"明显合法"的 LLM 行为。

    具体定义：被拒命令的"主程序"不应是 transcript 里高频出现的 QE / scientific 工具。
    白名单里的 mpirun / pw.x / python3 / numpy 类不应被拒。
    """
    HIGH_FREQ_LEGITIMATE = {
        "ls", "cat", "head", "tail", "grep", "echo", "cd", "sleep",
        "python3", "pw.x", "mpirun", "which", "mkdir", "ps", "sed", "awk",
    }
    rejects = []
    for cmd in historical_commands:
        r = check_command(cmd, tmp_path)
        if r is not None:
            # 取首字（粗略）
            first = cmd.lstrip().split()[0] if cmd.strip() else ""
            first = first.split("/")[-1]
            if first in HIGH_FREQ_LEGITIMATE:
                rejects.append((cmd, r))
    if rejects:
        print(f"\n=== {len(rejects)} commands with legitimate 'first program' rejected ===")
        for cmd, reason in rejects[:20]:
            short = cmd if len(cmd) < 120 else cmd[:117] + "..."
            print(f"  REASON: {reason}")
            print(f"  CMD:    {short}")
    # 阈值：高频合法工具的误拒率 < 5%（稍宽，因为可能有 path / python 内容触发）
    # 基数：1100 命令，5% ≈ 55；超过说明设计有问题
    assert len(rejects) < 60, (
        f"{len(rejects)} commands starting with legitimate programs were "
        f"rejected; see stdout for details"
    )
