"""file_io tool — write_file + read_file, paths resolved relative to cwd.

错误处理:任何 IO 错误(FileNotFoundError / IsADirectoryError / PermissionError /
其他 OSError)返 {"error": "..."} tool result,而不是 raise。让 LLM 看到错误后
self-recover(譬如 read 不存在的 .out → 先跑 pw.x 再 read);raise 会让整个
solver subprocess crash 丢掉整个 run 的投资。
"""
from pathlib import Path
from typing import Any


def write_file(path: str | Path, content: str, cwd: Path) -> dict[str, Any]:
    """写文件；相对路径以 cwd 为基底；自动建父目录。"""
    p = _resolve(path, cwd)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        p.write_bytes(data)
    except OSError as e:
        return {"error": f"write_file failed: {type(e).__name__}: {e}"}
    return {"bytes_written": len(data), "path": str(p)}


def read_file(
    path: str | Path,
    cwd: Path,
    offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    """读文件；按行切片（offset 从 0 起）；返回 content + total_lines。

    文件不存在 / 不是常规文件 / 读失败 → 返 {"error": "..."} 而非 raise。
    """
    p = _resolve(path, cwd)
    if not p.exists():
        return {"error": f"file not found: {path}"}
    if not p.is_file():
        return {"error": f"not a regular file: {path}"}
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError as e:
        return {"error": f"read_file failed: {type(e).__name__}: {e}"}
    total = len(lines)
    if limit is None:
        sliced = lines[offset:]
    else:
        sliced = lines[offset:offset + limit]
    return {"content": "".join(sliced), "total_lines": total}


def _resolve(path: str | Path, cwd: Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (cwd / p)
