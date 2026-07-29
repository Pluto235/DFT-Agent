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
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from .bash_guard import check_command
from .qe_input_lint import intercept_bash
from .qe_output_parse import is_pw_x_invocation as _is_pwx_for_diag
from .. import pw_wall_estimator


# FIX-15: harness wall awareness + bash-level timeout cap.
# The bench harness gives each solver subprocess a wall-clock budget
# (--timeout, default 14400s = 4h). If a single bash command's timeout
# exceeds that, Popen.communicate hangs until the harness SIGKILLs the
# entire solver — the bash tool never returns tool_result to the LLM,
# the transcript dies mid-turn, and FIX-7's timeout diagnostic (which
# only fires in the TimeoutExpired branch) is dead code. Capping every
# bash command's effective timeout to 80% of the harness wall guarantees
# the watchdog (GNU `timeout` coreutils) can always SIGTERM the child
# before the harness kills us, so we still produce a clean tool_result.
def _harness_wall_s() -> int:
    """Harness wall-clock budget (s) for this solver subprocess.

    Read at CALL time, not import time. The bench harness exports
    SOLVER_HARNESS_WALL_S before spawning the solver, but this module is
    imported (transitively, via solve.py) before the environment is fully
    wired; an import-time read froze the value at the 14400 default even
    when the harness ran an 8h (28800s) budget, so _BASH_CAP_S came out at
    11520s and chopped legitimate ~6h magnetic vc-relax runs (FIX-39, S-3).
    """
    return int(os.environ.get("SOLVER_HARNESS_WALL_S", "14400"))


def _bash_cap_s() -> int:
    """Per-command timeout ceiling = 80% of the harness wall.

    Leaves margin so the GNU `timeout` watchdog always fires before the
    harness SIGKILLs the whole solver. This is the SINGLE source of truth
    for the per-command cap; pw_wall_estimator no longer self-caps (FIX-39),
    so a big run gets its full estimate as long as it fits under this
    budget-scaled ceiling (23040s under an 8h budget, not a fixed 12000s).
    """
    return int(_harness_wall_s() * 0.8)


# QE / MPI executable markers. A process running one of these inside this
# run's workdir AFTER a timeout is an orphan worth reaping: mpirun can
# `setsid` pw.x into its own session, escaping the killpg() of the bash
# child's process group, so it survives the watchdog and contends for cores
# with any restart (the Fe3O4/LaMnO3 M3_T2 death-spiral, FIX-39 / S-3).
_QE_PROC_MARKERS = ("pw.x", "mpirun", "mpiexec", "dos.x", "bands.x",
                    "projwfc.x", "ph.x", "pp.x")


def _proc_matches_workdir(proc_cwd, cmdline: str, workdir) -> bool:
    """True if a process (cwd=`proc_cwd`, command=`cmdline`) is a QE/MPI
    process rooted inside `workdir` — i.e. an orphan from THIS run worth
    reaping. Scoped to workdir so a concurrent run (distinct workdir under
    runs_local/<ts>_<task>/) is NEVER touched."""
    if proc_cwd is None:
        return False
    try:
        Path(proc_cwd).resolve().relative_to(Path(workdir).resolve())
    except (ValueError, OSError):
        return False
    return any(m in cmdline for m in _QE_PROC_MARKERS)


def _rm_rf_targets_qe_restart(cmd: str, cwd) -> "str | None":
    """If `cmd` runs an `rm -r…` whose target holds QE restart state (a `*.save`
    directory with the converged density + wavefunctions, or such a dir itself),
    return the offending target token. Else None.

    Wiping it forces a vc-relax restart from the initial guess instead of a warm
    restart (FIX-27 anti-pattern; CuO M3_T1 2026-06-01 did it 4× → 8h timeout).
    `mv`-ing the dir aside is NOT matched, preserving the corruption escape."""
    cwd = Path(cwd)
    for seg in re.split(r"&&|\|\||;|\n", cmd):
        toks = seg.split()
        if not toks or toks[0] != "rm":
            continue
        flags = [t for t in toks[1:] if t.startswith("-")]
        if not any("r" in f.lower() for f in flags):  # need a recursive flag
            continue
        for t in toks[1:]:
            if t.startswith("-"):
                continue
            target = t.rstrip("/")
            if target.endswith("/*"):
                target = target[:-2]
            try:
                p = (Path(target) if target.startswith("/") else cwd / target).resolve()
            except Exception:
                continue
            if not p.is_dir():
                continue
            try:
                if p.name.endswith(".save"):
                    return t
                if any(c.name.endswith(".save") and c.is_dir() for c in p.iterdir()):
                    return t
            except OSError:
                continue
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

        # FIX-42 (S-1, FIX-27 hardening): refuse `rm -rf <outdir>` when the
        # target holds QE restart state (a *.save dir). Wiping it forces a
        # vc-relax restart from the initial guess → restart-from-scratch loop →
        # 8h timeout (CuO 2026-06-01 did this 4×). `mv` aside is not blocked.
        rm_hit = _rm_rf_targets_qe_restart(cmd, self.cwd)
        if rm_hit is not None:
            err = (
                f"[bash tool] refusing `rm -rf {rm_hit}` — it holds Quantum "
                f"ESPRESSO restart state (a *.save dir with the converged charge "
                f"density + wavefunctions). Deleting it forces the next pw.x to "
                f"restart from the initial guess, not a warm restart — the cause "
                f"of repeated vc-relax timeouts.\n"
                f"To resume a crashed/timed-out vc-relax: KEEP {rm_hit} and re-run "
                f"the SAME pw.x with `startingwfc='file'` + `startingpot='file'` in "
                f"&ELECTRONS (continues from the last ionic step + SCF iteration, "
                f"FIX-27). If it is genuinely corrupt (relax.out says 'cannot "
                f"read'), `mv {rm_hit} {rm_hit}.bad` aside instead of deleting."
            )
            self._log(cmd, exit_code=2, wall_s=0.0)
            return {"stdout": "", "stderr": err, "exit_code": 2, "wall_s": 0.0,
                    "cwd_after": str(self.cwd), "lint_diff": "", "blocked": True}

        # v3 hard rule: before any pw.x call, ensure the input file has the
        # three vc-relax convergence thresholds at least as tight as v3
        # defaults. The lint is a no-op for non-pw.x commands and for inputs
        # that aren't vc-relax. When patched, the diff is propagated to the
        # LLM via the bash tool result so it learns the expected shape.
        # Rule E (crystal_b data-row validation) can REJECT the invocation
        # outright when the bands.in path is malformed — pw.x would still
        # run but produce useless output (Rb-style fermi-slice failure),
        # so we surface the reason and refuse to spawn the subprocess.
        lint = intercept_bash(cmd, self.cwd, log_path=self.workdir / "lint.log")
        if lint.rejected:
            fname = lint.file_path.name if lint.file_path else "<unknown>"
            err = (
                f"[qe_input_lint] rejected pw.x invocation — input file "
                f"`{fname}` has a fatal physics/syntax error.\n"
                f"\n--- qe_input_lint reject reason ---\n"
                f"{lint.reject_reason}\n"
                f"---\n"
                f"pw.x was NOT run. Fix the input file and re-issue the command."
            )
            self._log(cmd, exit_code=2, wall_s=0.0)
            return {"stdout": "", "stderr": err, "exit_code": 2,
                    "wall_s": 0.0, "cwd_after": str(self.cwd),
                    "lint_diff": "", "blocked": True}
        lint_diff = lint.diff_text if lint.patched else ""

        # === FIX-6: pw.x wall-time estimator ===
        # LLM 给的 timeout_s 严重低估时把它抬到估算值，避免 wallclock 截断。
        # 估算器从 .in 文件读 nat / n_kpoints / ecutwfc / calculation，给出
        # 含 1.5× safety 的 wall_s。任何 parse 失败 → est=None → 不抬。
        #
        # FIX-15 (2026-05-23) + FIX-39 (S-3, 2026-05-31): the estimator now
        # returns the RAW estimate (no fixed 12000s self-cap); BashTool caps
        # the effective timeout to _bash_cap_s() = 80% of the harness wall,
        # read at CALL time. That single ceiling both prevents the WO3 49h
        # Popen-hang AND scales with the real budget (23040s under 8h), so a
        # legitimate ~6h big magnetic-cell vc-relax is no longer chopped to
        # 12000s → killed → restarted → orphaned (the Fe3O4/LaMnO3 0.000).
        cap = _bash_cap_s()
        harness_wall = _harness_wall_s()
        estimator_note = ""
        try:
            est_s, est_dbg = pw_wall_estimator.estimate_for_command(cmd, self.cwd)
        except Exception:
            est_s, est_dbg = None, {}
        if est_s is not None and est_s > timeout:
            estimator_note = (
                f"[pw_wall_estimator] timeout_s raised {timeout}s → {est_s}s "
                f"based on n_atoms={est_dbg.get('n_atoms')}, "
                f"n_kpoints={est_dbg.get('n_kpoints')}, "
                f"ecutwfc={est_dbg.get('ecutwfc')} Ry, "
                f"calculation={est_dbg.get('calculation')}."
            )
            timeout = est_s

        # Cap any timeout (LLM-given OR estimator-raised) at 80% of the harness
        # wall so the watchdog always SIGTERMs before the harness SIGKILLs us.
        if timeout > cap:
            estimator_note = (
                (estimator_note + "\n" if estimator_note else "")
                + f"[bash tool] timeout_s {timeout}s capped to {cap}s "
                f"(80% of {harness_wall}s harness wall — leave margin so "
                f"watchdog fires before harness SIGKILL)."
            )
            timeout = cap

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
        #
        # FIX-15: wrap with GNU `timeout` so the OS layer can always kill
        # the child even if Popen.communicate hangs (observed when grandchild
        # mpirun → pw.x detaches via setsid and holds pipe FDs open). Exit
        # code 124 means timeout fired SIGTERM; 137 means SIGKILL after
        # --kill-after grace. We give Popen.communicate an extra 60s window
        # past `timeout` so the OS layer always wins the race.
        t0 = time.monotonic()
        proc = subprocess.Popen(
            ["timeout", "--signal=TERM", "--kill-after=30s",
             f"{timeout}s", "bash", "-c", cmd],
            cwd=str(self.cwd),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace",
            start_new_session=True,
        )
        timed_out_by_os = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout + 60)
            exit_code = proc.returncode
            # GNU timeout exits 124 when it sent SIGTERM (and the child obeyed
            # or was SIGKILLed by --kill-after). Treat as our timeout path.
            if exit_code in (124, 137):
                timed_out_by_os = True
        except subprocess.TimeoutExpired:
            # OS layer didn't fire — extremely rare (would mean `timeout`
            # itself hung or got SIGSTOP'd). Fall back to manual killpg.
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
            timed_out_by_os = True
            exit_code = -1

        # === FIX-7: timeout diagnostic ===
        # pw.x 被 wallclock 截时，LLM 历史上常误诊为 "pw.x 路径/输入错误"
        # 转而调 CPU build 或重写 .in（0519 WO3 即典型）。给一条强诊断把
        # 路径锁定到 "更大 timeout × 3 重发完全相同命令"。非 pw.x 走原短消息。
        #
        # FIX-7 cap-edge (2026-05-25): 当 timeout 已撞 _BASH_CAP_S，
        # min(timeout*3, cap) == timeout，原 "用 cap 重发" 建议会让 LLM
        # 原地打转再烧一轮 cap。已达 cap 时切到 "缩问题规模 / declare
        # incomplete" 分支，明确禁止重发同命令。
        if timed_out_by_os:
            # FIX-39 (S-3): killpg above only reaches the bash child's process
            # group; mpirun may have setsid'd pw.x into its own session. Reap
            # any QE/mpirun orphan still rooted in THIS workdir so it can't
            # contend for cores with a restart (the Fe3O4/LaMnO3 death-spiral).
            self._reap_stale_qe_processes()
            if _is_pwx_for_diag(cmd):
                preface = (
                    f"\n[bash tool] pw.x 因 timeout_s={timeout}s 被强制终止 "
                    f"(GNU timeout SIGTERM → SIGKILL after 30s grace)。\n"
                    f"⚠ 这不是 pw.x 命令失败、不是输入文件 / pw.x 路径 / GPU "
                    f"wrapper 出问题；进程在 timeout 那一刻仍在正常推进 SCF "
                    f"迭代，是被 wallclock 提前打断。\n"
                )
                if timeout >= cap:
                    advice = (
                        f"⛔ 当前 timeout_s 已达 bash cap "
                        f"({cap}s ≈ 80% harness wall {harness_wall}s) "
                        f"—— 继续抬 timeout 无空间，重发完全相同的命令只会再烧"
                        f"一轮 cap。下一步必须切策略：\n"
                        f"  (a) 缩问题规模：ecutwfc -10 Ry（保 ecutrho/ecutwfc "
                        f"比例≥10×）、k_mesh 各方向减半、或拆 supercell；\n"
                        f"  (b) SCF 收敛慢（charge sloshing / d-electron 不稳）："
                        f"加 mixing_beta=0.3、electron_maxstep=200；\n"
                        f"  (c) vc-relax 在 ionic step 1 内卡死：先切 "
                        f"calculation='scf' 验证单点 SCF 能在 cap 内收敛，再回"
                        f"vc-relax；若是 magnetic 材料，starting_magnetization "
                        f"减半或改 nspin=1 验证；\n"
                        f"  (d) 上述都不可行 → 提交 incomplete，reason 注明 "
                        f"wall-time exhausted at cap={cap}s。\n"
                        f"不要修改 pw.x 路径、不要切 CPU build、不要绕到 mock。"
                    )
                else:
                    advice = (
                        f"建议下一步：用 timeout_s = {min(timeout * 3, cap)}s"
                        f"（当前值 × 3，cap 在 {cap}s）重发完全相同的命令；"
                        f"不要修改 input 文件、不要切换 pw.x 路径、不要降 ecutwfc / "
                        f"缩 k 网格作为绕路。"
                    )
                diag = preface + advice
                if estimator_note:
                    diag += f"\n（{estimator_note}）"
                stderr = (stderr or "") + diag
            else:
                stderr = (stderr or "") + (
                    f"\n[bash tool] timeout after {timeout}s (GNU timeout)"
                )
            exit_code = -1
        wall_s = time.monotonic() - t0
        self._log(cmd, exit_code=exit_code, wall_s=wall_s)

        # PR-B: post-bash hook for pw.x invocations. Parse the redirected
        # output into structured facts; write a JSONL row for the verifier
        # and append a short advisory line to stdout for the executor LLM.
        # All exceptions are swallowed — never break the bash tool on parse
        # failure (the LLM still sees raw stdout).
        qe_facts_advisory = ""
        try:
            from agent.tools import qe_output_parse as _qe_out
            if _qe_out.is_pw_x_invocation(cmd):
                out_path = _qe_out.detect_output_path(cmd, self.cwd)
                if out_path is not None:
                    facts = _qe_out.extract_facts(out_path)
                    _qe_out.append_facts_row(
                        self.workdir, command=cmd,
                        output_path=out_path, facts=facts,
                    )
                    qe_facts_advisory = _qe_out.render_advisory(facts)
        except Exception:
            qe_facts_advisory = ""
        if qe_facts_advisory:
            stdout = (stdout or "") + "\n" + qe_facts_advisory + "\n"

        # FIX-6: 把 estimator_note 顶到 stdout 最前面，让 LLM 直接看到下次该
        # 设多大 timeout（不只是 stderr 报 timeout 时才告诉它）。
        if estimator_note:
            stdout = estimator_note + "\n" + (stdout or "")

        return {"stdout": stdout or "", "stderr": stderr or "", "exit_code": exit_code,
                "wall_s": wall_s, "cwd_after": str(self.cwd), "lint_diff": lint_diff}

    def _reap_stale_qe_processes(self) -> int:
        """Best-effort SIGKILL of orphaned QE/mpirun processes whose cwd is
        inside this run's workdir. Called after a wallclock timeout, because
        an mpirun-setsid'd pw.x escapes the killpg of the bash process group.
        Linux /proc only; fully exception-guarded — never breaks the tool."""
        import glob
        killed = 0
        try:
            my_pid = os.getpid()
            for proc_dir in glob.glob("/proc/[0-9]*"):
                try:
                    pid = int(os.path.basename(proc_dir))
                except ValueError:
                    continue
                if pid == my_pid:
                    continue
                try:
                    cwd = Path(os.readlink(f"{proc_dir}/cwd"))
                except OSError:
                    continue  # process gone or cwd unreadable
                try:
                    with open(f"{proc_dir}/cmdline", "rb") as fh:
                        cmdline = fh.read().replace(b"\x00", b" ").decode(
                            "utf-8", "replace")
                except OSError:
                    continue
                if _proc_matches_workdir(cwd, cmdline, self.workdir):
                    try:
                        os.kill(pid, signal.SIGKILL)
                        killed += 1
                    except OSError:
                        pass
        except Exception:
            pass
        return killed

    def _log(self, cmd: str, exit_code: int, wall_s: float) -> None:
        # bench audit greps bash.log for pw.x, network commands, etc.
        # Format matches the v1 reference: timestamped lines, one per call.
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = f"[{ts}] cwd={self.cwd} rc={exit_code} wall={wall_s:.2f}s\n  $ {cmd}\n"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line)
