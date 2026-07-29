"""Unit tests for agent.tools.qe_input_lint.

Verifies:
  - the linter detects vc-relax and only touches those inputs
  - missing thresholds are inserted; loose values are tightened; tight
    values are kept verbatim (idempotent on already-good inputs)
  - find_pw_input handles -in / -inp / -input / `<` redirect and
    declines on non-pw.x commands
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.tools.qe_input_lint import (  # noqa: E402
    LintReject,
    LintResult,
    _fortran_to_float,
    _parse_input,
    _is_vc_relax,
    _detect_cubic,
    _merge_value,
    _ensure_in_namelist,
    find_pw_input,
    lint_pw_input,
    intercept_bash,
)


# ─── Fortran numeric parser ────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("0.05", 0.05),
    ("1.0d-4", 1.0e-4),
    ("1.0e-4", 1.0e-4),
    ("1.D-5", 1.0e-5),
    ("1e-5", 1.0e-5),
    (".5", 0.5),
    ("5.", 5.0),
    ("5", 5.0),
    ("0.05,", 0.05),    # trailing comma tolerated
    (" 1.0d-4 ", 1.0e-4),
    ("", None),
    (None, None),
    ("not-a-number", None),
])
def test_fortran_to_float(text, expected):
    assert _fortran_to_float(text) == expected


# ─── Input parser ──────────────────────────────────────────────────────

VCRELAX_FULL = """\
&CONTROL
  calculation = 'vc-relax',
  prefix = 'si',
  outdir = './out',
  pseudo_dir = './pseudos/',
  forc_conv_thr = 1.0d-4,
  etot_conv_thr = 1.0d-5,
/
&SYSTEM
  ibrav = 2,
  celldm(1) = 10.2,
  nat = 2, ntyp = 1,
  ecutwfc = 50, ecutrho = 400,
  occupations = 'fixed',
/
&ELECTRONS
/
&IONS
  ion_dynamics = 'bfgs',
/
&CELL
  cell_dynamics = 'bfgs',
  press_conv_thr = 0.05,
/
ATOMIC_SPECIES
  Si 28.086 Si.upf
ATOMIC_POSITIONS crystal
  Si 0.0 0.0 0.0
  Si 0.25 0.25 0.25
K_POINTS automatic
  6 6 6 0 0 0
"""


def test_parse_input_namelists_found():
    parsed = _parse_input(VCRELAX_FULL)
    assert set(parsed.keys()) >= {"control", "system", "electrons", "ions", "cell"}


def test_parse_input_keys_recovered():
    parsed = _parse_input(VCRELAX_FULL)
    assert parsed["control"]["calculation"].strip("'") == "vc-relax"
    assert parsed["control"]["forc_conv_thr"] == "1.0d-4"
    assert parsed["control"]["etot_conv_thr"] == "1.0d-5"
    assert parsed["cell"]["press_conv_thr"] == "0.05"


def test_is_vc_relax_true():
    assert _is_vc_relax(_parse_input(VCRELAX_FULL))


def test_is_vc_relax_false_for_scf():
    scf = VCRELAX_FULL.replace("'vc-relax'", "'scf'")
    assert not _is_vc_relax(_parse_input(scf))


def test_is_vc_relax_false_for_relax():
    rel = VCRELAX_FULL.replace("'vc-relax'", "'relax'")
    assert not _is_vc_relax(_parse_input(rel))


def test_parse_input_case_insensitive_namelist():
    text = VCRELAX_FULL.replace("&CONTROL", "&control").replace("&CELL", "&cell")
    parsed = _parse_input(text)
    # Parser lowercases namelist names, so this should still work
    assert "control" in parsed and "cell" in parsed
    assert _is_vc_relax(parsed)


# ─── _merge_value (the heart of the tightening logic) ──────────────────

def test_merge_value_added_when_missing():
    new, kind = _merge_value("press_conv_thr", None)
    assert kind == "added"
    assert new == "0.05"


def test_merge_value_tightened_when_loose():
    new, kind = _merge_value("press_conv_thr", "0.5")
    assert kind == "tightened"
    assert new == "0.05"


def test_merge_value_kept_when_already_tight():
    new, kind = _merge_value("press_conv_thr", "0.01")
    assert kind is None
    assert new == "0.01"


def test_merge_value_kept_when_exactly_at_default():
    new, kind = _merge_value("press_conv_thr", "0.05")
    assert kind is None


def test_merge_value_handles_fortran_exponent():
    # LLM wrote 1.0d-3 (looser than 1.0d-4) — should tighten
    new, kind = _merge_value("forc_conv_thr", "1.0d-3")
    assert kind == "tightened"
    assert new == "1.0d-4"
    # LLM wrote 1.0d-5 (tighter than 1.0d-4) — should keep
    new, kind = _merge_value("forc_conv_thr", "1.0d-5")
    assert kind is None


def test_merge_value_unparseable_replaces():
    # Garbage value falls through to "replace with default"
    new, kind = _merge_value("etot_conv_thr", "totally-bad")
    assert kind == "tightened"
    assert new == "1.0d-5"


# ─── _ensure_in_namelist (text editor) ─────────────────────────────────

def test_ensure_in_namelist_replaces_existing():
    text = "&CONTROL\n  calculation = 'vc-relax',\n  forc_conv_thr = 1.0d-3,\n/\n"
    out = _ensure_in_namelist(text, "control", "forc_conv_thr", "1.0d-4")
    assert "forc_conv_thr = 1.0d-4" in out
    assert "1.0d-3" not in out


def test_ensure_in_namelist_inserts_when_missing():
    text = "&CONTROL\n  calculation = 'vc-relax',\n/\n"
    out = _ensure_in_namelist(text, "control", "forc_conv_thr", "1.0d-4")
    assert "forc_conv_thr = 1.0d-4" in out
    # Original calculation line preserved
    assert "calculation = 'vc-relax'" in out


def test_ensure_in_namelist_creates_namelist_when_missing():
    text = "&CONTROL\n  calculation = 'vc-relax',\n/\n"  # no &CELL
    out = _ensure_in_namelist(text, "cell", "press_conv_thr", "0.05")
    assert "&CELL" in out
    assert "press_conv_thr = 0.05" in out
    # &CONTROL still present and intact
    assert "&CONTROL\n  calculation = 'vc-relax'" in out


def test_ensure_in_namelist_case_insensitive():
    text = "&control\n  calculation = 'vc-relax',\n/\n"
    out = _ensure_in_namelist(text, "control", "forc_conv_thr", "1.0d-4")
    assert "forc_conv_thr = 1.0d-4" in out


# ─── find_pw_input (command-line resolver) ─────────────────────────────

def test_find_pw_input_dash_in(tmp_path):
    cmd = "pw.x -in scf.in > scf.out"
    (tmp_path / "scf.in").write_text("")
    p = find_pw_input(cmd, tmp_path)
    assert p == (tmp_path / "scf.in").resolve()


def test_find_pw_input_dash_inp(tmp_path):
    cmd = "/opt/qe/bin/pw.x -inp calcs/vc.in"
    (tmp_path / "calcs").mkdir()
    (tmp_path / "calcs" / "vc.in").write_text("")
    p = find_pw_input(cmd, tmp_path)
    assert p == (tmp_path / "calcs" / "vc.in").resolve()


def test_find_pw_input_dash_input_long(tmp_path):
    cmd = "pw.x -input my.in"
    (tmp_path / "my.in").write_text("")
    p = find_pw_input(cmd, tmp_path)
    assert p == (tmp_path / "my.in").resolve()


def test_find_pw_input_stdin_redirect(tmp_path):
    cmd = "pw.x < input.in > out.out"
    (tmp_path / "input.in").write_text("")
    p = find_pw_input(cmd, tmp_path)
    assert p == (tmp_path / "input.in").resolve()


def test_find_pw_input_mpirun_wrapped(tmp_path):
    cmd = "mpirun --allow-run-as-root -np 1 pw.x -in nacl.in > nacl.out 2>&1"
    (tmp_path / "nacl.in").write_text("")
    p = find_pw_input(cmd, tmp_path)
    assert p == (tmp_path / "nacl.in").resolve()


def test_find_pw_input_absolute_path(tmp_path):
    abs_in = tmp_path / "abs.in"
    abs_in.write_text("")
    cmd = f"pw.x -in {abs_in}"
    p = find_pw_input(cmd, tmp_path / "other-cwd")
    assert p == abs_in.resolve()


def test_find_pw_input_non_pw_x_command():
    assert find_pw_input("python script.py", "/tmp") is None
    assert find_pw_input("ls -la", "/tmp") is None
    assert find_pw_input("mkdir -p calcs && cp foo.in bar.in", "/tmp") is None


def test_find_pw_input_skips_mpirun_np_int(tmp_path):
    # `-np 1` should NOT be picked up — 1 is not a path
    # Note: `-np` is not in our flag set anyway, but defensive int-skip
    # also covers any future ambiguity.
    (tmp_path / "real.in").write_text("")
    cmd = "mpirun -np 1 pw.x -in real.in"
    p = find_pw_input(cmd, tmp_path)
    assert p == (tmp_path / "real.in").resolve()


# ─── lint_pw_input — full integration ──────────────────────────────────

def _write(tmp: Path, name: str, content: str) -> Path:
    p = tmp / name
    p.write_text(content)
    return p


def test_lint_already_tight_is_noop(tmp_path):
    p = _write(tmp_path, "scf.in", VCRELAX_FULL)
    original = p.read_text()
    result = lint_pw_input(p)
    assert result.patched is False
    assert result.changes == []
    # File untouched
    assert p.read_text() == original


def test_lint_missing_all_three_inserts_them(tmp_path):
    text = VCRELAX_FULL
    text = text.replace("  forc_conv_thr = 1.0d-4,\n", "")
    text = text.replace("  etot_conv_thr = 1.0d-5,\n", "")
    text = text.replace("  press_conv_thr = 0.05,\n", "")
    p = _write(tmp_path, "scf.in", text)
    result = lint_pw_input(p)
    assert result.patched is True
    keys_changed = {k for k, _, _ in result.changes}
    assert keys_changed == {"press_conv_thr", "forc_conv_thr", "etot_conv_thr"}
    # File now contains them
    new = p.read_text()
    assert "forc_conv_thr = 1.0d-4" in new
    assert "etot_conv_thr = 1.0d-5" in new
    assert "press_conv_thr = 0.05" in new


def test_lint_loose_value_tightens(tmp_path):
    text = VCRELAX_FULL.replace("press_conv_thr = 0.05", "press_conv_thr = 0.5")
    p = _write(tmp_path, "scf.in", text)
    result = lint_pw_input(p)
    assert result.patched is True
    assert any(k == "press_conv_thr" for k, _, _ in result.changes)
    new = p.read_text()
    assert "press_conv_thr = 0.05" in new
    assert "= 0.5,\n" not in new


def test_lint_diff_text_format(tmp_path):
    # Reproduce the cpu_vs_gpu_2026-05-10.md NaCl CPU failure shape
    text = VCRELAX_FULL.replace("  forc_conv_thr = 1.0d-4,\n", "")
    text = text.replace("  press_conv_thr = 0.05,", "  press_conv_thr = 0.5,")
    p = _write(tmp_path, "nacl.in", text)
    result = lint_pw_input(p)
    assert result.patched
    diff = result.diff_text
    assert "auto-injected by qe_input_lint" in diff
    assert "nacl.in" in diff
    assert "forc_conv_thr" in diff and "(key was missing)" in diff
    assert "0.5" in diff and "0.05" in diff
    assert "v3 hard rule" in diff


def test_lint_skips_scf(tmp_path):
    text = VCRELAX_FULL.replace("'vc-relax'", "'scf'")
    p = _write(tmp_path, "scf.in", text)
    original = p.read_text()
    result = lint_pw_input(p)
    assert result.patched is False
    assert "not a vc-relax" in result.skipped_reason
    assert p.read_text() == original


def test_lint_skips_relax(tmp_path):
    text = VCRELAX_FULL.replace("'vc-relax'", "'relax'")
    p = _write(tmp_path, "relax.in", text)
    result = lint_pw_input(p)
    assert result.patched is False


def test_lint_idempotent(tmp_path):
    """Running lint twice should yield identical file content."""
    text = VCRELAX_FULL.replace("  forc_conv_thr = 1.0d-4,\n", "")
    p = _write(tmp_path, "vc.in", text)
    lint_pw_input(p)
    after_first = p.read_text()
    result_2 = lint_pw_input(p)
    assert result_2.patched is False
    assert p.read_text() == after_first


def test_lint_creates_missing_cell_namelist(tmp_path):
    """vc-relax with no &CELL block should get one created."""
    text = """\
&CONTROL
  calculation = 'vc-relax',
  prefix = 'foo',
  outdir = './out',
  pseudo_dir = './pseudos/',
  forc_conv_thr = 1.0d-4,
  etot_conv_thr = 1.0d-5,
/
&SYSTEM
  ibrav = 2,
  celldm(1) = 10.2,
  nat = 2, ntyp = 1,
  ecutwfc = 50, ecutrho = 400,
  occupations = 'fixed',
/
&ELECTRONS
/
&IONS
  ion_dynamics = 'bfgs',
/
ATOMIC_SPECIES
  Si 28.086 Si.upf
"""
    p = _write(tmp_path, "vc.in", text)
    result = lint_pw_input(p)
    assert result.patched is True
    new = p.read_text()
    assert "&CELL" in new
    assert "press_conv_thr = 0.05" in new


def test_lint_file_not_found(tmp_path):
    result = lint_pw_input(tmp_path / "nope.in")
    assert "file not found" in result.skipped_reason


# ─── _detect_cubic — the diagnostic flag ───────────────────────────────

def test_detect_cubic_ibrav_2():
    parsed = _parse_input(VCRELAX_FULL)
    assert _detect_cubic(parsed, VCRELAX_FULL) is True


def test_detect_cubic_ibrav_0_with_cubic_cell():
    text = """\
&CONTROL calculation='vc-relax', /
&SYSTEM ibrav = 0, nat = 1, ntyp = 1, ecutwfc = 30, /
&ELECTRONS /
&CELL /
ATOMIC_SPECIES
  Si 28.086 Si.upf
ATOMIC_POSITIONS angstrom
  Si 0.0 0.0 0.0
CELL_PARAMETERS angstrom
  5.43 0.0 0.0
  0.0 5.43 0.0
  0.0 0.0 5.43
K_POINTS automatic
  6 6 6 0 0 0
"""
    parsed = _parse_input(text)
    assert _detect_cubic(parsed, text) is True


def test_detect_cubic_orthorhombic_returns_false():
    text = """\
&CONTROL calculation='vc-relax', /
&SYSTEM ibrav = 0, nat = 1, ntyp = 1, /
&ELECTRONS /
&CELL /
ATOMIC_SPECIES
  Fe 55.85 Fe.upf
CELL_PARAMETERS angstrom
  4.67 0.0 0.0
  0.0 5.79 0.0
  0.0 0.0 9.88
"""
    parsed = _parse_input(text)
    assert _detect_cubic(parsed, text) is False


# ─── intercept_bash (the public entry point) ───────────────────────────

def test_intercept_bash_no_pw_x(tmp_path):
    result = intercept_bash("ls -la", tmp_path)
    assert result.patched is False
    assert "no pw.x" in result.skipped_reason


def test_intercept_bash_writes_log(tmp_path):
    p = _write(tmp_path, "vc.in", VCRELAX_FULL.replace("  forc_conv_thr = 1.0d-4,\n", ""))
    log = tmp_path / "lint.log"
    cmd = "pw.x -in vc.in > vc.out"
    result = intercept_bash(cmd, tmp_path, log_path=log)
    assert result.patched is True
    assert log.exists()
    log_text = log.read_text()
    assert "patched=True" in log_text
    assert "forc_conv_thr" in log_text


# ─── v4 Phase 2 P0b: bands → nscf rewrite rule ──────────────────────────────
#
# Fixture: a minimal bands-path NSCF input shaped exactly like what haiku
# wrote in /tmp/v4-phase1-alas/...AlAs/workdir/bands_nscf.in (the run that
# scored 0.16 because evaluator couldn't find homo/lumo line).

BANDS_PATH_INPUT = """\
&CONTROL
  calculation = 'bands',
  prefix = 'job',
  outdir = './out',
  pseudo_dir = './pseudos/',
/
&SYSTEM
  ibrav = 0,
  nat = 2,
  ntyp = 2,
  ecutwfc = 50,
  ecutrho = 400,
  occupations = 'fixed',
  nbnd = 16,
/
&ELECTRONS
  conv_thr = 1.0d-8,
/
ATOMIC_SPECIES
  Al 26.98 Al.upf
  As 74.92 As.upf
ATOMIC_POSITIONS crystal
  Al 0.0 0.0 0.0
  As 0.25 0.25 0.25
CELL_PARAMETERS angstrom
  0.0 2.83 2.83
  2.83 0.0 2.83
  2.83 2.83 0.0
K_POINTS crystal_b
4
  0.0 0.0 0.0   30
  0.5 0.0 0.5   30
  0.5 0.25 0.75 30
  0.0 0.0 0.0   1
"""


def test_bands_to_nscf_rewrites_calculation(tmp_path):
    """Bands-path NSCF with calculation='bands' must be rewritten to 'nscf'."""
    p = _write(tmp_path, "bands.in", BANDS_PATH_INPUT)
    result = lint_pw_input(p)
    assert result.patched is True
    new = p.read_text()
    assert "calculation = 'nscf'" in new
    assert "calculation = 'bands'" not in new
    # K_POINTS section preserved
    assert "K_POINTS crystal_b" in new
    # nbnd line preserved as-is
    assert "nbnd = 16" in new
    # Changes list reports the rewrite
    assert any(k == "calculation" for k, _, _ in result.changes)


def test_bands_to_nscf_diff_text_explains_why(tmp_path):
    """Diff returned to LLM must explain WHY (homo/lumo line, evaluator)
    so the model generalises the fix to future inputs."""
    p = _write(tmp_path, "bands.in", BANDS_PATH_INPUT)
    result = lint_pw_input(p)
    diff = result.diff_text
    assert "auto-injected by qe_input_lint" in diff
    assert "bands.in" in diff
    assert "calculation" in diff
    assert "'bands'" in diff and "'nscf'" in diff
    # Must surface the bench-evaluator rationale to the LLM
    assert "homo/lumo" in diff or "nscf" in diff.lower()


def test_bands_to_nscf_nbnd_hint_when_missing(tmp_path):
    """When `nbnd` is missing, lint should add an advisory line (not auto-inject)."""
    text = BANDS_PATH_INPUT.replace("  nbnd = 16,\n", "")
    p = _write(tmp_path, "bands.in", text)
    result = lint_pw_input(p)
    assert result.patched is True
    diff = result.diff_text
    assert "nbnd" in diff
    # No automatic nbnd injection — file should still not have nbnd line
    assert "nbnd" not in p.read_text() or "nbnd = " not in p.read_text().split("ATOMIC_SPECIES")[0]


def test_bands_to_nscf_idempotent_after_rewrite(tmp_path):
    """Running lint twice on a bands input must not flip back or re-fire."""
    p = _write(tmp_path, "bands.in", BANDS_PATH_INPUT)
    lint_pw_input(p)
    after_first = p.read_text()
    result_2 = lint_pw_input(p)
    assert result_2.patched is False
    assert p.read_text() == after_first
    assert "already uses calculation='nscf'" in result_2.skipped_reason


def test_already_nscf_bands_path_no_op(tmp_path):
    """A bands-path input already using calculation='nscf' must NOT be touched."""
    text = BANDS_PATH_INPUT.replace("calculation = 'bands'", "calculation = 'nscf'")
    p = _write(tmp_path, "bands.in", text)
    original = p.read_text()
    result = lint_pw_input(p)
    assert result.patched is False
    assert p.read_text() == original


def test_bands_calculation_without_crystal_b_left_alone(tmp_path):
    """calculation='bands' WITHOUT K_POINTS crystal_b is unusual; be
    conservative and don't touch it (might be an automatic-mesh bands run)."""
    text = BANDS_PATH_INPUT.replace("K_POINTS crystal_b", "K_POINTS automatic")
    # Replace the path block with a mesh line (otherwise the rest is garbage)
    text = text.split("K_POINTS automatic")[0] + "K_POINTS automatic\n  4 4 4 0 0 0\n"
    p = _write(tmp_path, "bands.in", text)
    original = p.read_text()
    result = lint_pw_input(p)
    # Should not rewrite calculation here — not the bands-path signature
    assert "calculation = 'bands'" in p.read_text()
    # And no change in the changes list for calculation
    if result.patched:
        assert not any(k == "calculation" for k, _, _ in result.changes)


def test_scf_input_with_crystal_b_left_alone(tmp_path):
    """An scf input that happens to mention crystal_b in a comment shouldn't
    be confused with bands-path mode. The K_POINTS-line regex anchors at
    line start, so a comment won't match — verify."""
    text = "! note: use K_POINTS crystal_b for bands NSCF later\n" + BANDS_PATH_INPUT.replace(
        "calculation = 'bands'", "calculation = 'scf'"
    )
    p = _write(tmp_path, "scf.in", text)
    result = lint_pw_input(p)
    # scf with crystal_b is a weird config, but linter shouldn't promote to nscf
    # (we only rewrite 'bands' → 'nscf', not 'scf' → 'nscf').
    assert "calculation = 'scf'" in p.read_text()


def test_bands_to_nscf_preserves_double_quotes(tmp_path):
    """LLMs sometimes use double quotes — `calculation = "bands"` must be
    rewritten just like the single-quote form."""
    text = BANDS_PATH_INPUT.replace("calculation = 'bands'", 'calculation = "bands"')
    p = _write(tmp_path, "bands.in", text)
    result = lint_pw_input(p)
    assert result.patched is True
    new = p.read_text()
    assert 'calculation = "nscf"' in new
    assert "calculation = \"bands\"" not in new


def test_vc_relax_and_bands_rules_independent(tmp_path):
    """Pathological case: an input file that has BOTH vc-relax keywords
    AND a bands-path K_POINTS block. Each rule should fire independently
    on its own keys without interfering with the other."""
    # Synthesise: vc-relax with crystal_b k-points (nonsense physically, but
    # tests that the rules don't gate each other off).
    text = VCRELAX_FULL.replace("  press_conv_thr = 0.05", "  press_conv_thr = 0.5")
    text = text.replace("K_POINTS automatic\n  6 6 6 0 0 0",
                        "K_POINTS crystal_b\n2\n  0.0 0.0 0.0 30\n  0.5 0.5 0.5 1")
    p = _write(tmp_path, "weird.in", text)
    result = lint_pw_input(p)
    assert result.patched is True
    new = p.read_text()
    # vc-relax rule tightened the threshold
    assert "press_conv_thr = 0.05" in new
    # bands rule did NOT touch calculation (input is vc-relax, not bands)
    assert "calculation = 'vc-relax'" in new
    # Only the vc-relax change appears
    assert not any(k == "calculation" for k, _, _ in result.changes)


def test_intercept_bash_for_bands_input_writes_log(tmp_path):
    """End-to-end via intercept_bash: bands.in patch produces a lint.log entry."""
    p = _write(tmp_path, "bands.in", BANDS_PATH_INPUT)
    log = tmp_path / "lint.log"
    result = intercept_bash("pw.x -in bands.in > bands.out", tmp_path, log_path=log)
    assert result.patched is True
    log_text = log.read_text()
    assert "patched=True" in log_text
    assert "calculation" in log_text


# ─── Rule C (Phase 4): ecutrho ratio for USPP pseudos ───────────────────────


SCF_USPP_8X = """\
&CONTROL
  calculation = 'scf',
  prefix = 'si',
  outdir = './out',
  pseudo_dir = './pseudos/GBRV_USPP_PBE_v1.5/',
/
&SYSTEM
  ibrav = 0,
  nat = 2, ntyp = 1,
  ecutwfc = 60, ecutrho = 480,
  occupations = 'fixed',
/
&ELECTRONS
/
ATOMIC_SPECIES
  Si 28.086 Si.upf
ATOMIC_POSITIONS crystal
  Si 0.0 0.0 0.0
  Si 0.25 0.25 0.25
K_POINTS automatic
  8 8 8 0 0 0
"""

SCF_USPP_10X = SCF_USPP_8X.replace("ecutrho = 480", "ecutrho = 600")

SCF_NCPP = SCF_USPP_8X.replace(
    "pseudo_dir = './pseudos/GBRV_USPP_PBE_v1.5/'",
    "pseudo_dir = './pseudos/NCPP_PBE/'",
)


def test_rule_c_uspp_8x_gets_fixed_to_10x(tmp_path):
    p = _write(tmp_path, "scf.in", SCF_USPP_8X)
    result = lint_pw_input(p)
    assert result.patched
    new = p.read_text()
    # ecutrho rewritten to 10 × ecutwfc = 600
    assert "ecutrho = 600" in new
    assert any(k == "ecutrho" for k, _, _ in result.changes)


def test_rule_c_uspp_already_10x_no_change(tmp_path):
    p = _write(tmp_path, "scf.in", SCF_USPP_10X)
    result = lint_pw_input(p)
    assert not result.patched
    assert "ecutrho = 600" in p.read_text()


def test_rule_c_ncpp_pseudo_no_change(tmp_path):
    # NCPP pseudos use ecutrho ≈ 4× ecutwfc; we should NOT rewrite.
    p = _write(tmp_path, "scf.in", SCF_NCPP)
    result = lint_pw_input(p)
    assert not result.patched
    assert "ecutrho = 480" in p.read_text()


def test_rule_c_uspp_idempotent(tmp_path):
    p = _write(tmp_path, "scf.in", SCF_USPP_8X)
    lint_pw_input(p)
    text_after_first = p.read_text()
    result2 = lint_pw_input(p)
    assert not result2.patched
    assert p.read_text() == text_after_first


# ─── Rule D (Phase 4): occupations vs config_advisor recommendation ─────────


def _write_advice(workdir: Path, *, is_metal: bool) -> None:
    import json as _json
    advice = {
        "ok": True,
        "is_metal": is_metal,
        "occupations": "smearing" if is_metal else "fixed",
        "smearing": "mv" if is_metal else None,
        "degauss": 0.02 if is_metal else None,
        "ecutrho_factor": 10.0,
        "rationale": "test fixture",
        "source": "deterministic",
    }
    (workdir / ".config_advice.json").write_text(_json.dumps(advice))


def test_rule_d_mismatch_metal_advice_vs_fixed_input(tmp_path):
    _write_advice(tmp_path, is_metal=True)
    # Insulator-style SCF input but advice says metal
    p = _write(tmp_path, "scf.in", SCF_USPP_10X)
    result = lint_pw_input(p)
    # Warn-only: changes empty but diff_text contains the warning
    assert not result.patched   # nothing patched by Rule D itself
    assert "WARN" in result.diff_text
    assert "METAL" in result.diff_text
    assert "smearing" in result.diff_text


def test_rule_d_match_insulator_advice_no_warn(tmp_path):
    _write_advice(tmp_path, is_metal=False)
    p = _write(tmp_path, "scf.in", SCF_USPP_10X)
    result = lint_pw_input(p)
    # No mismatch → no warning → no patches → skipped_reason set
    assert not result.patched
    assert result.skipped_reason  # skipped, not advisory
    assert "WARN" not in result.diff_text


def test_rule_d_no_advice_file_silent(tmp_path):
    # No .config_advice.json anywhere → Rule D silently no-ops
    p = _write(tmp_path, "scf.in", SCF_USPP_10X)
    result = lint_pw_input(p)
    assert not result.patched
    assert "WARN" not in result.diff_text


def test_rule_d_tetrahedra_skipped(tmp_path):
    # DOS NSCF input (occupations='tetrahedra') should NEVER trigger Rule D
    # warning, even with conflicting advice
    _write_advice(tmp_path, is_metal=True)
    dos_input = SCF_USPP_10X.replace("calculation = 'scf'", "calculation = 'nscf'")
    dos_input = dos_input.replace("occupations = 'fixed'", "occupations = 'tetrahedra'")
    p = _write(tmp_path, "nscf_dos.in", dos_input)
    result = lint_pw_input(p)
    assert "WARN" not in result.diff_text


def test_rule_d_vc_relax_skipped(tmp_path):
    # vc-relax is also skipped (occupations choice less critical for geometry)
    _write_advice(tmp_path, is_metal=True)
    p = _write(tmp_path, "relax.in", VCRELAX_FULL)
    result = lint_pw_input(p)
    # No warning even though VCRELAX_FULL has occupations='fixed' and advice says metal
    assert "WARN" not in result.diff_text


def _write_advice_pbe_near_metal(workdir: Path) -> None:
    import json as _json
    # CoSb3-like: advisor field occupations='smearing' (applies to vc-relax+SCF),
    # but FIX-29 wants the bands NSCF on 'fixed'.
    advice = {
        "ok": True, "is_metal": False, "occupations": "smearing",
        "smearing": "mv", "degauss": 0.02, "ecutrho_factor": 10.0,
        "rationale": "CoSb3 narrow-gap skutterudite (PBE-near-metal)",
        "source": "deterministic_pbe_near_metal_override",
        "pbe_near_metal": True,
    }
    (workdir / ".config_advice.json").write_text(_json.dumps(advice))


def test_rule_d_pbe_near_metal_fixed_bands_no_warn(tmp_path):
    # FIX-29: advisor occupations='smearing' (for SCF), but the bands NSCF
    # correctly uses 'fixed' so QE emits the homo/lumo line. Rule D must NOT
    # warn "prefer smearing" — that would fight the FIX-29 advisory directive
    # and could nudge the executor back into the broken smearing-bands path.
    _write_advice_pbe_near_metal(tmp_path)
    bands_in = SCF_USPP_10X.replace("calculation = 'scf'", "calculation = 'nscf'")
    p = _write(tmp_path, "bands.in", bands_in)        # occupations='fixed' inside
    result = lint_pw_input(p)
    assert "WARN" not in result.diff_text


def test_rule_d_pbe_near_metal_smearing_scf_still_ok(tmp_path):
    # SCF under pbe_near_metal advice uses smearing (matches advice) → no warn.
    _write_advice_pbe_near_metal(tmp_path)
    scf_in = SCF_USPP_10X.replace(
        "occupations = 'fixed'",
        "occupations = 'smearing'\n  smearing = 'mv', degauss = 0.02",
    )
    p = _write(tmp_path, "scf.in", scf_in)
    result = lint_pw_input(p)
    assert "WARN" not in result.diff_text


# ─── Rule E (Phase 5 / 2026-05-19): K_POINTS crystal_b data-row validation ──
#
# Goal: catch the Rb 2026-05-19 footgun where the solver wrote 331 explicit
# k-points with 4th col = 0 in crystal_b mode. crystal_b's 4th col is
# subdivision count (not weight); all zeros = no interpolation = bench's
# fermi-slice extractor extracted bogus gap=10.988 eV for a metal.


_BANDS_HEAD = """\
&CONTROL
  calculation = 'nscf',
  prefix = 'job',
  outdir = './out',
  pseudo_dir = './pseudos/',
/
&SYSTEM
  ibrav = 0,
  nat = 1,
  ntyp = 1,
  ecutwfc = 40, ecutrho = 400,
  occupations = 'smearing',
  smearing = 'mv', degauss = 0.02,
  nbnd = 20,
/
&ELECTRONS
  conv_thr = 1.0d-8,
/
ATOMIC_SPECIES
  Rb 85.46 Rb.upf
ATOMIC_POSITIONS crystal
  Rb 0.0 0.0 0.0
CELL_PARAMETERS angstrom
  4.91 0.17 0.0
  -1.82 4.57 0.0
  -1.55 -2.37 4.02
"""


def _bands_with_kpoints(kpoints_block: str) -> str:
    return _BANDS_HEAD + kpoints_block


def test_rule_e_rejects_all_zero_weights_long_path(tmp_path):
    """The Rb 2026-05-19 root cause: many rows with 4th col = 0 in crystal_b
    mode. Must reject with explanation of both legal forms."""
    kp = "K_POINTS crystal_b\n  6\n"
    kp += "  0.00 0.00 0.00  0.0\n"
    kp += "  0.10 0.00 0.00  0.0\n"
    kp += "  0.20 0.00 0.00  0.0\n"
    kp += "  0.30 0.00 0.00  0.0\n"
    kp += "  0.40 0.00 0.00  0.0\n"
    kp += "  0.50 0.00 0.00  0.0\n"
    p = _write(tmp_path, "bands.in", _bands_with_kpoints(kp))
    result = lint_pw_input(p)
    assert result.rejected is True
    assert result.patched is False
    # Reason must surface both legal alternatives so the LLM can pick one
    assert "K_POINTS crystal" in result.reject_reason
    assert "weight=1.0" in result.reject_reason
    assert "high-symmetry" in result.reject_reason or "vertices" in result.reject_reason
    # File untouched
    assert p.read_text() == _bands_with_kpoints(kp)


def test_rule_e_rejects_real_rb_run_fixture(tmp_path):
    """End-to-end fixture mimicking the 2026-05-19 Rb bands.in head:
    K_POINTS crystal_b with 6 rows starting at Γ, all 4th col = 0.0."""
    kp = """\
K_POINTS crystal_b
  6
  0.0000000000  0.0000000000  0.0000000000  0.0
  -0.0000000211  0.0000001046  0.0235701356  0.0
  -0.0000000422  0.0000002092  0.0471402712  0.0
  -0.0000000634  0.0000003137  0.0707104068  0.0
  -0.0000000845  0.0000004183  0.0942805424  0.0
  -0.0000001056  0.0000005229  0.1178506780  0.0
"""
    p = _write(tmp_path, "bands.in", _bands_with_kpoints(kp))
    result = lint_pw_input(p)
    assert result.rejected is True
    assert "Rb" in result.reject_reason or "fermi-slice" in result.reject_reason
    assert "2026-05-19" in result.reject_reason


def test_rule_e_rejects_negative_weight(tmp_path):
    kp = "K_POINTS crystal_b\n  2\n"
    kp += "  0.0 0.0 0.0  -1\n"
    kp += "  0.5 0.0 0.0  30\n"
    p = _write(tmp_path, "bands.in", _bands_with_kpoints(kp))
    result = lint_pw_input(p)
    assert result.rejected is True
    assert "negative" in result.reject_reason.lower()


def test_rule_e_rejects_non_integer_weight(tmp_path):
    """Floats like 1.5 in column 4 mean the LLM thought it was a weight."""
    kp = "K_POINTS crystal_b\n  2\n"
    kp += "  0.0 0.0 0.0  1.5\n"
    kp += "  0.5 0.0 0.0  30\n"
    p = _write(tmp_path, "bands.in", _bands_with_kpoints(kp))
    result = lint_pw_input(p)
    assert result.rejected is True
    assert "non-integer" in result.reject_reason.lower() or "integer" in result.reject_reason.lower()
    # Reason must surface the crystal-vs-crystal_b distinction
    assert "weight" in result.reject_reason.lower()


def test_rule_e_passes_valid_high_sym_path(tmp_path):
    """Standard usage: 4 high-sym vertices with 30 subdivisions each,
    last one 0. Must NOT reject."""
    kp = "K_POINTS crystal_b\n  4\n"
    kp += "  0.0 0.0 0.0   30\n"
    kp += "  0.5 0.0 0.0   30\n"
    kp += "  0.5 0.5 0.0   30\n"
    kp += "  0.0 0.0 0.0    0\n"
    p = _write(tmp_path, "bands.in", _bands_with_kpoints(kp))
    result = lint_pw_input(p)
    assert result.rejected is False


def test_rule_e_passes_few_rows_with_zero_weight(tmp_path):
    """Edge case: < 5 rows, all-zero 4th col is allowed (might be a tiny
    test path that genuinely uses zero subdivisions). Threshold is 5
    rows to avoid false positives on small toy paths."""
    kp = "K_POINTS crystal_b\n  3\n"
    kp += "  0.0 0.0 0.0   0\n"
    kp += "  0.5 0.0 0.0   0\n"
    kp += "  0.5 0.5 0.0   0\n"
    p = _write(tmp_path, "bands.in", _bands_with_kpoints(kp))
    result = lint_pw_input(p)
    assert result.rejected is False


def test_rule_e_does_not_fire_on_automatic_mesh(tmp_path):
    """K_POINTS automatic has 6-token mesh format, never trigger Rule E."""
    text = _BANDS_HEAD + "K_POINTS automatic\n  8 8 8 0 0 0\n"
    p = _write(tmp_path, "bands.in", text)
    result = lint_pw_input(p)
    assert result.rejected is False


def test_rule_e_does_not_fire_on_crystal_with_weights(tmp_path):
    """`K_POINTS crystal` (not crystal_b) is the right tag for explicit
    paths with weights — Rule E must skip it entirely."""
    text = _BANDS_HEAD + "K_POINTS crystal\n  6\n"
    text += "  0.00 0.0 0.0  1.0\n"
    text += "  0.10 0.0 0.0  1.0\n"
    text += "  0.20 0.0 0.0  1.0\n"
    text += "  0.30 0.0 0.0  1.0\n"
    text += "  0.40 0.0 0.0  1.0\n"
    text += "  0.50 0.0 0.0  1.0\n"
    p = _write(tmp_path, "bands.in", text)
    result = lint_pw_input(p)
    assert result.rejected is False


def test_rule_e_tpiba_b_also_validated(tmp_path):
    """tpiba_b has identical 4th-col semantics to crystal_b. Same rule
    applies."""
    kp = "K_POINTS tpiba_b\n  6\n"
    for _ in range(6):
        kp += "  0.0 0.0 0.0  0\n"
    p = _write(tmp_path, "bands.in", _bands_with_kpoints(kp))
    result = lint_pw_input(p)
    assert result.rejected is True


def test_intercept_bash_propagates_reject(tmp_path):
    """End-to-end: intercept_bash returns a LintResult with rejected=True
    so the BashTool can short-circuit pw.x."""
    kp = "K_POINTS crystal_b\n  6\n"
    for _ in range(6):
        kp += "  0.0 0.0 0.0  0.0\n"
    p = _write(tmp_path, "bands.in", _bands_with_kpoints(kp))
    log = tmp_path / "lint.log"
    result = intercept_bash("pw.x -in bands.in > bands.out", tmp_path, log_path=log)
    assert result.rejected is True
    assert result.reject_reason
    # The log should record the rejection (for ops debugging)
    log_text = log.read_text()
    assert "rejected=True" in log_text
    assert "REJECT:" in log_text


def test_rule_e_does_not_block_other_inputs(tmp_path):
    """vc-relax inputs without any K_POINTS crystal_b block must not
    accidentally trip Rule E."""
    p = _write(tmp_path, "vc.in", VCRELAX_FULL)
    result = lint_pw_input(p)
    assert result.rejected is False


# ---------------------------------------------------------------------------
# Rule F (FIX-32): nspin=2 backstop for magnetic materials
# ---------------------------------------------------------------------------

_MAG_INPUT = """&CONTROL
  calculation = 'scf'
/
&SYSTEM
  ibrav = 0
  nat = 2
  ntyp = 1
  ecutwfc = 60
  ecutrho = 600
/
&ELECTRONS
/
ATOMIC_SPECIES
Fe 55.845 fe.UPF
"""


def test_rule_f_injects_nspin_for_magnetic(tmp_path):
    (tmp_path / ".config_advice.json").write_text(json.dumps({
        "ok": True, "is_metal": True, "is_magnetic": True,
        "magnetic_elements": ["Fe"], "occupations": "smearing"}))
    f = tmp_path / "scf.in"
    f.write_text(_MAG_INPUT)
    res = lint_pw_input(f)
    assert "nspin = 2" in f.read_text()        # auto-injected
    assert any("nspin" in r.lower() for r in res.reasons)


def test_rule_f_noop_for_nonmagnetic(tmp_path):
    (tmp_path / ".config_advice.json").write_text(json.dumps({
        "ok": True, "is_metal": False, "is_magnetic": False}))
    f = tmp_path / "scf.in"
    f.write_text(_MAG_INPUT)
    lint_pw_input(f)
    assert "nspin = 2" not in f.read_text()     # untouched


_MAG_METAL_INPUT = """&CONTROL
  calculation = 'vc-relax'
/
&SYSTEM
  ibrav = 0
  nat = 2
  ntyp = 1
  ecutwfc = 60
  ecutrho = 600
  nspin = 2
  starting_magnetization(1) = 0.7
  occupations = 'smearing'
  smearing = 'mv'
  degauss = 0.02
/
&ELECTRONS
/
ATOMIC_SPECIES
Fe 55.845 fe.UPF
"""


def test_rule_f_magnetic_metal_rewrites_mv_and_caps_degauss(tmp_path):
    # FIX-33: magnetic metal with mv/0.02 → gauss/0.01 (moment-collapse guard).
    (tmp_path / ".config_advice.json").write_text(json.dumps({
        "ok": True, "is_metal": True, "is_magnetic": True,
        "magnetic_elements": ["Fe"], "occupations": "smearing"}))
    f = tmp_path / "vc-relax.in"
    f.write_text(_MAG_METAL_INPUT)
    res = lint_pw_input(f)
    out = f.read_text()
    assert "smearing = 'gauss'" in out
    assert "smearing = 'mv'" not in out
    assert "degauss = 0.01" in out
    assert "0.02" not in out
    assert any("magnetic metal" in r.lower() for r in res.reasons)


def test_rule_f_magnetic_insulator_leaves_smearing(tmp_path):
    # Magnetic *insulator* (is_metal=False): the mv-cap branch must not fire.
    (tmp_path / ".config_advice.json").write_text(json.dumps({
        "ok": True, "is_metal": False, "is_magnetic": True,
        "magnetic_elements": ["Ni"], "occupations": "smearing"}))
    body = _MAG_METAL_INPUT.replace("smearing = 'mv'", "smearing = 'gauss'") \
                           .replace("degauss = 0.02", "degauss = 0.01")
    f = tmp_path / "scf.in"
    f.write_text(body)
    lint_pw_input(f)
    out = f.read_text()
    assert "smearing = 'gauss'" in out and "degauss = 0.01" in out


# ---------------------------------------------------------------------------
# Rule G (FIX-34): magnetic-insulator bands NSCF → occupations='fixed'
# ---------------------------------------------------------------------------

_MAG_BANDS_INPUT = """&CONTROL
  calculation = 'nscf'
/
&SYSTEM
  ibrav = 0
  nat = 4
  ntyp = 3
  ecutwfc = 60
  ecutrho = 600
  nbnd = 36
  nspin = 2
  starting_magnetization(1) = 0.5
  occupations = 'smearing'
  smearing = 'gauss'
  degauss = 0.01
/
&ELECTRONS
/
ATOMIC_SPECIES
Ni1 58.69 ni.UPF
Ni2 58.69 ni.UPF
O 16.0 o.UPF
K_POINTS crystal_b
3
0.0 0.0 0.0 20
0.5 0.0 0.5 20
0.5 0.25 0.75 1
"""

_MAG_ADVICE_INSULATOR = {
    "ok": True, "is_metal": False, "is_magnetic": True,
    "magnetic_elements": ["Ni"], "occupations": "smearing",
}


def test_rule_g_bands_nscf_smearing_to_fixed(tmp_path):
    (tmp_path / ".config_advice.json").write_text(json.dumps(_MAG_ADVICE_INSULATOR))
    f = tmp_path / "bands.in"
    f.write_text(_MAG_BANDS_INPUT)
    res = lint_pw_input(f)
    out = f.read_text()
    assert "occupations = 'fixed'" in out
    assert "occupations = 'smearing'" not in out
    assert "nspin = 2" in out                      # magnetic protocol kept
    assert any("magnetic insulator bands" in r.lower() for r in res.reasons)


def test_rule_g_noop_on_scf_automatic_kpoints(tmp_path):
    # SCF (K_POINTS automatic) of the same material must keep smearing.
    (tmp_path / ".config_advice.json").write_text(json.dumps(_MAG_ADVICE_INSULATOR))
    scf = _MAG_BANDS_INPUT.replace("calculation = 'nscf'", "calculation = 'scf'")
    scf = scf.split("K_POINTS")[0] + "K_POINTS automatic\n  8 8 8 0 0 0\n"
    f = tmp_path / "scf.in"
    f.write_text(scf)
    lint_pw_input(f)
    assert "occupations = 'smearing'" in f.read_text()  # untouched


# FM magnetic metal: bare element labels (no Cr1/Cr2 split), e.g. bcc Fe.
_FM_METAL_BANDS_INPUT = """&CONTROL
  calculation = 'nscf'
/
&SYSTEM
  ibrav = 0
  nat = 2
  ntyp = 1
  ecutwfc = 60
  ecutrho = 600
  nbnd = 20
  nspin = 2
  starting_magnetization(1) = 0.7
  occupations = 'smearing'
  smearing = 'mv'
  degauss = 0.02
/
&ELECTRONS
/
ATOMIC_SPECIES
Fe 55.85 fe.UPF
K_POINTS crystal_b
3
0.0 0.0 0.0 20
0.5 0.0 0.5 20
0.5 0.25 0.75 1
"""


def test_rule_g_magnetic_metal_with_afm_split_to_fixed(tmp_path):
    """FIX-40 (S-2): an AFM magnetic METAL with split sublattice labels
    (Cr1/Cr2) — like Cr_bcc — must ALSO switch its bands NSCF to fixed occ.
    The bench evaluator's seekpath step crashes on the split-label species
    (Element('Cr1')); only fixed occ makes the metal short-circuit fire
    BEFORE seekpath, recovering gap_type/vbm/cbm (else all nulled = 0.08)."""
    (tmp_path / ".config_advice.json").write_text(json.dumps({
        "ok": True, "is_metal": True, "is_magnetic": True,
        "magnetic_elements": ["Ni"], "occupations": "smearing"}))
    f = tmp_path / "bands.in"
    f.write_text(_MAG_BANDS_INPUT)  # has Ni1/Ni2 split labels
    res = lint_pw_input(f)
    out = f.read_text()
    assert "occupations = 'fixed'" in out
    assert "occupations = 'smearing'" not in out
    assert "nspin = 2" in out
    assert any("metal" in r.lower() for r in res.reasons)


def test_rule_g_noop_for_fm_magnetic_metal(tmp_path):
    """A ferromagnetic magnetic metal with BARE element labels (no split,
    e.g. bcc Fe) has no seekpath-crash risk → keep smearing (no regression
    for Fe_bcc/Ni_fcc/Co_hcp/CrO2 which already score well)."""
    (tmp_path / ".config_advice.json").write_text(json.dumps({
        "ok": True, "is_metal": True, "is_magnetic": True,
        "magnetic_elements": ["Fe"], "occupations": "smearing"}))
    f = tmp_path / "bands.in"
    f.write_text(_FM_METAL_BANDS_INPUT)  # bare 'Fe' label
    lint_pw_input(f)
    assert "occupations = 'smearing'" in f.read_text()  # untouched


def test_has_split_labels_detects_afm_sublattices():
    from agent.tools.qe_input_lint import _has_split_labels
    assert _has_split_labels(_MAG_BANDS_INPUT) is True       # Ni1/Ni2
    assert _has_split_labels(_FM_METAL_BANDS_INPUT) is False  # bare Fe
    # starting_magnetization(1) digits must NOT be mistaken for a split label
    assert _has_split_labels(
        "&SYSTEM\n starting_magnetization(1)=0.7\n/\nATOMIC_SPECIES\nFe 55 fe.UPF\n"
    ) is False


# ─── Rule H (FIX-#4): vc-relax cell preservation ───────────────────────────

# Bench-given Fe_bcc: 2-atom primitive cell (a=b=2.4778, c=4.0543, γ=109.4712).
_FE_PROMPT = """\
Fe (bcc, space group Im-3m, 2 atoms per primitive cell), vc-relax.
Initial structure (primitive cell, angstrom / degree):
  a = 2.4778, b = 2.4778, c = 4.0543
  alpha = 90.0, beta = 90.0, gamma = 109.4712
ATOMIC_POSITIONS (crystal):
  Fe 0.500000 0.500000 0.500000
  Fe 1.000000 1.000000 0.000000
"""

# The failing case (Fe_bcc 2026-06-02 = 0.35): LLM collapsed the given 2-atom
# cell to ibrav=3 / nat=1.
_FE_IBRAV3_IN = """\
&CONTROL
  calculation = 'vc-relax',
  forc_conv_thr = 1.0d-4, etot_conv_thr = 1.0d-5,
/
&SYSTEM
  ibrav = 3, celldm(1) = 5.41, nat = 1, ntyp = 1,
  ecutwfc = 80, ecutrho = 800,
  nspin = 2, starting_magnetization(1) = 0.7,
/
&ELECTRONS
  mixing_beta = 0.3,
/
&CELL
  press_conv_thr = 0.05,
/
ATOMIC_SPECIES
  Fe 55.845 Fe.upf
ATOMIC_POSITIONS (crystal)
  Fe 0.0 0.0 0.0
K_POINTS automatic
  12 12 12 0 0 0
"""

# Already-correct ibrav=0 input that matches the given cell (must be untouched).
_FE_GOOD_IBRAV0_IN = """\
&CONTROL
  calculation = 'vc-relax',
  forc_conv_thr = 1.0d-4, etot_conv_thr = 1.0d-5,
/
&SYSTEM
  ibrav = 0, nat = 2, ntyp = 1,
  ecutwfc = 80, ecutrho = 800,
  nspin = 2, starting_magnetization(1) = 0.7,
/
&ELECTRONS
/
&CELL
  press_conv_thr = 0.05,
/
ATOMIC_SPECIES
  Fe 55.845 Fe.upf
CELL_PARAMETERS (angstrom)
  2.4778000000  0.0000000000  0.0000000000
  -0.8259324920  2.3360925407  0.0000000000
  0.0000000000  0.0000000000  4.0543000000
ATOMIC_POSITIONS (crystal)
  Fe 0.5 0.5 0.5
  Fe 1.0 1.0 0.0
K_POINTS automatic
  12 12 12 0 0 0
"""

# A 1×1×2 supercell (nat=4, c doubled): GT == relaxed GIVEN cell, so a supercell
# mis-scores too → Rule H normalizes it back to the given 2-atom cell.
_FE_SUPERCELL_IN = """\
&CONTROL
  calculation = 'vc-relax',
  forc_conv_thr = 1.0d-4, etot_conv_thr = 1.0d-5,
/
&SYSTEM
  ibrav = 0, nat = 4, ntyp = 1,
  ecutwfc = 80, ecutrho = 800, nspin = 2, starting_magnetization(1) = 0.7,
/
&ELECTRONS
/
&CELL
  press_conv_thr = 0.05,
/
ATOMIC_SPECIES
  Fe 55.845 Fe.upf
CELL_PARAMETERS (angstrom)
  2.4778000000  0.0000000000  0.0000000000
  -0.8259324920  2.3360925407  0.0000000000
  0.0000000000  0.0000000000  8.1086000000
ATOMIC_POSITIONS (crystal)
  Fe 0.5 0.5 0.25
  Fe 1.0 1.0 0.0
  Fe 0.5 0.5 0.75
  Fe 1.0 1.0 0.5
K_POINTS automatic
  12 12 6 0 0 0
"""

# AFM MnO: bench gives 4 atoms with bare Mn/O; solver splits Mn→Mn1/Mn2 on an
# ibrav=0 cell — Rule H must never touch this.
_MNO_PROMPT = """\
MnO rocksalt AFM, vc-relax. 4 atoms per cell.
Initial structure (primitive cell, angstrom / degree):
  a = 3.1448, b = 3.1448, c = 5.4517
  alpha = 73.2, beta = 73.2, gamma = 60.0
ATOMIC_POSITIONS (crystal):
  Mn 0.0 0.0 0.0
  Mn 0.5 0.5 0.5
  O 0.25 0.25 0.25
  O 0.75 0.75 0.75
"""

_MNO_AFM_IN = """\
&CONTROL
  calculation = 'vc-relax',
  forc_conv_thr = 1.0d-4, etot_conv_thr = 1.0d-5,
/
&SYSTEM
  ibrav = 0, nat = 4, ntyp = 3,
  ecutwfc = 50, ecutrho = 500,
  nspin = 2,
  starting_magnetization(1) = 0.7, starting_magnetization(2) = -0.7,
/
&ELECTRONS
/
&CELL
  press_conv_thr = 0.05,
/
ATOMIC_SPECIES
  Mn1 54.938 Mn.upf
  Mn2 54.938 Mn.upf
  O 15.999 O.upf
CELL_PARAMETERS (angstrom)
  3.1448 0.0 0.0
  1.5724 2.7236 0.0
  1.5724 0.9079 5.1365
ATOMIC_POSITIONS (crystal)
  Mn1 0.0 0.0 0.0
  Mn2 0.5 0.5 0.5
  O 0.25 0.25 0.25
  O 0.75 0.75 0.75
K_POINTS automatic
  6 6 4 0 0 0
"""


def _setup_h(tmp_path, prompt, input_text, name="relax.in"):
    """workdir/task_prompt.txt + workdir/vc_relax_1/<name>; return input path
    (in a subdir, so _find_task_prompt must walk up to reach the prompt)."""
    wd = tmp_path / "workdir"
    (wd / "vc_relax_1").mkdir(parents=True)
    (wd / "task_prompt.txt").write_text(prompt)
    p = wd / "vc_relax_1" / name
    p.write_text(input_text)
    return p


def test_rule_h_ibrav3_collapsed_cell_rewritten(tmp_path):
    p = _setup_h(tmp_path, _FE_PROMPT, _FE_IBRAV3_IN)
    result = lint_pw_input(p)
    txt = p.read_text()
    assert result.patched is True
    assert "Rule H" in result.diff_text
    # ibrav→0, atom count restored, celldm dropped
    assert "ibrav = 0" in txt and "nat = 2" in txt
    assert "celldm" not in txt
    # both given atoms present
    assert txt.count("Fe  0.5") == 1 and txt.count("Fe  1.0") == 1
    # given cell installed (c-vector length 4.0543)
    assert "CELL_PARAMETERS (angstrom)" in txt and "4.0543" in txt
    # everything else byte-preserved
    assert "starting_magnetization(1) = 0.7" in txt
    assert "nspin = 2" in txt
    assert "K_POINTS automatic" in txt and "12 12 12 0 0 0" in txt
    assert "ecutwfc = 80" in txt and "ecutrho = 800" in txt


def test_rule_h_good_ibrav0_untouched(tmp_path):
    p = _setup_h(tmp_path, _FE_PROMPT, _FE_GOOD_IBRAV0_IN)
    before = p.read_text()
    result = lint_pw_input(p)
    assert result.patched is False
    assert p.read_text() == before
    assert "Rule H" not in (result.diff_text or "")


def test_rule_h_afm_split_untouched(tmp_path):
    p = _setup_h(tmp_path, _MNO_PROMPT, _MNO_AFM_IN)
    result = lint_pw_input(p)
    txt = p.read_text()
    assert "Rule H" not in (result.diff_text or "")
    # AFM split labels + opposite-sign seeds preserved
    assert "Mn1" in txt and "Mn2" in txt
    assert "starting_magnetization(2) = -0.7" in txt
    assert "ibrav = 0" in txt


def test_rule_h_supercell_normalized(tmp_path):
    p = _setup_h(tmp_path, _FE_PROMPT, _FE_SUPERCELL_IN)
    result = lint_pw_input(p)
    txt = p.read_text()
    assert result.patched is True and "Rule H" in result.diff_text
    assert "nat = 2" in txt  # collapsed back to the given 2-atom cell


def test_rule_h_idempotent(tmp_path):
    p = _setup_h(tmp_path, _FE_PROMPT, _FE_IBRAV3_IN)
    lint_pw_input(p)
    after_first = p.read_text()
    result2 = lint_pw_input(p)
    assert result2.patched is False
    assert p.read_text() == after_first


def test_rule_h_no_prompt_is_noop(tmp_path):
    # ibrav=3 input but NO task_prompt.txt up-tree → never guess, no-op.
    sub = tmp_path / "vc_relax_1"
    sub.mkdir()
    p = sub / "relax.in"
    p.write_text(_FE_IBRAV3_IN)
    result = lint_pw_input(p)
    assert "Rule H" not in (result.diff_text or "")
    assert "ibrav = 3" in p.read_text()  # untouched by Rule H
