"""bash_guard regression: 绝对路径 QE 可执行文件必须被拒。

WO3 vc_relax 2026-05-19 16:48 的真实故障链：agent 第一次 `pw.x` 调用
被 bash 工具按 timeout_s=3600 砍后，判断 "pw.x 找不到"，切到
/path/to/qe/QuantumE-7.5/bin/pw.x（CPU build，没有 GPU 加速），
跑了 7200s 又被砍，最终 4 小时 harness budget 烧光。

guard 在 _split_segments 把 `prog` 剥成 basename 之前，扫描原始 toks，
任何 `/` 出现且 basename 是 QE 可执行文件的 token 都直接拒。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.tools.bash_guard import check_command  # noqa: E402


CWD = Path("/tmp")


def test_absolute_path_pw_x_rejected():
    reason = check_command(
        "/path/to/qe/QuantumE-7.5/bin/pw.x -in scf.in", CWD
    )
    assert reason is not None
    assert "绝对路径" in reason or "absolute" in reason.lower()


def test_bare_pw_x_allowed():
    reason = check_command("pw.x -in scf.in > scf.out 2>&1", CWD)
    assert reason is None


def test_absolute_path_other_qe_binaries_rejected():
    for binary in ["bands.x", "dos.x", "projwfc.x", "ph.x", "pp.x"]:
        reason = check_command(
            f"/usr/local/qe-cpu/{binary} -in foo.in", CWD
        )
        assert reason is not None, \
            f"绝对路径 {binary} 应该被拒，但 check_command 返回 None"
        assert binary in reason


def test_relative_path_with_slash_also_rejected():
    # ./pw.x 也含 `/`；basename 是 pw.x → 拒。agent 没理由把 binary 放 workdir。
    reason = check_command("./pw.x -in scf.in", CWD)
    assert reason is not None


def test_pw_x_inside_path_arg_not_misflagged():
    # 文件名里恰好含 "pw.x" 字符但不是 binary 调用：路径 token 不是绝对路径
    # 形式（cp pw.x.bak / pw.x.tmp），不应被拒。
    reason = check_command("ls pw.x.bak", CWD)
    # 这条预期是放行（pw.x.bak basename != pw.x）
    assert reason is None


def test_cp_of_absolute_path_pw_x_also_rejected():
    # `cp /path/to/qe/.../pw.x .`：agent 想把 CPU binary 拷进 workdir。
    # toks 里出现 `/.../pw.x`，basename 命中 → 拒。这是预期行为（防止
    # agent 通过 cp 绕过 PATH 限制）。
    reason = check_command(
        "cp /path/to/qe/QuantumE-7.5/bin/pw.x .", CWD
    )
    assert reason is not None


def test_mpirun_pw_x_bare_still_allowed():
    # `mpirun pw.x -in scf.in`：mpirun + bare pw.x 没问题。
    reason = check_command("mpirun -np 4 pw.x -in scf.in", CWD)
    assert reason is None


def test_mpirun_absolute_pw_x_rejected():
    reason = check_command(
        "mpirun -np 4 /opt/qe/bin/pw.x -in scf.in", CWD
    )
    assert reason is not None
    assert "pw.x" in reason


def test_alm_anphon_abspath_also_rejected():
    # ALAMODE binaries 也在 QE_EXECUTABLES 集合里
    for binary in ["alm", "anphon"]:
        reason = check_command(f"/opt/alamode/bin/{binary} input.in", CWD)
        assert reason is not None, f"{binary} 绝对路径应被拒"
