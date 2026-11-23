"""CalcRegistry — VASPilot-style calc_id store.

Every structured tool that touches a QE calculation flows through one
CalcRecord. The registry is the only shared state between tools — it
replaces "tool A returns stdout, tool B greps it" with "tool A returns
calc_id, tool B looks it up".

Persistence: JSON file at <workdir>/calcs.json. Whole-file rewrite on
every update (cheap; total file size stays small — a few KB even after
50+ calcs).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


_VALID_STATUS = {"created", "running", "completed", "failed"}


@dataclass
class CalcRecord:
    calc_id: str
    calc_type: str               # "vc-relax" | "relax" | "scf" | "nscf" | "bands" | "dos"
    prefix: str                  # QE prefix
    input_path: str              # workdir-relative .in path
    output_path: str | None = None  # workdir-relative .out path (set by run_pw)
    status: str = "created"      # created → running → completed | failed
    params: dict = field(default_factory=dict)    # ecutwfc, ecutrho, kpts, ...
    pseudos: dict = field(default_factory=dict)   # {"Si": "pseudos/Si.upf"}
    bash_command: str | None = None
    exit_code: int | None = None
    wall_s: float | None = None
    created_ts: float = field(default_factory=time.time)
    completed_ts: float | None = None
    lint_diff: str | None = None         # qe_input_lint patches text
    summary: str | None = None           # short post-run summary
    error_signature: str | None = None   # for stuck detector pattern matching

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUS:
            raise ValueError(f"invalid status {self.status!r}; expected one of {_VALID_STATUS}")


class CalcRegistry:
    """Persistent calc_id → CalcRecord store, scoped to one workdir."""

    def __init__(self, workdir: Path, filename: str = "calcs.json"):
        self.workdir = Path(workdir)
        self.path = self.workdir / filename
        self._records: dict[str, CalcRecord] = {}
        self._counter: int = 0
        if self.path.exists():
            self.load()

    # ---- core API ----------------------------------------------------------

    def register(
        self,
        calc_type: str,
        prefix: str,
        input_path: str,
        *,
        params: dict | None = None,
        pseudos: dict | None = None,
        lint_diff: str | None = None,
    ) -> str:
        self._counter += 1
        calc_id = f"calc_{self._counter:03d}_{calc_type.replace('-', '_')}_{prefix}"
        # ensure uniqueness even on filename collision (very rare)
        if calc_id in self._records:
            calc_id = f"{calc_id}_{uuid.uuid4().hex[:6]}"
        rec = CalcRecord(
            calc_id=calc_id,
            calc_type=calc_type,
            prefix=prefix,
            input_path=input_path,
            params=params or {},
            pseudos=pseudos or {},
            lint_diff=lint_diff,
        )
        self._records[calc_id] = rec
        self.save()
        return calc_id

    def update(self, calc_id: str, **fields: Any) -> CalcRecord:
        if calc_id not in self._records:
            raise KeyError(f"unknown calc_id: {calc_id!r}")
        rec = self._records[calc_id]
        for k, v in fields.items():
            if not hasattr(rec, k):
                raise AttributeError(f"CalcRecord has no field {k!r}")
            setattr(rec, k, v)
        if rec.status not in _VALID_STATUS:
            raise ValueError(f"invalid status {rec.status!r}")
        if rec.status in ("completed", "failed") and rec.completed_ts is None:
            rec.completed_ts = time.time()
        self.save()
        return rec

    def get(self, calc_id: str) -> CalcRecord | None:
        return self._records.get(calc_id)

    def list(
        self,
        *,
        status: str | None = None,
        calc_type: str | None = None,
    ) -> list[CalcRecord]:
        recs = list(self._records.values())
        if status is not None:
            recs = [r for r in recs if r.status == status]
        if calc_type is not None:
            recs = [r for r in recs if r.calc_type == calc_type]
        return sorted(recs, key=lambda r: r.created_ts)

    def latest(self, *, calc_type: str | None = None) -> CalcRecord | None:
        recs = self.list(calc_type=calc_type)
        return recs[-1] if recs else None

    # ---- persistence -------------------------------------------------------

    def save(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        payload = {
            "counter": self._counter,
            "records": {cid: asdict(rec) for cid, rec in self._records.items()},
        }
        # atomic write: tmp + rename
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(self.path)

    def load(self) -> None:
        payload = json.loads(self.path.read_text())
        self._counter = int(payload.get("counter", 0))
        self._records = {
            cid: CalcRecord(**rec_data)
            for cid, rec_data in payload.get("records", {}).items()
        }

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, calc_id: str) -> bool:
        return calc_id in self._records
