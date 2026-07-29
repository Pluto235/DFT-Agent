"""Generate an HTML benchmark report for runs_subset42, mimicking baseline-2026-05-10.html.

Inputs:
  runs_dir/<UTC>_<task_id>/
    report.json        # harness output: task_id, model, total_score, wall, exit_code
    transcript.json    # solver: list[{usage:{...}, wall_s, ...}], for token + ai_sec
    gpu_metrics.json   # gpu_sampler.py output: util/mem/power/temp p95 + bottleneck
    *.out              # QE output(s); we grep "PWSCF ... WALL" for dft_sec
  ./bench/questions_subset42/MANIFEST.json

Output:
  --out subset42-2026-05-10.html (single self-contained HTML file)

Layout mimics baseline:
  §1.1 cards (completion/cost/wall/ai/dft/tokens/cache/gpu_util/mem/power)
  §1.2 model pricing comparison table
  §2 distribution histograms (12 bins each)
  §3 per-task detail table (clickable sort, regex filter)
"""
from __future__ import annotations
import argparse
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_BENCH = Path("./bench")
DEFAULT_MANIFEST = REPO_BENCH / "questions_subset42" / "MANIFEST.json"
PRICING_PATH = Path(__file__).parent / "model_pricing.json"

QE_WALL_RE = re.compile(r"PWSCF\s*:\s*([\d.]+)s\s*CPU\s+([\d.]+)s\s*WALL")


BASELINE_TABLE_RE = re.compile(
    r'<table id="per-task-table">.*?<tbody>(.*?)</tbody>.*?</table>',
    re.DOTALL,
)
BASELINE_ROW_RE = re.compile(r'<tr>\s*<td>([^<]+)</td>(.+?)</tr>', re.DOTALL)
BASELINE_SORT_RE = re.compile(r'data-sort="([^"]+)"')


def parse_baseline_html(path: Path, want_ids: set[str]) -> dict[str, dict]:
    """Extract per-task data from baseline-2026-05-10.html §3 table.

    Cell order (data-sort attrs): score, cost_usd, total_sec, ai_sec, dft_sec,
    n_dft_calls, rounds, input_tok, output_tok, cache_hit, cache_miss,
    gpu_util_p95, mem_mib_p95, power_w_p95, temp_c_p95.
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    m = BASELINE_TABLE_RE.search(text)
    if not m:
        return {}
    out: dict[str, dict] = {}
    for row_m in BASELINE_ROW_RE.finditer(m.group(1)):
        tid = row_m.group(1)
        if tid not in want_ids:
            continue
        sorts = BASELINE_SORT_RE.findall(row_m.group(2))
        if len(sorts) < 7:
            continue
        try:
            out[tid] = {
                "score": float(sorts[0]),
                "cost_usd": float(sorts[1]),
                "total_sec": float(sorts[2]),
                "ai_sec": float(sorts[3]),
                "dft_sec": float(sorts[4]),
                "rounds": int(float(sorts[6])),
            }
        except (ValueError, IndexError):
            continue
    return out


# ---------------------------------------------------------------------------
# Per-task aggregation
# ---------------------------------------------------------------------------


def parse_qe_wall(workdir: Path) -> float:
    """Sum WALL across all .out files (best-effort)."""
    total = 0.0
    for out in workdir.rglob("*.out"):
        try:
            text = out.read_text(errors="replace")
        except Exception:
            continue
        for m in QE_WALL_RE.finditer(text):
            total += float(m.group(2))
    return total


def aggregate_transcript(transcript_path: Path) -> dict[str, Any]:
    if not transcript_path.exists():
        return {"turns": 0, "input": 0, "output": 0, "cache_read": 0,
                "cache_create": 0, "ai_sec": 0.0}
    try:
        ts = json.loads(transcript_path.read_text())
    except Exception:
        return {"turns": 0, "input": 0, "output": 0, "cache_read": 0,
                "cache_create": 0, "ai_sec": 0.0}
    if not isinstance(ts, list):
        ts = ts.get("turns", []) if isinstance(ts, dict) else []

    inp = out = cr = cc = 0
    wall = 0.0
    for turn in ts:
        u = turn.get("usage") or {}
        inp += u.get("input_tokens", 0)
        out += u.get("output_tokens", 0)
        cr  += u.get("cache_read_input_tokens", 0)
        cc  += u.get("cache_creation_input_tokens", 0)
        wall += turn.get("wall_s") or 0.0
    return {"turns": len(ts), "input": inp, "output": out,
            "cache_read": cr, "cache_create": cc, "ai_sec": wall}


def compute_cost(model: str, inp: int, out: int, cr: int, cc: int,
                 pricing: dict) -> float:
    p = pricing.get(model)
    if not p:
        return 0.0
    cost = (
        inp * p["input"] / 1e6 +
        out * p["output"] / 1e6 +
        cr  * p["input"] * p.get("cache_read_factor", 1.0) / 1e6 +
        cc  * p["input"] * p.get("cache_write_factor", 1.0) / 1e6
    )
    return cost


def collect_run(run_dir: Path, pricing: dict) -> dict[str, Any] | None:
    rj_path = run_dir / "report.json"
    if not rj_path.exists():
        return None
    try:
        rj = json.loads(rj_path.read_text())
    except Exception:
        return None

    task_id = rj.get("task_id") or run_dir.name.split("_", 1)[-1]
    model = rj.get("model", "claude-sonnet-4-6")
    score = rj.get("total_score")
    failed = score is None
    if failed:
        score = 0.0
    wall_total = rj.get("solver_wall_clock_sec") or 0.0
    rc = rj.get("solver_exit_code", -1)
    status = rj.get("solver_status", "ok")
    failure_reason = rj.get("failure_reason", "")

    workdir = run_dir / "workdir"
    if not workdir.exists():
        # harness sometimes has files at run_dir root if workdir wasn't separated
        workdir = run_dir

    dft_sec = parse_qe_wall(workdir)
    transcript = aggregate_transcript(workdir / "transcript.json")
    if transcript["turns"] == 0:
        # fallback to old layout
        transcript = aggregate_transcript(run_dir / "transcript.json")

    cost = compute_cost(model, transcript["input"], transcript["output"],
                        transcript["cache_read"], transcript["cache_create"], pricing)

    gpu_path = run_dir / "gpu_metrics.json"
    gpu = {}
    if gpu_path.exists():
        try:
            gpu = json.loads(gpu_path.read_text())
        except Exception:
            gpu = {}

    # tokens for histograms / table
    inp_total = transcript["input"] + transcript["cache_read"] + transcript["cache_create"]
    out_total = transcript["output"]

    return {
        "task_id": task_id,
        "model": model,
        "score": float(score),
        "failed": failed,
        "status": status,
        "failure_reason": failure_reason,
        "cost_usd": cost,
        "total_sec": float(wall_total),
        "ai_sec": float(transcript["ai_sec"]),
        "dft_sec": dft_sec,
        "rounds": transcript["turns"],
        "input_tokens": inp_total,
        "output_tokens": out_total,
        "cache_hit_tokens": transcript["cache_read"],
        "cache_miss_tokens": transcript["input"] + transcript["cache_create"],
        "gpu_util_p95": gpu.get("gpu_util_p95", 0.0),
        "mem_mib_p95": gpu.get("mem_mib_p95", 0.0),
        "power_w_p95": gpu.get("power_w_p95", 0.0),
        "temp_c_p95": gpu.get("temp_c_p95", 0.0),
        "bottleneck": gpu.get("bottleneck", "—"),
        "rc": rc,
        "run_dir": str(run_dir),
    }


# ---------------------------------------------------------------------------
# Aggregations + histograms
# ---------------------------------------------------------------------------


def histogram(values: list[float], bins: int = 12) -> list[tuple[float, float, int]]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi <= lo:
        hi = lo + 1.0
    width = (hi - lo) / bins
    edges = [lo + i * width for i in range(bins + 1)]
    counts = [0] * bins
    for v in values:
        idx = min(bins - 1, max(0, int((v - lo) / width)))
        counts[idx] += 1
    return [(edges[i], edges[i + 1], counts[i]) for i in range(bins)]


def fmt_num(x: float, decimals: int = 0) -> str:
    if abs(x) >= 1e6:
        return f"{x/1e6:.2f}M"
    if abs(x) >= 1e3:
        return f"{x/1e3:.1f}K"
    if decimals > 0:
        return f"{x:.{decimals}f}"
    return f"{int(round(x))}"


def fmt_seconds(s: float) -> str:
    if s >= 3600:
        return f"{s/3600:.1f} 小时"
    if s >= 60:
        return f"{s/60:.1f} 分钟"
    return f"{s:.1f} 秒"


def fmt_usd(x: float) -> str:
    return f"${x:.2f}"


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Hiragino Sans GB',
       'Microsoft YaHei', sans-serif;
       max-width: 1500px; margin: 0 auto; padding: 30px; color: #2c3e50; line-height: 1.7; }
h1 { color: #34495e; border-bottom: 3px solid #3498db; padding-bottom: 10px; }
h2 { color: #34495e; margin-top: 50px; border-bottom: 1px solid #bdc3c7; padding-bottom: 5px; }
h3 { color: #34495e; margin-top: 30px; }
p { margin: 10px 0; }
.meta { background: #ecf0f1; padding: 14px 20px; border-radius: 6px; margin: 15px 0;
        line-height: 1.7; }
.meta strong { color: #2c3e50; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 13px; }
th { background: #34495e; color: white; padding: 8px 10px; text-align: left;
     font-weight: 600; cursor: pointer; user-select: none; position: sticky; top: 0; }
th:hover { background: #2c3e50; }
th.sort-asc::after  { content: ' ↑'; }
th.sort-desc::after { content: ' ↓'; }
td { border-bottom: 1px solid #ecf0f1; padding: 6px 10px; vertical-align: top; }
tr:hover td { background: #f8f9fa; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.score-1 { color: #27ae60; font-weight: bold; }
.score-mid { color: #e67e22; }
.score-low { color: #c0392b; font-weight: bold; }
.bottleneck-compute { color: #e74c3c; font-weight: 600; }
.bottleneck-underutilized { color: #95a5a6; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
         gap: 12px; margin: 15px 0; }
.card { background: #ecf0f1; padding: 14px 16px; border-radius: 6px; border-left: 4px solid #3498db; }
.card-label { color: #7f8c8d; font-size: 12px; }
.card-value { font-size: 22px; font-weight: 600; margin: 4px 0; color: #2c3e50; }
.card-sub { color: #7f8c8d; font-size: 12px; line-height: 1.4; }
.hist { font-family: ui-monospace, 'SF Mono', monospace; font-size: 12px; margin: 8px 0; }
.hist-row { display: grid; grid-template-columns: 160px 1fr 50px; gap: 8px;
            align-items: center; margin: 1px 0; }
.bar-container { background: #ecf0f1; height: 18px; border-radius: 2px; overflow: hidden; }
.bar-fill { background: linear-gradient(90deg, #3498db, #2980b9); height: 100%;
            display: block; }
.note { background: #fffbe6; border-left: 4px solid #f1c40f; padding: 12px 16px;
        margin: 12px 0; font-size: 14px; color: #7d6608; border-radius: 4px;
        line-height: 1.7; }
.disclaimer { color: #7f8c8d; font-size: 13px; margin: 10px 0; line-height: 1.7; }
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
              gap: 18px; margin: 15px 0; }
.stat-block { background: white; padding: 14px 16px; border-radius: 6px;
              border: 1px solid #ecf0f1; }
.stat-block h4 { margin: 0 0 6px 0; color: #34495e; font-size: 14px; }
.stat-block .desc { color: #7f8c8d; font-size: 12px; margin-bottom: 8px; line-height: 1.5; }
.filter-input { padding: 6px 10px; font-size: 13px; border: 1px solid #bdc3c7;
                border-radius: 4px; margin-bottom: 10px; width: 240px; }
.scroll-table { max-height: 600px; overflow-y: auto; border: 1px solid #ecf0f1; }
"""

JS = r"""
// Click table headers to sort by data-sort numeric value (or text fallback).
document.querySelectorAll('table#per-task-table th').forEach((th, idx) => {
  th.addEventListener('click', () => {
    const tbody = th.closest('table').querySelector('tbody');
    const rows = Array.from(tbody.rows);
    const cur = th.classList.contains('sort-asc') ? 'asc'
              : th.classList.contains('sort-desc') ? 'desc' : 'none';
    document.querySelectorAll('table#per-task-table th').forEach(x => x.classList.remove('sort-asc', 'sort-desc'));
    const dir = (cur === 'asc') ? 'desc' : 'asc';
    th.classList.add(dir === 'asc' ? 'sort-asc' : 'sort-desc');
    rows.sort((a, b) => {
      const av = a.cells[idx].dataset.sort ?? a.cells[idx].textContent;
      const bv = b.cells[idx].dataset.sort ?? b.cells[idx].textContent;
      const an = parseFloat(av), bn = parseFloat(bv);
      if (!isNaN(an) && !isNaN(bn)) return dir === 'asc' ? an - bn : bn - an;
      return dir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
    });
    rows.forEach(r => tbody.appendChild(r));
  });
});

// Filter input: hide rows whose first cell text doesn't match.
const fil = document.getElementById('per-task-table-filter');
if (fil) fil.addEventListener('input', () => {
  const q = fil.value.toLowerCase();
  document.querySelectorAll('table#per-task-table tbody tr').forEach(tr => {
    tr.style.display = tr.cells[0].textContent.toLowerCase().includes(q) ? '' : 'none';
  });
});
"""


def render_card(label: str, value: str, sub: str = "") -> str:
    return (
        f'<div class="card"><div class="card-label">{label}</div>'
        f'<div class="card-value">{value}</div>'
        f'<div class="card-sub">{sub}</div></div>'
    )


def render_baseline_section(rows: list[dict], baseline: dict[str, dict],
                            baseline_label: str = "DeepSeek-v4-pro (baseline)") -> str:
    """Render §X 'vs baseline' comparison. Returns empty string if no baseline data."""
    if not baseline:
        return ""

    paired = [(r, baseline.get(r["task_id"])) for r in rows]
    paired = [(r, b) for r, b in paired if b is not None]
    if not paired:
        return ""

    n = len(paired)
    bl_pass = sum(1 for _, b in paired if b["score"] >= 0.5)
    our_pass = sum(1 for r, _ in paired if not r["failed"] and r["score"] >= 0.5)
    bl_score_sum = sum(b["score"] for _, b in paired)
    our_score_sum = sum(r["score"] for r, _ in paired)
    bl_cost = sum(b["cost_usd"] for _, b in paired)
    our_cost = sum(r["cost_usd"] for r, _ in paired)
    bl_wall = sum(b["total_sec"] for _, b in paired)
    our_wall = sum(r["total_sec"] for r, _ in paired)
    bl_ai   = sum(b["ai_sec"] for _, b in paired)
    our_ai  = sum(r["ai_sec"] for r, _ in paired)
    bl_dft  = sum(b["dft_sec"] for _, b in paired)
    our_dft = sum(r["dft_sec"] for r, _ in paired)

    def ratio(num, den):
        return num / den if den > 0 else 0.0

    cards = "".join([
        render_card("通过数（score≥0.5）",
                    f"{our_pass}/{n}",
                    f"baseline {bl_pass}/{n}"),
        render_card("平均得分",
                    f"{our_score_sum/n:.3f}",
                    f"baseline {bl_score_sum/n:.3f}（Δ {our_score_sum/n - bl_score_sum/n:+.3f}）"),
        render_card("总成本（美元）",
                    f"${our_cost:.4f}",
                    f"baseline ${bl_cost:.4f}（{ratio(our_cost, bl_cost):.1f}×）"),
        render_card("总挂钟",
                    f"{our_wall/3600:.2f} h",
                    f"baseline {bl_wall/3600:.2f} h（{ratio(our_wall, bl_wall):.1f}×）"),
        render_card("AI 思考累计",
                    f"{our_ai/3600:.2f} h",
                    f"baseline {bl_ai/3600:.2f} h（{ratio(our_ai, bl_ai):.1f}×）"),
        render_card("DFT 计算累计",
                    f"{our_dft/3600:.2f} h",
                    f"baseline {bl_dft/3600:.2f} h（{ratio(our_dft, bl_dft):.1f}×）"),
    ])

    table_rows = []
    for r, b in paired:
        bs = b["score"]; us = r["score"]
        delta = us - bs
        d_cls = "score-1" if delta >= -0.02 else "score-mid" if delta >= -0.20 else "score-low"
        delta_str = f"{delta:+.3f}" if not r["failed"] else "失败"
        our_score_str = f"{us:.3f}" if not r["failed"] else "✗"
        table_rows.append(
            f'<tr>'
            f'<td>{r["task_id"]}</td>'
            f'<td class="num" data-sort="{bs}">{bs:.3f}</td>'
            f'<td class="num {("score-1" if us >= 0.95 else "score-mid" if us >= 0.5 else "score-low") if not r["failed"] else "score-low"}" '
            f'data-sort="{us if not r["failed"] else -1}">{our_score_str}</td>'
            f'<td class="num {d_cls}" data-sort="{delta if not r["failed"] else -99}">{delta_str}</td>'
            f'<td class="num" data-sort="{b["cost_usd"]}">${b["cost_usd"]:.4f}</td>'
            f'<td class="num" data-sort="{r["cost_usd"]}">${r["cost_usd"]:.4f}</td>'
            f'<td class="num" data-sort="{ratio(r["cost_usd"], b["cost_usd"])}">{ratio(r["cost_usd"], b["cost_usd"]):.1f}×</td>'
            f'<td class="num" data-sort="{b["total_sec"]}">{b["total_sec"]:.0f}</td>'
            f'<td class="num" data-sort="{r["total_sec"]}">{r["total_sec"]:.0f}</td>'
            f'<td class="num" data-sort="{ratio(r["total_sec"], b["total_sec"])}">{ratio(r["total_sec"], b["total_sec"]):.1f}×</td>'
            f'<td class="num" data-sort="{b["ai_sec"]}">{b["ai_sec"]:.0f}</td>'
            f'<td class="num" data-sort="{r["ai_sec"]}">{r["ai_sec"]:.0f}</td>'
            f'<td class="num" data-sort="{b["dft_sec"]}">{b["dft_sec"]:.0f}</td>'
            f'<td class="num" data-sort="{r["dft_sec"]}">{r["dft_sec"]:.0f}</td>'
            f'</tr>'
        )

    return f"""
<h2>§4 与 baseline 同题对照</h2>

<p>把这 {n} 道题在 baseline（<code>{baseline_label}</code>，跑于 2026-05-10）上的成绩
和本次跑（<code>claude-sonnet-4-6</code>，warpagent-qe-v1 solver）做<b>同题对照</b>。
分数差 Δ 用三色：绿色 = 与 baseline 持平/胜出（≥-0.02）、黄色 = 中等差距（-0.02 ~ -0.20）、红色 = 明显落后（&lt;-0.20）。</p>

<h3>4.1 总览</h3>
<div class="cards">
{cards}
</div>

<h3>4.2 每题对照</h3>
<div class="scroll-table">
<table>
<thead><tr>
<th>题目编号</th>
<th>baseline 得分</th><th>我们得分</th><th>Δ</th>
<th>baseline 成本</th><th>我们成本</th><th>成本比</th>
<th>baseline 挂钟（秒）</th><th>我们挂钟（秒）</th><th>挂钟比</th>
<th>baseline AI（秒）</th><th>我们 AI（秒）</th>
<th>baseline DFT（秒）</th><th>我们 DFT（秒）</th>
</tr></thead>
<tbody>
{"".join(table_rows)}
</tbody>
</table>
</div>
"""


def render_histogram_block(title: str, desc: str, values: list[float],
                           fmt=lambda v: f"{v:.0f}") -> str:
    if not values:
        return f'<div class="stat-block"><h4>{title}</h4><div class="desc">{desc}</div><p>无数据</p></div>'
    n = len(values)
    avg = statistics.mean(values)
    med = statistics.median(values)
    p95 = sorted(values)[max(0, int(round(0.95 * (n - 1))))]
    lo, hi = min(values), max(values)
    hist = histogram(values, bins=12)
    max_count = max((c for _, _, c in hist), default=1)

    rows_html = []
    for a, b, c in hist:
        bar_pct = (c / max_count * 100) if max_count else 0
        rows_html.append(
            f'<div class="hist-row"><span>{fmt(a)} – {fmt(b)}</span>'
            f'<span class="bar-container"><span class="bar-fill" style="width: {bar_pct:.1f}%"></span></span>'
            f'<span class="num">{c}</span></div>'
        )
    return (
        f'<div class="stat-block">'
        f'<h4>{title}</h4>'
        f'<div class="desc">{desc}</div>'
        f'<div>题数 {n} ｜ 平均 {fmt(avg)} ｜ 中位 {fmt(med)} ｜ 95 分位 {fmt(p95)} ｜ 最小 {fmt(lo)} ｜ 最大 {fmt(hi)}</div>'
        f'<div class="hist">{"".join(rows_html)}</div>'
        f'</div>'
    )


def score_class(s: float) -> str:
    if s >= 0.999: return "score-1"
    if s >= 0.5:   return "score-mid"
    return "score-low"


def bottleneck_class(b: str) -> str:
    if b in ("GPU 算力", "GPU 显存", "GPU 温度"):
        return "bottleneck-compute"
    if b == "GPU 闲置":
        return "bottleneck-underutilized"
    return ""


# ---------------------------------------------------------------------------
# §1.2 model cost projection
# ---------------------------------------------------------------------------


def model_cost_table(rows: list[dict], pricing: dict, this_model: str) -> str:
    total_inp = sum(r["cache_miss_tokens"] for r in rows)  # uncached portion
    total_inp_full = sum(r["input_tokens"] for r in rows)  # all input incl. cached
    total_out = sum(r["output_tokens"] for r in rows)
    total_cache_hit = sum(r["cache_hit_tokens"] for r in rows)

    htmls = ['<table><thead><tr>'
             '<th>大语言模型</th><th>厂商</th><th>定位</th>'
             '<th>输入价（$/M）</th><th>输出价（$/M）</th>'
             '<th class="num">不用缓存（$）</th>'
             '<th class="num">实际享缓存价（$）</th>'
             '<th class="num">理论享缓存价（$）</th>'
             '<th>说明</th></tr></thead><tbody>']

    for model, p in pricing.items():
        if model.startswith("_"):
            continue
        no_cache = total_inp_full * p["input"] / 1e6 + total_out * p["output"] / 1e6
        # actual: only deepseek + sonnet have configured cache discount; for the model
        # we actually used we pass the real cost.
        real = "—"
        if model == this_model:
            # use the same compute_cost machinery on aggregate
            # split: cache_hit treated as cache_read; cache_miss split into uncached vs cache_create
            # here we use a uniform approximation = cost over actual run (sum from rows)
            actual_cost = sum(r["cost_usd"] for r in rows)
            real = f"${actual_cost:.2f}"
        elif "cache_read_factor" in p and "cache_write_factor" in p and total_cache_hit > 0:
            # theoretical: 97% hit assumption mirrors baseline
            hit_ratio = total_cache_hit / max(1, total_inp_full)
            cached_in = total_inp_full * hit_ratio
            fresh_in  = total_inp_full * (1 - hit_ratio)
            theo = (cached_in * p["input"] * p["cache_read_factor"] +
                    fresh_in * p["input"] * p["cache_write_factor"]) / 1e6
            theo += total_out * p["output"] / 1e6
            theo_s = f"${theo:.2f}"
        else:
            theo_s = "—"
        if model == this_model:
            theo_s = "—"
        else:
            theo_s = theo_s if "cache_read_factor" in p else "—"

        note = p.get("note", "")
        marker = "★ " if model == this_model else ""
        htmls.append(
            f'<tr>'
            f'<td>{marker}{model}</td>'
            f'<td>{p["vendor"]}</td>'
            f'<td>{p["tier"]}</td>'
            f'<td class="num">${p["input"]:.3f}</td>'
            f'<td class="num">${p["output"]:.3f}</td>'
            f'<td class="num">${no_cache:.2f}</td>'
            f'<td class="num">{real}</td>'
            f'<td class="num">{theo_s}</td>'
            f'<td>{note}</td>'
            f'</tr>'
        )
    htmls.append('</tbody></table>')
    return "".join(htmls)


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------


def build_report(rows: list[dict], pricing: dict, this_model: str,
                 manifest: dict, out_path: Path,
                 expected_t1: int, expected_t2: int,
                 title: str | None = None,
                 subtitle_html: str | None = None,
                 baseline_data: dict | None = None,
                 baseline_label: str = "DeepSeek-v4-pro (baseline)") -> None:
    rows.sort(key=lambda r: r["task_id"])
    n_done = len(rows)
    n_ok = sum(1 for r in rows if not r["failed"])
    n_fail = sum(1 for r in rows if r["failed"])

    # T1/T2 split (this report is T1-focused; T2 may be 0)
    t1 = [r for r in rows if "_T1_" in r["task_id"]]
    t2 = [r for r in rows if "_T2_" in r["task_id"]]

    total_cost = sum(r["cost_usd"] for r in rows)
    total_wall = sum(r["total_sec"] for r in rows)
    total_ai = sum(r["ai_sec"] for r in rows)
    total_dft = sum(r["dft_sec"] for r in rows)
    total_inp_full = sum(r["input_tokens"] for r in rows)
    total_out = sum(r["output_tokens"] for r in rows)
    total_cache_hit = sum(r["cache_hit_tokens"] for r in rows)
    cache_hit_rate = total_cache_hit / max(1, total_inp_full)

    gpu_util_avg = (statistics.mean([r["gpu_util_p95"] for r in rows]) if rows else 0.0)
    gpu_util_max = (max([r["gpu_util_p95"] for r in rows], default=0.0))
    mem_p95_avg  = (statistics.mean([r["mem_mib_p95"] for r in rows]) if rows else 0.0)
    mem_max      = (max([r["mem_mib_p95"] for r in rows], default=0.0))
    pow_avg      = (statistics.mean([r["power_w_p95"] for r in rows]) if rows else 0.0)
    pow_max      = (max([r["power_w_p95"] for r in rows], default=0.0))

    n_compute = sum(1 for r in rows if r["bottleneck"] in ("GPU 算力", "GPU 显存", "GPU 温度"))
    n_idle = sum(1 for r in rows if r["bottleneck"] == "GPU 闲置")

    cards = [
        render_card("题目数 / 通过数",
                    f"{n_done} / {n_ok}",
                    f"T1 {len(t1)}/{expected_t1}, T2 {len(t2)}/{expected_t2}；失败 {n_fail} 题"),
        render_card("API 调用总成本",
                    fmt_usd(total_cost),
                    f"平均每题 ${total_cost/max(1,n_done):.4f}"),
        render_card("全部题目耗时之和",
                    fmt_seconds(total_wall),
                    f"包含 AI 思考、DFT 计算、文件 IO"),
        render_card("AI 思考 / 等模型回复时间",
                    fmt_seconds(total_ai),
                    f"占总耗时的 {total_ai/max(1,total_wall)*100:.0f}%"),
        render_card("DFT 实际计算时间",
                    fmt_seconds(total_dft),
                    f"占总耗时的 {total_dft/max(1,total_wall)*100:.0f}%"),
        render_card("AI 处理的文字总量",
                    f"{total_inp_full/1e6:.2f}M token",
                    f"输入 {total_inp_full/1e6:.2f}M，输出 {total_out/1e3:.1f}K"),
        render_card("提示词缓存命中率",
                    f"{cache_hit_rate*100:.1f}%",
                    f"{total_cache_hit/1e6:.2f}M 命中 / {(total_inp_full-total_cache_hit)/1e6:.2f}M 未命中"),
        render_card("GPU 计算利用率",
                    f"{gpu_util_avg:.1f}%",
                    f"取每题 95 分位再求平均；最高 {gpu_util_max:.0f}%"),
        render_card("GPU 显存峰值占用",
                    f"{mem_max/1024:.1f} GiB",
                    f"H100 单卡 80 GiB；本批次最高仅 {mem_max:.0f} MiB"),
        render_card("GPU 功耗水平",
                    f"{pow_avg:.0f} 瓦",
                    f"H100 SXM 满载 ~700 瓦；本批次最高 {pow_max:.0f} 瓦"),
    ]

    # --- §3 table rows ---
    table_rows = []
    for r in rows:
        sc = r["score"]
        sc_cls = score_class(sc)
        bn_cls = bottleneck_class(r["bottleneck"])
        status_str = "✗ 失败/超时" if r["failed"] else "✓ 完成"
        status_cls = "score-low" if r["failed"] else "score-1"
        table_rows.append(
            f'<tr>'
            f'<td>{r["task_id"]}</td>'
            f'<td class="{status_cls}">{status_str}</td>'
            f'<td class="num {sc_cls}" data-sort="{sc}">{sc:.2f}</td>'
            f'<td class="num" data-sort="{r["cost_usd"]}">${r["cost_usd"]:.4f}</td>'
            f'<td class="num" data-sort="{r["total_sec"]}">{r["total_sec"]:.0f}</td>'
            f'<td class="num" data-sort="{r["ai_sec"]}">{r["ai_sec"]:.0f}</td>'
            f'<td class="num" data-sort="{r["dft_sec"]}">{r["dft_sec"]:.0f}</td>'
            f'<td class="num" data-sort="{r["rounds"]}">{r["rounds"]}</td>'
            f'<td class="num" data-sort="{r["input_tokens"]}">{r["input_tokens"]:,}</td>'
            f'<td class="num" data-sort="{r["output_tokens"]}">{r["output_tokens"]:,}</td>'
            f'<td class="num" data-sort="{r["cache_hit_tokens"]}">{r["cache_hit_tokens"]:,}</td>'
            f'<td class="num" data-sort="{r["cache_miss_tokens"]}">{r["cache_miss_tokens"]:,}</td>'
            f'<td class="num" data-sort="{r["gpu_util_p95"]}">{r["gpu_util_p95"]:.1f}</td>'
            f'<td class="num" data-sort="{r["mem_mib_p95"]}">{r["mem_mib_p95"]:.0f}</td>'
            f'<td class="num" data-sort="{r["power_w_p95"]}">{r["power_w_p95"]:.1f}</td>'
            f'<td class="num" data-sort="{r["temp_c_p95"]}">{r["temp_c_p95"]:.1f}</td>'
            f'<td class="{bn_cls}">{r["bottleneck"]}</td>'
            f'</tr>'
        )

    # --- histograms ---
    hists = []
    if rows:
        hists.append(render_histogram_block(
            "题目得分（满分 1.0）",
            "由评测器自动比对 AI 提交的结果与标准答案给出，越接近 1.0 越好。",
            [r["score"] for r in rows], fmt=lambda v: f"{v:.2f}"))
        hists.append(render_histogram_block(
            "单题总耗时（秒）",
            "AI 接到题目到提交答案之间的真实时间，包含思考、DFT 计算、文件读写等所有环节。",
            [r["total_sec"] for r in rows], fmt=lambda v: f"{v:.0f}"))
        hists.append(render_histogram_block(
            "AI 思考 / 等模型回复（秒）",
            "调用大语言模型 API 的累计等待时间。这部分按 token 计费。",
            [r["ai_sec"] for r in rows], fmt=lambda v: f"{v:.0f}"))
        hists.append(render_histogram_block(
            "DFT 实际计算时间（秒）",
            "Quantum ESPRESSO 在 GPU 上跑量子力学计算的累计时间。",
            [r["dft_sec"] for r in rows], fmt=lambda v: f"{v:.0f}"))
        hists.append(render_histogram_block(
            "对话轮数",
            "AI 与系统来回交互的轮数。每轮 = 一次模型响应 + 一次工具调用。",
            [r["rounds"] for r in rows], fmt=lambda v: f"{v:.0f}"))
        hists.append(render_histogram_block(
            "单题 API 成本（美元）",
            "按本次模型实际计费规则计算的单题花费，已考虑提示词缓存折扣。",
            [r["cost_usd"] for r in rows], fmt=lambda v: f"${v:.4f}"))
        hists.append(render_histogram_block(
            "输入 token 数（含缓存命中）",
            "AI 收到的全部输入文字量（系统提示 + 历史对话 + 工具回执）。",
            [r["input_tokens"] for r in rows], fmt=lambda v: f"{v/1e3:.0f}K"))
        hists.append(render_histogram_block(
            "输出 token 数",
            "AI 生成的全部回复文字量（含推理过程 + 工具调用参数）。",
            [r["output_tokens"] for r in rows], fmt=lambda v: f"{v/1e3:.0f}K"))
        hists.append(render_histogram_block(
            "GPU 计算利用率 95 分位（%）",
            "题目运行期间，GPU 算力使用率排序后的 95 分位值。",
            [r["gpu_util_p95"] for r in rows], fmt=lambda v: f"{v:.1f}"))
        hists.append(render_histogram_block(
            "GPU 显存峰值（MiB）",
            "题目运行期间显存占用的 95 分位。",
            [r["mem_mib_p95"] for r in rows], fmt=lambda v: f"{v:.0f}"))
        hists.append(render_histogram_block(
            "GPU 功耗 95 分位（瓦）",
            "GPU 实测瞬时功耗的 95 分位。",
            [r["power_w_p95"] for r in rows], fmt=lambda v: f"{v:.0f}"))
        hists.append(render_histogram_block(
            "GPU 温度峰值（℃）",
            "题目期间 GPU 温度最高值。H100 工作温度上限约 90℃。",
            [r["temp_c_p95"] for r in rows], fmt=lambda v: f"{v:.0f}"))

    # --- assemble ---
    now = datetime.now(timezone.utc).strftime("%Y 年 %m 月 %d 日 %H:%M UTC")
    if title is None:
        title = "DFT-Agent 评测基准 · v1 solver × Sonnet 4.6 子集结果"
    if subtitle_html is None:
        subtitle_html = (
            f"本报告记录的是用<b>本仓库 solver</b>（warpagent-qe-v1，Anthropic SDK + "
            f"Claude Sonnet 4.6）跑 <b>{expected_t1+expected_t2} 题子集</b>的成绩"
            f"（T1 {expected_t1} 题 + T2 {expected_t2} 题）。"
        )
    body_top = f"""
<h1>{title}</h1>

<div class="meta">
{subtitle_html}
</div>

<div class="meta">
<strong>测试设置</strong>：用 <code>{this_model}</code> 模型 + 本仓库 v1 solver
（<code>warpagent-qe-v1</code>），在 questions_subset42 子集上跑 21 道 T1。<br>
<strong>硬件</strong>：H100 80GB（GPU QE wrapper at <code>~/qe-gpu-wrapper</code>）。
<strong>报告生成时间</strong>：{now}。
</div>

<h2>§1 跑这一轮测试需要多少资源？</h2>

<h3>1.1 资源消耗一览</h3>
<div class="cards">
{"".join(cards)}
</div>
"""
    if n_done > 0:
        body_top += f"""
<div class="note">
<strong>性能瓶颈分布</strong>：GPU 闲置 {n_idle} 题 ｜ GPU 算力/显存/温度受限 {n_compute} 题
<br><br>分析方法：每题运行期间的 GPU 算力使用率、显存容量、温度三维度归一化，最大值 ≥ 0.30
判定为受该维度限制，否则归类为「GPU 闲置」。
</div>
"""

    body_top += f"""
<h3>1.2 换其他大模型来跑要多少钱？</h3>

<p>这次用 <code>{this_model}</code> 跑 {n_done} 道题，按本次实际享缓存价付费。
如果换成其他大模型，按相同的输入/输出 token 量估算，预计花费如下表。</p>

<p class="disclaimer">
<strong>不用缓存</strong>：所有输入文字按标准价计费；
<strong>实际享缓存价</strong>：仅本次实际使用的模型（标 ★）有；
<strong>理论享缓存价</strong>：假设缓存命中率与本次实测一致 {cache_hit_rate*100:.1f}%。
</p>
{model_cost_table(rows, pricing, this_model)}

<h2>§2 各项性能指标的分布情况</h2>
<div class="stats-grid">
{"".join(hists)}
</div>

<h2>§3 全部 {n_done} 道题的明细数据</h2>
<p>点击表头按列排序，输入框可按题目编号过滤。</p>
<input class="filter-input" id="per-task-table-filter" placeholder="按题目编号搜索…">
<div class="scroll-table">
<table id="per-task-table">
<thead><tr>
<th>题目编号</th><th>状态</th><th>得分</th><th>成本（美元）</th>
<th>总耗时（秒）</th><th>AI 思考（秒）</th><th>DFT 计算（秒）</th>
<th>对话轮数</th><th>输入 token</th><th>输出 token</th>
<th>缓存命中 token</th><th>缓存未中 token</th>
<th>GPU 95% 利用率</th><th>显存峰值（MiB）</th><th>功耗峰值（瓦）</th><th>GPU 温度峰值（℃）</th>
<th>性能瓶颈</th>
</tr></thead>
<tbody>
{"".join(table_rows)}
</tbody>
</table>
</div>
"""

    if expected_t2 > 0 and len(t2) == 0:
        body_top += f"""
<div class="note">
<strong>注意：本次只跑了 21 道 T1。</strong> 21 道 T2（能带 / 态密度）任务在 solver
升级到 v2（增加 bands_dos 支持）后另行补跑。
</div>
"""

    if baseline_data:
        body_top += render_baseline_section(rows, baseline_data, baseline_label)

    html = f"""<!DOCTYPE html>
<html lang="zh-cn">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{CSS}</style>
</head>
<body>
{body_top}
<script>{JS}</script>
</body>
</html>
"""
    out_path.write_text(html, encoding="utf-8")
    print(f"Wrote {out_path}  ({len(html):,} bytes; {n_done} runs)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model", default="claude-sonnet-4-6",
                    help="Model used in this run; used to pick pricing in §1.2")
    ap.add_argument("--manifest-path", type=Path, default=DEFAULT_MANIFEST,
                    help="Subset MANIFEST.json path (default: questions_subset42).")
    ap.add_argument("--title", type=str, default=None,
                    help="Override the HTML <title> and <h1>.")
    ap.add_argument("--subtitle", type=str, default=None,
                    help="Override the leading meta paragraph (HTML allowed).")
    ap.add_argument("--baseline-html", type=Path, default=None,
                    help="Path to baseline HTML report (e.g. baseline-2026-05-10.html). "
                         "If provided, adds a §4 vs-baseline comparison section.")
    ap.add_argument("--baseline-label", type=str, default="DeepSeek-v4-pro (baseline)",
                    help="Display label for the baseline column.")
    args = ap.parse_args()

    pricing = json.loads(PRICING_PATH.read_text())
    manifest = json.loads(args.manifest_path.read_text())

    expected_t1 = sum(1 for t in manifest["selected_tasks"] if t["group"] == "T1")
    expected_t2 = sum(1 for t in manifest["selected_tasks"] if t["group"] == "T2")

    rows = []
    for p in sorted(args.runs_dir.iterdir()):
        if not p.is_dir():
            continue
        # Skip directories that don't look like run dirs
        if "_M" not in p.name:
            continue
        r = collect_run(p, pricing)
        if r is None:
            continue
        rows.append(r)

    baseline_data = {}
    if args.baseline_html:
        want_ids = {t["task_id"] for t in manifest["selected_tasks"]}
        baseline_data = parse_baseline_html(args.baseline_html, want_ids)
        print(f"baseline: matched {len(baseline_data)}/{len(want_ids)} tasks "
              f"from {args.baseline_html}")

    build_report(rows, pricing, args.model, manifest, args.out,
                 expected_t1, expected_t2,
                 title=args.title, subtitle_html=args.subtitle,
                 baseline_data=baseline_data,
                 baseline_label=args.baseline_label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
