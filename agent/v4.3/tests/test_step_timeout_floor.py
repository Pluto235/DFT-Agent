"""Tests for agent.step_timeout_floor.floor_for_command.

The floor is `max(LLM_choice, floor)` applied in loop.py:228-236. These
tests pin the floor itself; the integration into loop.py is exercised by
the existing test_orchestrator / test_executor suites once they import
step_kind through.

Concrete regression target: CoSb3 2026-05-19 dos_nscf timeout_s=120 → with
step_kind='dos' the floor is now 1800, so the LLM can no longer undershoot.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.step_timeout_floor import (  # noqa: E402
    STEP_KIND_FLOOR_S,
    floor_for_command,
)


# ---------- Priority 1: step_kind_hint ---------------------------------

def test_step_kind_hint_dos_returns_1800(tmp_path):
    assert floor_for_command("anything", tmp_path, step_kind_hint="dos") == 1800


def test_step_kind_hint_dos_nscf_alias(tmp_path):
    assert floor_for_command("anything", tmp_path, step_kind_hint="dos_nscf") == 1800


def test_step_kind_hint_scf_returns_300(tmp_path):
    assert floor_for_command("ls", tmp_path, step_kind_hint="scf") == 300


def test_step_kind_hint_vc_relax_returns_900(tmp_path):
    assert floor_for_command("ls", tmp_path, step_kind_hint="vc_relax") == 900


def test_step_kind_hint_with_dash_normalized(tmp_path):
    # planner 可能用 "vc-relax"，floor 表用下划线
    assert floor_for_command("ls", tmp_path, step_kind_hint="vc-relax") == 900


def test_step_kind_hint_unknown_falls_back_to_scf(tmp_path):
    # 未知 step kind 走 scf 级 floor，保守不给过低
    assert floor_for_command("ls", tmp_path, step_kind_hint="unknown_kind") == 300


def test_step_kind_hint_overrides_command_inference(tmp_path):
    # hint 比 command 推断优先
    (tmp_path / "vc.in").write_text("&CONTROL\n calculation='vc-relax' /\n")
    # hint=scf 但 input 是 vc-relax → 用 hint
    assert floor_for_command(
        "pw.x -in vc.in", tmp_path, step_kind_hint="scf",
    ) == 300


# ---------- Priority 2: infer from pw.x input file ---------------------

def test_pwx_vc_relax_input_detected(tmp_path):
    (tmp_path / "relax.in").write_text(
        "&CONTROL\n calculation = 'vc-relax'\n prefix = 'foo' /\n"
    )
    assert floor_for_command("pw.x -in relax.in", tmp_path) == 900


def test_pwx_scf_input_detected(tmp_path):
    (tmp_path / "scf.in").write_text(
        "&CONTROL\n calculation = 'scf' /\n"
    )
    assert floor_for_command("pw.x -in scf.in > scf.out", tmp_path) == 300


def test_pwx_nscf_with_crystal_b_kpoints_is_bands(tmp_path):
    (tmp_path / "bands.in").write_text(
        "&CONTROL\n calculation = 'nscf' /\n"
        "&SYSTEM\n ibrav = 0 /\n"
        "K_POINTS crystal_b\n"
        "3\n 0.0 0.0 0.0 30\n 0.5 0.0 0.0 30\n 0.5 0.5 0.0 0\n"
    )
    assert floor_for_command("pw.x -in bands.in", tmp_path) == 600


def test_pwx_nscf_with_tpiba_b_kpoints_is_bands(tmp_path):
    (tmp_path / "bands.in").write_text(
        "&CONTROL\n calculation='nscf' /\n"
        "K_POINTS tpiba_b\n"
        "2\n 0 0 0 30\n 0.5 0 0 0\n"
    )
    assert floor_for_command("pw.x -in bands.in", tmp_path) == 600


def test_pwx_nscf_with_automatic_kpoints_is_dos(tmp_path):
    (tmp_path / "dos.in").write_text(
        "&CONTROL\n calculation = 'nscf' /\n"
        "K_POINTS automatic\n 24 24 24 0 0 0\n"
    )
    assert floor_for_command("pw.x -in dos.in", tmp_path) == 1800


def test_pwx_relax_calc_treated_as_vc_relax(tmp_path):
    (tmp_path / "ions.in").write_text(
        "&CONTROL\n calculation = 'relax' /\n"
    )
    assert floor_for_command("pw.x -in ions.in", tmp_path) == 900


def test_pwx_input_missing_falls_back_to_scf_floor(tmp_path):
    # 路径写了但文件没创建
    assert floor_for_command("pw.x -in does_not_exist.in", tmp_path) == 300


def test_pwx_input_no_calculation_field_falls_back_to_scf(tmp_path):
    (tmp_path / "broken.in").write_text("&SYSTEM\n ibrav = 0 /\n")
    assert floor_for_command("pw.x -in broken.in", tmp_path) == 300


def test_pwx_with_flag_space_input(tmp_path):
    (tmp_path / "scf.in").write_text("&CONTROL\n calculation = 'scf' /\n")
    # 各种 flag 写法都得识别
    assert floor_for_command("pw.x  -in scf.in", tmp_path) == 300
    assert floor_for_command("pw.x -in scf.in 2>&1", tmp_path) == 300


# ---------- Priority 3: non-pw.x default 60s ---------------------------

def test_non_pwx_command_returns_60s(tmp_path):
    assert floor_for_command("ls -la", tmp_path) == 60
    assert floor_for_command("grep -c 'JOB DONE' scf.out", tmp_path) == 60
    assert floor_for_command(
        "python3 -c 'print(1)'", tmp_path,
    ) == 60


# ---------- Real CoSb3 regression --------------------------------------

def test_cosb3_dos_nscf_floor_is_1800(tmp_path):
    """Regression for CoSb3 2026-05-19T18-17-00Z failure.

    Agent set timeout_s=120 / 600 for dos_nscf. With ExecutorAgent passing
    step_kind='dos', the floor now is 1800 → max(120, 1800) = 1800."""
    # ExecutorAgent 调 run_loop 时传 step.kind='dos'（plan 里的 step.kind 之一）
    assert floor_for_command(
        "pw.x -in dos_nscf.in", tmp_path, step_kind_hint="dos",
    ) == 1800


def test_table_consistency():
    """Sanity: floor 表里所有值都 >= 60s（_DEFAULT_FLOOR_S）."""
    for kind, floor in STEP_KIND_FLOOR_S.items():
        assert floor >= 60, f"{kind} floor {floor} < default 60s"
