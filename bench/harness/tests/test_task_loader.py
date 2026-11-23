import json
import subprocess
import sys
from pathlib import Path
import pytest

from harness.lib.task_loader import (
    derive_initial_files,
    get_network_allowed,
    load_task,
    render_prompt,
)


BENCH_ROOT = Path(__file__).parent.parent.parent


def test_load_si_t1():
    task = load_task(BENCH_ROOT, "M1_T1_Si_vc_relax")
    assert task["id"] == "M1_T1_Si_vc_relax"
    assert task["material"]["formula"] == "Si"
    assert task["task"]["type"] == "vc_relax"


def test_load_unknown_task_errors():
    with pytest.raises(FileNotFoundError):
        load_task(BENCH_ROOT, "M9_T9_fake_task")


def test_render_prompt_contains_formula_and_slots():
    task = load_task(BENCH_ROOT, "M1_T1_Si_vc_relax")
    rendered = render_prompt(BENCH_ROOT, task)
    assert "Si" in rendered
    # 初始结构渲染为 CELL_PARAMETERS + ATOMIC_POSITIONS 文本块
    assert "CELL_PARAMETERS" in rendered or "celldm" in rendered or "lattice" in rendered.lower()
    assert "ATOMIC_POSITIONS" in rendered or "Si " in rendered
    assert "Si.upf" in rendered
    # submit 指引应被追加
    assert "submit" in rendered.lower()
    assert "vc_relax_out" in rendered  # T1 required key


def test_render_prompt_t1_submit_keys():
    """T1 要求 task_outputs 含 vc_relax_out 单个 key。"""
    task = load_task(BENCH_ROOT, "M1_T1_Si_vc_relax")
    rendered = render_prompt(BENCH_ROOT, task)
    # 检查 submit 指引块里明确列出 key
    assert '"vc_relax_out"' in rendered


def test_get_network_allowed_v1_3_default_false():
    """v1.3 schema 默认 task.policy.network_allowed=false。"""
    task = load_task(BENCH_ROOT, "M1_T1_Si_vc_relax")
    assert task["task"]["policy"]["network_allowed"] is False
    assert get_network_allowed(task) is False


def test_get_network_allowed_v1_3_explicit_true():
    """task.policy.network_allowed=true 的题目（构造）→ helper 返回 True。"""
    task = {"task": {"type": "vc_relax", "prompt": None,
                     "policy": {"network_allowed": True}}}
    assert get_network_allowed(task) is True


def test_get_network_allowed_v1_2_legacy_fallback_false():
    """v1.2 老 schema（无 task.policy 字段）→ 兜底返回 False（兼容）。"""
    legacy_task = {"task": {"type": "vc_relax", "prompt": None}}
    assert get_network_allowed(legacy_task) is False


def test_render_prompt_includes_network_policy_when_disallowed():
    """v1.3 默认 task → rendered prompt 含 'Network Policy' 段。"""
    task = load_task(BENCH_ROOT, "M1_T1_Si_vc_relax")
    rendered = render_prompt(BENCH_ROOT, task)
    assert "Network Policy" in rendered
    assert "curl" in rendered or "wget" in rendered  # 黑名单关键词之一


def test_render_prompt_omits_network_policy_when_allowed():
    """task.policy.network_allowed=true → rendered prompt 不含 Network Policy 段。"""
    task = load_task(BENCH_ROOT, "M1_T1_Si_vc_relax")
    task["task"]["policy"]["network_allowed"] = True
    rendered = render_prompt(BENCH_ROOT, task)
    assert "Network Policy" not in rendered


def test_render_prompt_t2_submit_keys():
    """T2 (bands_dos) 要求 task_outputs 含 5 个 key：scf_out / bands_out / bands_data / dos_dat / nscf_dos_out。

    防退化保护：bands_data 是 bands.x 后处理的 .dat 文件（含每条 k 路径上每条带的本征值），
    evaluator 提取 band_gap_ev / vbm_k_label / cbm_k_label 必需。早期版本曾漏过此 key
    导致 evaluator 报 `task_outputs missing keys ['bands_data']`。nscf_dos_out 是 DOS-NSCF
    pw.x 输出，evaluator 只从它解析 dos_e_fermi（无 fallback）；提交清单曾漏列此 key，
    导致 run.py 抽取阶段报 `task_outputs missing required keys ['nscf_dos_out']`。
    """
    task = load_task(BENCH_ROOT, "M1_T2_Si_bands_dos")
    rendered = render_prompt(BENCH_ROOT, task)
    for key in ('"scf_out"', '"bands_out"', '"bands_data"', '"dos_dat"', '"nscf_dos_out"'):
        assert key in rendered, f"T2 prompt 缺 submit key {key}"
    # 也确保示例 JSON 块里包含所有 5 个 key
    assert "bands.dat" in rendered  # 描述里提到 bands.dat 文件名
    assert "filband" in rendered    # 提示 agent 在 bands.x input 里设 filband


def test_derive_initial_files_si_t1():
    """T1 单元素 Si → task_prompt.txt + pseudos/Si.upf 共 2 项。"""
    task = load_task(BENCH_ROOT, "M1_T1_Si_vc_relax")
    files = derive_initial_files(BENCH_ROOT, task)
    assert len(files) == 2
    names = [f["name"] for f in files]
    assert names == ["task_prompt.txt", "pseudos/Si.upf"]
    # task_prompt 字节数 = render_prompt 输出 utf-8 长度
    assert files[0]["size"] == len(render_prompt(BENCH_ROOT, task).encode("utf-8"))
    assert files[0]["size"] > 100  # 渲染 prompt 至少几百字节
    # setup.sh 尚未下载赝势时 size 为 None；下载后必须是非空文件。
    assert files[1]["size"] is None or files[1]["size"] > 0
    assert files[1]["description"] == "Si pseudopotential (GBRV_USPP_PBE_v1.5)"


def test_derive_initial_files_t2_multi_element():
    """T2 多元素 GaAs → task_prompt.txt + 两个赝势文件。"""
    task = load_task(BENCH_ROOT, "M1_T2_GaAs_bands_dos")
    files = derive_initial_files(BENCH_ROOT, task)
    assert len(files) == 3
    names = sorted(f["name"] for f in files)
    assert names == ["pseudos/As.upf", "pseudos/Ga.upf", "task_prompt.txt"]
    # setup.sh 尚未下载赝势时 size 为 None；下载后必须是非空文件。
    for f in files:
        if f["name"].startswith("pseudos/"):
            assert f["size"] is None or f["size"] > 0
            assert "(GBRV_USPP_PBE_v1.5)" in f["description"]


def test_cli_lists_only_loadable_tasks_without_setup(tmp_path):
    output = subprocess.check_output(
        [sys.executable, "-S", str(BENCH_ROOT / "harness/run.py"), "--list-tasks"],
        cwd=tmp_path, text=True,
    )
    expected = sorted(
        json.loads(path.read_text())["id"]
        for path in (BENCH_ROOT / "questions").glob("*/*.json")
    )
    assert output.splitlines() == expected
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(("subset", "count"), [
    ("stable", 245), ("exploratory", 30), ("held_out", 41), ("frozen", 100),
])
def test_release_subset(subset, count):
    ids = subprocess.check_output(
        [sys.executable, str(BENCH_ROOT / "harness/run.py"), "--list-tasks", subset],
        text=True,
    ).splitlines()
    assert len(set(ids)) == len(ids) == count
    if subset == "frozen":
        import csv
        with (BENCH_ROOT.parent / "results/frozen_matched_scores.csv").open() as scores:
            assert set(ids) == {row["task_id"] for row in csv.DictReader(scores)}
    else:
        for task_id in ids:
            task = load_task(BENCH_ROOT, task_id)
            assert (task["metadata"].get("is_stable") is False) == (subset == "held_out")
            assert task_id.startswith("M3_") == (subset == "exploratory")


def test_release_has_inputs_for_each_scored_task():
    pseudos = BENCH_ROOT / "pseudos/GBRV_USPP_PBE_v1.5/MANIFEST.json"
    filenames = {entry["canonical_name"] for entry in json.loads(pseudos.read_text())["files"]}
    for path in (BENCH_ROOT / "questions").glob("M*_T*/*.json"):
        task = load_task(BENCH_ROOT, path.stem)
        assert task["id"] == path.stem
        assert path.parent.name == "_".join(task["id"].split("_")[:2])
        if isinstance(task["difficulty"], dict):
            assert path.parent.name == "_".join(task["difficulty"][k] for k in ("material_tier", "task_tier"))
        else:
            assert task["schema_version"] == "1.2" and task["difficulty"] == "M3"
        assert render_prompt(BENCH_ROOT, task).strip()
        assert set(task["material"]["pseudopotential"]["files"].values()) <= filenames
        if task["metadata"].get("is_stable") is False:
            continue
        for field, weight in task["scoring"]["field_weights"].items():
            if weight:
                value = task["ground_truth"][field]["value"]
                assert value is not None, (path.stem, field)
                if isinstance(value, dict) and "ref_file" in value:
                    assert (BENCH_ROOT / value["ref_file"].removeprefix("bench/")).stat().st_size
