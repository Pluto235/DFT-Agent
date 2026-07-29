from pathlib import Path
from harness.lib.audit import audit_run, audit_passed, GATING_FLAGS


def _write_bash_log(path: Path, lines: list[str]):
    path.write_text("\n".join(lines) + "\n")


def test_all_pass(tmp_path):
    # 构造一个"合格"的 run：pw.x 被调过，output 含 JOB DONE + WALL ≥ 1s + ratio 健康，路径在 workdir，文件非空
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "relax.out").write_text(
        "a" * 1800 + "\n     PWSCF        :     30.00s CPU     45.67s WALL\n\nJOB DONE.\n"
    )  # > 1 KB + 含 WALL=45.67s
    bash_log = workdir / "bash.log"
    _write_bash_log(bash_log, [
        "[2026-04-24 ...] [cwd=/wd] [timeout=900s] [wall=30.50s] [exit=0]",
        "$ pw.x -in relax.in > relax.out",
    ])

    result = audit_run(
        workdir=workdir,
        task_outputs={"vc_relax_out": "relax.out"},
        task_type="vc_relax",
        solver_wall_s=60.0,  # ratio = 45.67 / 60 = 76% — 远高于阈值
    )
    assert result["qe_invoked"] is True
    assert result["job_done"] is True
    assert result["paths_in_workdir"] is True
    assert result["files_nonempty"] is True
    assert result["qe_wallclock_ok"] is True
    assert result["no_network_access"] is True


def test_no_qe_invocation(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "relax.out").write_text("JOB DONE.\n")
    bash_log = workdir / "bash.log"
    _write_bash_log(bash_log, [
        "[...] $ echo hi",
    ])

    result = audit_run(workdir=workdir,
                      task_outputs={"vc_relax_out": "relax.out"},
                      task_type="vc_relax",
                      solver_wall_s=30.0)
    assert result["qe_invoked"] is False


def test_missing_job_done(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "relax.out").write_text("a" * 2000)
    bash_log = workdir / "bash.log"
    _write_bash_log(bash_log, [
        "[...] [wall=30.50s] $ pw.x -in relax.in",
    ])

    result = audit_run(workdir=workdir,
                      task_outputs={"vc_relax_out": "relax.out"},
                      task_type="vc_relax",
                      solver_wall_s=30.0)
    assert result["job_done"] is False


def test_path_outside_workdir(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "bash.log").write_text("[...] [wall=30.50s] $ pw.x -in relax.in")
    outside = tmp_path / "cheat.out"
    outside.write_text("JOB DONE.\n")

    result = audit_run(workdir=workdir,
                      task_outputs={"vc_relax_out": str(outside.resolve())},
                      task_type="vc_relax",
                      solver_wall_s=30.0)
    assert result["paths_in_workdir"] is False


def test_empty_output(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "relax.out").write_text("")
    (workdir / "bash.log").write_text("[...] [wall=30.50s] $ pw.x -in relax.in")

    result = audit_run(workdir=workdir,
                      task_outputs={"vc_relax_out": "relax.out"},
                      task_type="vc_relax",
                      solver_wall_s=30.0)
    assert result["files_nonempty"] is False


def test_suspicious_short_wallclock(tmp_path):
    """QE output 里 WALL < 1s（软地板）→ 标红，无论 ratio。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "relax.out").write_text(
        "a" * 1900 + "\n     PWSCF        :      0.30s CPU      0.50s WALL\n\nJOB DONE.\n"
    )
    (workdir / "bash.log").write_text("[...] [wall=30.50s] $ pw.x -in relax.in")

    result = audit_run(workdir=workdir,
                      task_outputs={"vc_relax_out": "relax.out"},
                      task_type="vc_relax",
                      solver_wall_s=0.6)  # 即使 ratio = 0.5/0.6 = 83%，1s 软地板仍触发 false
    assert result["qe_wallclock_ok"] is False


def test_wallclock_ok_for_background_pwx(tmp_path):
    """agent 用 `pw.x ... &` 后台跑：bash header wall=0.01s 但 QE output WALL 大。应该通过。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "relax.out").write_text(
        "a" * 1800 + "\n     PWSCF        :     30.00s CPU     51.87s WALL\n\nJOB DONE.\n"
    )
    (workdir / "bash.log").write_text("[...] [wall=0.01s] $ pw.x -in relax.in > relax.out &")

    result = audit_run(workdir=workdir,
                      task_outputs={"vc_relax_out": "relax.out"},
                      task_type="vc_relax",
                      solver_wall_s=60.0)  # ratio = 51.87/60 = 86%
    assert result["qe_wallclock_ok"] is True


def test_no_network_access_curl_detected(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "relax.out").write_text("a" * 2000 + "\nJOB DONE.\n")
    bash_log = workdir / "bash.log"
    _write_bash_log(bash_log, [
        "[...] [wall=30.50s] [exit=0]",
        "$ pw.x -in relax.in > relax.out",
        "[...] [wall=0.10s] [exit=0]",
        "$ curl https://materialsproject.org/api/v2/materials/mp-149 > si_gt.json",
    ])

    result = audit_run(workdir=workdir,
                      task_outputs={"vc_relax_out": "relax.out"},
                      task_type="vc_relax",
                      solver_wall_s=30.0)
    assert result["no_network_access"] is False


def test_no_network_access_pip_install_detected(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "relax.out").write_text("a" * 2000 + "\nJOB DONE.\n")
    _write_bash_log(workdir / "bash.log", [
        "[...] [wall=30.50s] $ pw.x -in relax.in",
        "[...] [wall=5.00s] $ pip install numpy",
    ])
    result = audit_run(workdir=workdir,
                      task_outputs={"vc_relax_out": "relax.out"},
                      task_type="vc_relax",
                      solver_wall_s=30.0)
    assert result["no_network_access"] is False


def test_enforce_no_network_false_bypasses_curl_check(tmp_path):
    """task.policy.network_allowed=true → enforce_no_network=False；即使 bash.log
    含违规命令，no_network_access flag 也 forced True（相当于绕过检查）。
    覆盖 v1.3 schema.task.policy.network_allowed=true 路径。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "relax.out").write_text("a" * 2000 + "\nJOB DONE.\n")
    _write_bash_log(workdir / "bash.log", [
        "[...] [wall=30.50s] [exit=0] $ pw.x -in relax.in > relax.out",
        "[...] [wall=0.10s] [exit=0] $ curl https://materialsproject.org/api/v2/materials/mp-149 > si_gt.json",
    ])
    result = audit_run(
        workdir=workdir,
        task_outputs={"vc_relax_out": "relax.out"},
        task_type="vc_relax",
        solver_wall_s=30.0,
        enforce_no_network=False,
    )
    assert result["no_network_access"] is True


def test_enforce_no_network_true_default_keeps_detection(tmp_path):
    """enforce_no_network 默认 True；与原 test_no_network_access_curl_detected 同行为，
    显式覆盖 v1.3 schema.task.policy.network_allowed=false（默认）路径。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "relax.out").write_text("a" * 2000 + "\nJOB DONE.\n")
    _write_bash_log(workdir / "bash.log", [
        "[...] [wall=30.50s] [exit=0] $ pw.x -in relax.in > relax.out",
        "[...] [wall=0.10s] [exit=0] $ wget https://example.com/data.json",
    ])
    result = audit_run(
        workdir=workdir,
        task_outputs={"vc_relax_out": "relax.out"},
        task_type="vc_relax",
        solver_wall_s=30.0,
        enforce_no_network=True,
    )
    assert result["no_network_access"] is False


def test_no_network_access_url_in_stdout_not_flagged(tmp_path):
    """stdout 里出现 URL（比如 QE 打印的引用）不算违规——只看 $ 打头的命令。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "relax.out").write_text("a" * 2000 + "\nJOB DONE.\n")
    _write_bash_log(workdir / "bash.log", [
        "[...] [wall=30.50s] $ pw.x -in relax.in > relax.out",
        "--- stdout ---",
        "QE cites: https://www.quantum-espresso.org/",
        "",
    ])
    result = audit_run(workdir=workdir,
                      task_outputs={"vc_relax_out": "relax.out"},
                      task_type="vc_relax",
                      solver_wall_s=30.0)
    assert result["no_network_access"] is True


# === Wallclock ratio (B 方案) 新增测试：ratio + 1s 软地板 + 兜底 ===


def test_wallclock_ratio_normal_passes(tmp_path):
    """高 ratio（QE WALL 占 solver 大头）→ true。任何合理阈值都过。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "relax.out").write_text(
        "a" * 1800 + "\n     PWSCF        :     79.20s CPU     80.00s WALL\n\nJOB DONE.\n"
    )
    (workdir / "bash.log").write_text("[...] $ pw.x -in relax.in > relax.out")
    result = audit_run(
        workdir=workdir,
        task_outputs={"vc_relax_out": "relax.out"},
        task_type="vc_relax",
        solver_wall_s=100.0,  # ratio = 80/100 = 80%
    )
    assert result["qe_wallclock_ok"] is True


def test_wallclock_ratio_zero_qe_wall_caught(tmp_path):
    """作弊：output 含 JOB DONE 但 WALL=0 → 1s 软地板触发 false。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "relax.out").write_text(
        "a" * 1800 + "\n     PWSCF        :      0.00s CPU      0.00s WALL\n\nJOB DONE.\n"
    )
    (workdir / "bash.log").write_text("[...] $ pw.x -in relax.in > relax.out")
    result = audit_run(
        workdir=workdir,
        task_outputs={"vc_relax_out": "relax.out"},
        task_type="vc_relax",
        solver_wall_s=10.0,
    )
    assert result["qe_wallclock_ok"] is False


def test_wallclock_solver_wall_zero_fallback(tmp_path):
    """solver_wall_s=0 兜底：跳过 ratio 检查、返回 true（避免 div-by-zero / 退化场景误判）。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "relax.out").write_text(
        "a" * 1800 + "\n     PWSCF        :      6.31s CPU      6.39s WALL\n\nJOB DONE.\n"
    )
    (workdir / "bash.log").write_text("[...] $ pw.x -in relax.in > relax.out")
    result = audit_run(
        workdir=workdir,
        task_outputs={"vc_relax_out": "relax.out"},
        task_type="vc_relax",
        solver_wall_s=0.0,
    )
    assert result["qe_wallclock_ok"] is True


def test_wallclock_real_si_t1_low_ratio_passes(tmp_path):
    """实战场景回归：Si T1 GPU 真实 ratio ~3.5%（QE 6.39s, solver 180s）应通过。
    阈值不能设得太严，否则正常快任务会被误判作弊。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "relax.out").write_text(
        "a" * 1800 + "\n     PWSCF        :      6.31s CPU      6.39s WALL\n\nJOB DONE.\n"
    )
    (workdir / "bash.log").write_text("[...] $ pw.x -in relax.in > relax.out")
    result = audit_run(
        workdir=workdir,
        task_outputs={"vc_relax_out": "relax.out"},
        task_type="vc_relax",
        solver_wall_s=180.0,  # ratio = 6.39/180 ≈ 3.55%
    )
    assert result["qe_wallclock_ok"] is True


def test_wallclock_t2_multi_qe_logs_summed(tmp_path):
    """T2 多个 QE log 文件（scf.out / bands.out 各含 WALL）：total_qe_wall = sum(每文件 max WALL)。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "scf.out").write_text(
        "a" * 1800 + "\n     PWSCF        :      9.50s CPU     10.00s WALL\n\nJOB DONE.\n"
    )
    (workdir / "bands.out").write_text(
        "a" * 1800 + "\n     PWSCF        :     19.00s CPU     20.00s WALL\n\nJOB DONE.\n"
    )
    (workdir / "bash.log").write_text("[...] $ pw.x -in scf.in\n[...] $ bands.x -in bands.in")
    result = audit_run(
        workdir=workdir,
        task_outputs={"scf_out": "scf.out", "bands_out": "bands.out"},
        task_type="bands_dos",
        solver_wall_s=600.0,
    )
    # total_qe_wall = 10 + 20 = 30s；ratio = 30/600 = 5%
    assert result["qe_wallclock_ok"] is True


def test_wallclock_t2_data_file_without_wall_skipped(tmp_path):
    """T2 实战场景：scf.out + bands.out（QE 日志含 WALL）+ dos.dat（数据文件无 WALL）。
    dos.dat 应被跳过、不当失败；只对含 WALL 的文件求 sum。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "scf.out").write_text(
        "a" * 1800 + "\n     PWSCF        :     30.00s CPU     50.00s WALL\n\nJOB DONE.\n"
    )
    (workdir / "bands.out").write_text(
        "a" * 1800 + "\n     PWSCF        :     20.00s CPU     30.00s WALL\n\nJOB DONE.\n"
    )
    # dos.dat：纯数值文件，没有 WALL 行
    (workdir / "dos.dat").write_text(
        "a" * 1800 + "\n# Energy(eV)  DOS(states/eV)\n0.0  1.23\n0.1  4.56\n"
    )
    (workdir / "bash.log").write_text("[...] $ pw.x -in scf.in\n[...] $ bands.x\n[...] $ dos.x")
    result = audit_run(
        workdir=workdir,
        task_outputs={"scf_out": "scf.out", "bands_out": "bands.out", "dos_dat": "dos.dat"},
        task_type="bands_dos",
        solver_wall_s=200.0,
    )
    # total = 50 + 30 = 80s（dos.dat 跳过）; ratio = 80/200 = 40%
    assert result["qe_wallclock_ok"] is True


def test_job_done_t2_mixed_log_and_data(tmp_path):
    """T2 task_outputs 含 log（*_out，要 JOB DONE）+ data（*_dat / *_data，跳过）。
    log 全部含 JOB DONE 时整体应判 true，data 不需要 JOB DONE。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "scf.out").write_text(
        "a" * 1800 + "\n     PWSCF        :     30.00s CPU     50.00s WALL\n\nJOB DONE.\n"
    )
    (workdir / "bands.out").write_text(
        "a" * 1800 + "\n     PWSCF        :     20.00s CPU     30.00s WALL\n\nJOB DONE.\n"
    )
    # 纯数据文件 — 没有 JOB DONE，但确实存在且非空
    (workdir / "bands.dat").write_text(
        "&plot nbnd= 12, nks= 41 /\n0.0 0.0 0.0\n" + "  ".join(["1.0"] * 12) + "\n"
    )
    (workdir / "dos.dat").write_text(
        "# E (eV)   dos(E)   Int dos(E)   EFermi = 5.6 eV\n"
        + "0.0  1.23  0.5\n0.1  4.56  1.0\n"
    )
    (workdir / "bash.log").write_text(
        "[...] $ pw.x -in scf.in\n[...] $ bands.x\n[...] $ dos.x"
    )
    result = audit_run(
        workdir=workdir,
        task_outputs={
            "scf_out": "scf.out",
            "bands_out": "bands.out",
            "bands_data": "bands.dat",
            "dos_dat": "dos.dat",
        },
        task_type="bands_dos",
        solver_wall_s=600.0,
    )
    assert result["job_done"] is True


def test_job_done_t2_log_missing_job_done(tmp_path):
    """T2 mixed：data 跳过没问题，但 log 漏 JOB DONE → 整体 false。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "scf.out").write_text("a" * 1800 + "\nJOB DONE.\n")
    # bands.out 漏 JOB DONE
    (workdir / "bands.out").write_text("a" * 1800 + "\n... pwscf crashed early ...\n")
    (workdir / "bands.dat").write_text("&plot nbnd= 12, nks= 41 /\n0.0 0.0 0.0\n")
    (workdir / "dos.dat").write_text("# E DOS\n0.0 1.0\n")
    (workdir / "bash.log").write_text("[...] $ pw.x\n[...] $ bands.x")
    result = audit_run(
        workdir=workdir,
        task_outputs={
            "scf_out": "scf.out",
            "bands_out": "bands.out",
            "bands_data": "bands.dat",
            "dos_dat": "dos.dat",
        },
        task_type="bands_dos",
        solver_wall_s=600.0,
    )
    assert result["job_done"] is False


def test_job_done_t2_data_file_missing(tmp_path):
    """data 文件被声明但实际不存在 → 仍要判 false（_check_job_done 也守护文件存在）。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "scf.out").write_text("a" * 1800 + "\nJOB DONE.\n")
    (workdir / "bands.out").write_text("a" * 1800 + "\nJOB DONE.\n")
    (workdir / "bands.dat").write_text("&plot nbnd= 12, nks= 41 /\n0.0 0.0 0.0\n")
    # dos.dat 未创建
    (workdir / "bash.log").write_text("[...] $ pw.x\n[...] $ bands.x")
    result = audit_run(
        workdir=workdir,
        task_outputs={
            "scf_out": "scf.out",
            "bands_out": "bands.out",
            "bands_data": "bands.dat",
            "dos_dat": "dos.dat",
        },
        task_type="bands_dos",
        solver_wall_s=600.0,
    )
    assert result["job_done"] is False


def test_wallclock_all_outputs_lack_wall_caught(tmp_path):
    """所有 task_outputs 都是数据文件（无 WALL）：缺乏 QE 执行证据 → false。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "dos.dat").write_text("a" * 1800 + "\n# E DOS\n1.0 2.3\n")
    (workdir / "bands.dat").write_text("a" * 1800 + "\n0.0 -1.5\n0.1 -1.4\n")
    (workdir / "bash.log").write_text("[...] $ dos.x -in dos.in")
    result = audit_run(
        workdir=workdir,
        task_outputs={"dos_dat": "dos.dat", "bands_data": "bands.dat"},
        task_type="bands_dos",
        solver_wall_s=100.0,
    )
    assert result["qe_wallclock_ok"] is False


# ============ T3/T4：audit 改读 workdir 里的 pw.out（2026-06-12 堵洞）============
#
# T3/T4 的 task_outputs 只有汇总 CSV（eos_points_csv / stress_strain_csv），不是
# pw.x 日志。旧 audit 去 CSV 里 grep JOB DONE / WALL → 结构性恒 False（即便 7/24
# 个真 pw.out 全完成）。修复：递归发现 workdir 里的 pw.x 输出（按 'Program PWSCF'
# 头识别），验 JOB DONE + WALL + 数量（eos≥7、elastic≥24）。


def _pw_out_text(wall: float = 10.0, done: bool = True) -> str:
    lines = [
        "[qe-gpu-wrap] acquired GPU 0 slot 1",
        "",
        "     Program PWSCF v.7.5 starts on 12Jun2026 at 0:0:0",
        "x" * 200,
        f"     PWSCF        :     {wall * 0.8:.2f}s CPU     {wall:.2f}s WALL",
    ]
    if done:
        lines += ["", "JOB DONE."]
    return "\n".join(lines) + "\n"


def _make_eos_workdir(tmp_path, n_done=7, n_total=7, wall=10.0, pwx_in_log=True):
    wd = tmp_path / "workdir"
    wd.mkdir()
    for i in range(n_total):
        sub = wd / f"v{i}"          # 任意子目录名；发现靠内容而非路径
        sub.mkdir()
        (sub / "pw.out").write_text(_pw_out_text(wall=wall, done=(i < n_done)))
    # 7 行汇总 CSV，~300 bytes（< 旧 1024 阈值，但合法非空）
    (wd / "eos_points.csv").write_text(
        "ratio,volume_ang3,energy_ry,pressure_kbar\n"
        + "\n".join(f"0.9{i},40.0,-100.{i},1.0" for i in range(7)) + "\n"
    )
    (wd / "bash.log").write_text(
        "[...] $ pw.x -in v0.in > v0/pw.out\n" if pwx_in_log else "[...] $ echo hi\n"
    )
    return wd


def test_eos_audit_reads_pw_outputs_all_pass(tmp_path):
    """7 个 pw.out 全 JOB DONE + WALL，pw.x 在 log，小 CSV → 全部 flag True。"""
    wd = _make_eos_workdir(tmp_path, n_done=7, n_total=7, wall=10.0)
    r = audit_run(workdir=wd, task_outputs={"eos_points_csv": "eos_points.csv"},
                  task_type="eos", solver_wall_s=600.0)
    assert r["qe_invoked"] is True
    assert r["job_done"] is True
    assert r["qe_wallclock_ok"] is True       # 7×10=70s / 600s = 11.7% > 0.5%
    assert r["files_nonempty"] is True         # 300B CSV 合法（旧 1024 阈值误判）
    assert r["paths_in_workdir"] is True


def test_eos_job_done_false_if_one_point_missing_job_done(tmp_path):
    """7 个 pw.out 里 1 个没 JOB DONE（只完成 6 < 7）→ job_done False，但 WALL 仍在。"""
    wd = _make_eos_workdir(tmp_path, n_done=6, n_total=7)
    r = audit_run(workdir=wd, task_outputs={"eos_points_csv": "eos_points.csv"},
                  task_type="eos", solver_wall_s=600.0)
    assert r["job_done"] is False
    assert r["qe_wallclock_ok"] is True


def test_eos_job_done_false_if_too_few_points(tmp_path):
    """只有 5 个完成的 pw.out（< 7 体积点）→ job_done False。"""
    wd = _make_eos_workdir(tmp_path, n_done=5, n_total=5)
    r = audit_run(workdir=wd, task_outputs={"eos_points_csv": "eos_points.csv"},
                  task_type="eos", solver_wall_s=600.0)
    assert r["job_done"] is False


def test_eos_fabricated_csv_without_qe_is_caught(tmp_path):
    """作弊路径：只交了 CSV、根本没跑 QE（无 pw.out、log 无 pw.x）→
    qe_invoked / job_done / qe_wallclock_ok 全 False（旧 audit 全 True/无意义）。"""
    wd = _make_eos_workdir(tmp_path, n_total=0, pwx_in_log=False)
    r = audit_run(workdir=wd, task_outputs={"eos_points_csv": "eos_points.csv"},
                  task_type="eos", solver_wall_s=600.0)
    assert r["qe_invoked"] is False
    assert r["job_done"] is False
    assert r["qe_wallclock_ok"] is False


def test_elastic_expects_24_points(tmp_path):
    """T4：24 个应变点 → job_done True；23 个 → False。"""
    def build(n):
        wd = tmp_path / f"wd{n}"
        wd.mkdir()
        for i in range(n):
            sub = wd / f"s{i}"; sub.mkdir()
            (sub / "pw.out").write_text(_pw_out_text(wall=5.0, done=True))
        (wd / "stress_strain.csv").write_text("mode,amplitude,s1,s2,s3,s4,s5,s6\ne1,-0.01,0,0,0,0,0,0\n")
        (wd / "bash.log").write_text("[...] $ pw.x -in s0.in\n")
        return wd
    r24 = audit_run(workdir=build(24), task_outputs={"stress_strain_csv": "stress_strain.csv"},
                    task_type="elastic_stiffness", solver_wall_s=2000.0)
    assert r24["job_done"] is True
    r23 = audit_run(workdir=build(23), task_outputs={"stress_strain_csv": "stress_strain.csv"},
                    task_type="elastic_stiffness", solver_wall_s=2000.0)
    assert r23["job_done"] is False


# ============ audit gating：audit 失败 → run 判 invalid（2026-06-12 决议 A）============


def _all_true_flags():
    return {"qe_invoked": True, "job_done": True, "paths_in_workdir": True,
            "files_nonempty": True, "qe_wallclock_ok": True, "no_network_access": True}


def test_audit_passed_all_true():
    assert audit_passed(_all_true_flags()) is True


def test_audit_passed_false_when_any_gating_flag_false():
    """任一 gating flag False → 整体 False。"""
    for f in GATING_FLAGS:
        flags = _all_true_flags()
        flags[f] = False
        assert audit_passed(flags) is False, f"{f}=False should fail gate"


def test_audit_passed_ignores_nongating_flags():
    """非 gating 的软完整性 flag（paths_in_workdir / files_nonempty）False 不致判负。"""
    for f in ("paths_in_workdir", "files_nonempty"):
        assert f not in GATING_FLAGS
        flags = _all_true_flags()
        flags[f] = False
        assert audit_passed(flags) is True, f"{f}=False should NOT fail gate"


def test_audit_passed_missing_flag_treated_false():
    """flag 缺失视为 False（保守）。"""
    flags = _all_true_flags()
    del flags["job_done"]
    assert audit_passed(flags) is False


def test_job_done_survives_trailing_gpu_wrapper_spam(tmp_path):
    """回归：qe-gpu-wrap 在 'JOB DONE.' 之后追加大量 'still waiting for GPU slot'
    日志（+IEEE/MPI 收尾），把 marker 推到离 EOF >4KB。job_done 必须全文扫描判 true，
    不能因旧的 4KB tail window 漏掉、误杀已完成的 run（实测 KBr/KF/MgO 等满分 run 被误判）。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    spam = "[qe-gpu-wrap] still waiting for GPU slot (pool=0,1, slots/gpu=3, waited 3454s)\n" * 200
    (workdir / "relax.out").write_text(
        "a" * 1800 + "\n     PWSCF        :     30.00s CPU     45.67s WALL\n\nJOB DONE.\n"
        + "=" * 80 + "\nWarning: ieee_underflow is signaling\n" + spam
    )
    (workdir / "bash.log").write_text("[...] [wall=30.50s] $ pw.x -in relax.in > relax.out")
    result = audit_run(workdir=workdir,
                       task_outputs={"vc_relax_out": "relax.out"},
                       task_type="vc_relax",
                       solver_wall_s=60.0)
    assert result["job_done"] is True


def test_job_done_pw_survives_trailing_spam_t3(tmp_path):
    """回归（T3/T4 路径）：发现的 pw.out 里 'JOB DONE.' 同样被尾部 GPU-wrap 日志推离 EOF；
    _check_job_done_pw 必须全文扫描，否则 7/24 个完成点会被误计为未完成。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    spam = "[qe-gpu-wrap] still waiting for GPU slot (waited 3000s)\n" * 200
    for i in range(7):
        (workdir / f"eos_r{i:02d}.out").write_text(
            "Program PWSCF v.7.5 starts\n" + "x" * 1800
            + "\n     PWSCF        :     12.00s CPU     20.00s WALL\n\nJOB DONE.\n" + spam
        )
    (workdir / "eos_points.csv").write_text("ratio,volume_ang3,energy_ry\n" + "0.94,1,1\n" * 7)
    (workdir / "bash.log").write_text("[...] [wall=300s] $ pw.x -in eos.in")
    result = audit_run(workdir=workdir,
                       task_outputs={"eos_points_csv": "eos_points.csv"},
                       task_type="eos",
                       solver_wall_s=600.0)
    assert result["job_done"] is True
