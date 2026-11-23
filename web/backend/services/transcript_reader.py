"""读 run_dir/workdir/transcript.json + bash.log → TranscriptSnapshot。

依赖 transcript_adapter 做 raw → flat lift。
增量切片：since_turn=N 返回 flat[N:]，current_turn = len(flat)。

容错：
- 文件不存在 → 空 turns（run 还没产生 turn）
- JSONDecodeError（solver atomic rewrite 中途读到 partial JSON）→ fallback 空 turns，下次 poll 拿到完整版
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from backend.schemas.run import TranscriptSnapshot
from backend.services.transcript_adapter import adapt_raw_transcript


def read_transcript_snapshot(
    *,
    run_id: str,
    status: str,
    run_dir: Path,
    since_turn: int,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> TranscriptSnapshot:
    workdir = run_dir / "workdir"
    transcript_path = workdir / "transcript.json"
    bash_log_path = workdir / "bash.log"
    if not transcript_path.exists():
        return TranscriptSnapshot(
            run_id=run_id, status=status, current_turn=0,
            since_turn=since_turn, turns=[], etag=None,
        )
    try:
        raw = json.loads(transcript_path.read_text())
    except json.JSONDecodeError:
        return TranscriptSnapshot(
            run_id=run_id, status=status, current_turn=since_turn,
            since_turn=since_turn, turns=[], etag=None,
        )
    if not isinstance(raw, list):
        return TranscriptSnapshot(
            run_id=run_id, status=status, current_turn=0,
            since_turn=since_turn, turns=[], etag=None,
        )
    all_flat = adapt_raw_transcript(
        raw, bash_log_path, started_at=started_at, finished_at=finished_at,
    )
    sliced = all_flat[since_turn:]
    mtime = transcript_path.stat().st_mtime
    etag = hashlib.sha1(f"{mtime}".encode()).hexdigest()[:16]
    return TranscriptSnapshot(
        run_id=run_id,
        status=status,
        current_turn=len(all_flat),
        since_turn=since_turn,
        turns=sliced,
        etag=etag,
    )
