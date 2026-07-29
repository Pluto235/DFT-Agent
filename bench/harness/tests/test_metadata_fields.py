"""Verify metadata fields (is_stable / tags) round-trip through task JSON loading."""
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
QUESTIONS_DIR = REPO_ROOT / "questions"

# Issue cases 清单（is_stable=false → web-fresh normal role 不可见）
T1_ISSUE_CASES_6 = [
    "M1_T1_BN_hex_vc_relax",
    "M1_T1_Zn_vc_relax",
    "M1_T1_InAs_vc_relax",
    "M1_T1_K_vc_relax",
    "M1_T1_In_vc_relax",
    "M1_T1_Sn_beta_vc_relax",
]

T2_ISSUE_CASES_15 = [
    # 4 not_converged + 7 failed_kept_mp + 2 marginal + 2 tetra_failed
    "M1_T2_Ag_bands_dos",
    "M1_T2_Au_bands_dos",
    "M1_T2_Be_bands_dos",
    "M1_T2_Cu_bands_dos",        # tetra_failed (0506 rerun)
    "M1_T2_GaAs_bands_dos",
    "M1_T2_InAs_bands_dos",
    "M1_T2_InSb_bands_dos",
    "M1_T2_In_bands_dos",
    "M1_T2_KCl_bands_dos",
    "M1_T2_K_bands_dos",
    "M1_T2_Mg_bands_dos",        # marginal
    "M1_T2_Pb_bands_dos",        # marginal
    "M1_T2_Sn_beta_bands_dos",
    "M1_T2_Zn_bands_dos",
    "M1_T2_ZnO_bands_dos",       # tetra_failed (0506 rerun)
]


def _load_task(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_v1_3_questions_have_metadata_field():
    """All 116 retained v1.3 questions must include top-level metadata."""
    paths = list(QUESTIONS_DIR.glob("M1_T[12]/*.json"))
    assert len(paths) == 116, f"expected 116 v1.3 questions, got {len(paths)}"
    missing = [p.name for p in paths if "metadata" not in _load_task(p)]
    assert missing == [], f"questions missing metadata: {missing[:5]}..."


def test_v1_3_questions_default_is_stable_missing():
    """Only the 21 retained issue cases may set is_stable=false."""
    sample_t1 = _load_task(QUESTIONS_DIR / "M1_T1" / "M1_T1_Si_vc_relax.json")
    sample_t2 = _load_task(QUESTIONS_DIR / "M1_T2" / "M1_T2_Si_bands_dos.json")
    # Si T1 / T2 都不在 issue cases 27 道里——不应有 is_stable
    assert "is_stable" not in sample_t1["metadata"], (
        "Si T1 不属于 issue cases；不应有 is_stable 字段"
    )
    assert "is_stable" not in sample_t2["metadata"], (
        "Si T2 不属于 issue cases；不应有 is_stable 字段"
    )


def test_metadata_extension_writable_and_readable(tmp_path):
    """metadata 顶层支持 is_stable / tags / physics_notes / suggested_* 5 新字段。"""
    sample_path = QUESTIONS_DIR / "M1_T1" / "M1_T1_Si_vc_relax.json"
    task = _load_task(sample_path)
    # 注入 5 新字段
    task["metadata"]["is_stable"] = False
    task["metadata"]["tags"] = ["magnetic", "placeholder_gt"]
    task["metadata"]["physics_notes"] = "Type-II AFM"
    task["metadata"]["suggested_initial_moments"] = {"Ni1": 2.0, "Ni2": -2.0, "O": 0.0}
    task["metadata"]["suggested_hubbard_u"] = {"Ni": 6.5}
    # 写入临时文件再读回
    out = tmp_path / "test.json"
    out.write_text(json.dumps(task, indent=2), encoding="utf-8")
    reloaded = _load_task(out)
    assert reloaded["metadata"]["is_stable"] is False
    assert reloaded["metadata"]["tags"] == ["magnetic", "placeholder_gt"]
    assert reloaded["metadata"]["physics_notes"] == "Type-II AFM"
    assert reloaded["metadata"]["suggested_initial_moments"]["Ni1"] == 2.0
    assert reloaded["metadata"]["suggested_hubbard_u"]["Ni"] == 6.5


def test_t1_issue_cases_marked_unstable():
    """6 道 T1 issue cases (4 FAIL + 2 marginal Δl_rel>1%) 必须 metadata.is_stable=false。"""
    for tid in T1_ISSUE_CASES_6:
        task = _load_task(QUESTIONS_DIR / "M1_T1" / f"{tid}.json")
        assert task["metadata"].get("is_stable") is False, (
            f"{tid} 应 metadata.is_stable=false（issue case 隐身），实际 {task['metadata'].get('is_stable')}"
        )


def test_t2_issue_cases_marked_unstable():
    """15 道 M1 T2 non-converged 题目必须 metadata.is_stable=false。

    0506 tetra rerun 后：5 道 fixed-occ failed 但 tetra NSCF 成功（AlSb / InP / Li / Na / NaCl）
    flip 回 visible；2 道 fixed OK 但 tetra fail（Cu / ZnO）新加 issue。净 21 - 5 + 2 = 18。
    """
    for tid in T2_ISSUE_CASES_15:
        task = _load_task(QUESTIONS_DIR / "M1_T2" / f"{tid}.json")
        gq = task["metadata"].get("gt_quality")
        assert task["metadata"].get("is_stable") is False, (
            f"{tid} 应 metadata.is_stable=false (gt_quality={gq})，实际 {task['metadata'].get('is_stable')}"
        )


def test_t2_converged_questions_have_no_is_stable_flag():
    """38 道 M1 T2 self_run_converged 题不应被误标 is_stable=false（防误标）。

    0506 tetra rerun 后 self_run_converged 65 → 63（Cu/ZnO 移到 self_run_tetra_failed）；
    rescue 5 道（AlSb/InP/Li/Na/NaCl）gt_quality 改为 self_run_tetra_rescued，不在此覆盖范围内。
    """
    paths = list(QUESTIONS_DIR.glob("M1_T2/*.json"))
    converged = [
        p for p in paths
        if _load_task(p)["metadata"].get("gt_quality") == "self_run_converged"
    ]
    assert len(converged) == 38, f"M1 T2 self_run_converged 应 38 道，实际 {len(converged)}"
    mismarked = [
        p.stem for p in converged
        if _load_task(p)["metadata"].get("is_stable") is False
    ]
    assert mismarked == [], (
        f"T2 self_run_converged 题不应有 is_stable=false（误标），实际 {mismarked}"
    )


def test_t2_tetra_rescued_visible():
    """0506 tetra rerun 抢救的 5 道（AlSb/InP/Li/Na/NaCl）应 visible 且 gt_quality=self_run_tetra_rescued。"""
    rescued = ["M1_T2_AlSb_bands_dos", "M1_T2_InP_bands_dos", "M1_T2_Li_bands_dos",
               "M1_T2_Na_bands_dos", "M1_T2_NaCl_bands_dos"]
    for tid in rescued:
        task = _load_task(QUESTIONS_DIR / "M1_T2" / f"{tid}.json")
        md = task["metadata"]
        assert md.get("is_stable") is not False, (
            f"{tid} 是 tetra rescue 道，不应 is_stable=false，实际 {md.get('is_stable')!r}"
        )
        assert md.get("gt_quality") == "self_run_tetra_rescued", (
            f"{tid} gt_quality 应 self_run_tetra_rescued，实际 {md.get('gt_quality')}"
        )


def test_v1_3_normal_visible_count_95():
    """116 retained questions minus 21 issue cases leaves 95 visible tasks."""
    paths = list(QUESTIONS_DIR.glob("M1_T[12]/*.json"))
    assert len(paths) == 116, f"v1.3 物理题数应 116，实际 {len(paths)}"
    visible = [p for p in paths if _load_task(p)["metadata"].get("is_stable", True)]
    assert len(visible) == 95, f"normal user 可见题数应 95（116-21），实际 {len(visible)}"
    hidden = [p.stem for p in paths if not _load_task(p)["metadata"].get("is_stable", True)]
    expected_hidden = sorted(T1_ISSUE_CASES_6 + T2_ISSUE_CASES_15)
    assert sorted(hidden) == expected_hidden, (
        f"hidden 题不对：实际 {sorted(hidden)} vs 期望 {expected_hidden}"
    )
