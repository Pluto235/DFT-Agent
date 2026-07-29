from pathlib import Path
import pytest
from agent.tools.file_io import write_file, read_file


def test_write_creates_file(tmp_path):
    result = write_file(tmp_path / "a.txt", "hello\nworld\n", cwd=tmp_path)
    assert (tmp_path / "a.txt").read_text() == "hello\nworld\n"
    assert result["bytes_written"] == 12
    assert Path(result["path"]).resolve() == (tmp_path / "a.txt").resolve()


def test_write_creates_parent_dirs(tmp_path):
    write_file(tmp_path / "sub/deep/a.txt", "x", cwd=tmp_path)
    assert (tmp_path / "sub/deep/a.txt").read_text() == "x"


def test_write_relative_path(tmp_path):
    """相对路径要以 cwd 为基底解析。"""
    result = write_file("rel.txt", "y", cwd=tmp_path)
    assert (tmp_path / "rel.txt").read_text() == "y"


def test_read_basic(tmp_path):
    (tmp_path / "f.txt").write_text("a\nb\nc\n")
    result = read_file(tmp_path / "f.txt", cwd=tmp_path)
    assert result["content"] == "a\nb\nc\n"
    assert result["total_lines"] == 3


def test_read_with_offset_and_limit(tmp_path):
    (tmp_path / "f.txt").write_text("\n".join(str(i) for i in range(10)) + "\n")
    result = read_file(tmp_path / "f.txt", offset=3, limit=2, cwd=tmp_path)
    assert result["content"] == "3\n4\n"
    assert result["total_lines"] == 10


# ============ error path: 不能 raise，让 LLM 自己 recover ============

def test_read_missing_file_returns_error(tmp_path):
    """读不存在文件 → 返 error tool result(不 raise)。"""
    result = read_file("nope.txt", cwd=tmp_path)
    assert "error" in result
    assert "not found" in result["error"].lower()
    assert "content" not in result


def test_read_directory_returns_error(tmp_path):
    """读目录 → 返 error tool result(不 raise IsADirectoryError)。"""
    (tmp_path / "sub").mkdir()
    result = read_file("sub", cwd=tmp_path)
    assert "error" in result
    assert "not a regular file" in result["error"]


def test_write_to_dir_returns_error(tmp_path):
    """写到一个已存在的目录路径 → 返 error tool result(不 raise)。"""
    (tmp_path / "sub").mkdir()
    result = write_file("sub", "content", cwd=tmp_path)
    assert "error" in result
    assert "bytes_written" not in result
