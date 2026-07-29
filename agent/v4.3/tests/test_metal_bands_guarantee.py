"""Unit tests for agent.subagents.metal_bands_guarantee (FIX-2).

The deterministic pw.x/bands.x runner is monkeypatched (mirrors
test_magnetic_probe.py) so no real QE runs. Fermi is fixed at 17.900 eV.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.subagents import metal_bands_guarantee as mbg  # noqa: E402

# Submitted (sparse) bands.dat: a 0.178 eV void straddling E_F=17.900
# (nearest below 17.7950, nearest above 17.9730) — the Ni_fcc failure shape.
_SPARSE_BANDS = """ &plot nbnd=  2, nks=   2 /
           0.000000  0.000000  0.000000
   17.7950   20.0000
           0.500000  0.000000  0.500000
   17.9730   19.5000
"""

# Densified bands.dat the (fake) re-run produces: an eigenvalue 17.9030 just
# below and 17.9080 just above E_F → void 0.005 eV < 10 meV.
_DENSE_BANDS = """ &plot nbnd=  2, nks=   2 /
           0.000000  0.000000  0.000000
   17.9030   20.0000
           0.500000  0.000000  0.500000
   17.9080   19.5000
"""

_BANDS_NSCF_IN = """&CONTROL
  calculation = 'bands',
  prefix = 'ni', outdir = './out',
/
&SYSTEM
  ibrav = 0, nat = 1, ntyp = 1, ecutwfc = 80, ecutrho = 800, nspin = 2,
  occupations = 'smearing', smearing = 'gauss', degauss = 0.01,
/
&ELECTRONS
/
K_POINTS crystal_b
3
  0.000000  0.000000  0.000000  30
  0.500000  0.000000  0.500000  30
  1.000000  0.000000  0.000000  1
"""


def _setup(tmp_path, bands_dat_text=_SPARSE_BANDS, fermi_line=True):
    """Create a workdir with bands_1/{bands.dat,bands_nscf.out,bands_nscf.in}.
    Returns (workdir, task_outputs)."""
    wd = tmp_path / "workdir"
    (wd / "bands_1").mkdir(parents=True)
    (wd / "scf_1").mkdir(parents=True)
    (wd / "bands_1" / "bands.dat").write_text(bands_dat_text)
    out = "the Fermi energy is 17.9000 ev\n" if fermi_line else "JOB DONE.\n"
    (wd / "bands_1" / "bands_nscf.out").write_text(out + "JOB DONE.\n")
    (wd / "bands_1" / "bands_nscf.in").write_text(_BANDS_NSCF_IN)
    (wd / "scf_1" / "scf.out").write_text(out)
    task_outputs = {
        "scf_out": "scf_1/scf.out",
        "bands_out": "bands_1/bands_nscf.out",
        "bands_data": "bands_1/bands.dat",
        "dos_dat": "dos_1/dos.dat",
    }
    return wd, task_outputs


def _fake_run_factory(dense_text=_DENSE_BANDS, calls=None):
    """Fake QE runner: NSCF re-run is a stub; bands.x writes `dense_text` to the
    filband named in its &BANDS input."""
    def fake_run(input_text, out_path, *, exe_path=None, **kw):
        if calls is not None:
            calls.append(str(out_path))
        if "pp" in str(out_path):  # bands.x post-process
            m = re.search(r"filband\s*=\s*'([^']+)'", input_text)
            (Path(out_path).parent / m.group(1)).write_text(dense_text)
        else:  # NSCF re-run stub
            Path(out_path).write_text("the Fermi energy is 17.9000 ev\nJOB DONE.\n")
        return 0
    return fake_run


def test_metal_void_triggers_densify_and_replaces(tmp_path, monkeypatch):
    wd, task_outputs = _setup(tmp_path)
    monkeypatch.setattr(mbg, "_run_pw_x", _fake_run_factory())
    res = mbg.run(workdir=wd, task_outputs=task_outputs, pw_x_path="/opt/qe/bin/pw.x",
                  qe_backend="cpu", is_metal=True)
    assert res.densified is True
    assert res.replaced_bands_data is True
    assert res.void_before == pytest.approx(0.178, abs=1e-3)
    assert res.void_after is not None and res.void_after < mbg.BENCH_METAL_GAP_EV
    assert res.new_subdiv and res.new_subdiv > 30
    # the submitted bands.dat on disk now carries the near-E_F eigenvalue
    assert "17.9080" in (wd / "bands_1" / "bands.dat").read_text()
    assert (wd / ".metal_bands_guarantee.json").exists()


def test_insulator_never_triggers(tmp_path, monkeypatch):
    wd, task_outputs = _setup(tmp_path)
    before = (wd / "bands_1" / "bands.dat").read_text()
    calls: list[str] = []
    monkeypatch.setattr(mbg, "_run_pw_x", _fake_run_factory(calls=calls))
    res = mbg.run(workdir=wd, task_outputs=task_outputs, pw_x_path="pw.x",
                  qe_backend="cpu", is_metal=False)
    assert res.replaced_bands_data is False
    assert res.failure_reason == "not metal"
    assert calls == []  # never ran QE
    assert (wd / "bands_1" / "bands.dat").read_text() == before


def test_void_below_threshold_is_noop(tmp_path, monkeypatch):
    # Already metal-resolvable: nearest eigs straddle E_F within 5 meV.
    wd, task_outputs = _setup(tmp_path, bands_dat_text=_DENSE_BANDS)
    before = (wd / "bands_1" / "bands.dat").read_text()
    calls: list[str] = []
    monkeypatch.setattr(mbg, "_run_pw_x", _fake_run_factory(calls=calls))
    res = mbg.run(workdir=wd, task_outputs=task_outputs, pw_x_path="pw.x",
                  qe_backend="cpu", is_metal=True)
    assert res.replaced_bands_data is False
    assert res.void_before is not None and res.void_before < mbg.BENCH_METAL_GAP_EV
    assert calls == []  # no re-run needed
    assert (wd / "bands_1" / "bands.dat").read_text() == before


def test_densify_caps_and_gives_up_gracefully(tmp_path, monkeypatch):
    # bands.x keeps returning a still-sparse path → after max_iters, give up and
    # leave the original bands.dat intact (never corrupt on failure).
    wd, task_outputs = _setup(tmp_path)
    before = (wd / "bands_1" / "bands.dat").read_text()
    monkeypatch.setattr(mbg, "_run_pw_x", _fake_run_factory(dense_text=_SPARSE_BANDS))
    res = mbg.run(workdir=wd, task_outputs=task_outputs, pw_x_path="pw.x",
                  qe_backend="cpu", is_metal=True, max_iters=2)
    assert res.replaced_bands_data is False
    assert res.densified is False
    assert (wd / "bands_1" / "bands.dat").read_text() == before


def test_fermi_unparseable_is_noop(tmp_path, monkeypatch):
    wd, task_outputs = _setup(tmp_path, fermi_line=False)
    # also blank the scf fallback Fermi
    (wd / "scf_1" / "scf.out").write_text("JOB DONE.\n")
    before = (wd / "bands_1" / "bands.dat").read_text()
    calls: list[str] = []
    monkeypatch.setattr(mbg, "_run_pw_x", _fake_run_factory(calls=calls))
    res = mbg.run(workdir=wd, task_outputs=task_outputs, pw_x_path="pw.x",
                  qe_backend="cpu", is_metal=True)
    assert res.replaced_bands_data is False
    assert res.failure_reason == "fermi unparseable"
    assert calls == []
    assert (wd / "bands_1" / "bands.dat").read_text() == before


def test_run_pw_x_timeout_is_noop(tmp_path, monkeypatch):
    wd, task_outputs = _setup(tmp_path)
    before = (wd / "bands_1" / "bands.dat").read_text()

    def timeout_run(input_text, out_path, **kw):
        raise subprocess.TimeoutExpired(cmd="pw.x", timeout=kw.get("timeout_s", 1))

    monkeypatch.setattr(mbg, "_run_pw_x", timeout_run)
    res = mbg.run(workdir=wd, task_outputs=task_outputs, pw_x_path="pw.x",
                  qe_backend="cpu", is_metal=True)
    assert res.replaced_bands_data is False
    assert (wd / "bands_1" / "bands.dat").read_text() == before  # untouched
