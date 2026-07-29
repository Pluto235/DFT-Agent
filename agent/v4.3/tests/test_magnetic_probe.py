from pathlib import Path
from agent.subagents import magnetic_probe as mp
from agent.subagents._magnetism import MagneticProfile

_PROMPT = """For NiO (rocksalt, space group Fm-3m), relax the cell.

Initial structure (primitive cell, lattice constants in angstrom, angles in degree):
  a = 4.17, b = 4.17, c = 4.17
  alpha = 90.0, beta = 90.0, gamma = 90.0

ATOMIC_POSITIONS (crystal, i.e. fractional coordinates of the primitive cell):
  Ni  0.000000  0.000000  0.000000
  Ni  0.500000  0.500000  0.000000
  O   0.500000  0.000000  0.000000
  O   0.000000  0.500000  0.000000

Pseudopotentials: GBRV_USPP_PBE_v1.5 (scalar-relativistic).
UPF files available in the symlinked ./pseudos/ directory:
  Ni: ni.UPF
  O: o.UPF
"""

def test_probe_picks_fm_when_fm_energy_lower(tmp_path, monkeypatch):
    # Inject a fake pw.x runner: FM lower than AFM -> FM wins (NiO case)
    energies = {"FM": -1502.0179532, "AFM": -1502.01004008}
    def fake_run(input_text, out_path, **kw):
        ordering = "AFM" if "AFM" in str(out_path) or "afm" in str(out_path) else "FM"
        out_path.write_text(f"!    total energy =   {energies[ordering]} Ry\nJOB DONE.\n")
        return 0
    monkeypatch.setattr(mp, "_run_pw_x", fake_run)
    gs = mp.run(original_prompt=_PROMPT, workdir=tmp_path, pw_x_path="/bin/true",
                qe_backend="cpu", is_magnetic=True, magnetic_elements=["Ni"],
                seed={"Ni": 0.5})
    assert gs.ok is True
    assert gs.ordering == "FM"
    assert gs.reason == "energy_comparison"
    assert "Ni" in gs.atomic_species_block
    assert (tmp_path / ".magnetic_ground_state.json").exists()

def test_probe_not_magnetic_returns_not_ok(tmp_path):
    gs = mp.run(original_prompt="garbage", workdir=tmp_path, pw_x_path="/bin/true",
                qe_backend="cpu", is_magnetic=False, magnetic_elements=[], seed={})
    assert gs.ok is False


def test_probe_degrades_gracefully_when_one_scf_times_out(tmp_path, monkeypatch):
    # FIX-35: a timed-out probe SCF (TimeoutExpired) must not crash run() —
    # _energy_for returns None and select_ground_state falls back on whichever
    # ordering converged.
    import subprocess
    energies = {"FM": -1502.0179532}
    def flaky_run(input_text, out_path, **kw):
        if "afm" in str(out_path).lower():
            raise subprocess.TimeoutExpired(cmd="pw.x", timeout=kw.get("timeout_s", 1))
        out_path.write_text(f"!    total energy =   {energies['FM']} Ry\nJOB DONE.\n")
        return 0
    monkeypatch.setattr(mp, "_run_pw_x", flaky_run)
    gs = mp.run(original_prompt=_PROMPT, workdir=tmp_path, pw_x_path="/bin/true",
                qe_backend="cpu", is_magnetic=True, magnetic_elements=["Ni"],
                seed={"Ni": 0.5})
    assert gs.ok is True              # FM converged → still usable
    assert gs.ordering == "FM"
    assert gs.afm_energy_ry is None
    assert gs.reason == "afm_failed"
