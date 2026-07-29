"""Regression tests for the local-audit step in solve.py.

The bench's harness audit (audit.py:_is_log_output) exempts pure-data
task_outputs (bands.dat / dos.dat with `*_data` or `*_dat` key suffixes)
from the JOB DONE. marker check. solve.py's _audit_task_outputs must
mirror that exemption — otherwise every T2 (bands_dos) submission is
rejected locally before it can reach bench scoring (observed in
/tmp/smoke_subsub_t2/...AlN_bands_dos prior to this fix).

These tests pin:
  - vc_relax_out / scf_out / bands_out / dos_out → JOB DONE required
  - bands_data / dos_dat → JOB DONE skipped, but size/path checks intact
  - unknown suffix → default to strict (JOB DONE required)
  - absolute paths / escapes / sub-1024B files / missing files → still rejected
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from solve import _audit_task_outputs, _is_log_output  # noqa: E402


# ───────────────────────── _is_log_output unit tests ─────────────────────────

@pytest.mark.parametrize("key,expected", [
    # *_out → log (need JOB DONE)
    ("vc_relax_out", True),
    ("scf_out", True),
    ("bands_out", True),
    ("dos_out", True),
    ("nscf_out", True),
    # *_data / *_dat → data file (skip JOB DONE)
    ("bands_data", False),
    ("dos_dat", False),
    ("phonon_data", False),  # forward-compatible: any *_data is data
    # unknown suffix → conservative (treat as log)
    ("phonon_xyz", True),
    ("result_json", True),
    ("anything_else", True),
])
def test_is_log_output_table(key: str, expected: bool) -> None:
    assert _is_log_output(key) is expected, (
        f"key={key!r} got={_is_log_output(key)} expected={expected}"
    )


# ───────────────────────── _audit_task_outputs end-to-end ─────────────────────

def _make_pw_log(path: Path, size_bytes: int = 4096, with_marker: bool = True) -> None:
    """Write a fake QE-style .out file. If with_marker, the tail contains
    'JOB DONE.'; otherwise it contains only filler so the marker check fails.
    """
    filler = "PWSCF v.7.4 boring filler line\n" * 200
    body = filler[:size_bytes]
    if with_marker:
        body = body[: -len("JOB DONE.\n")] + "JOB DONE.\n"
    path.write_text(body, encoding="utf-8")


def _make_data_file(path: Path, size_bytes: int = 4096) -> None:
    """Write a fake bands.dat / dos.dat — pure ASCII numeric data, no JOB DONE."""
    line = "  0.0000   1.2345\n"
    n = (size_bytes // len(line)) + 1
    path.write_text(line * n, encoding="utf-8")


# ── happy paths ─────────────────────────────────────────────────────────────

def test_vc_relax_out_with_job_done_passes(tmp_path: Path) -> None:
    """T1 happy path — vc_relax_out must keep working unchanged."""
    p = tmp_path / "vc.out"
    _make_pw_log(p, with_marker=True)
    errs = _audit_task_outputs({"vc_relax_out": "vc.out"}, tmp_path)
    assert errs == [], errs


def test_t2_all_four_outputs_pass(tmp_path: Path) -> None:
    """T2 happy path — scf_out/bands_out/dos_out need JOB DONE;
    bands_data/dos_dat must skip it. Mirrors the M1_T2 submission shape."""
    _make_pw_log(tmp_path / "scf.out", with_marker=True)
    _make_pw_log(tmp_path / "bands.out", with_marker=True)
    _make_pw_log(tmp_path / "dos.out", with_marker=True)
    _make_data_file(tmp_path / "bands.dat")
    _make_data_file(tmp_path / "dos.dat")
    errs = _audit_task_outputs({
        "scf_out": "scf.out",
        "bands_out": "bands.out",
        "dos_out": "dos.out",
        "bands_data": "bands.dat",
        "dos_dat": "dos.dat",
    }, tmp_path)
    assert errs == [], errs


# ── data-file specific behaviour ────────────────────────────────────────────

def test_bands_data_without_job_done_passes(tmp_path: Path) -> None:
    """The core T2-blocking bug: pure-data file with no JOB DONE. must pass."""
    p = tmp_path / "bands.dat"
    _make_data_file(p)
    assert "JOB DONE." not in p.read_text()
    errs = _audit_task_outputs({"bands_data": "bands.dat"}, tmp_path)
    assert errs == [], errs


def test_dos_dat_without_job_done_passes(tmp_path: Path) -> None:
    p = tmp_path / "dos.dat"
    _make_data_file(p)
    assert "JOB DONE." not in p.read_text()
    errs = _audit_task_outputs({"dos_dat": "dos.dat"}, tmp_path)
    assert errs == [], errs


def test_bands_data_under_1024_bytes_still_rejected(tmp_path: Path) -> None:
    """Size check is independent of JOB DONE check — data files <1024B
    are still rejected."""
    p = tmp_path / "bands.dat"
    p.write_text("only 14 bytes\n", encoding="utf-8")
    errs = _audit_task_outputs({"bands_data": "bands.dat"}, tmp_path)
    assert errs and "too small" in errs[0]


# ── log-shaped keys still require JOB DONE. ─────────────────────────────────

@pytest.mark.parametrize("key", ["vc_relax_out", "scf_out", "bands_out", "dos_out"])
def test_log_key_without_job_done_rejected(tmp_path: Path, key: str) -> None:
    """A log-output key missing JOB DONE. must still fail (T1 zero regression)."""
    p = tmp_path / "x.out"
    _make_pw_log(p, with_marker=False)
    assert "JOB DONE." not in p.read_text()
    errs = _audit_task_outputs({key: "x.out"}, tmp_path)
    assert errs and "JOB DONE." in errs[0], errs


def test_unknown_suffix_defaults_to_strict(tmp_path: Path) -> None:
    """Forward-compat: a key we have not seen before must default to
    requiring JOB DONE. (conservative)."""
    p = tmp_path / "blob"
    _make_data_file(p)  # no JOB DONE.
    errs = _audit_task_outputs({"phonon_xyz": "blob"}, tmp_path)
    assert errs and "JOB DONE." in errs[0], errs


# ── path / file integrity checks unchanged ──────────────────────────────────

def test_absolute_path_rejected(tmp_path: Path) -> None:
    _make_data_file(tmp_path / "bands.dat")
    errs = _audit_task_outputs(
        {"bands_data": str((tmp_path / "bands.dat").resolve())},
        tmp_path,
    )
    assert errs and "absolute path forbidden" in errs[0]


def test_path_escaping_workdir_rejected(tmp_path: Path) -> None:
    """A path that resolves outside workdir must be rejected, even for
    a data-suffix key."""
    outside_dir = tmp_path.parent / "outside-workdir"
    outside_dir.mkdir(exist_ok=True)
    _make_data_file(outside_dir / "bands.dat")
    errs = _audit_task_outputs(
        {"bands_data": "../outside-workdir/bands.dat"},
        tmp_path,
    )
    assert errs and "escapes workdir" in errs[0]


def test_missing_file_rejected(tmp_path: Path) -> None:
    errs = _audit_task_outputs({"bands_data": "missing.dat"}, tmp_path)
    assert errs and "not a regular file" in errs[0]


def test_empty_string_path_rejected(tmp_path: Path) -> None:
    errs = _audit_task_outputs({"bands_data": ""}, tmp_path)
    assert errs and "non-empty string" in errs[0]
