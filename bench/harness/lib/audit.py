"""Anti-fraud 审计：检查 QE 真被调、JOB DONE、路径在 workdir 内、文件非空、wall clock 合理。"""
import re
from pathlib import Path
from typing import Any


_QE_BINS_BY_TASK = {
    "vc_relax": ["pw.x"],
    "bands_dos": ["pw.x", "bands.x", "dos.x"],
    "eos": ["pw.x"],
    "elastic_stiffness": ["pw.x"],
}

# T3/T4 的 QE 证据不在 task_outputs（那是汇总 CSV，不含 JOB DONE / WALL），而是
# workdir 里散落的 pw.out（每个体积点 / 应变点一个）。这些任务的 job_done /
# wallclock 改为递归发现 workdir 里的 pw.x 输出来验，而非 grep CSV。
# 期望完成的 pw.x 计算数 = 方法学网格点数：eos 7 体积点、elastic 24 应变点
# (6 Voigt 模式 × 4 振幅)。少于此数 = 没把所有点跑完。
_QE_MIN_OUTPUTS_BY_TASK = {
    "eos": 7,
    "elastic_stiffness": 24,
}

# wallclock 检测（B 方案：ratio + 软地板，详见 todo/bench/audit-wallclock-threshold.md）
#
# QE 真实墙钟占 solver 总墙钟的最低比例。低于此值视为 solver 没真在跑 QE。
# 0.005 (0.5%) 基于 0428 Run #1 实测：Si T1 GPU 单次 vc-relax 的真实 ratio ≈ 3.5%
# (QE WALL 6.39s / solver wall 180s)，留 ~7× 安全边际避免快任务误报；
# 详细 5 道实测分布见 todo §Step 4。
_QE_WALL_MIN_RATIO = 0.005
# QE 真实墙钟绝对软地板：再快的 QE GPU 任务也至少要 1s（GPU 启动 + 装载赝势 + 1 SCF iter）
_QE_WALL_MIN_ABS_S = 1.0
# 文件非空阈值
_FILE_MIN_BYTES = 1024

# audit 失败 gate 分数（2026-06-12 决议 A）：以下 flag 任一为 False → 该 run 判
# invalid（run.py 把 total_score 归 0，但保留 total_score_raw 供分析）。这些是
# "结果不可信 / 作弊"信号：没真调 QE / 没跑完 / wallclock 作弊 / 违反禁网。
# paths_in_workdir、files_nonempty 是较软的完整性检查，暂不 gate（需要时加进来即可）。
GATING_FLAGS = ("qe_invoked", "job_done", "qe_wallclock_ok", "no_network_access")


def audit_passed(flags: dict[str, bool]) -> bool:
    """所有 gating flag 均为 True → 通过。缺失的 flag 视为 False（保守）。"""
    return all(bool(flags.get(f, False)) for f in GATING_FLAGS)

# 网络命令黑名单（MVP 硬编码禁网；bench v1.3 会加 task.policy.network_allowed 字段后参数化）
_NETWORK_CMD_PATTERNS = [
    r"\bcurl\b",
    r"\bwget\b",
    r"\bssh\b",
    r"\bscp\b",
    r"\brsync\b.*-e\s+ssh",
    r"\bnc\s",
    r"\bpip\s+install\b",
    r"\bconda\s+install\b",
    r"\bapt(?:-get)?\s+install\b",
    r"\bbrew\s+install\b",
    r"\bgit\s+clone\b",
    r"https?://",
]


def audit_run(
    *,
    workdir: Path,
    task_outputs: dict[str, str],
    task_type: str,
    solver_wall_s: float,
    enforce_no_network: bool = True,
) -> dict[str, bool]:
    """返回 6 个 audit flag bool。

    solver_wall_s = solver subprocess 总墙钟耗时（秒），由 harness/lib/solver_runner.py
    用 time.monotonic() 测得，传入用于 _check_wallclock 算 QE/solver ratio。

    enforce_no_network 由调用方按 task.policy.network_allowed 派生：
      - True（默认 / network_allowed=false）：扫 bash.log 检测网络命令
      - False（network_allowed=true 的题）：no_network_access flag 直接 True，
        相当于 bypass 检查（题目允许联网）
    """
    workdir = Path(workdir).resolve()
    bash_log_path = workdir / "bash.log"
    bash_log = bash_log_path.read_text(encoding="utf-8", errors="replace") if bash_log_path.exists() else ""

    # 入口过滤：task_outputs 语义是 key→file_path (str)，但 solver 偶尔会塞标量
    # (T3/T4 早期 prompt 契约不清, shijie 0606 handoff issue #1)。下游 4 个 helper
    # 全都 _resolve(workdir, path) → Path(path) 假 str；遇 float 会 TypeError 拖垮
    # 整个 audit (已打满分的 run 都会被判 failed)。这里入口过滤 + 整体 try/except,
    # 把非 str value 完全隔离在 audit 之外。
    task_outputs_paths = {
        k: v for k, v in (task_outputs or {}).items() if isinstance(v, str)
    }

    required_bins = _QE_BINS_BY_TASK.get(task_type, [])
    # T3/T4：QE 证据是 workdir 里发现的 pw.out，不是 task_outputs 的 CSV。
    min_pw = _QE_MIN_OUTPUTS_BY_TASK.get(task_type)
    try:
        if min_pw is not None:
            pw_outputs = _discover_pw_outputs(workdir)
            job_done = _check_job_done_pw(pw_outputs, min_pw)
            wallclock_ok = _check_wallclock_pw(pw_outputs, solver_wall_s)
        else:
            job_done = _check_job_done(workdir, task_outputs_paths)
            wallclock_ok = _check_wallclock(workdir, task_outputs_paths, solver_wall_s)
        flags: dict[str, bool] = {
            "qe_invoked": _check_qe_invoked(bash_log, required_bins),
            "job_done": job_done,
            "paths_in_workdir": _check_paths_in_workdir(workdir, task_outputs_paths),
            "files_nonempty": _check_files_nonempty(workdir, task_outputs_paths),
            "qe_wallclock_ok": wallclock_ok,
            "no_network_access": _check_no_network_access(bash_log) if enforce_no_network else True,
        }
    except Exception as exc:
        # 最后防线: 任何 audit helper 抛未捕获异常都不该让已打分的 run 变 failed。
        # 全部 flag 设 False (审计未通过, 但不阻断 report.json 落地)。
        import logging
        logging.getLogger("audit").warning(
            "audit_run helper raised %s: %s — degrading all flags to False",
            type(exc).__name__, exc,
        )
        flags = {
            "qe_invoked": False, "job_done": False, "paths_in_workdir": False,
            "files_nonempty": False, "qe_wallclock_ok": False,
            "no_network_access": False if enforce_no_network else True,
        }
    return flags


def _check_no_network_access(bash_log: str) -> bool:
    """扫 bash.log 里 $ 开头的命令行；任一网络指令命中 → False。
    注意：只检查命令行，不看 stdout/stderr（那里可能出现 URL 但不是违规）。
    """
    # 查找 $ 后跟空格和命令（形如 [xxx] $ cmd）
    cmd_pattern = r"\$\s+(.+)$"
    for match in re.finditer(cmd_pattern, bash_log, re.MULTILINE):
        cmd = match.group(1)
        for pattern in _NETWORK_CMD_PATTERNS:
            if re.search(pattern, cmd, re.IGNORECASE):
                return False
    return True


def _check_qe_invoked(bash_log: str, required_bins: list[str]) -> bool:
    if not required_bins:
        return True
    for binname in required_bins:
        # 命令行里出现 `pw.x`（作为词边界，避免匹配 pw.xxx）
        if re.search(rf"\b{re.escape(binname)}\b", bash_log):
            continue
        return False
    return True


def _is_log_output(key: str) -> bool:
    """task_output key 命名约定：

      * `*_out`            → QE 程序日志（pw.x / bands.x / dos.x），需检 `JOB DONE.`
                             例如 scf_out / bands_out / relax_out / dos_out
      * `*_data` / `*_dat` → 纯数据文件（bands.dat / dos.dat），不含 JOB DONE
                             仅检文件存在 + 非空，不 grep
      * 其它后缀           → 默认按 log 处理（保守，宁误报也不漏过）

    未来加新工具（ph.x / pp.x …）按同 pattern 命名 task_output key 即可。
    """
    if key.endswith("_out"):
        return True
    if key.endswith("_data") or key.endswith("_dat"):
        return False
    return True


def _check_job_done(workdir: Path, task_outputs: dict[str, str]) -> bool:
    for key, path in task_outputs.items():
        p = _resolve(workdir, path)
        if not p.exists():
            return False
        # 数据文件（bands.dat / dos.dat）跳过 JOB DONE 检查，但仍要求存在
        if not _is_log_output(key):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return False
        # Scan the whole file, not just a tail window: the qe-gpu-wrap wrapper
        # appends "still waiting for GPU slot" log lines (and QE appends IEEE /
        # MPI-shutdown messages) AFTER "JOB DONE.", which can push the marker
        # many KB from EOF and defeat a fixed tail window (false-negative gate).
        if "JOB DONE." not in text:
            return False
    return True


def _check_paths_in_workdir(workdir: Path, task_outputs: dict[str, str]) -> bool:
    workdir = workdir.resolve()
    for path in task_outputs.values():
        p = _resolve(workdir, path)
        try:
            p.resolve().relative_to(workdir)
        except ValueError:
            return False
    return True


def _check_files_nonempty(workdir: Path, task_outputs: dict[str, str]) -> bool:
    for key, path in task_outputs.items():
        p = _resolve(workdir, path)
        if not p.exists():
            return False
        # 汇总 CSV（eos_points_csv / stress_strain_csv）合法体积就很小（7/24 行,
        # ~300B–1KB），不适用 QE 日志的 1KB 阈值；只要非空即可。
        min_bytes = 1 if key.endswith("_csv") else _FILE_MIN_BYTES
        if p.stat().st_size < min_bytes:
            return False
    return True


def _check_wallclock(workdir: Path, task_outputs: dict[str, str], solver_wall_s: float) -> bool:
    """B 方案 ratio 检测：QE 真实墙钟 / solver 总墙钟 ≥ 阈值 + 绝对软地板。

    从每个 output 文件内部读 `X.XXs WALL` 行（多行取 max 作该文件的 QE wall），
    多个 output 文件的 max 求和得 total_qe_wall（T2 多步 SCF/NSCF/DOS 各贡献一份）。

    判 false：
      - 任一 output 文件不存在或没有 WALL 行（QE 未正常完成）
      - total_qe_wall < 1s（绝对软地板：真实 QE 至少要 GPU 启动 + 1 SCF iter）
      - total_qe_wall / solver_wall_s < _QE_WALL_MIN_RATIO（作弊路径：solver 没真在跑 QE）

    兜底：solver_wall_s ≤ 0 或 task_outputs 为空 → True（退化场景不强制判负）。

    比从 bash.log 看 shell 调用 wall 更 robust——agent 可能合法用 `pw.x ... &`
    后台跑、bash 瞬间返回，QE 自己写的 WALL 才是真实耗时。
    """
    if not task_outputs or solver_wall_s <= 0:
        return True
    total_qe_wall = 0.0
    files_with_wall = 0
    for path in task_outputs.values():
        p = _resolve(workdir, path)
        if not p.exists():
            return False  # 引用的文件不存在
        content = p.read_text(encoding="utf-8", errors="replace")
        walls = re.findall(r"([\d.]+)\s*s\s+WALL\b", content)
        if not walls:
            # 数据文件（如 T2 的 dos.dat / bands.dat）没有 WALL 行，跳过
            continue
        # 取该文件内最大值（QE summary 行通常是各阶段中最大）
        total_qe_wall += max(float(w) for w in walls)
        files_with_wall += 1
    if files_with_wall == 0:
        return False  # 所有 task_outputs 都没有 WALL → 没有 QE 执行证据
    if total_qe_wall < _QE_WALL_MIN_ABS_S:
        return False
    return (total_qe_wall / solver_wall_s) >= _QE_WALL_MIN_RATIO


def _discover_pw_outputs(workdir: Path) -> list[Path]:
    """递归找 workdir 里所有 pw.x 输出（按 'Program PWSCF' 头识别）。

    与 solver 的目录布局解耦——v1 把点放在 v0.94/pw.out、runs/strains/<mode>/pw.out
    等不同位置，这里靠**文件内容**而非路径模式发现，对任何 solver 都成立。
    只读前 4KB 判头（QE banner 在文件最前面），避免整文件读入。
    """
    outs: list[Path] = []
    for p in sorted(workdir.rglob("*.out")):
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:4096]
        except Exception:
            continue
        if "Program PWSCF" in head:
            outs.append(p)
    return outs


def _check_job_done_pw(pw_outputs: list[Path], min_count: int) -> bool:
    """T3/T4：发现的 pw.out 里含 'JOB DONE.' 的数量 ≥ 期望网格点数。

    少于 min_count = 没把所有体积点/应变点跑完（漏点或崩点），判 false。
    多于无妨（solver 可能多跑参考点）。"""
    done = 0
    for p in pw_outputs:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # full-file scan (see _check_job_done): the marker can sit far from EOF
        # behind appended GPU-wrapper / IEEE / MPI trailing output.
        if "JOB DONE." in text:
            done += 1
    return done >= min_count


def _check_wallclock_pw(pw_outputs: list[Path], solver_wall_s: float) -> bool:
    """T3/T4 wallclock：QE 真实墙钟（所有 pw.out 的 WALL 之和）/ solver 总墙钟。

    判 false：没有任何 pw.out 含 WALL（无 QE 执行证据）/ 总 WALL < 1s 软地板 /
    ratio < 阈值（solver 没真在跑 QE）。兜底 solver_wall_s ≤ 0 → True。
    与 T1/T2 的 _check_wallclock 同口径，只是证据源换成发现的 pw.out。"""
    if solver_wall_s <= 0:
        return True
    total_qe_wall = 0.0
    files_with_wall = 0
    for p in pw_outputs:
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        walls = re.findall(r"([\d.]+)\s*s\s+WALL\b", content)
        if not walls:
            continue
        total_qe_wall += max(float(w) for w in walls)
        files_with_wall += 1
    if files_with_wall == 0:
        return False
    if total_qe_wall < _QE_WALL_MIN_ABS_S:
        return False
    return (total_qe_wall / solver_wall_s) >= _QE_WALL_MIN_RATIO


def _resolve(workdir: Path, path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (workdir / p)
