"""Bash tool for the agent. Single-shot subprocess per call, with cwd
persistence semantics (per dft-agent-bench spec §11):

  - `cd <dir>`            on its own line → persists; tracked cwd updates
  - `cd <dir> && cmd`     → does NOT persist; cwd reverts after the call

Every command + exit code is appended to <workdir>/bash.log so the bench
audit can grep for pw.x / mpirun and detect forbidden network commands.

Background commands (`&` / nohup / disown / setsid) are rejected before
spawning a subprocess: backgrounded pw.x cannot be reliably killed when
the wallclock cap fires, and the prompt forbids them. On timeout we
killpg() the child's process group so mpirun/pw.x don't orphan onto init.
"""
from __future__ import annotations
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from .bash_guard import check_command
from .qe_input_lint import intercept_bash


# --- QE-call detection (source-of-truth for dashboard 调用次数) ---------------
# The web dashboard counts QE binary calls from <workdir>/qe_calls.jsonl
# (web/backend/services/qe_stats.py::_read_qe_trace, "Path 0"). Its bash.log
# fallback parser does NOT match v5.1's bash.log header format, so without
# this trace every v4.2/v5.1 run reported call_count=0 even when QE ran.
# Detection mirrors qe_stats `_segment_invokes_qe` so counts agree.
_QE_BINS = ("pw.x", "dos.x", "pp.x", "projwfc.x", "bands.x", "ph.x")
_MPI_LAUNCHERS = ("mpirun", "mpiexec", "srun")
_PRE_TOKENS = ("time", "nohup", "exec", "sudo")
_QUERY_FLAGS = ("--version", "--help", "-version", "-help", "-V", "-h")
_SEG_SPLIT_RE = re.compile(r"&&|\|\||;|\||&|\n")


def _segment_qe_binary(seg: str) -> str | None:
    """If this shell segment's main command actually launches a QE binary,
    return its basename (e.g. 'pw.x'); else None. Mirrors qe_stats."""
    tokens = seg.strip().split()
    main = None
    args: list[str] = []
    for tok in tokens:
        # leading env-var assignment FOO=bar (main not yet seen) → skip
        if main is None and "=" in tok and not tok.startswith("-"):
            head = tok.split("=", 1)[0]
            if head and head[0].isalpha() and all(c.isalnum() or c == "_" for c in head):
                continue
        if main is None and tok in _PRE_TOKENS:
            continue
        if main is None:
            main = tok
        else:
            args.append(tok)
    if main is None:
        return None
    main_base = main.rsplit("/", 1)[-1]
    if main_base in _QE_BINS:
        if any(a in _QUERY_FLAGS for a in args):
            return None
        return main_base
    if main_base in _MPI_LAUNCHERS:
        for i, a in enumerate(args):
            a_base = a.rsplit("/", 1)[-1]
            if a_base in _QE_BINS:
                if any(qa in _QUERY_FLAGS for qa in args[i + 1:]):
                    return None
                return a_base
    return None


def _qe_binary_invoked(cmd: str) -> str | None:
    """First QE binary actually launched anywhere in `cmd`, or None."""
    for seg in _SEG_SPLIT_RE.split(cmd):
        b = _segment_qe_binary(seg)
        if b is not None:
            return b
    return None


# Single-line `cd <dir>` (no &&, no semicolons). Allows trailing whitespace.
_CD_ONLY = re.compile(r"^\s*cd\s+(\S+)\s*$")

# Patterns that legitimately contain `&` and must NOT trigger the
# background-command guard:
#   `&&`            sequence operator
#   `||`            sequence operator (no `&` but kept for symmetry)
#   `\d+>&\d+`      numeric fd dup, e.g. `2>&1`
#   `>&\d+`         redirect stdout to fd, e.g. `>&2`
#   `&>` / `&>>`    combined stdout+stderr redirect to file
_LEGIT_AMP = re.compile(r"&&|\|\||\d+>&\d+|>&\d+|&>>?")

# Always-background tokens, regardless of `&` placement.
_BG_TOKEN = re.compile(r"\b(?:nohup|disown|setsid)\b")

# Backgrounding `&` requires `&` followed by whitespace, `;`, `|`, or
# end-of-line. A `&` followed immediately by an alphanumeric character
# (e.g. `&CONTROL` inside a Fortran heredoc) is just text in a quoted
# body — bash will not background on it. Without this distinction the
# guard rejects every `cat << EOF`-style heredoc that contains a QE
# input file, forcing the LLM into chr(38)-style contortions and
# burning 30+ turns on input writing alone (observed in Cu/SiO2 v3
# runs at 2026-05-10).
_BG_AMP_RE = re.compile(r"&(?=[\s;|]|$)", re.MULTILINE)


def _is_background(cmd: str) -> tuple[bool, str]:
    """Return (is_background, reason). Strip legitimate `&`-containing
    operators first, then flag any *bg-shaped* `&` (followed by whitespace,
    `;`, `|`, or EOL) or always-background keyword.

    Test cases:
        cmd && cmd2          → False (sequence)
        cmd 2>&1             → False (fd dup)
        cmd &> log.txt       → False (combined redirect)
        cat <<EOF\\n&CONTROL  → False (& inside heredoc text)
        cmd &                → True  (trailing &)
        pw.x ... 2>&1 &      → True  (trailing & after fd dup)
        cmd1 & cmd2          → True  (& separator)
        nohup pw.x           → True  (nohup keyword)
    """
    if _BG_TOKEN.search(cmd):
        return True, "uses nohup / disown / setsid"
    stripped = _LEGIT_AMP.sub(" ", cmd)
    if _BG_AMP_RE.search(stripped):
        return True, "trailing `&` backgrounds the command"
    return False, ""


class BashTool:
    def __init__(self, workdir: Path, log_path: Path | None = None,
                 default_timeout_s: int = 1800):
        self.workdir = Path(workdir).resolve()
        self.cwd = self.workdir
        self.log_path = Path(log_path) if log_path else self.workdir / "bash.log"
        self.default_timeout_s = default_timeout_s

    def run(self, command: str, timeout_s: int | None = None) -> dict:
        timeout = timeout_s if timeout_s is not None else self.default_timeout_s
        cmd = command.rstrip("\n")

        # Detect a pure `cd` so we can persist it without spawning a shell
        # (also so the audit doesn't think a `cd` alone is a real command).
        m = _CD_ONLY.match(cmd)
        if m:
            target = m.group(1)
            new_cwd = (self.cwd / target).resolve() if not target.startswith("/") else Path(target).resolve()
            try:
                new_cwd.relative_to(self.workdir)
            except ValueError:
                self._log(cmd, exit_code=1, wall_s=0.0)
                return {"stdout": "", "stderr": f"cd: {target}: outside workdir is forbidden",
                        "exit_code": 1, "wall_s": 0.0, "cwd_after": str(self.cwd),
                        "lint_diff": ""}
            if not new_cwd.is_dir():
                self._log(cmd, exit_code=1, wall_s=0.0)
                return {"stdout": "", "stderr": f"cd: {target}: No such directory",
                        "exit_code": 1, "wall_s": 0.0, "cwd_after": str(self.cwd),
                        "lint_diff": ""}
            self.cwd = new_cwd
            self._log(cmd, exit_code=0, wall_s=0.0)
            return {"stdout": "", "stderr": "", "exit_code": 0,
                    "wall_s": 0.0, "cwd_after": str(self.cwd), "lint_diff": ""}

        # Reject background commands BEFORE spawning. Returning rc=2 with a
        # clear stderr lets the LLM rewrite in-loop instead of silently
        # losing pw.x to a wallclock kill (see prompt rule §4).
        is_bg, reason = _is_background(cmd)
        if is_bg:
            err = (
                f"[bash tool] background commands forbidden ({reason}). "
                "Run pw.x in foreground; pass timeout_s for wallclock cap."
            )
            self._log(cmd, exit_code=2, wall_s=0.0)
            return {"stdout": "", "stderr": err, "exit_code": 2,
                    "wall_s": 0.0, "cwd_after": str(self.cwd), "lint_diff": ""}

        # bash_guard: whitelist + blacklist + path + python content checks.
        guard_reason = check_command(cmd, self.cwd)
        if guard_reason is not None:
            self._log(cmd, exit_code=2, wall_s=0.0)
            return {"stdout": "", "stderr": f"[bash_guard] {guard_reason}",
                    "exit_code": 2, "wall_s": 0.0,
                    "cwd_after": str(self.cwd), "lint_diff": "", "blocked": True}

        # v3 hard rule: before any pw.x call, ensure the input file has the
        # three vc-relax convergence thresholds at least as tight as v3
        # defaults. The lint is a no-op for non-pw.x commands and for inputs
        # that aren't vc-relax. When patched, the diff is propagated to the
        # LLM via the bash tool result so it learns the expected shape.
        lint = intercept_bash(cmd, self.cwd, log_path=self.workdir / "lint.log",
                              workdir=self.workdir)
        lint_diff = "\n\n".join(filter(None, [
            lint.diff_text if lint.patched else "",
            lint.advisory_text,
        ]))

        # errors="replace" because QE / mpirun / NVIDIA hooks occasionally
        # emit non-UTF-8 bytes (e.g. degree symbols, latin-1 chars). With
        # plain text=True a single bad byte throws UnicodeDecodeError out
        # of subprocess.run and crashes the whole solve loop — losing all
        # prior context. Replace bad bytes silently so the LLM still gets
        # a usable transcript of stdout/stderr to reason about.
        #
        # start_new_session=True puts the child (bash) in its own process
        # group so we can killpg() the entire QE/mpirun process tree on
        # timeout — subprocess.run's default kill only signals the immediate
        # child and orphans pw.x onto init.
        t0 = time.monotonic()
        proc = subprocess.Popen(
            ["bash", "-c", cmd],
            cwd=str(self.cwd),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace",
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = proc.pid
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    stdout, stderr = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
            stderr = (stderr or "") + f"\n[bash tool] timeout after {timeout}s; pgid {pgid} killed"
            exit_code = -1
        wall_s = time.monotonic() - t0
        self._log(cmd, exit_code=exit_code, wall_s=wall_s)
        # Only commands that reach here actually executed (cd / background /
        # guard-blocked paths returned earlier). Record real QE binary
        # launches so the dashboard's preferred trace source has data.
        qe_bin = _qe_binary_invoked(cmd)
        if qe_bin is not None:
            self._log_qe_call(cmd, qe_bin, exit_code=exit_code, wall_s=wall_s)
        return {"stdout": stdout or "", "stderr": stderr or "", "exit_code": exit_code,
                "wall_s": wall_s, "cwd_after": str(self.cwd), "lint_diff": lint_diff}

    def _log(self, cmd: str, exit_code: int, wall_s: float) -> None:
        # bench audit greps bash.log for pw.x, network commands, etc.
        # Format matches the v1 reference: timestamped lines, one per call.
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = f"[{ts}] cwd={self.cwd} rc={exit_code} wall={wall_s:.2f}s\n  $ {cmd}\n"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line)

    def _log_qe_call(self, cmd: str, binary: str, exit_code: int,
                     wall_s: float) -> None:
        """Append one JSON line per executed QE binary call to
        <workdir>/qe_calls.jsonl. The web dashboard reads `wall_sec` and
        counts lines (qe_stats `_read_qe_trace`); the other fields are
        diagnostic. Lives next to bash.log so both share the workdir."""
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "binary": binary,
            "cmd": cmd,
            "exit_code": exit_code,
            "wall_sec": round(float(wall_s), 3),
        }
        path = self.log_path.parent / "qe_calls.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
