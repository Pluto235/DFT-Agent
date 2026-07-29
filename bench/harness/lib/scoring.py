"""Scoring: 按 bench schema scoring rules 给每字段打分并 weighted 聚合。

支持的 rules:
  - exact            值完全相等 → 1.0，否则 0.0
  - relative         |v-t|/|t| ≤ tol → 1.0，超出则线性衰减到 2*tol 处归 0
  - absolute         |v-t|     ≤ tol → 1.0，超出则线性衰减到 2*tol 处归 0
  - graded_absolute  分级 absolute：满分 |err| ≤ tol/2（数值噪声地板），
                     tol/2 → tol 线性衰减到 0；err ≥ tol 即 0 分（**硬归零，**
                     不像 absolute 还有 [tol, 2*tol] 部分分尾巴）。
                     2026-05-25 议题 1 决议：仅 T2 band_gap_ev 用，区分小误差精度。
                     与 absolute 的区别：tolerance 在 graded 里是"硬归零点"而非
                     "全分上限"。其它能量/几何类 absolute 字段未迁移（followup T-1）。
  - curve_f1         F1 / Dice 面积重叠评分；直接 score=F1；GT.value 给 ref_file 路径
                     见 _score_curve_f1() 文档（2026-05-17 决议）

历史：早期使用 curve_l2（RMSE + best-shift），2026-05-17 全量迁移至 curve_f1
后 _score_curve_l2 已删除（见 todo/bench/t2-dos-scoring-f1-overhaul.md S5）。
"""
import warnings
from pathlib import Path
from typing import Any, Union

# 解析 GT ref_file 时把 'bench/...' 视作 repo 根的相对路径。
# 历史 GT 写法保留 'bench/' 前缀；当前实际路径在 repos/dft-agent-bench/<...>，
# 所以解析时先按原样试，再去掉 'bench/' 前缀。
_BENCH_ROOT = Path(__file__).parent.parent.parent.resolve()

# T3/T4 收紧打分曲线（2026-06-12 shijie + DFT 专家决议）。
# 这条曲线里 `tolerance` 语义是"零分线"（旧 relative 是"满分线"，故此处更严约 3.3×）：
#   满分带  err ≤ _FULL_MARK_RATIO · tol
#   零分    err ≥ tol（硬归零，无 [tol, 2tol] 尾巴）
#   中间    线性
# 0.3 复现专家对 10% 字段的"3% 满分 / 10% 零分"，并让每个字段按自身 tolerance 自动缩放。
_FULL_MARK_RATIO = 0.3


def _graded_score(err: float, tol: float, partial: bool) -> float:
    """收紧曲线核心：满分 err ≤ 0.3·tol；零分 err ≥ tol；中间线性。

    - partial=False → boolean(err ≤ 0.3·tol)，与 graded_absolute 的最严模式对齐。
    - tol ≤ 0 → 退化为 exact（err==0 满分，否则 0）。
    被 graded_relative / matrix_frobenius_relative / eos_curve_rmse / stress_curve_rmse 共用。
    """
    if tol <= 0:
        return 1.0 if err <= 0 else 0.0
    full = _FULL_MARK_RATIO * tol
    if err <= full:
        return 1.0
    if not partial or err >= tol:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (err - full) / (tol - full)))


def score_fields(
    ground_truth: dict[str, Any],
    extracted: dict[str, Any],
    scoring: dict[str, Any],
    space_group_number: int | None = None,
) -> dict[str, Any]:
    """返回 {"per_field_scores": {...}, "per_field_meta": {...}, "total_score": float}.

    per_field_meta 只为返回 dict 的规则（如 curve_f1）填字段；标量规则不进 meta。

    space_group_number：题目所属晶系的空间群号（task["material"]["space_group_number"]）。
    仅 `elastic_symmetry_reduced` 规则用——派生 Laue 类后把弹性张量投影到该晶系标准型
    逐独立常数打分。其它规则忽略此参数。缺省 None → 该规则退化到 Mandel 特征值。
    """
    weights = scoring.get("field_weights", {})
    partial_credit = scoring.get("partial_credit", True)
    per_field: dict[str, float] = {}
    per_field_meta: dict[str, dict[str, Any]] = {}

    for field, weight in weights.items():
        gt = ground_truth.get(field)
        if gt is None:
            per_field[field] = 0.0
            continue
        if field not in extracted:
            per_field[field] = 0.0
            continue
        result = _score_one(gt, extracted[field], partial_credit, space_group_number)
        if isinstance(result, dict):
            score_val = float(result.get("score", 0.0))
            per_field[field] = score_val
            # 把 score 之外的字段全部存进 meta（precision / recall / A_pred 等）
            meta = {k: v for k, v in result.items() if k != "score"}
            if meta:
                per_field_meta[field] = meta
        else:
            per_field[field] = float(result)

    total_weight = sum(weights.values())
    if total_weight > 0:
        total = sum(per_field[f] * weights[f] for f in weights) / total_weight
    else:
        total = 0.0
    return {
        "per_field_scores": per_field,
        "per_field_meta": per_field_meta,
        "total_score": round(total, 6),
    }


def _score_one(
    gt: dict[str, Any], value: Any, partial_credit: bool,
    space_group_number: int | None = None,
) -> Union[float, dict[str, Any]]:
    """返回 float（exact/relative/absolute）或 dict（curve_f1 等带元信息规则）。

    上层 score_fields 区分类型：dict → 拆出 score 进 per_field、其余进 per_field_meta。
    """
    rule = gt.get("rule")
    if rule == "exact":
        return 1.0 if value == gt["value"] else 0.0
    if rule == "k_label_match":
        return _score_k_label_match(
            value, gt["value"], tolerance=gt.get("tolerance", 0.05),
            partial=partial_credit,
        )
    if rule == "relative":
        return _score_with_tolerance(
            value, gt["value"], gt.get("tolerance", 0.01),
            kind="relative", partial=partial_credit,
        )
    if rule == "graded_relative":
        return _score_graded_relative(
            value, gt["value"], gt.get("tolerance", 0.1),
            partial=partial_credit,
        )
    if rule == "absolute":
        return _score_with_tolerance(
            value, gt["value"], gt.get("tolerance", 1.0),
            kind="absolute", partial=partial_credit,
        )
    if rule == "graded_absolute":
        return _score_graded_absolute(
            value, gt["value"], gt.get("tolerance", 1.0),
            partial=partial_credit,
        )
    if rule == "curve_f1":
        return _score_curve_f1(
            extracted=value,
            gt_value=gt.get("value"),
            tolerance=gt.get("tolerance", 0.1),  # 不使用，签名兼容保留
            partial=partial_credit,
        )
    if rule == "eos_curve_rmse_mev_per_atom":
        return _score_eos_curve_rmse_mev_per_atom(
            extracted=value,
            gt_value=gt.get("value"),
            tolerance=gt.get("tolerance", 5.0),
            partial=partial_credit,
        )
    if rule == "matrix_frobenius_relative":
        return _score_matrix_frobenius_relative(
            extracted=value,
            gt_value=gt.get("value"),
            tolerance=gt.get("tolerance", 0.15),
            partial=partial_credit,
        )
    if rule == "matrix_eigenvalue_relative":
        return _score_matrix_eigenvalue_relative(
            extracted=value,
            gt_value=gt.get("value"),
            tolerance=gt.get("tolerance", 0.08),
            partial=partial_credit,
        )
    if rule == "elastic_symmetry_reduced":
        return _score_elastic_symmetry_reduced(
            extracted=value,
            gt_value=gt.get("value"),
            tolerance=gt.get("tolerance", 0.1),
            partial=partial_credit,
            space_group_number=space_group_number,
        )
    if rule == "stress_curve_rmse_gpa":
        return _score_stress_curve_rmse_gpa(
            extracted=value,
            gt_value=gt.get("value"),
            tolerance=gt.get("tolerance", 1.0),
            partial=partial_credit,
        )
    # 未知规则 → 0
    return 0.0


def _parse_k_coord_str(s: Any) -> tuple[float, float, float] | None:
    """从 "(x, y, z)" 形态字符串解析出 3-tuple；不是 coord 字符串返 None。
    高对称 label 如 "GAMMA"/"X"/"metal"/"OFF_PATH" 返 None（caller 走 label 字符串路径）。"""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not (s.startswith("(") and s.endswith(")")):
        return None
    try:
        parts = [p.strip() for p in s[1:-1].split(",")]
        if len(parts) != 3:
            return None
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except (ValueError, AttributeError):
        return None


def _frac_distance_periodic(a: tuple[float, float, float],
                             b: tuple[float, float, float]) -> float:
    """Minimum-image fractional Euclidean distance (镜像 evaluator.py:_frac_distance)。"""
    import math
    s = 0.0
    for ai, bi in zip(a, b):
        d = ai - bi
        d -= round(d)  # bring into [-0.5, 0.5]
        s += d * d
    return math.sqrt(s)


def _score_k_label_match(value: Any, target: Any, *, tolerance: float, partial: bool) -> float:
    """Score k-point label/coord match。

    pred / gt 各自可以是:
      - High-sym label 字符串 (e.g. "GAMMA", "X", "metal", "OFF_PATH")
      - Coord 字符串 "(x.xxx, y.yyy, z.zzz)" (杨博 0521: 不再用 OFF_PATH 占位,
        非 high-sym 点直接打 coord)

    判定:
      - 两边都 coord: parse → frac distance (周期边界) → tolerance 内 1.0,
        2×tolerance 外 0.0, 线性 decay (与 _score_with_tolerance absolute 一致)
      - 两边都 label: exact string match
      - 类型混搭 (一个 label 一个 coord, 或 None): 0.0
    """
    if value is None or target is None:
        return 0.0
    v_coord = _parse_k_coord_str(value)
    t_coord = _parse_k_coord_str(target)
    if v_coord is not None and t_coord is not None:
        d = _frac_distance_periodic(v_coord, t_coord)
        if d <= tolerance:
            return 1.0
        if not partial:
            return 0.0
        if tolerance <= 0:
            return 0.0
        decay = 1.0 - (d / tolerance - 1.0)
        return max(0.0, min(1.0, decay))
    if v_coord is None and t_coord is None:
        return 1.0 if value == target else 0.0
    return 0.0  # mixed type


def _score_graded_absolute(
    value: Any, target: float, tol: float, partial: bool = True
) -> float:
    """分级 absolute：满分 |err| ≤ tol/2（噪声地板），tol/2 → tol 线性衰减到 0。

    与 `absolute` 的差异：
      - absolute:        [0, tol] 一律满分，(tol, 2*tol] 线性 1→0，(2*tol, ∞) 归 0
      - graded_absolute: [0, tol/2] 一律满分，(tol/2, tol) 线性 1→0，[tol, ∞) 归 0
        → tolerance 在 graded 里是"硬归零点"，比 absolute 严

    曲线（tol=0.1 为例）：
      err=0.00   → 1.0
      err=0.05   → 1.0   (地板边缘)
      err=0.075  → 0.5   (半分点 = 3·tol/4)
      err=0.10   → 0.0   (硬归零)
      err=0.15   → 0.0

    partial=False 时退化为 boolean："err ≤ tol/2 → 1.0，否则 0.0"。与
    `_score_with_tolerance` 的 partial=False 语义对齐（最严模式 = 硬阈值）。

    目前仅用于 T2 band_gap_ev（议题 1, 2026-05-25 落地; 见 v2 handoff §1-Q5）。
    """
    try:
        v = float(value)
        t = float(target)
        tolf = float(tol)
    except (TypeError, ValueError):
        return 0.0
    if tolf <= 0:
        return 1.0 if v == t else 0.0
    half = tolf / 2.0          # 满分地板 = tol/2；衰减带 [tol/2, tol]
    err = abs(v - t)
    if err <= half:
        return 1.0
    if not partial:
        return 0.0    # 严格模式：tol/2 外即 0
    return max(0.0, min(1.0, 1.0 - (err - half) / half))


def _score_with_tolerance(
    value: float, target: float, tol: float, kind: str, partial: bool
) -> float:
    try:
        v = float(value)
        t = float(target)
        tolf = float(tol)
    except (TypeError, ValueError):
        return 0.0

    if kind == "relative":
        if t == 0:
            err = abs(v - t)
        else:
            err = abs(v - t) / abs(t)
    elif kind == "absolute":
        err = abs(v - t)
    else:
        return 0.0

    if err <= tolf:
        return 1.0
    if not partial:
        return 0.0
    # 线性衰减：err = tol → 1.0；err = 2 * tol → 0.0；更大 → 0
    if tolf <= 0:
        return 0.0
    decay = 1.0 - (err / tolf - 1.0)
    return max(0.0, min(1.0, decay))


def _score_graded_relative(
    value: float, target: float, tol: float, partial: bool
) -> float:
    """相对误差 |v-t|/|t| 套收紧曲线（满分 0.3·tol、零分 tol）。

    T3 v0/b0、T4 K/G 用（从旧 `relative` 切过来）。与 `relative` 的差异：tol 是
    零分线而非满分线，所以同一个 tolerance 下严约 3.3×。t==0 时退化为绝对误差。
    """
    try:
        v = float(value)
        t = float(target)
        tolf = float(tol)
    except (TypeError, ValueError):
        return 0.0
    err = abs(v - t) if t == 0 else abs(v - t) / abs(t)
    return _graded_score(err, tolf, partial)


def _score_curve_f1(
    *,
    extracted: Any,
    gt_value: Any,
    tolerance: float,
    partial: bool,
) -> dict[str, Any]:
    """Curve-vs-curve F1 / Dice 面积重叠评分（2026-05-17 决议）。

    不做横轴 shift，不做归一化，不读 tolerance，直接 score = F1。
    （取代历史 curve_l2 的 RMSE+best-shift 算法，S5 已删。）

    输入约定：
      - extracted: dict {"energy_ev": [...], "dos": [...]}
            energy_ev 已 relative-to-EF（extractor 端已减 E_F），单位 eV。
      - gt_value: dict {"ref_file": "<path>"}
            ref 文件格式：纯 ASCII，2 列 (E_relative_EF_eV, DOS)，可有 # 注释行。
      - tolerance / partial: 不使用，保留签名兼容。

    算法：
      1. pred 排序 + 在 GT 横轴网格上插值（np.interp，越界 → 0）。
      2. pred、GT 的 DOS 都 clip 到 [0, +∞)（负值视为数值噪声归 0）。
      3. 三个面积（np.trapezoid）：
           A_pred = ∫ pred dE
           A_gt   = ∫ gt   dE
           A_int  = ∫ min(pred, gt) dE
      4. P = A_int / A_pred；R = A_int / A_gt；F1 = 2PR / (P+R)。
      5. A_pred 或 A_gt ≤ 0 → score=0（无可比面积）。

    返回 dict：
      {"score": F1, "precision": P, "recall": R,
       "A_pred": ..., "A_gt": ..., "A_intersect": ...}

    失败保护：warning + 返 zeros，不抛异常。
    """
    _zero = {"score": 0.0, "precision": 0.0, "recall": 0.0,
             "A_pred": 0.0, "A_gt": 0.0, "A_intersect": 0.0}

    # 1. extracted 结构校验
    if not isinstance(extracted, dict):
        warnings.warn("curve_f1: extracted is not a dict", stacklevel=2)
        return dict(_zero)
    e_pred_l = extracted.get("energy_ev")
    y_pred_l = extracted.get("dos")
    if not isinstance(e_pred_l, list) or not isinstance(y_pred_l, list):
        warnings.warn("curve_f1: extracted missing energy_ev / dos lists", stacklevel=2)
        return dict(_zero)
    if len(e_pred_l) != len(y_pred_l) or len(e_pred_l) < 2:
        warnings.warn(
            f"curve_f1: extracted curve too short or mismatched "
            f"(len={len(e_pred_l)}/{len(y_pred_l)})",
            stacklevel=2,
        )
        return dict(_zero)

    # 2. gt_value + ref_file 校验
    if not isinstance(gt_value, dict) or "ref_file" not in gt_value:
        warnings.warn("curve_f1: GT.value missing ref_file", stacklevel=2)
        return dict(_zero)
    ref_path = _resolve_ref_file(gt_value["ref_file"])
    if ref_path is None:
        warnings.warn(
            f"curve_f1: ref_file not found: {gt_value['ref_file']}", stacklevel=2,
        )
        return dict(_zero)
    try:
        gt_energy, gt_dos = _read_2col_dat(ref_path)
    except Exception as e:
        warnings.warn(f"curve_f1: failed reading ref_file {ref_path}: {e}", stacklevel=2)
        return dict(_zero)
    if len(gt_energy) < 2:
        warnings.warn(f"curve_f1: ref_file {ref_path} has too few points", stacklevel=2)
        return dict(_zero)

    try:
        import numpy as np
    except ImportError:
        warnings.warn("curve_f1: numpy not available", stacklevel=2)
        return dict(_zero)
    # numpy 2.0 起 np.trapz 被 np.trapezoid 取代；兼容两者。
    _trapezoid = getattr(np, "trapezoid", np.trapz)

    # 3. 排序 + pred 插值到 GT 网格（积分窗口 = GT 范围；pred 越界 → 0）
    e_pred = np.asarray(e_pred_l, dtype=float)
    y_pred_arr = np.asarray(y_pred_l, dtype=float)
    e_gt = np.asarray(gt_energy, dtype=float)
    y_gt = np.asarray(gt_dos, dtype=float)
    o_pred = np.argsort(e_pred); e_pred = e_pred[o_pred]; y_pred_arr = y_pred_arr[o_pred]
    o_gt = np.argsort(e_gt); e_gt = e_gt[o_gt]; y_gt = y_gt[o_gt]

    y_pred_on_gt = np.interp(e_gt, e_pred, y_pred_arr, left=0.0, right=0.0)

    # 负 DOS 视为数值噪声，clip 到 0
    y_pred_on_gt = np.clip(y_pred_on_gt, 0.0, None)
    y_gt_clipped = np.clip(y_gt, 0.0, None)

    # 4. 三个面积（无归一化）
    A_pred = float(_trapezoid(y_pred_on_gt, e_gt))
    A_gt = float(_trapezoid(y_gt_clipped, e_gt))
    A_int = float(_trapezoid(np.minimum(y_pred_on_gt, y_gt_clipped), e_gt))

    # 5. 退化 case
    if A_pred <= 0 or A_gt <= 0:
        return {"score": 0.0, "precision": 0.0, "recall": 0.0,
                "A_pred": A_pred, "A_gt": A_gt, "A_intersect": A_int}

    # 6. F1（β=1）
    P = A_int / A_pred
    R = A_int / A_gt
    F1 = 2 * P * R / (P + R) if (P + R) > 0 else 0.0

    return {"score": float(F1), "precision": float(P), "recall": float(R),
            "A_pred": A_pred, "A_gt": A_gt, "A_intersect": A_int}


def _resolve_ref_file(ref: str) -> Path | None:
    """解析 GT ref_file 路径：先按 repo 根 / cwd 试，再去掉 'bench/' 前缀重试。

    历史写法把所有 GT data 路径前缀写作 'bench/...'（workspace 视角），
    当前实际位置在 repos/dft-agent-bench/<...>。解析顺序：
      1. ref 当作相对 _BENCH_ROOT（dft-agent-bench repo 根）
      2. ref 当作相对 cwd
      3. ref 去掉 'bench/' 前缀，再当作相对 _BENCH_ROOT
      4. ref 去掉 'bench/' 前缀，再当作相对 cwd
      5. 全部失败 → None
    """
    candidates: list[Path] = []
    p = Path(ref)
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.append(_BENCH_ROOT / p)
        candidates.append(Path.cwd() / p)
        if str(p).startswith("bench/"):
            stripped = Path(str(p)[len("bench/"):])
            candidates.append(_BENCH_ROOT / stripped)
            candidates.append(Path.cwd() / stripped)
    for c in candidates:
        if c.exists():
            return c.resolve()
    return None


def _read_2col_dat(path: Path) -> tuple[list[float], list[float]]:
    """读 2 列 ASCII（# 开头注释行跳过；多余列丢弃）。返回 (col0, col1)。"""
    xs: list[float] = []
    ys: list[float] = []
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) < 2:
            continue
        try:
            x = float(parts[0])
            y = float(parts[1])
        except ValueError:
            continue
        xs.append(x)
        ys.append(y)
    return xs, ys


def _score_eos_curve_rmse_mev_per_atom(
    *,
    extracted: Any,
    gt_value: Any,
    tolerance: float,
    partial: bool,
) -> dict[str, Any]:
    """T3 EOS curve scoring：energy-zero alignment 后逐点 RMSE (meV/atom)。

    Inputs:
      - extracted: list[float] (length = N，ΔE/atom in meV/atom on volume_ratio_grid)
      - gt_value:  {"ref_file": "<path to 2-col CSV>"} (volume_ratio, delta_e_meV_per_atom)
      - tolerance: RMSE threshold in meV/atom (typical 5.0)

    Algorithm:
      1. Read ref_file → (gt_ratios, gt_curve)
      2. Defensive zero-align both extracted and gt_curve (subtract min)
      3. Require same length (we assume same volume_ratio_grid; otherwise warn + 0)
      4. RMSE = sqrt(mean((ext − gt)²))
      5. Score: rmse ≤ tol → 1.0；rmse ∈ [tol, 2·tol] → 线性衰减到 0；rmse > 2·tol → 0

    Returns: {"score", "rmse_mev_per_atom", "n_points"}
    """
    _zero = {"score": 0.0, "rmse_mev_per_atom": float("inf"), "n_points": 0}

    if not isinstance(extracted, list):
        warnings.warn("eos_curve_rmse: extracted is not a list", stacklevel=2)
        return dict(_zero)
    if len(extracted) < 2:
        warnings.warn(
            f"eos_curve_rmse: too few points (n={len(extracted)})", stacklevel=2
        )
        return dict(_zero)

    if not isinstance(gt_value, dict) or "ref_file" not in gt_value:
        warnings.warn("eos_curve_rmse: GT.value missing ref_file", stacklevel=2)
        return dict(_zero)
    ref_path = _resolve_ref_file(gt_value["ref_file"])
    if ref_path is None:
        warnings.warn(
            f"eos_curve_rmse: ref_file not found: {gt_value['ref_file']}", stacklevel=2,
        )
        return dict(_zero)
    try:
        _gt_ratios, gt_curve = _read_2col_dat(ref_path)
    except Exception as e:
        warnings.warn(
            f"eos_curve_rmse: failed reading ref_file {ref_path}: {e}", stacklevel=2,
        )
        return dict(_zero)
    if len(gt_curve) != len(extracted):
        warnings.warn(
            f"eos_curve_rmse: length mismatch (ext={len(extracted)} vs gt={len(gt_curve)})",
            stacklevel=2,
        )
        return dict(_zero)

    try:
        import numpy as np
    except ImportError:
        warnings.warn("eos_curve_rmse: numpy not available", stacklevel=2)
        return dict(_zero)

    ext_arr = np.array(extracted, dtype=float)
    gt_arr = np.array(gt_curve, dtype=float)
    # Defensive zero-align (extracted 已 zero-aligned by extractor；GT 也应该如此)
    ext_arr -= float(ext_arr.min())
    gt_arr -= float(gt_arr.min())

    rmse = float(np.sqrt(np.mean((ext_arr - gt_arr) ** 2)))

    # 收紧曲线（2026-06-12）：满分 rmse ≤ 0.3·tol、零分 rmse ≥ tol。
    score = _graded_score(rmse, tolerance, partial)

    return {
        "score": score,
        "rmse_mev_per_atom": rmse,
        "n_points": len(extracted),
    }


def _score_matrix_frobenius_relative(
    *,
    extracted: Any,
    gt_value: Any,
    tolerance: float,
    partial: bool,
) -> dict[str, Any]:
    """T4 Cij matrix scoring: Frobenius distance / Frobenius norm of GT.

    Inputs:
      extracted: 6×6 list[list[float]]
      gt_value:  6×6 list[list[float]] (直接给值，**不**走 ref_file)
      tolerance: relative threshold (typical 0.15)

    Algorithm:
      rel = ‖C_ext − C_gt‖_F / ‖C_gt‖_F
      Score: rel ≤ tol → 1；rel ∈ [tol, 2·tol] → 线性衰减；rel > 2·tol → 0

    Returns: {"score", "frobenius_rel", "frobenius_dist", "frobenius_gt"}
    """
    _zero = {"score": 0.0, "frobenius_rel": float("inf"),
             "frobenius_dist": 0.0, "frobenius_gt": 0.0}

    if not isinstance(extracted, list) or len(extracted) != 6:
        warnings.warn("matrix_frobenius_relative: extracted not 6x6 list", stacklevel=2)
        return dict(_zero)
    if not isinstance(gt_value, list) or len(gt_value) != 6:
        warnings.warn("matrix_frobenius_relative: gt_value not 6x6 list", stacklevel=2)
        return dict(_zero)

    try:
        import numpy as np
    except ImportError:
        warnings.warn("matrix_frobenius_relative: numpy not available", stacklevel=2)
        return dict(_zero)

    try:
        C_ext = np.array(extracted, dtype=float)
        C_gt = np.array(gt_value, dtype=float)
    except Exception as e:
        warnings.warn(f"matrix_frobenius_relative: numeric conversion: {e}", stacklevel=2)
        return dict(_zero)

    if C_ext.shape != (6, 6) or C_gt.shape != (6, 6):
        warnings.warn(
            f"matrix_frobenius_relative: shape mismatch {C_ext.shape} vs {C_gt.shape}",
            stacklevel=2,
        )
        return dict(_zero)

    diff = float(np.linalg.norm(C_ext - C_gt, ord="fro"))
    gt_norm = float(np.linalg.norm(C_gt, ord="fro"))
    if gt_norm < 1e-9:
        warnings.warn("matrix_frobenius_relative: GT Frobenius norm ≈ 0", stacklevel=2)
        return dict(_zero)
    rel = diff / gt_norm

    # 收紧曲线（2026-06-12）：满分 rel ≤ 0.3·tol、零分 rel ≥ tol。
    score = _graded_score(rel, tolerance, partial)

    return {
        "score": score,
        "frobenius_rel": rel,
        "frobenius_dist": diff,
        "frobenius_gt": gt_norm,
    }


def _score_matrix_eigenvalue_relative(
    *,
    extracted: Any,
    gt_value: Any,
    tolerance: float,
    partial: bool,
) -> dict[str, Any]:
    """T4 Cij scoring：排序后特征值的相对 L2 比较（2026-06-12 取代 raw Frobenius）。

    raw 6×6 Voigt 矩阵的 Frobenius 距离随原胞取向变化——GT 与 solver 用不同的原胞
    取向时，同一个弹性张量的 raw 矩阵能差 10–50%（实测 T4 中位 17%），而物理一致
    （K/G/特征值匹配 <2%）。故改比排序后特征值的相对 L2 距离：

    ⚠️ 注意（2026-06-25 纠偏）：这里取的是**原始 Voigt 矩阵**的特征值，它**不是**真正的
    旋转不变量（Voigt 基非正交；只在立方点群的轴置换=带符号置换下不变，一般 3D 旋转下会
    变）。实践中 GT 与 solver 多用轴置换相关的原胞基，故够用；但真不变量是 Mandel/Kelvin
    归一化后的谱，见 `elastic_symmetry_reduced`（迁移后取代本规则）。

      rel = ‖sort(eig(C_ext)) − sort(eig(C_gt))‖ / ‖eig(C_gt)‖

    再套收紧曲线（满分 ≤ 0.3·tol、零分 ≥ tol）。比较前对称化（C+Cᵀ)/2 防数值不对称；
    含 NaN/inf 或 eigvalsh 不收敛的病态矩阵 → 0 分（不抛异常）。

    Born 稳定性特判（2026-06-23）：稳定晶体的刚度张量必须正定。提交含明显负特征值
    （min eig < −1e-3·max|eig|，容忍 FP 噪声）= 物理不稳 → 标 `born_unstable` 并把分数
    **闸到 0**（即便排序特征值碰巧接近 GT 也不给分）；若全部特征值为负（整体翻号，最常见
    根因是 QE 应力未取负的符号约定 bug）另标 `sign_convention_suspected`，让该失败模式在
    report 里和"张量彻底算错"区分开。

    Returns: {"score", "eig_rel", "eig_ext", "eig_gt",
              "born_unstable", "sign_convention_suspected"}
    """
    _zero = {"score": 0.0, "eig_rel": float("inf"), "eig_ext": [], "eig_gt": [],
             "born_unstable": False, "sign_convention_suspected": False}

    if not isinstance(extracted, list) or len(extracted) != 6:
        warnings.warn("matrix_eigenvalue_relative: extracted not 6x6 list", stacklevel=2)
        return dict(_zero)
    if not isinstance(gt_value, list) or len(gt_value) != 6:
        warnings.warn("matrix_eigenvalue_relative: gt_value not 6x6 list", stacklevel=2)
        return dict(_zero)

    try:
        import numpy as np
    except ImportError:
        warnings.warn("matrix_eigenvalue_relative: numpy not available", stacklevel=2)
        return dict(_zero)

    try:
        C_ext = np.array(extracted, dtype=float)
        C_gt = np.array(gt_value, dtype=float)
    except Exception as e:
        warnings.warn(f"matrix_eigenvalue_relative: numeric conversion: {e}", stacklevel=2)
        return dict(_zero)

    if C_ext.shape != (6, 6) or C_gt.shape != (6, 6):
        warnings.warn(
            f"matrix_eigenvalue_relative: shape mismatch {C_ext.shape} vs {C_gt.shape}",
            stacklevel=2,
        )
        return dict(_zero)

    if not (np.all(np.isfinite(C_ext)) and np.all(np.isfinite(C_gt))):
        warnings.warn("matrix_eigenvalue_relative: non-finite entries (病态矩阵)", stacklevel=2)
        return dict(_zero)

    # 对称化后取特征值（symmetric → eigvalsh 实数 + 升序稳定）
    try:
        ev_ext = np.sort(np.linalg.eigvalsh((C_ext + C_ext.T) / 2.0))
        ev_gt = np.sort(np.linalg.eigvalsh((C_gt + C_gt.T) / 2.0))
    except np.linalg.LinAlgError as e:
        warnings.warn(f"matrix_eigenvalue_relative: eigvalsh failed ({e})", stacklevel=2)
        return dict(_zero)

    gt_norm = float(np.linalg.norm(ev_gt))
    if gt_norm < 1e-9:
        warnings.warn("matrix_eigenvalue_relative: GT eigenvalue norm ≈ 0", stacklevel=2)
        return dict(_zero)
    rel = float(np.linalg.norm(ev_ext - ev_gt) / gt_norm)

    # Born 稳定性：提交张量须正定（稳定晶体的物理要求）。负性门槛取 -1e-3·max|eig|
    # 以容忍 FP 噪声；全负 = 整体翻号（QE 应力未取负的符号约定 bug 的特征签名）。
    max_abs = float(np.max(np.abs(ev_ext))) if ev_ext.size else 0.0
    neg_floor = -1e-3 * max_abs
    born_unstable = bool(ev_ext.size and float(ev_ext[0]) < neg_floor)
    sign_flip = bool(ev_ext.size and float(ev_ext[-1]) < neg_floor)

    # 收紧曲线（满分 rel ≤ 0.3·tol、零分 rel ≥ tol），与其它 T3/T4 字段共用。
    # born 不稳 → 直接闸 0（不稳张量不是稳定材料的合法答案，哪怕排序特征值碰巧接近）。
    score = 0.0 if born_unstable else _graded_score(rel, tolerance, partial)

    return {
        "score": score,
        "eig_rel": rel,
        "eig_ext": ev_ext.tolist(),
        "eig_gt": ev_gt.tolist(),
        "born_unstable": born_unstable,
        "sign_convention_suspected": sign_flip,
    }


# ─── P1: 按晶系对称约化的逐独立常数打分（elastic_symmetry_reduced） ───────────
# 旧 matrix_eigenvalue_relative 对原始 Voigt 矩阵取特征值，但 Voigt 基非正交 →
# 那些"特征值"不是真旋转不变量，且被 GT 自身的破简并噪声放大，把物理正确的张量误判 0
# （实测 NaCl/NaF/LiF：K、G 满分而 Cij=0）。新规则用空间群 → Laue 类，把 GT 和提交
# **都**投影到该晶系标准型，逐独立常数（cubic: C11/C12/C44；hex: +C13/C33 …）走
# 同一条收紧曲线 _graded_score 后等权平均。对称投影天然吸收破对称噪声（杂散 off-diagonal
# 不进任何独立常数），且给出逐常数可解释明细。不支持的晶系/缺空间群 → 退化到 Mandel
# 归一化特征值（真旋转不变量，物理 3D 旋转下不变，Voigt 原始特征值做不到）。
_W_MANDEL = (1.0, 1.0, 1.0, 2.0 ** 0.5, 2.0 ** 0.5, 2.0 ** 0.5)


def _laue_class(spg: int | None) -> str | None:
    if spg is None:
        return None
    spg = int(spg)
    if spg >= 195: return "cubic"
    if spg >= 168: return "hexagonal"
    if spg >= 143: return "trigonal"
    if spg >= 75:  return "tetragonal"
    if spg >= 16:  return "orthorhombic"
    if spg >= 3:   return "monoclinic"
    return "triclinic"


def _symmetry_constants(C, laue: str):
    """从（已对称化的）6×6 抽出该 Laue 类的独立弹性常数 dict。
    只实现简并结构干净的 4 类；trigonal(含额外 C14)/monoclinic/triclinic 返回 None
    → 交给 Mandel 特征值退化路径（避免投影丢信息）。"""
    import numpy as np
    m = lambda *ij: float(np.mean([C[i, j] for i, j in ij]))
    if laue == "cubic":
        return {"C11": m((0, 0), (1, 1), (2, 2)),
                "C12": m((0, 1), (0, 2), (1, 2)),
                "C44": m((3, 3), (4, 4), (5, 5))}
    if laue == "hexagonal":  # C66=(C11−C12)/2 为非独立，不单独计分
        return {"C11": m((0, 0), (1, 1)), "C33": m((2, 2)),
                "C12": m((0, 1)), "C13": m((0, 2), (1, 2)), "C44": m((3, 3), (4, 4))}
    if laue == "tetragonal":  # 4/mmm：C66 独立
        return {"C11": m((0, 0), (1, 1)), "C33": m((2, 2)), "C12": m((0, 1)),
                "C13": m((0, 2), (1, 2)), "C44": m((3, 3), (4, 4)), "C66": m((5, 5))}
    if laue == "orthorhombic":
        return {"C11": m((0, 0)), "C22": m((1, 1)), "C33": m((2, 2)),
                "C12": m((0, 1)), "C13": m((0, 2)), "C23": m((1, 2)),
                "C44": m((3, 3)), "C55": m((4, 4)), "C66": m((5, 5))}
    return None


def _mandel_eigs(C):
    """Mandel/Kelvin 归一化后的特征值（真旋转不变量）。病态 → None。"""
    import numpy as np
    w = np.array(_W_MANDEL)
    M = np.array(C, dtype=float) * np.outer(w, w)
    try:
        return np.sort(np.linalg.eigvalsh(M))
    except np.linalg.LinAlgError:
        return None


def _score_elastic_symmetry_reduced(
    *,
    extracted: Any,
    gt_value: Any,
    tolerance: float,
    partial: bool,
    space_group_number: int | None,
) -> dict[str, Any]:
    """见上方注释。

    **分数 = Mandel/Kelvin 归一化特征值的相对 L2（真旋转不变量）**。逐独立常数的
    lab-frame 投影**非旋转不变**（实测六方 Be 绕轴 30° → 0.49），故只放进
    meta["constants"] 作诊断、**不参与打分**。Born 稳定性特判与 matrix_eigenvalue_relative
    同口径（min eig < −1e-3·max|eig| → born_unstable，闸 0；全负 → sign_convention_suspected）。

    返回 dict：
      正常: {"score","mode":"mandel_invariant","laue","eig_rel","eig_ext","eig_gt",
             "born_unstable","sign_convention_suspected", 可选 "constants":{name:{gt,ext,rel}}}
      非法输入: {"score":0.0,"mode":"invalid"}
    """
    _zero = {"score": 0.0, "mode": "invalid"}
    try:
        import numpy as np
    except ImportError:
        warnings.warn("elastic_symmetry_reduced: numpy 不可用", stacklevel=2)
        return dict(_zero)
    try:
        C_ext = np.array(extracted, dtype=float)
        C_gt = np.array(gt_value, dtype=float)
    except Exception as e:
        warnings.warn(f"elastic_symmetry_reduced: 数值转换失败 {e}", stacklevel=2)
        return dict(_zero)
    if C_ext.shape != (6, 6) or C_gt.shape != (6, 6):
        warnings.warn("elastic_symmetry_reduced: 非 6×6", stacklevel=2)
        return dict(_zero)
    if not (np.all(np.isfinite(C_ext)) and np.all(np.isfinite(C_gt))):
        warnings.warn("elastic_symmetry_reduced: 含 NaN/inf（病态矩阵）", stacklevel=2)
        return dict(_zero)
    # 比较前对称化（弹性张量本应对称；防数值不对称）
    C_ext = (C_ext + C_ext.T) / 2.0
    C_gt = (C_gt + C_gt.T) / 2.0

    laue = _laue_class(space_group_number)
    # 分数 = Mandel 特征值相对 L2（真旋转不变量）。
    ev_ext, ev_gt = _mandel_eigs(C_ext), _mandel_eigs(C_gt)
    if ev_ext is None or ev_gt is None:
        return dict(_zero)
    gt_norm = float(np.linalg.norm(ev_gt))
    if gt_norm < 1e-9:
        warnings.warn("elastic_symmetry_reduced: GT Mandel 特征值范数≈0", stacklevel=2)
        return dict(_zero)
    rel = float(np.linalg.norm(ev_ext - ev_gt) / gt_norm)
    # Born 稳定性（与 matrix_eigenvalue_relative 同口径）：负门槛容忍 FP 噪声；
    # born 不稳 → 闸 0；全负 = 整体翻号（QE 应力未取负的符号约定 bug 签名）。
    max_abs = float(np.max(np.abs(ev_ext))) if ev_ext.size else 0.0
    neg_floor = -1e-3 * max_abs
    born_unstable = bool(ev_ext.size and float(ev_ext[0]) < neg_floor)
    sign_flip = bool(ev_ext.size and float(ev_ext[-1]) < neg_floor)
    score = 0.0 if born_unstable else _graded_score(rel, tolerance, partial)
    out: dict[str, Any] = {
        "score": score, "mode": "mandel_invariant", "laue": laue, "eig_rel": rel,
        "eig_ext": ev_ext.tolist(), "eig_gt": ev_gt.tolist(),
        "born_unstable": born_unstable, "sign_convention_suspected": sign_flip}
    # 诊断（不参与打分；frame-dependent，仅对同取向数据可解释"哪个常数偏"）。
    gt_consts = _symmetry_constants(C_gt, laue) if laue else None
    if gt_consts is not None:
        ext_consts = _symmetry_constants(C_ext, laue)
        out["constants"] = {
            name: {"gt": gv, "ext": ext_consts[name],
                   "rel": (abs(ext_consts[name] - gv) / abs(gv) if abs(gv) > 1e-12
                           else abs(ext_consts[name] - gv))}
            for name, gv in gt_consts.items()}
    return out


def _score_stress_curve_rmse_gpa(
    *,
    extracted: Any,
    gt_value: Any,
    tolerance: float,
    partial: bool,
) -> dict[str, Any]:
    """T4 stress-strain curve scoring: 24×6=144 stress RMSE in GPa.

    Inputs:
      extracted: list[float]  (length = 24×6=144；mode-major, amp-minor, σ comp)
      gt_value:  {"ref_file": "<8-col CSV (mode_str, amp, σ1..σ6)>"}
      tolerance: RMSE threshold (GPa, typical 1.0)

    Algorithm:
      flat GT 24×6 → 144 floats，pointwise RMSE，tol→1, 2·tol→0 衰减。
    """
    _zero = {"score": 0.0, "rmse_gpa": float("inf"), "n_points": 0}

    if not isinstance(extracted, list) or len(extracted) < 6:
        warnings.warn("stress_curve_rmse: extracted too short", stacklevel=2)
        return dict(_zero)

    if not isinstance(gt_value, dict) or "ref_file" not in gt_value:
        warnings.warn("stress_curve_rmse: GT.value missing ref_file", stacklevel=2)
        return dict(_zero)

    ref_path = _resolve_ref_file(gt_value["ref_file"])
    if ref_path is None:
        warnings.warn(
            f"stress_curve_rmse: ref_file not found: {gt_value['ref_file']}", stacklevel=2,
        )
        return dict(_zero)

    try:
        gt_flat: list[float] = []
        for line in Path(ref_path).read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = [p.strip() for p in s.replace(",", " ").split() if p.strip()]
            if len(parts) < 8:
                continue
            # skip header (mode str → not a float; amplitude must parse)
            try:
                _ = float(parts[1])
            except ValueError:
                continue
            for i in range(6):
                gt_flat.append(float(parts[2 + i]))
    except Exception as e:
        warnings.warn(f"stress_curve_rmse: ref_file parse error: {e}", stacklevel=2)
        return dict(_zero)

    if len(gt_flat) != len(extracted):
        warnings.warn(
            f"stress_curve_rmse: length mismatch (ext={len(extracted)} vs gt={len(gt_flat)})",
            stacklevel=2,
        )
        return dict(_zero)

    try:
        import numpy as np
    except ImportError:
        return dict(_zero)

    ext_arr = np.array(extracted, dtype=float)
    gt_arr = np.array(gt_flat, dtype=float)
    rmse = float(np.sqrt(np.mean((ext_arr - gt_arr) ** 2)))

    # 收紧曲线（2026-06-12）：满分 rmse ≤ 0.3·tol、零分 rmse ≥ tol。
    score = _graded_score(rmse, tolerance, partial)

    return {
        "score": score,
        "rmse_gpa": rmse,
        "n_points": len(extracted),
    }
