"""submit tool — 写 result.json；调用方在写后自行 exit(0)。"""
import json
from pathlib import Path
from typing import Any


def write_result_json(out_path: Path, payload: dict[str, Any]) -> None:
    """把 agent 最终产物写到 out_path；覆盖式写；父目录不存在自动建。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
