"""FIX-42 (S-1, FIX-27 hardening): block `rm -rf <outdir>` when the target
holds QE restart state (a `*.save` dir with the converged density / wavefns).

Wiping it forces a vc-relax restart from the initial guess instead of a warm
restart — the CuO M3_T1 2026-06-01 run did `rm -rf vc_relax_1/out` 4× and
never converged within the 8h wall (FIX-27 anti-pattern, previously only
*discouraged* in the prompt). The bash tool now refuses it and points the LLM
to warm-restart (startingwfc='file') or `mv` the dir aside.
"""
from __future__ import annotations
from pathlib import Path

from agent.tools.bash import BashTool, _rm_rf_targets_qe_restart


def _make_qe_outdir(workdir: Path, rel: str = "vc_relax_1/out") -> Path:
    save = workdir / rel / "pwscf.save"
    save.mkdir(parents=True)
    (save / "charge-density.dat").write_text("density")
    (save / "wfc1.dat").write_text("wfc")
    return workdir / rel


# ---- pure predicate -------------------------------------------------------

def test_predicate_flags_rm_rf_of_restart_dir(tmp_path):
    _make_qe_outdir(tmp_path)
    hit = _rm_rf_targets_qe_restart("rm -rf vc_relax_1/out && mkdir -p vc_relax_1/out", tmp_path)
    assert hit is not None and "vc_relax_1/out" in hit


def test_predicate_flags_rm_rf_glob_of_restart_dir(tmp_path):
    _make_qe_outdir(tmp_path)
    assert _rm_rf_targets_qe_restart("rm -rf vc_relax_1/out/*", tmp_path) is not None


def test_predicate_ignores_dir_without_save(tmp_path):
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "notes.txt").write_text("x")
    assert _rm_rf_targets_qe_restart("rm -rf scratch", tmp_path) is None


def test_predicate_ignores_non_recursive_or_non_rm(tmp_path):
    _make_qe_outdir(tmp_path)
    assert _rm_rf_targets_qe_restart("rm vc_relax_1/out/wfc1.dat", tmp_path) is None  # no -r
    assert _rm_rf_targets_qe_restart("ls -rf vc_relax_1/out", tmp_path) is None       # not rm


# ---- BashTool integration -------------------------------------------------

def test_bash_blocks_rm_rf_restart_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLVER_BASH_GUARD", "off")
    _make_qe_outdir(tmp_path)
    tool = BashTool(workdir=tmp_path, default_timeout_s=10)
    res = tool.run("rm -rf vc_relax_1/out && mkdir -p vc_relax_1/out")
    assert res["exit_code"] == 2
    assert res.get("blocked") is True
    low = res["stderr"].lower()
    assert "startingwfc" in low or "warm-restart" in low or "warm restart" in low
    # the dir must NOT have been wiped
    assert (tmp_path / "vc_relax_1/out/pwscf.save/charge-density.dat").exists()


def test_bash_allows_rm_rf_plain_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLVER_BASH_GUARD", "off")
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "tmp.txt").write_text("x")
    tool = BashTool(workdir=tmp_path, default_timeout_s=10)
    res = tool.run("rm -rf scratch")
    assert res["exit_code"] == 0
    assert not (tmp_path / "scratch").exists()
