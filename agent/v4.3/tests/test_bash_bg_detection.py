"""Regression test for BashTool's background-command detection.

The v2-polish bg-detection guard was over-eager: it flagged any `&` left
after stripping legitimate ampersand operators, including `&` characters
inside heredoc text bodies like `&CONTROL` (a Fortran namelist marker).
That made every QE input file written via `cat << EOF` get rejected,
forcing the LLM into chr(38)/sed/perl contortions and burning 30+ turns.

This test pins the corrected behaviour: only `&` followed by whitespace /
`;` / `|` / EOL is backgrounding; `&` followed by alphanumerics is text.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.tools.bash import _is_background  # noqa: E402
from agent.tools.bash import BashTool  # noqa: E402


@pytest.mark.parametrize("cmd,expected_bg", [
    # Sequence / pipe / redirect operators must not trip
    ("cmd && cmd2", False),
    ("cmd || cmd2", False),
    ("cmd 2>&1", False),
    ("cmd &> log.txt", False),
    ("cmd &>> log.txt", False),
    # Heredoc bodies with Fortran namelist markers must not trip
    ("cat << 'EOF'\n&CONTROL\nEOF", False),
    ("cat > vc.in << 'EOF'\n&CONTROL\n  calculation = 'vc-relax'\n/\n&SYSTEM\n/\nEOF",
     False),
    # Quoted strings containing `&` must not trip
    ("echo '&CONTROL'", False),
    ('printf "&CONTROL\\n"', False),
    # `&` glued to a letter is not bg
    ("printf %s &CONTROL", False),
    # Trailing `&` IS backgrounding
    ("cmd &", True),
    ("pw.x -in foo > bar.out &", True),
    ("pw.x ... 2>&1 &", True),
    ("cmd1 & cmd2", True),
    ("cmd &\necho done", True),
    # Always-background tokens
    ("nohup pw.x -in foo &", True),
    ("nohup pw.x -in foo", True),
    ("disown %1", True),
    ("setsid pw.x", True),
])
def test_is_background_truthtable(cmd, expected_bg):
    got, reason = _is_background(cmd)
    assert got is expected_bg, f"cmd={cmd!r} got={got} reason={reason!r}"


def test_cd_outside_workdir_forbidden(tmp_path):
    tool = BashTool(workdir=tmp_path)
    outside = tmp_path.parent

    r = tool.run(f"cd {outside}")

    assert r["exit_code"] == 1
    assert "outside workdir" in r["stderr"]
    assert r["cwd_after"] == str(tmp_path.resolve())


def test_cd_inside_workdir_persists(tmp_path):
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    tool = BashTool(workdir=tmp_path)

    r = tool.run("cd subdir")

    assert r["exit_code"] == 0
    assert r["cwd_after"] == str(subdir.resolve())
