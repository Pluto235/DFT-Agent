import io
from pathlib import Path
import pytest
from agent.tools.bash import run_bash


def test_basic_exec(tmp_path):
    log = io.StringIO()
    result, new_cwd = run_bash("echo hello", tmp_path, timeout_s=5, log_file=log)
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]
    assert result["stderr"] == ""
    assert new_cwd == tmp_path


def test_exit_code_nonzero(tmp_path):
    log = io.StringIO()
    result, _ = run_bash("false", tmp_path, timeout_s=5, log_file=log)
    assert result["exit_code"] != 0


def test_cd_updates_cwd(tmp_path):
    (tmp_path / "sub").mkdir()
    log = io.StringIO()
    _, new_cwd = run_bash("cd sub", tmp_path, timeout_s=5, log_file=log)
    assert new_cwd == (tmp_path / "sub").resolve()


def test_cd_with_extras_does_not_update_cwd(tmp_path):
    """`cd sub && ls` 是一条子 shell 命令；cwd 只在它自己的进程里变，
    不应该影响后续 bash() 调用的起始 cwd。"""
    (tmp_path / "sub").mkdir()
    log = io.StringIO()
    _, new_cwd = run_bash("cd sub && ls", tmp_path, timeout_s=5, log_file=log)
    assert new_cwd == tmp_path  # 保持原 cwd


def test_timeout(tmp_path):
    log = io.StringIO()
    result, _ = run_bash("sleep 2", tmp_path, timeout_s=1, log_file=log)
    assert result["exit_code"] == -1
    assert "TIMEOUT" in result["stderr"]


def test_log_appends(tmp_path):
    log = io.StringIO()
    run_bash("echo one", tmp_path, timeout_s=5, log_file=log)
    run_bash("echo two", tmp_path, timeout_s=5, log_file=log)
    contents = log.getvalue()
    assert "$ echo one" in contents
    assert "$ echo two" in contents


def test_truncate_long_stdout(tmp_path):
    log = io.StringIO()
    # 生成 500 行输出 → 应被截断
    result, _ = run_bash("seq 1 500", tmp_path, timeout_s=5, log_file=log)
    lines_in_display = result["stdout"].count("\n")
    assert lines_in_display < 500
    assert "omitted" in result["stdout"]
