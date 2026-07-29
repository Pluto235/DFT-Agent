"""Tests for FIX-7: pw.x-aware timeout diagnostic in BashTool.

When pw.x is wallclock-killed, the message back to the LLM must explicitly
state (a) this is not a pw.x/path/input failure, (b) retry with timeout_s × 3,
(c) do not change input or pw.x path. Non-pw.x commands keep the short
message — the long diagnostic would mislead.

Also tests FIX-6's estimator_note is attached to the timeout stderr when
the estimator had pre-warned (only fires when the estimator found a .in
file; here we deliberately use commands without .in to keep the test
isolated to the diagnostic-text path).
"""
from __future__ import annotations

import os

import pytest

from agent.tools.bash import BashTool


@pytest.fixture(autouse=True)
def _bash_guard_off(monkeypatch):
    # bash_guard rejects most things in tests; disable it so we can run
    # plain `sleep` and pw.x-token-bearing dummy commands.
    monkeypatch.setenv("SOLVER_BASH_GUARD", "off")


def test_pwx_timeout_emits_long_diagnostic(tmp_path):
    tool = BashTool(workdir=tmp_path, default_timeout_s=60)
    # `sleep 3` actually triggers the timeout; `pw.x --version` after `&&`
    # is never reached. The diag check looks at the command string only.
    cmd = "sleep 3 && pw.x --version"
    result = tool.run(cmd, timeout_s=1)
    assert result["exit_code"] == -1
    err = result["stderr"]
    assert "pw.x" in err
    assert "wallclock" in err or "wallclock 提前打断" in err
    # FIX-15: GNU timeout SIGTERM → SIGKILL after 30s grace
    assert "GNU timeout" in err
    # Retry advice: timeout_s × 3 should appear (1 × 3 = 3)
    assert "timeout_s = 3s" in err
    assert "不要修改 input" in err or "不要切换 pw.x 路径" in err


def test_nonpwx_timeout_emits_short_message(tmp_path):
    tool = BashTool(workdir=tmp_path, default_timeout_s=60)
    cmd = "sleep 3"
    result = tool.run(cmd, timeout_s=1)
    assert result["exit_code"] == -1
    err = result["stderr"]
    # Short message present (FIX-15: now via GNU timeout)
    assert "timeout after" in err
    assert "GNU timeout" in err
    # Long-form pw.x advice MUST NOT appear for non-pw.x commands
    assert "wallclock 提前打断" not in err
    assert "timeout_s = 3s" not in err
    assert "不要切换 pw.x" not in err


def test_pwx_timeout_includes_estimator_note_when_available(tmp_path, monkeypatch):
    """When the estimator pre-warned (real .in file), the diagnostic must
    splice that note into the timeout stderr so the LLM sees both the
    pre-flight warning and the post-mortem advice in one place.
    """
    # Write a minimal valid pw.x input — estimator returns ≥ 30s.
    (tmp_path / "wo3.in").write_text(
        "&CONTROL\n  calculation = 'vc-relax'\n/\n"
        "&SYSTEM\n  ibrav=0\n  nat=32\n  ntyp=2\n  ecutwfc=80\n/\n"
        "&ELECTRONS\n/\n&IONS\n/\n&CELL\n/\n"
        "ATOMIC_SPECIES\n  W 183 W.upf\n  O 16 O.upf\n"
        "ATOMIC_POSITIONS crystal\n  W 0 0 0\n"
        "CELL_PARAMETERS angstrom\n  7 0 0\n  0 7 0\n  0 0 7\n"
        "K_POINTS automatic\n  4 4 4 0 0 0\n"
    )
    tool = BashTool(workdir=tmp_path, default_timeout_s=60)
    # Command parses as pw.x (so estimator engages), then immediately falls
    # through to `sleep 3` to trigger the timeout deterministically. The
    # estimator will raise timeout from 1s → 22000s (way too high), so we
    # need to instead reach the timeout via a *non-raisable* path. Trick:
    # the estimator only raises when est > timeout — so we can pass
    # timeout_s=99999, which the estimator will *not* raise (22000 < 99999),
    # but `sleep` still finishes well before 99999. That's the wrong shape.
    #
    # Instead, the cleanest way to test the splice path is to monkey-patch
    # the estimator to return a small value (5s) so the raise applies and
    # the note is set; then sleep beyond the raised timeout.
    from agent import pw_wall_estimator

    def _fake_estimate(cmd, cwd):
        return 2, {
            "n_atoms": 32, "n_kpoints": 64, "ecutwfc": 80.0,
            "calculation": "vc-relax",
        }
    monkeypatch.setattr(pw_wall_estimator, "estimate_for_command", _fake_estimate)

    # `;` not `&&`: even if pw.x exits 127 (not in PATH), sleep still runs
    # so the timeout fires deterministically at the raised value (2s).
    cmd = "pw.x -in wo3.in > wo3.out 2>/dev/null; sleep 5"
    result = tool.run(cmd, timeout_s=1)  # estimator raises 1 → 2
    assert result["exit_code"] == -1
    err = result["stderr"]
    assert "pw_wall_estimator" in err
    # FIX-15: estimator note format includes the raise + features. The
    # post-FIX-15 splice puts the entire estimator_note in parens after the
    # diag (the explicit "已给出预警" suffix was dropped — the note already
    # speaks for itself: "[pw_wall_estimator] timeout_s raised 1s → 2s ...").
    assert "raised 1s → 2s" in err
    # The new timeout = 2s, so retry advice is 2 × 3 = 6s
    assert "timeout_s = 6s" in err


def test_pwx_timeout_at_cap_recommends_strategy_change(tmp_path, monkeypatch):
    """FIX-7 cap-edge: when pw.x already ran at the bash cap and still got
    SIGTERM, advising "retry with same timeout" is useless — the next call
    gets capped to the same value, burning another cap-length wall for the
    same outcome. The diagnostic must instead direct the LLM to shrink the
    problem (or declare incomplete).

    Repro: LLM passed timeout_s = _BASH_CAP_S (or estimator raised it that
    high), pw.x timed out → old message read
    `用 timeout_s = {min(cap*3, cap) = cap}s 重发完全相同的命令` → LLM
    obediently repeats with same timeout → next failure → cycle.
    """
    from agent.tools import bash as bash_mod
    # Shrink the cap so timeout_s=2 already equals cap (no room to grow).
    # FIX-39: the cap is now a call-time function, so patch the function.
    monkeypatch.setattr(bash_mod, "_bash_cap_s", lambda: 2)

    tool = bash_mod.BashTool(workdir=tmp_path, default_timeout_s=60)
    cmd = "sleep 5 && pw.x --version"
    result = tool.run(cmd, timeout_s=2)
    assert result["exit_code"] == -1
    err = result["stderr"]

    # MUST NOT issue the old retry-with-bigger-timeout instruction. The
    # ASCII prefix `建议下一步：用 timeout_s = ` is unique to that
    # instruction; the new cap-edge message may still mention 重发 in a
    # negation ("不要重发" / "重发...只会再烧一轮"), so we anchor on the
    # exact directive shape instead of any substring.
    assert "建议下一步：用 timeout_s =" not in err

    # MUST explicitly flag that the cap is exhausted.
    assert "cap" in err
    assert ("已达" in err) or ("已耗尽" in err) or ("无空间" in err)

    # MUST direct the LLM toward strategy change — at least one concrete knob.
    assert any(k in err for k in (
        "ecutwfc", "k_mesh", "k 网格", "mixing_beta", "incomplete",
    ))

    # Still labels this as a wallclock kill, not a pw.x/path/input failure.
    assert "pw.x" in err
    assert "wallclock" in err or "wallclock 提前打断" in err


def test_pwx_success_prepends_estimator_note_to_stdout(tmp_path, monkeypatch):
    """When the estimator raises timeout and the command later succeeds,
    the estimator_note should be at the top of stdout for the LLM to learn.
    """
    (tmp_path / "si.in").write_text(
        "&CONTROL\n  calculation = 'scf'\n/\n"
        "&SYSTEM\n  ibrav=0\n  nat=2\n  ntyp=1\n  ecutwfc=40\n/\n"
        "&ELECTRONS\n/\n"
        "ATOMIC_SPECIES\n  Si 28 Si.upf\n"
        "ATOMIC_POSITIONS crystal\n  Si 0 0 0\n  Si 0.25 0.25 0.25\n"
        "CELL_PARAMETERS angstrom\n  3 0 0\n  0 3 0\n  0 0 3\n"
        "K_POINTS automatic\n  4 4 4 0 0 0\n"
    )
    from agent import pw_wall_estimator

    def _fake_estimate(cmd, cwd):
        return 30, {
            "n_atoms": 2, "n_kpoints": 64, "ecutwfc": 40.0,
            "calculation": "scf",
        }
    monkeypatch.setattr(pw_wall_estimator, "estimate_for_command", _fake_estimate)

    tool = BashTool(workdir=tmp_path, default_timeout_s=60)
    # estimator raises 1 → 30; echo runs in well under 30s
    cmd = "echo done_quickly # pw.x -in si.in"
    result = tool.run(cmd, timeout_s=1)
    assert result["exit_code"] == 0
    # Note appears at the very top of stdout (first line)
    first_line = result["stdout"].splitlines()[0] if result["stdout"] else ""
    assert first_line.startswith("[pw_wall_estimator]")
    assert "raised 1s → 30s" in first_line
