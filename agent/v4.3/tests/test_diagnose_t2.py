"""Unit tests for scripts/diagnose_t2.py.

These test the pieces of diagnose_t2 that don't need a real bench checkout:
  - task_id inference from run_dir name
  - task_outputs resolution from a complete result.json (incl. dos_data alias)
  - task_outputs resolution from an incomplete result.json (workdir scan)
  - clear ImportError surfacing when --bench-root is bogus

The full integration path (real extract_t2_fields + score_fields) is
covered separately by replaying a real T2 workdir, not by unit tests
(those need real QE outputs).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import diagnose_t2  # noqa: E402


# ─── _infer_task_id ─────────────────────────────────────────────────────────

def test_infer_task_id_from_utc_prefix(tmp_path: Path) -> None:
    d = tmp_path / "2026-05-11T04-35-26Z_M1_T2_AlN_bands_dos"
    d.mkdir()
    assert diagnose_t2._infer_task_id(d, None) == "M1_T2_AlN_bands_dos"


def test_infer_task_id_with_override(tmp_path: Path) -> None:
    d = tmp_path / "renamed_dir"
    d.mkdir()
    assert diagnose_t2._infer_task_id(d, "M2_T2_MoS2_bands_dos") == "M2_T2_MoS2_bands_dos"


def test_infer_task_id_rejects_unparseable_name(tmp_path: Path) -> None:
    """Without override and without an M{1,2}_T pattern, must abort
    cleanly (SystemExit) rather than guess."""
    d = tmp_path / "garbage_name"
    d.mkdir()
    with pytest.raises(SystemExit):
        diagnose_t2._infer_task_id(d, None)


# ─── _resolve_task_outputs ─────────────────────────────────────────────────

def _touch(p: Path, body: str = "data\n" * 200) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_resolve_from_complete_result_json(tmp_path: Path) -> None:
    """Happy path: result.json has all 4 canonical keys."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    result = {"task_outputs": {
        "scf_out": "scf.out",
        "bands_out": "bands.out",
        "bands_data": "bands.dat",
        "dos_dat": "dos.dat",
    }}
    out, notes = diagnose_t2._resolve_task_outputs(workdir, result)
    assert out == result["task_outputs"]
    assert notes == []


def test_resolve_aliases_dos_data_to_dos_dat(tmp_path: Path) -> None:
    """Some agents emit `dos_data` instead of `dos_dat` — must alias."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    result = {"task_outputs": {
        "scf_out": "scf.out",
        "bands_out": "bands.out",
        "bands_data": "bands.dat",
        "dos_data": "dos.dat",  # ← alias
    }}
    out, _ = diagnose_t2._resolve_task_outputs(workdir, result)
    assert "dos_dat" in out
    assert out["dos_dat"] == "dos.dat"
    assert "dos_data" not in out


def test_resolve_backfills_from_workdir_when_key_missing(tmp_path: Path) -> None:
    """If result.json lists 3 of 4 keys but the missing file is in workdir,
    backfill (with a note)."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    _touch(workdir / "dos.dat")
    result = {"task_outputs": {
        "scf_out": "scf.out",
        "bands_out": "bands.out",
        "bands_data": "bands.dat",
        # dos_dat missing on purpose
    }}
    out, notes = diagnose_t2._resolve_task_outputs(workdir, result)
    assert out["dos_dat"] == "dos.dat"
    assert any("backfilled 'dos_dat'" in n for n in notes), notes


def test_resolve_from_incomplete_result_json_scans_workdir(tmp_path: Path) -> None:
    """The original T2 bug: result.json was incomplete (no task_outputs),
    so we must reconstruct from workdir scan."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    for fname in ("scf.out", "bands.out", "bands.dat", "dos.dat"):
        _touch(workdir / fname)
    result = {"incomplete": True, "reason": "submit failed local audit"}
    out, notes = diagnose_t2._resolve_task_outputs(workdir, result)
    assert out == {
        "scf_out": "scf.out",
        "bands_out": "bands.out",
        "bands_data": "bands.dat",
        "dos_dat": "dos.dat",
    }
    assert any("reconstructing" in n for n in notes), notes


def test_resolve_incomplete_with_missing_files_reports_each(tmp_path: Path) -> None:
    """If a fallback file is also missing, note that — caller will exit
    with a clear error."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    _touch(workdir / "scf.out")  # only scf.out exists
    result = {"incomplete": True}
    out, notes = diagnose_t2._resolve_task_outputs(workdir, result)
    assert out == {"scf_out": "scf.out"}
    missing_keys = {"bands_out", "bands_data", "dos_dat"}
    flagged = {n for n in notes if "could not locate" in n}
    for key in missing_keys:
        assert any(repr(key) in n for n in flagged), \
            f"missing-fallback note for {key} not found: {flagged}"


# ─── bench-root error surfacing ─────────────────────────────────────────────

def test_bogus_bench_root_raises_systemexit(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        diagnose_t2._ensure_bench_imports(tmp_path / "definitely-not-here")
    msg = str(exc.value)
    assert "--bench-root" in msg


# ─── _format_table ──────────────────────────────────────────────────────────

def test_format_table_renders_columns_and_flags() -> None:
    """Make sure the printed table includes per-field scores, contribs,
    a total line, and a warning flag for sub-1.0 exact matches."""
    weights = {
        "band_gap_ev": 0.4,
        "vbm_k_label": 0.1,
    }
    gt = {
        "band_gap_ev": {"value": 1.5, "rule": "absolute", "tolerance": 0.1},
        "vbm_k_label": {"value": "GAMMA", "rule": "exact"},
    }
    extracted = {
        "band_gap_ev": 1.5,
        "vbm_k_label": "gamma",  # case mismatch — must flag
    }
    per_field = {"band_gap_ev": 1.0, "vbm_k_label": 0.0}
    total = (1.0 * 0.4 + 0.0 * 0.1) / 0.5  # = 0.8 normalized
    out = diagnose_t2._format_table(
        task_id="TEST",
        scoring_rule={"field_weights": weights},
        weights=weights, gt=gt, extracted=extracted,
        per_field=per_field, total=total,
    )
    assert "=== TEST ===" in out
    assert "band_gap_ev" in out
    assert "vbm_k_label" in out
    # Case-mismatch flag must surface
    assert "case" in out
    # Total line present
    assert "total" in out


# ─── _resolve_task_outputs: data files don't have to exist for resolution ──

def test_resolve_complete_result_does_not_require_files_on_disk(tmp_path: Path) -> None:
    """_resolve_task_outputs only inspects result.json keys; it doesn't
    verify the files exist (that's extract_t2_fields' job). Confirm we
    pass through the canonical 4 keys even when none of those files
    actually exist on disk."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    result = {"task_outputs": {
        "scf_out": "no_such.out",
        "bands_out": "also_missing.out",
        "bands_data": "nope.dat",
        "dos_dat": "neither.dat",
    }}
    out, notes = diagnose_t2._resolve_task_outputs(workdir, result)
    assert set(out.keys()) == set(result["task_outputs"].keys())
    assert notes == []
