"""Comparison report generator: MP GT vs canonical self-run GT.

Generates:
- comparison.csv (machine-readable, one row per M1 material)
- comparison.md (human-readable)
- convergence_summary.md (convergence status + distribution)
- gt_replacement_recommendation.md (which materials to replace)
- stats.md (histograms + GPU utilization)
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH_ROOT = HERE.parent
BENCH_T1_DIR = BENCH_ROOT / "questions"
CANONICAL_GT_DIR = HERE / "canonical_gt"
REPORTS_DIR = HERE / "reports"

# D2 tolerances (from settings_plan.md §6.1)
D2_LATTICE_REL = 0.010   # 1%
D2_ANGLE_ABS = 1.0       # 1°


CSV_COLUMNS = [
    "id", "formula", "tier", "category",
    "overall_status", "convergence_status",
    "delta_lattice_max_relative", "delta_lattice_max_angstrom",
    "ecutwfc_conv", "kmesh_conv_density",
    "a_mp", "a_self", "da_abs", "da_rel",
    "b_mp", "b_self", "db_abs", "db_rel",
    "c_mp", "c_self", "dc_abs", "dc_rel",
    "alpha_mp", "alpha_self", "dalpha_abs",
    "beta_mp", "beta_self", "dbeta_abs",
    "gamma_mp", "gamma_self", "dgamma_abs",
    "sg_mp", "sg_self", "sg_preserved",
    "point_group_mp", "point_group_self",
    "crystal_system_mp", "crystal_system_self",
    "within_d2_tolerance", "recommend_replace_mp_gt",
]


def compute_comparison(
    material_id: str,
    formula: str,
    tier: str,
    category: str,
    mp_gt: dict,
    self_gt: dict,
    convergence_status: str,
    ecutwfc_conv: float | None = None,
    kmesh_conv_density: float | None = None,
    delta_lattice_max_angstrom: float | None = None,
    delta_lattice_max_relative: float | None = None,
    overall_status: str = "OK",
) -> dict:
    """Compute one row of the comparison CSV.

    Args:
        mp_gt: flat dict {a, b, c, alpha, beta, gamma, space_group_number, ...} from MP
        self_gt: flat dict from canonical GT
        convergence_status: "converged" / "marginal" / "not_converged" / "failed"
    """
    row: dict = {
        "id": material_id, "formula": formula, "tier": tier, "category": category,
        "overall_status": overall_status,
        "convergence_status": convergence_status,
        "delta_lattice_max_angstrom": delta_lattice_max_angstrom,
        "delta_lattice_max_relative": delta_lattice_max_relative,
        "ecutwfc_conv": ecutwfc_conv, "kmesh_conv_density": kmesh_conv_density,
    }
    # Lattice constants: MP, self, abs diff, rel diff
    all_rel_ok = True
    for key in ("a", "b", "c"):
        mp_v, self_v = mp_gt.get(key), self_gt.get(key)
        row[f"{key}_mp"] = mp_v
        row[f"{key}_self"] = self_v
        if mp_v is None or self_v is None:
            row[f"d{key}_abs"] = None
            row[f"d{key}_rel"] = None
            all_rel_ok = False
            continue
        row[f"d{key}_abs"] = abs(self_v - mp_v)
        row[f"d{key}_rel"] = abs(self_v - mp_v) / max(abs(mp_v), 1e-12)
        if row[f"d{key}_rel"] >= D2_LATTICE_REL:
            all_rel_ok = False

    # Angles
    all_ang_ok = True
    for key in ("alpha", "beta", "gamma"):
        mp_v, self_v = mp_gt.get(key), self_gt.get(key)
        row[f"{key}_mp"] = mp_v
        row[f"{key}_self"] = self_v
        if mp_v is None or self_v is None:
            row[f"d{key}_abs"] = None
            all_ang_ok = False
            continue
        row[f"d{key}_abs"] = abs(self_v - mp_v)
        if row[f"d{key}_abs"] >= D2_ANGLE_ABS:
            all_ang_ok = False

    # Symmetry
    sg_mp = mp_gt.get("space_group_number")
    sg_self = self_gt.get("space_group_number")
    row["sg_mp"] = sg_mp
    row["sg_self"] = sg_self
    sg_preserved = (sg_mp == sg_self) if sg_mp is not None and sg_self is not None else False
    row["sg_preserved"] = sg_preserved
    row["point_group_mp"] = mp_gt.get("point_group")
    row["point_group_self"] = self_gt.get("point_group")
    row["crystal_system_mp"] = mp_gt.get("crystal_system")
    row["crystal_system_self"] = self_gt.get("crystal_system")

    # Within D2 tolerance requires: all lattice rel < 1%, all angle abs < 1°, SG preserved
    within = all_rel_ok and all_ang_ok and sg_preserved
    row["within_d2_tolerance"] = within

    # Recommend replace: 3 strict conditions (matches methodology §2.4):
    #   1. self-run status == "converged" (data reliable)
    #   2. at least one continuous field fails D2 (MP-vs-self diverges past scoring threshold)
    #   3. space group preserved (relaxation didn't drop into a different phase)
    row["recommend_replace_mp_gt"] = (
        convergence_status == "converged"
        and (not within)
        and (row.get("sg_preserved") is True)
    )
    return row


def write_csv(rows: list[dict], out_path: Path) -> None:
    """Write comparison rows to CSV with CSV_COLUMNS header."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def flatten_mp_gt(bench_json: dict) -> dict:
    """Extract flat dict from bench JSON ground_truth."""
    gt = bench_json.get("ground_truth", {})
    result = {}
    for key in ("a", "b", "c", "alpha", "beta", "gamma"):
        result[key] = gt.get(key, {}).get("value")
    for key in ("space_group_number", "point_group", "crystal_system"):
        result[key] = gt.get(key, {}).get("value")
    return result


def flatten_canonical_gt(canonical_json: dict) -> dict:
    """Extract flat dict from canonical GT ground_truth."""
    return flatten_mp_gt(canonical_json)  # same schema, reuse


def write_comparison_md(rows: list[dict], out_path: Path) -> None:
    """Top-level comparison summary (human-readable, self-contained)."""
    n_total = len(rows)
    n_converged = sum(1 for r in rows if r["convergence_status"] == "converged")
    n_marginal = sum(1 for r in rows if r["convergence_status"] == "marginal")
    n_not_conv = sum(1 for r in rows if r["convergence_status"] == "not_converged")
    n_failed = sum(1 for r in rows if r["convergence_status"] == "failed")
    n_within_d2 = sum(1 for r in rows if r["within_d2_tolerance"])
    n_recommend = sum(1 for r in rows if r["recommend_replace_mp_gt"])

    # Tier grouping retained for report compatibility.
    from collections import defaultdict
    by_tier: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        tier = r.get("tier") or r["id"].split("_")[0]
        by_tier[tier].append(r)

    lines: list[str] = []
    lines.append("# DFT 几何 GT 数据对比汇总报告（M1 结构优化任务）")
    lines.append("")
    lines.append("## 这份报告是什么")
    lines.append("")
    lines.append(
        "我们的 DFT agent benchmark 当前使用 Materials Project（MP）数据库的几何作为每道题的"
        "标准答案（GT），但 MP 是用 VASP 跑出来的，agent 实际是用 Quantum ESPRESSO（QE）解题。"
        "本报告对每道题独立用 QE+GBRV USPP+PBE 跑一遍结构优化，得到一份\"自跑 GT\"，"
        "然后把 MP GT 与自跑 GT 在 9 个字段上做对比，"
        "判断 (1) 哪些题的 MP GT 在 QE 下可以复现、可继续使用，"
        "(2) 哪些题的 MP GT 与独立 DFT 偏差大、应替换为自跑 GT。"
    )
    lines.append("")
    lines.append("详细方法和每道题字段对比见 `comparison_detailed.md`。本报告是顶层汇总。")
    lines.append("")

    lines.append("## 总体数字")
    lines.append("")
    lines.append("| 维度 | 数量 | 说明 |")
    lines.append("|---|---|---|")
    lines.append(f"| 总材料数 | {n_total} | M1 T1 题（vc-relax 结构优化）|")
    lines.append(f"| 自跑 GT 收敛（converged）| {n_converged} | 自跑扫描末两点 Δl_rel < 1% |")
    lines.append(f"| 自跑 GT 边缘（marginal）| {n_marginal} | Δl_rel 1%-2% |")
    lines.append(f"| 自跑 GT 未收敛（not_converged）| {n_not_conv} | Δl_rel ≥ 2% |")
    lines.append(f"| 自跑 GT 失败（failed）| {n_failed} | 扫描中至少一个 QE 计算挂了 |")
    lines.append(f"| 全字段过判分阈值 | {n_within_d2} | 6 个连续字段（a/b/c/α/β/γ）都满足：长度 1%、角度 1° |")
    lines.append(f"| 建议替换 GT | {n_recommend} | 自跑收敛 + 至少 1 字段超阈值 + 空间群保持 |")
    lines.append("")

    # Tier table
    lines.append("## 按材料梯度分组的偏差统计")
    lines.append("")
    lines.append("说明：当前公开版只统计 M1 材料。")
    lines.append("`max(Δl/l)`：a/b/c 三个晶格轴中 MP vs 自跑 的最大相对偏差。"
                 "对各向异性结构（六方/正交/单斜...），不同轴敏感度差距大，所以取最大值才能反映真实偏差。")
    lines.append("")
    lines.append("| 梯度 | 材料数 | max(Δl/l) 该梯度峰值 | max(Δl/l) 该梯度均值 | 全字段过阈值 | 建议替换 |")
    lines.append("|---|---|---|---|---|---|")

    def _row_max_rel(r):
        return max((r.get(k) or 0) for k in ("da_rel", "db_rel", "dc_rel"))

    for tier in sorted(by_tier.keys()):
        tr = by_tier[tier]
        rels = [_row_max_rel(r) for r in tr if any(r.get(k) is not None for k in ("da_rel", "db_rel", "dc_rel"))]
        max_rel = max(rels) if rels else 0.0
        mean_rel = sum(rels) / len(rels) if rels else 0.0
        n_w = sum(1 for r in tr if r["within_d2_tolerance"])
        n_rec = sum(1 for r in tr if r["recommend_replace_mp_gt"])
        lines.append(f"| {tier} | {len(tr)} | {max_rel*100:.2f}% | {mean_rel*100:.2f}% | {n_w} | {n_rec} |")
    lines.append("")

    # Anomalies
    failed_rows = [r for r in rows if r["convergence_status"] in ("failed", "not_converged")]
    broken_rows = [r for r in rows if not r["sg_preserved"] and r.get("sg_self") is not None]
    def _row_max_abs(r):
        return max((r.get(k) or 0) for k in ("da_abs", "db_abs", "dc_abs"))

    big_delta_rows = sorted(
        [r for r in rows if _row_max_abs(r) > 0.020],
        key=lambda r: -_row_max_abs(r),
    )

    lines.append("## 异常清单")
    lines.append("")
    lines.append(f"### 自跑数据未收敛或失败（{len(failed_rows)}）")
    lines.append("")
    lines.append("这些材料的自跑 GT 不可信，不能用于替换 MP GT。具体不可信的原因（精度不够 vs QE 跑挂）见 `comparison_detailed.md` 的状态分桶。")
    lines.append("")
    for r in failed_rows:
        lines.append(f"- {r['id']} ({r['formula']}): {r['convergence_status']}")
    lines.append("")

    lines.append(f"### 自跑 GT 与 MP 空间群不一致（{len(broken_rows)}）")
    lines.append("")
    lines.append("自跑结构优化后空间群编号变化，意味着 QE+PBE 把原结构松弛到了对称性更低的另一个能量极小。"
                 "可能原因：题目结构是亚稳相 / 软晶格 / 对称性破缺敏感，或 PBE 对该相能序判断与 MP 用的方法学不一致。"
                 "这些材料自跑 GT 不应直接替换。")
    lines.append("")
    for r in broken_rows:
        lines.append(f"- {r['id']} ({r['formula']}): MP 空间群 {r['sg_mp']} → 自跑 {r['sg_self']}")
    lines.append("")

    lines.append(f"### MP vs 自跑：晶格轴绝对差 > 0.020 Å（前 20 名，共 {len(big_delta_rows)} 道）")
    lines.append("")
    lines.append("绝对差取 a/b/c 三个轴中的最大值。绝对差大不一定意味着错误——"
                 "也可能说明 PBE 在该体系本来就和 MP 设定不一致。"
                 "结合空间群是否保持、自跑是否收敛，判断是否替换。")
    lines.append("")
    for r in big_delta_rows[:20]:
        max_abs = _row_max_abs(r)
        max_rel = max((r.get(k) or 0) for k in ("da_rel", "db_rel", "dc_rel"))
        lines.append(f"- {r['id']} ({r['formula']}): max(|Δa|,|Δb|,|Δc|) = {max_abs:.4f} Å, "
                    f"max 相对差 = {max_rel*100:.2f}%")
    lines.append("")

    # Conclusion
    lines.append("## 结论")
    lines.append("")
    pct_within = 100 * n_within_d2 / n_total if n_total else 0
    pct_replace = 100 * n_recommend / n_total if n_total else 0
    lines.append(f"- **{n_within_d2}/{n_total}（{pct_within:.1f}%）** 材料 6 字段全部过判分阈值，"
                 f"MP GT 在 QE+PBE 下也能复现，可继续使用。")
    lines.append(f"- **{n_recommend}/{n_total}（{pct_replace:.1f}%）** 材料满足替换条件（自跑收敛 + 阈值不通过 + 空间群保持），"
                 f"建议用自跑 GT 替换 MP GT，名单见 `gt_replacement_recommendation.md`。")
    lines.append(f"- 余下 **{n_total - n_within_d2 - n_recommend}** 道材料：要么自跑数据本身不可靠（marginal/not_converged/failed），"
                 f"要么虽然收敛但空间群变化不可替换；这些题目的 GT 处理要逐个看 `comparison_detailed.md` 决定。")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_convergence_summary_md(rows: list[dict], out_path: Path) -> None:
    """Self-run data convergence summary (self-contained)."""
    from collections import Counter
    statuses = Counter(r["convergence_status"] for r in rows)
    ecutwfc_counter = Counter(r.get("ecutwfc_conv") for r in rows)
    kmesh_counter = Counter(r.get("kmesh_conv_density") for r in rows)

    lines: list[str] = []
    lines.append("# 自跑 GT 数据收敛性总结")
    lines.append("")
    lines.append("## 这份报告是什么")
    lines.append("")
    lines.append(
        "我们对 M1 结构优化题各跑了一组 8 个 QE 计算的 2D 收敛扫描"
        "（5 个 ecutwfc 截断能 × 1 个 k-mesh 密度 + 1 个 ecutwfc × 3 个 k-mesh 密度），"
        "把每道题的最高精度计算结果当作\"自跑 GT\"。"
        "本报告只衡量**自跑 GT 数据本身的可信度**——也就是看 8 个计算最末两个结果之间的差距，"
        "差距小说明再加大计算精度也不会动几何，自跑 GT 可靠；差距大说明几何还在变，自跑 GT 不可信。"
        "这与 MP vs 自跑 的对比是两回事，详细字段对比见 `comparison.md` 和 `comparison_detailed.md`。"
    )
    lines.append("")

    lines.append("## 1. 收敛状态分布")
    lines.append("")
    lines.append(
        "判据（杨博 0427 拍板）：扫描中最末两点的最大相对轴长漂移 "
        "Δl_rel = max(|Δa|/|a|, |Δb|/|b|, |Δc|/|c|)。"
    )
    lines.append("")
    lines.append("| 状态 | 数量 | 阈值 / 含义 |")
    lines.append("|---|---|---|")
    explanations = {
        "converged": "Δl_rel < 1% —— 几何已稳定，自跑 GT 可信",
        "marginal": "Δl_rel 1%-2% —— 接近收敛但仍有小幅漂移，自跑 GT 大体可用",
        "not_converged": "Δl_rel ≥ 2% —— 几何还在显著变化，自跑 GT 不可信",
        "failed": "扫描中至少一个 QE 计算挂了（BFGS 不收敛 / 对称性破缺出题目目标群 / 等）",
    }
    for status in ("converged", "marginal", "not_converged", "failed"):
        if status in statuses:
            lines.append(f"| {status} | {statuses[status]} | {explanations.get(status, '')} |")
    lines.append("")

    lines.append("## 2. 收敛点采用的 ecutwfc 分布")
    lines.append("")
    lines.append(
        "对每道题，取扫描中最高 ecutwfc 那个 scan point 作为\"收敛点\"。"
        "本报告这一版扫描的 ecutwfc 上限是 80 Ry，所以非失败材料应全部为 80。"
        "（注：如果未来扫描扩展到更高 ecutwfc，这一栏会反映新的上限。）"
    )
    lines.append("")
    lines.append("| ecutwfc (Ry) | n |")
    lines.append("|---|---|")
    for e, n in sorted(ecutwfc_counter.items(), key=lambda x: (x[0] is None, x[0])):
        label = "失败/无数据" if e is None else f"{e}"
        lines.append(f"| {label} | {n} |")
    lines.append("")

    lines.append("## 3. 收敛点采用的 k-mesh 密度分布")
    lines.append("")
    lines.append(
        "k-mesh 密度（单位 pts/Å⁻¹）越大，布里渊区采样越密。"
        "扫描第二轮固定 ecutwfc=80 Ry，扫 k-mesh 密度 ∈ {20, 30, 40, 50}。"
    )
    lines.append("")
    lines.append("| k-mesh 密度 (pts/Å⁻¹) | n |")
    lines.append("|---|---|")
    for d, n in sorted(kmesh_counter.items(), key=lambda x: (x[0] is None, x[0])):
        label = "失败/无数据" if d is None else f"{d}"
        lines.append(f"| {label} | {n} |")
    lines.append("")

    # Δl_rel histogram (主) + Δl_aa (审计)
    bins_rel = [(0, 0.005), (0.005, 0.01), (0.01, 0.02), (0.02, 0.05), (0.05, float("inf"))]
    lines.append("## 4. Δl_rel 分布（自跑数据漂移精度直方图）")
    lines.append("")
    lines.append("横轴：Δl_rel = max(|Δa|/|a|, |Δb|/|b|, |Δc|/|c|)（无量纲，杨博 0427 判据）。")
    lines.append("前两档（< 1%）属于 converged，自跑 GT 可信；"
                 "中档（1-2%）属于 marginal，可用但有小幅漂移；"
                 "后两档（≥ 2%）属于 not_converged，需要更高精度才能稳定。")
    lines.append("")
    lines.append("| Δl_rel 范围 | 数量 |")
    lines.append("|---|---|")
    for lo, hi in bins_rel:
        n = sum(1 for r in rows
                if r.get("delta_lattice_max_relative") is not None
                and lo <= r["delta_lattice_max_relative"] < hi)
        lo_pct = f"{lo*100:.1f}%"
        hi_pct = "∞" if hi == float("inf") else f"{hi*100:.1f}%"
        lines.append(f"| [{lo_pct}, {hi_pct}) | {n} |")
    lines.append("")
    bins = [(0, 0.001), (0.001, 0.005), (0.005, 0.010), (0.010, float("inf"))]
    lines.append("## 4b. Δl_max 绝对 Å 分布（审计参考，旧阈值 0.001/0.005 Å）")
    lines.append("")
    lines.append(
        "横轴：Δl_max（绝对值，max(|Δa|, |Δb|, |Δc|)），单位 Å。"
        "把所有材料按这个值分桶，看自跑数据的漂移在多大量级。"
        "前两档（< 0.005 Å）属于 converged + marginal，自跑 GT 可信；"
        "后两档（≥ 0.005 Å）属于 not_converged，需要更高精度才能稳定。"
    )
    lines.append("")
    lines.append("| Δl_max 范围（Å）| 数量 |")
    lines.append("|---|---|")
    for lo, hi in bins:
        n = sum(1 for r in rows
                if r.get("delta_lattice_max_angstrom") is not None
                and lo <= r["delta_lattice_max_angstrom"] < hi)
        hi_str = "∞" if hi == float("inf") else f"{hi}"
        lines.append(f"| [{lo}, {hi_str}) | {n} |")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_gt_replacement_recommendation_md(rows: list[dict], out_path: Path) -> None:
    """Replacement recommendation list (self-contained)."""
    recommend = [r for r in rows if r["recommend_replace_mp_gt"]]

    lines: list[str] = []
    lines.append("# 建议替换 MP GT 的材料清单")
    lines.append("")
    lines.append("## 这份报告是什么")
    lines.append("")
    lines.append(
        "对每道结构优化题，我们对比 Materials Project（MP）数据库提供的几何 GT"
        "和我们独立用 QE+GBRV USPP+PBE 跑出来的几何（自跑 GT）。"
        "如果 MP GT 在 QE 设置下用 1% 判分阈值复现不出来——而自跑 GT 是可靠的——"
        "继续用 MP GT 当标准答案就不公平（agent 用 QE 解题再被它打分，永远过不了）。"
        "本报告列出**建议把 MP GT 替换成自跑 GT** 的材料。"
    )
    lines.append("")

    lines.append("## 替换规则")
    lines.append("")
    lines.append("一道题进入推荐替换名单，需同时满足以下 3 个条件：")
    lines.append("")
    lines.append("1. **自跑数据收敛**：自跑扫描末两点的最大相对轴长漂移 Δl_rel < 1%"
                 "（status = `converged`）——保证自跑 GT 数据本身可信。")
    lines.append("2. **MP 与自跑的差异超过判分阈值**：a/b/c 中至少一个轴的相对差 ≥ 1%，"
                 "或者 α/β/γ 中至少一个角的绝对差 ≥ 1°——说明继续用 MP GT 会让 agent 在 QE 下打不通过分。")
    lines.append("3. **空间群保持不变**：自跑结构优化没把材料松弛到对称性更低的另一个相，"
                 "确保替换的是同一个相的几何，而不是错误结构。")
    lines.append("")
    lines.append(f"## 建议替换名单（共 {len(recommend)} 道）")
    lines.append("")

    if not recommend:
        lines.append("没有材料满足全部 3 个替换条件。这意味着：")
        lines.append("- 要么所有自跑收敛的材料 MP GT 也都过判分阈值；")
        lines.append("- 要么所有 MP-自跑 偏差大的材料自跑数据本身不可靠（marginal/not_converged/failed）。")
    else:
        lines.append(
            "下表按 **三个晶格轴中最大的相对偏差** 从大到小排列。"
            "`max(Δa/a, Δb/b, Δc/c)` 是 a/b/c 三个轴中 \\|MP - 自跑\\| / \\|MP\\| 的最大值，单位百分比；"
            "`max(Δα, Δβ, Δγ)` 是 α/β/γ 三个角中 \\|MP - 自跑\\| 的最大值，单位度。"
            "完整 a/b/c/α/β/γ 字段对比见 `comparison_detailed.md` 中对应材料的详细表格。"
        )
        lines.append("")
        lines.append("| 材料 ID | 化学式 | max(Δl/l) | max(Δθ) | 空间群保持 | 建议操作 |")
        lines.append("|---|---|---|---|---|---|")
        # Compute max-axis-rel-diff for sorting and display
        def _max_rel(r):
            return max(
                (r.get(k) or 0) for k in ("da_rel", "db_rel", "dc_rel")
            )
        def _max_ang(r):
            return max(
                (r.get(k) or 0) for k in ("dalpha_abs", "dbeta_abs", "dgamma_abs")
            )
        for r in sorted(recommend, key=lambda x: -_max_rel(x)):
            max_rel = _max_rel(r)
            max_ang = _max_ang(r)
            sg_kept = "是" if r["sg_preserved"] else "否"
            action = "用自跑 GT 替换 MP GT" if r["sg_preserved"] else "暂不替换（空间群变化）"
            lines.append(f"| {r['id']} | {r['formula']} | {max_rel*100:.2f}% | "
                        f"{max_ang:.2f}° | {sg_kept} | {action} |")
        lines.append("")
        lines.append("## 替换操作")
        lines.append("")
        lines.append(
            "对名单中的每道题，把 `dft-agent-bench/questions/M*_T1/<material_id>.json` 里"
            "`ground_truth.{a,b,c,alpha,beta,gamma,space_group_number,point_group,crystal_system}` 的 `value` 字段"
            "替换为对应自跑 canonical GT JSON 中的值（`tools/gt_selfrun/canonical_gt/<material_id>.json` 下 `ground_truth` 块），"
            "tolerance 和 rule 字段保留不变。"
            "替换后建议 commit 到题库 repo 并打 tag 标记 GT 版本。"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_stats_md(rows: list[dict], out_path: Path) -> None:
    """Run-level stats: total wall time, by tier, by status."""
    import json
    from collections import defaultdict

    lines: list[str] = []
    lines.append("# 自跑扫描运行统计")
    lines.append("")
    lines.append("本报告统计 M1 题目完整 8 点收敛扫描的 QE 计算成本。")
    lines.append("")

    # Compute per-row wall time from canonical GT
    tier_walls: dict[str, list[float]] = defaultdict(list)
    status_walls: dict[str, list[float]] = defaultdict(list)
    total_wall = 0.0
    n_with_wall = 0
    for r in rows:
        mid = r["id"]
        canonical_path = Path(__file__).resolve().parent / "canonical_gt" / f"{mid}.json"
        if not canonical_path.exists():
            continue
        try:
            d = json.loads(canonical_path.read_text())
            wall = d.get("self_run_meta", {}).get("wall_time_sec_total")
        except (json.JSONDecodeError, OSError):
            continue
        if wall is None:
            continue
        n_with_wall += 1
        total_wall += wall
        tier = r.get("tier") or mid.split("_")[0]
        tier_walls[tier].append(wall)
        status_walls[r["convergence_status"]].append(wall)

    lines.append(f"- 总材料数：{len(rows)}")
    lines.append(f"- 有完整自跑数据的材料数：{n_with_wall}")
    if total_wall > 0:
        lines.append(f"- 累计 QE wall time：{total_wall:.0f} 秒 = {total_wall/3600:.1f} 小时（顺序累加，不计 GPU 并行加速）")
    lines.append("")

    if tier_walls:
        lines.append("## 按梯度分组的单题平均 wall time")
        lines.append("")
        lines.append("| 梯度 | 材料数 | 平均 wall (s) | 中位 wall (s) | 最大 wall (s) |")
        lines.append("|---|---|---|---|---|")
        for tier in sorted(tier_walls.keys()):
            ws = sorted(tier_walls[tier])
            n = len(ws)
            avg = sum(ws) / n
            med = ws[n // 2]
            mx = ws[-1]
            lines.append(f"| {tier} | {n} | {avg:.1f} | {med:.1f} | {mx:.1f} |")
        lines.append("")

    if status_walls:
        lines.append("## 按收敛状态分组的单题平均 wall time")
        lines.append("")
        lines.append("（同一道题做了 8 个 QE 计算，下面是 8 个累加。failed 类的部分计算挂了，"
                     "wall 时间可能比正常完成的小。）")
        lines.append("")
        lines.append("| 状态 | 材料数 | 平均 wall (s) | 中位 wall (s) |")
        lines.append("|---|---|---|---|")
        for status in ("converged", "marginal", "not_converged", "failed"):
            if status not in status_walls:
                continue
            ws = sorted(status_walls[status])
            n = len(ws)
            avg = sum(ws) / n
            med = ws[n // 2]
            lines.append(f"| {status} | {n} | {avg:.1f} | {med:.1f} |")
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    """Read all canonical GT + bench JSON, write 4 reports."""
    import sys as _sys

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []

    for canonical_path in sorted(CANONICAL_GT_DIR.glob("*.json")):
        canonical = json.loads(canonical_path.read_text())
        material_id = canonical.get("id", canonical_path.stem)

        # Find sister bench JSON
        bench_path = None
        candidate = BENCH_T1_DIR / "M1_T1" / f"{material_id}.json"
        if candidate.exists():
            bench_path = candidate
        if bench_path is None:
            print(f"[report] SKIP {material_id}: no bench JSON", file=_sys.stderr)
            continue

        bench_json = json.loads(bench_path.read_text())
        mp_gt_flat = flatten_mp_gt(bench_json)

        if "ground_truth" not in canonical:
            # failed aggregation
            row = compute_comparison(
                material_id=material_id,
                formula=bench_json.get("material", {}).get("formula", "?"),
                tier=material_id.split("_")[0],
                category=bench_json.get("material", {}).get("category", ""),
                mp_gt=mp_gt_flat, self_gt={},
                convergence_status="failed",
                overall_status=canonical.get("status", "failed"),
            )
        else:
            self_gt_flat = flatten_canonical_gt(canonical)
            meta = canonical.get("self_run_meta", {})
            ce = canonical.get("convergence_evidence", {})
            row = compute_comparison(
                material_id=material_id,
                formula=bench_json.get("material", {}).get("formula", "?"),
                tier=material_id.split("_")[0],
                category=bench_json.get("material", {}).get("category", ""),
                mp_gt=mp_gt_flat, self_gt=self_gt_flat,
                convergence_status=meta.get("convergence_status", "?"),
                ecutwfc_conv=ce.get("converged_params", {}).get("ecutwfc_ry"),
                kmesh_conv_density=ce.get("converged_params", {}).get("kmesh_density_pts_per_inv_angstrom"),
                delta_lattice_max_angstrom=meta.get("delta_lattice_max_angstrom"),
                delta_lattice_max_relative=meta.get("delta_lattice_max_relative"),
            )
        rows.append(row)

    write_csv(rows, REPORTS_DIR / "comparison.csv")
    write_comparison_md(rows, REPORTS_DIR / "comparison.md")
    write_convergence_summary_md(rows, REPORTS_DIR / "convergence_summary.md")
    write_gt_replacement_recommendation_md(rows, REPORTS_DIR / "gt_replacement_recommendation.md")
    write_stats_md(rows, REPORTS_DIR / "stats.md")

    print(f"[report] wrote 5 files to {REPORTS_DIR}/")
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())
