"""ProgressLedger — Magentic-One-style turn-by-turn stuck detector.

Every tool call appends a TurnRow. Seven heuristics (S1-S7) decide if the
agent is "stuck" (repeating, erroring, no progress). On trigger the hook
layer prepends a system-style nudge into the conversation:

    [ledger] You appear stuck (signal=S3: no verify_run ok=true in 10 min).
    Choices: (a) delegate to convergence_fixer subagent ...
    DO NOT repeat the same tool args.

The ledger ALSO emits a single `<workdir>/stuck.log` row for post-mortem.

Design rule: the ledger only DETECTS; the hook DECIDES what to inject.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ─────────────────────────────────────────────────────────────────────
# Constants — windows + thresholds for S1..S7
# ─────────────────────────────────────────────────────────────────────

S1_WINDOW = 6           # turns
S1_THRESHOLD = 3
S2_WINDOW = 8
S2_THRESHOLD = 6
S3_WALLCLOCK_S = 600.0
S4_WINDOW = 10
S4_THRESHOLD = 3
S5_WINDOW = 4
S5_THRESHOLD = 3
S6_WINDOW = 15
S7_TOKEN_FRACTION = 0.70

# Phrases that betray "I'm stuck in retry loop" mindset
_S5_PATTERNS = re.compile(
    r"(let me (?:try again|retry)|"
    r"still (?:not|isn['’]t|hasn['’]t)|"
    r"once more|"
    r"same (?:error|issue|problem) (?:again|persist)|"
    r"keeps? failing|"
    r"i (?:keep|continue) seeing)",
    re.IGNORECASE,
)

# verify_run emits "verdict: pass" as the first line of its content text
# (VerifyReport.to_text). Match it so has_verify_ok survives SDK serialization.
_VERDICT_PASS_RE = re.compile(r"verdict:\s*pass\b", re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────

def _hash(payload: Any) -> str:
    """Stable short hash of any JSON-serializable payload."""
    try:
        s = json.dumps(payload, sort_keys=True, default=str)
    except Exception:
        s = repr(payload)
    return hashlib.sha1(s.encode("utf-8", errors="replace")).hexdigest()[:12]


@dataclass
class TurnRow:
    turn: int
    tool: str
    tool_use_id: str
    payload_hash: str
    output_hash: str
    error: bool
    has_verify_ok: bool   # True iff this turn was verify_run returning ok
    ts: float
    bash_command: str | None = None  # canonical bash command (cd-stripped) for S4
    assistant_text: str | None = None  # most recent assistant utterance for S5
    calc_id: str | None = None         # extracted from tool_input or tool_response


@dataclass
class StuckSignal:
    signal: str           # "S1".."S7"
    reason: str           # one-line human-readable trigger
    suggestions: list[str] = field(default_factory=list)

    def render(self, recent_calc_ids: list[str] | None = None) -> str:
        cids = ", ".join(recent_calc_ids) if recent_calc_ids else "<none>"
        lines = [
            f"[ledger] You appear stuck (signal={self.signal}: {self.reason}).",
            f"Recent calc_ids: {cids}",
            "Choices:",
            *(f"  ({chr(ord('a') + i)}) {s}" for i, s in enumerate(self.suggestions)),
            "DO NOT repeat the same tool args.",
        ]
        return "\n".join(lines)


_BASH_CD_PREFIX = re.compile(r"^\s*cd\s+\S+\s*(?:&&|;)\s*", re.MULTILINE)


def _canonical_bash(cmd: str) -> str:
    """Strip a leading `cd dir && ` so re-runs from different cwd don't fool S4."""
    return _BASH_CD_PREFIX.sub("", cmd).strip()


# ─────────────────────────────────────────────────────────────────────
# ProgressLedger
# ─────────────────────────────────────────────────────────────────────

class ProgressLedger:
    """In-memory ledger of tool calls; stuck detector; structured logger."""

    def __init__(self, *, workdir: Path | None = None,
                 max_input_tokens: int = 200_000,
                 wall_budget_s: float | None = None):
        self.rows: list[TurnRow] = []
        self._turn = 0
        self.workdir = workdir
        self.stuck_log = (workdir / "stuck.log") if workdir else None
        self.max_input_tokens = max_input_tokens
        # A3: S3 (no-progress wallclock) scales with the harness budget so a
        # single legitimately-long pw.x step on a large cell doesn't trip a
        # false stuck-nudge. Falls back to the 600s default when budget unknown.
        self.s3_wallclock_s = (max(S3_WALLCLOCK_S, 0.2 * wall_budget_s)
                               if wall_budget_s else S3_WALLCLOCK_S)
        self.input_tokens_used = 0
        self.fired_signals: list[StuckSignal] = []
        self._latest_assistant_text: str | None = None

    # ---- record API --------------------------------------------------------

    def record_assistant_text(self, text: str) -> None:
        """Latest LLM message — used by S5 pattern matcher."""
        self._latest_assistant_text = text[-4000:] if text else None

    def record_token_usage(self, input_tokens: int) -> None:
        """Update running token total — used by S7."""
        self.input_tokens_used = max(self.input_tokens_used, int(input_tokens))

    def record_tool_use(
        self,
        *,
        tool: str,
        tool_use_id: str,
        tool_input: dict,
        tool_response: Any,
        error: bool,
        text: str | None = None,
    ) -> TurnRow:
        self._turn += 1

        # Special-case bash cmd extraction (works for built-in Bash and our
        # mcp__dft_qe__bash). Used by S4.
        bash_cmd: str | None = None
        if tool == "Bash" or tool.endswith("__bash"):
            cmd = (tool_input or {}).get("command")
            if isinstance(cmd, str):
                bash_cmd = _canonical_bash(cmd)

        # Detect verify_run ok responses. The SDK strips custom dict keys
        # (e.g. `verdict`) before the hook sees the response, so the primary
        # signal is the content text ("verdict: pass" — see VerifyReport.to_text).
        # The dict check is a fallback for direct unit tests / future SDKs.
        has_verify_ok = False
        if tool.endswith("__verify_run"):
            if text and _VERDICT_PASS_RE.search(text):
                has_verify_ok = True
            elif isinstance(tool_response, dict) and tool_response.get("verdict") == "pass":
                has_verify_ok = True

        # Extract calc_id from input or response (used by recent_calc_ids())
        calc_id = None
        if isinstance(tool_input, dict):
            calc_id = tool_input.get("calc_id")
        if not calc_id and isinstance(tool_response, dict):
            calc_id = tool_response.get("calc_id")

        row = TurnRow(
            turn=self._turn,
            tool=tool,
            tool_use_id=tool_use_id,
            payload_hash=_hash(tool_input),
            output_hash=_hash(tool_response),
            error=bool(error),
            has_verify_ok=has_verify_ok,
            ts=time.time(),
            bash_command=bash_cmd,
            assistant_text=self._latest_assistant_text,
            calc_id=calc_id if isinstance(calc_id, str) else None,
        )
        self.rows.append(row)
        # Reset latest text after attaching — same utterance shouldn't fool S5 forever
        self._latest_assistant_text = None
        return row

    # ---- stuck detection ---------------------------------------------------

    def check_stuck(self) -> StuckSignal | None:
        """Return the first triggered signal, or None.

        Evaluation order is S1..S7 — earlier signals are cheaper to verify
        and more actionable. Once a signal has fired in this session we
        won't re-fire the exact same one; the agent has already been told.
        """
        already_fired = {s.signal for s in self.fired_signals}
        for sig_fn in (self._s1, self._s2, self._s3, self._s4,
                       self._s5, self._s6, self._s7):
            sig = sig_fn()
            if sig is not None and sig.signal not in already_fired:
                self.fired_signals.append(sig)
                self._log_stuck(sig)
                return sig
        return None

    def recent_calc_ids(self, n: int = 5) -> list[str]:
        """Dedup'd list of the n most recent calc_ids touched, newest first."""
        seen: dict[str, None] = {}
        for r in reversed(self.rows):
            if r.calc_id and r.calc_id not in seen:
                seen[r.calc_id] = None
                if len(seen) >= n:
                    break
        return list(seen.keys())

    # ---- individual signals -----------------------------------------------

    def _s1(self) -> StuckSignal | None:
        """Same (tool, payload_hash) ≥ S1_THRESHOLD times in last S1_WINDOW turns."""
        window = self.rows[-S1_WINDOW:]
        if len(window) < S1_THRESHOLD:
            return None
        counts: dict[tuple[str, str], int] = {}
        for r in window:
            counts[(r.tool, r.payload_hash)] = counts.get((r.tool, r.payload_hash), 0) + 1
        bad = [(k, v) for k, v in counts.items() if v >= S1_THRESHOLD]
        if not bad:
            return None
        (tool, h), cnt = bad[0]
        return StuckSignal(
            signal="S1",
            reason=f"identical {tool} args called {cnt}× in last {S1_WINDOW} turns",
            suggestions=[
                "change at least one parameter before re-calling this tool",
                "read the latest read_qe_output / verify_run result; the args you tried twice already aren't working",
                "delegate to convergence_fixer subagent with the failing calc_id",
                "submit incomplete with diagnostic notes",
            ],
        )

    def _s2(self) -> StuckSignal | None:
        """≥ S2_THRESHOLD tool errors in last S2_WINDOW turns."""
        window = self.rows[-S2_WINDOW:]
        errs = sum(1 for r in window if r.error)
        if errs < S2_THRESHOLD:
            return None
        return StuckSignal(
            signal="S2",
            reason=f"{errs} tool errors in last {len(window)} turns",
            suggestions=[
                "stop calling tools blindly; read the latest error text fully",
                "use read_qe_output(window='errors') on the failing calc_id",
                "delegate to convergence_fixer subagent",
                "submit incomplete with a clear diagnostic note",
            ],
        )

    def _s3(self) -> StuckSignal | None:
        """No verify_run with verdict=pass in the last S3_WALLCLOCK_S seconds."""
        if not self.rows:
            return None
        last_ok = max((r.ts for r in self.rows if r.has_verify_ok), default=None)
        first_ts = self.rows[0].ts
        now = time.time()
        elapsed = now - (last_ok if last_ok is not None else first_ts)
        if elapsed < self.s3_wallclock_s:
            return None
        return StuckSignal(
            signal="S3",
            reason=f"no verify_run pass in {elapsed:.0f}s (>{self.s3_wallclock_s:.0f}s)",
            suggestions=[
                "run verify_run on whichever calc_id is closest to done",
                "if SCF unconverged → delegate to convergence_fixer subagent",
                "submit incomplete; partial credit is better than wallclock kill",
            ],
        )

    def _s4(self) -> StuckSignal | None:
        """Identical bash command (cd-stripped) ≥ S4_THRESHOLD in last S4_WINDOW."""
        window = self.rows[-S4_WINDOW:]
        cmds: dict[str, int] = {}
        for r in window:
            if r.bash_command:
                cmds[r.bash_command] = cmds.get(r.bash_command, 0) + 1
        bad = [(c, n) for c, n in cmds.items() if n >= S4_THRESHOLD]
        if not bad:
            return None
        cmd, n = bad[0]
        return StuckSignal(
            signal="S4",
            reason=f"bash command repeated {n}× in last {S4_WINDOW} turns: {cmd[:80]}",
            suggestions=[
                "stop running this exact command; previous results haven't changed",
                "try a different parameter (mixing_beta, smearing, k-mesh) and rerun",
                "use the structured tools (write_qe_input / run_pw) instead of raw bash",
            ],
        )

    def _s5(self) -> StuckSignal | None:
        """LLM text matches frustrated-retry patterns ≥ S5_THRESHOLD times in last S5_WINDOW."""
        window = self.rows[-S5_WINDOW:]
        hits = sum(1 for r in window if r.assistant_text and _S5_PATTERNS.search(r.assistant_text))
        if hits < S5_THRESHOLD:
            return None
        return StuckSignal(
            signal="S5",
            reason=f"agent expressed retry/frustration {hits}× in last {len(window)} turns",
            suggestions=[
                "STOP retrying. Switch tactics: change parameter, delegate, or submit incomplete.",
                "If you don't know what to change next, delegate to convergence_fixer",
            ],
        )

    def _s6(self) -> StuckSignal | None:
        """No new completed calc registered in last S6_WINDOW turns."""
        window = self.rows[-S6_WINDOW:]
        if len(window) < S6_WINDOW:
            return None
        any_completion = any(
            r.has_verify_ok or
            (r.tool.endswith("__run_pw") and not r.error)
            for r in window
        )
        if any_completion:
            return None
        return StuckSignal(
            signal="S6",
            reason=f"no completed pw.x run in last {S6_WINDOW} turns",
            suggestions=[
                "you're spending turns on writes / reads without ever running pw.x",
                "call run_pw on the most recent calc_id and verify the result",
                "if all run_pw attempts have failed, delegate to convergence_fixer",
            ],
        )

    def _s7(self) -> StuckSignal | None:
        """Input tokens used > S7_TOKEN_FRACTION of budget with no verify_ok yet."""
        if self.max_input_tokens <= 0:
            return None
        fraction = self.input_tokens_used / self.max_input_tokens
        any_ok = any(r.has_verify_ok for r in self.rows)
        if fraction < S7_TOKEN_FRACTION or any_ok:
            return None
        return StuckSignal(
            signal="S7",
            reason=f"{self.input_tokens_used} tokens used ({fraction:.0%} of budget) "
                   f"with no verify_run pass yet",
            suggestions=[
                "you're running out of context budget; commit to a submit decision",
                "if a calc looks close to done, run verify_run and submit",
                "otherwise submit incomplete with diagnostic notes",
            ],
        )

    # ---- logging -----------------------------------------------------------

    def _log_stuck(self, sig: StuckSignal) -> None:
        if self.stuck_log is None:
            return
        self.stuck_log.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        recent = "\n".join(
            f"  turn={r.turn} tool={r.tool} err={r.error} h={r.payload_hash}/{r.output_hash}"
            for r in self.rows[-10:]
        )
        block = (
            f"[{ts}] STUCK {sig.signal}: {sig.reason}\n"
            f"  suggestions: {sig.suggestions}\n"
            f"  recent rows:\n{recent}\n\n"
        )
        with self.stuck_log.open("a", encoding="utf-8") as f:
            f.write(block)

    # ---- accessors ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self.rows)

    def __repr__(self) -> str:
        return (f"<ProgressLedger turns={len(self.rows)} "
                f"errors={sum(1 for r in self.rows if r.error)} "
                f"fired={[s.signal for s in self.fired_signals]}>")
