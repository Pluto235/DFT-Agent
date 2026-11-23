// Curve compare viz for result page. Used for curve_f1-typed fields (T2 dos_curve etc.).
//
// Two components:
//   - CurveCompareRow: replaces a row in the "field-level scoring" diff-table grid.
//     Renders 5 grid cells (field/pred/gt/score/visual) with a click-to-expand
//     button in the pred cell. Expanded body spans the full grid width via
//     gridColumn: '1 / -1' and renders <CurveCompare>.
//   - CurveCompare: SVG line chart (agent solid blue + GT dashed green) +
//     axes / EF 0 line / peak markers / legend / numerical summary
//     (F1 / precision / recall / A_pred / A_gt / A_intersect).
//
// 2026-05-17：评分体系从 RMSE+best-shift 切到 F1=2·A∩/(A_pred+A_gt)；不平移、
// 直接体现能量错位。删除 pred_aligned / normalized_rmse / best_shift_ev 字段。
//
// SVG-only (no chart.js / Plotly dep — keeps fresh repo babel-standalone constraint).

const { useState } = React;

function CurveCompareRow({ runId, fieldName, score, unit, tolerance, lang }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  const fetchOnce = async () => {
    if (data || loading) return;
    setLoading(true);
    setError(null);
    try {
      // 走 data.js 的 apiFetch — 自动从 sessionStorage AUTH_TOKEN 注入 Bearer
      // header（raw fetch 在多租户模式下会被 auth_middleware 401 拒）
      const path = `/api/runs/${encodeURIComponent(runId)}/curve_field/${encodeURIComponent(fieldName)}`;
      const out = await apiFetch(path);
      setData(out);
    } catch (e) {
      setError(e?.message || `HTTP ${e?.status || '?'}`);
    } finally {
      setLoading(false);
    }
  };

  const handleToggle = async () => {
    const willOpen = !open;
    setOpen(willOpen);
    if (willOpen) await fetchOnce();
  };

  const sCls = (typeof scoreFieldClass === 'function') ? scoreFieldClass(score) : '';

  return <>
    <div className="field">{fieldName}</div>
    <div className="pred">
      <button className="curve-expand-btn" onClick={handleToggle}>
        <span style={{ display: 'inline-block', width: 12 }}>{open ? '▾' : '▸'}</span>
        <span>curve</span>
      </button>
    </div>
    <div className="gt">
      <span style={{ color: 'var(--ink-3)' }}>ref data</span>
      {unit ? <span style={{ color: 'var(--ink-3)', marginLeft: 4, fontSize: 11 }}>{unit}</span> : null}
    </div>
    <div className="score-cell">{score.toFixed(2)}</div>
    <div className="visual"><span className="bar"><div className={sCls} style={{ width: `${score*100}%` }} /></span></div>
    {open && <div className="curve-compare-body" style={{ gridColumn: '1 / -1' }}>
      {loading && <div className="curve-state">loading curve data…</div>}
      {error && <div className="curve-state curve-error">⚠ {error}</div>}
      {data && <CurveCompare data={data} unit={unit} tolerance={tolerance} />}
    </div>}
  </>;
}

function CurveCompare({ data, unit, tolerance }) {
  const { pred, gt, gt_full, scoring_window, summary, score, precision, recall, A_pred, A_gt, A_intersect } = data;
  // 显示用 gt_full（如果有，含全 raw 范围），评分仍用 gt（[-5,+5] 窗口）。
  // scoring_window 给可视化高亮用。
  const gtDisplay = gt_full || gt;

  // ---- compute "auto" bounds from full data union (used as reset target) ----
  let autoMin = Infinity, autoMax = -Infinity, dMaxRaw = -Infinity;
  for (const e of pred.energy_ev)       { if (e < autoMin) autoMin = e; if (e > autoMax) autoMax = e; }
  for (const e of gtDisplay.energy_ev)  { if (e < autoMin) autoMin = e; if (e > autoMax) autoMax = e; }
  for (const d of pred.dos)      { if (d > dMaxRaw) dMaxRaw = d; }
  for (const d of gtDisplay.dos) { if (d > dMaxRaw) dMaxRaw = d; }
  if (dMaxRaw <= 0) dMaxRaw = 1;

  // ---- zoom state: viewport [vMin, vMax] over x-axis; reset → auto bounds ----
  const [viewport, setViewport] = useState(null);  // null = auto
  const eMin = viewport ? viewport[0] : autoMin;
  const eMax = viewport ? viewport[1] : autoMax;

  // dMax: recompute based on what's visible in current viewport (so peak detail isn't lost)
  let dMax = 0;
  for (let i = 0; i < pred.energy_ev.length; i++) {
    const e = pred.energy_ev[i];
    if (e >= eMin && e <= eMax && pred.dos[i] > dMax) dMax = pred.dos[i];
  }
  for (let i = 0; i < gtDisplay.energy_ev.length; i++) {
    const e = gtDisplay.energy_ev[i];
    if (e >= eMin && e <= eMax && gtDisplay.dos[i] > dMax) dMax = gtDisplay.dos[i];
  }
  if (dMax <= 0) dMax = dMaxRaw;
  dMax = dMax * 1.10;
  const dMin = 0;

  const zoomFactor = 0.7;  // each zoom-in shrinks viewport by this factor
  const handleZoomIn = () => {
    const center = (eMin + eMax) / 2;
    const halfRange = (eMax - eMin) / 2 * zoomFactor;
    setViewport([center - halfRange, center + halfRange]);
  };
  const handleZoomOut = () => {
    const center = (eMin + eMax) / 2;
    const halfRange = (eMax - eMin) / 2 / zoomFactor;
    setViewport([
      Math.max(autoMin, center - halfRange),
      Math.min(autoMax, center + halfRange),
    ]);
  };
  const handleZoomScoring = () => {
    if (scoring_window) {
      const pad = (scoring_window.e_max - scoring_window.e_min) * 0.1;
      setViewport([scoring_window.e_min - pad, scoring_window.e_max + pad]);
    }
  };
  const handleReset = () => setViewport(null);

  const width = 640, height = 320;
  const m = { top: 16, right: 18, bottom: 38, left: 56 };
  const innerW = width - m.left - m.right;
  const innerH = height - m.top - m.bottom;

  const x = e => m.left + ((e - eMin) / (eMax - eMin)) * innerW;
  const y = d => m.top + innerH - ((d - dMin) / (dMax - dMin)) * innerH;

  // Build polyline points as a single string (avoid 2 * 1000 nodes for perf)
  const buildPoints = (es, ds) => {
    const out = new Array(es.length);
    for (let i = 0; i < es.length; i++) out[i] = `${x(es[i]).toFixed(1)},${y(ds[i]).toFixed(1)}`;
    return out.join(' ');
  };

  // X ticks: every 1 or 2 eV depending on range
  const xRange = eMax - eMin;
  const xStep = xRange > 12 ? 2 : 1;
  const xTicks = [];
  for (let v = Math.ceil(eMin / xStep) * xStep; v <= eMax; v += xStep) xTicks.push(v);

  // Y ticks: 5 evenly spaced
  const yTicks = [];
  for (let i = 0; i <= 5; i++) yTicks.push(dMin + (dMax - dMin) * i / 5);

  const peakShiftStr = summary.peak_e_shift_ev > 0
    ? `+${summary.peak_e_shift_ev.toFixed(3)}`
    : summary.peak_e_shift_ev.toFixed(3);

  // Numeric formatters (guard against undefined for backwards-compat)
  const fmt = (v, n=4) => (typeof v === 'number' && Number.isFinite(v)) ? v.toFixed(n) : '—';

  return <div className="curve-compare">
    {/* chart column: toolbar + SVG packed into single grid cell */}
    <div className="curve-chart-col">
      {/* zoom toolbar — themed via .curve-zoom-* classes in styles.css */}
      <div className="curve-zoom-toolbar">
        <button className="curve-zoom-btn" onClick={handleZoomIn} title="zoom in">+</button>
        <button className="curve-zoom-btn" onClick={handleZoomOut} title="zoom out">−</button>
        <button className="curve-zoom-btn curve-zoom-btn-text" onClick={handleReset} title="reset to full range">reset</button>
        {scoring_window && <button className="curve-zoom-btn curve-zoom-btn-text" onClick={handleZoomScoring}
          title="zoom to F1 scoring window [-5, +5] eV">fit scoring window</button>}
        <span className="curve-zoom-range">x ∈ [{eMin.toFixed(2)}, {eMax.toFixed(2)}] eV</span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} className="curve-svg" preserveAspectRatio="xMidYMid meet">
      <defs>
        <clipPath id={`chart-clip-${data.run_id || 'curve'}`}>
          <rect x={m.left} y={m.top} width={innerW} height={innerH} />
        </clipPath>
      </defs>
      {/* axes */}
      <line x1={m.left} y1={m.top + innerH} x2={m.left + innerW} y2={m.top + innerH} stroke="var(--ink-4, #999)" strokeWidth="1" />
      <line x1={m.left} y1={m.top} x2={m.left} y2={m.top + innerH} stroke="var(--ink-4, #999)" strokeWidth="1" />
      {/* X ticks + grid */}
      {xTicks.map(v => <g key={`x${v}`}>
        <line x1={x(v)} y1={m.top} x2={x(v)} y2={m.top + innerH} stroke="var(--ink-4, #ddd)" strokeOpacity="0.25" />
        <line x1={x(v)} y1={m.top + innerH} x2={x(v)} y2={m.top + innerH + 4} stroke="var(--ink-4, #999)" />
        <text x={x(v)} y={m.top + innerH + 16} textAnchor="middle" fontSize="10" fill="var(--ink-3, #666)">{v}</text>
      </g>)}
      {/* Y ticks + grid */}
      {yTicks.map((v, i) => <g key={`y${i}`}>
        <line x1={m.left} y1={y(v)} x2={m.left + innerW} y2={y(v)} stroke="var(--ink-4, #ddd)" strokeOpacity="0.25" />
        <line x1={m.left - 4} y1={y(v)} x2={m.left} y2={y(v)} stroke="var(--ink-4, #999)" />
        <text x={m.left - 8} y={y(v) + 3} textAnchor="end" fontSize="10" fill="var(--ink-3, #666)">{v.toFixed(2)}</text>
      </g>)}
      {/* All zoomable content clipped to chart area so zoom doesn't bleed outside axes */}
      <g clipPath={`url(#chart-clip-${data.run_id || 'curve'})`}>
        {/* scoring window highlight (semi-transparent yellow band marking F1 integration range) */}
        {scoring_window && <rect
          x={x(scoring_window.e_min)}
          y={m.top}
          width={x(scoring_window.e_max) - x(scoring_window.e_min)}
          height={innerH}
          fill="rgba(245, 166, 35, 0.10)"
          stroke="rgba(245, 166, 35, 0.5)"
          strokeWidth="1"
          strokeDasharray="2,2" />}
        {/* EF 0 line (VBM for insulator, EF for metal) */}
        <line x1={x(0)} y1={m.top} x2={x(0)} y2={m.top + innerH}
          stroke="var(--ink-3, #888)" strokeDasharray="3,3" strokeOpacity="0.6" />
        {/* GT polyline (under) — uses gtDisplay (gt_full if available, else gt) */}
        <polyline points={buildPoints(gtDisplay.energy_ev, gtDisplay.dos)}
          fill="none" stroke="var(--ok, #1c8c3c)" strokeWidth="1.5" strokeDasharray="4,2" />
        {/* pred polyline (top) */}
        <polyline points={buildPoints(pred.energy_ev, pred.dos)}
          fill="none" stroke="var(--brand, #2c66c0)" strokeWidth="1.5" />
        {/* peak markers */}
        <circle cx={x(summary.pred_peak.e_ev)} cy={y(summary.pred_peak.dos)} r="3.5"
          fill="var(--brand, #2c66c0)" stroke="white" strokeWidth="1" />
        <circle cx={x(summary.gt_peak.e_ev)} cy={y(summary.gt_peak.dos)} r="3.5"
          fill="var(--ok, #1c8c3c)" stroke="white" strokeWidth="1" />
      </g>
      {/* axis labels */}
      <text x={m.left + innerW / 2} y={height - 6} textAnchor="middle" fontSize="11" fill="var(--ink-2, #444)">E − E_F (eV)</text>
      <text x={14} y={m.top + innerH / 2} textAnchor="middle" fontSize="11" fill="var(--ink-2, #444)"
        transform={`rotate(-90 14 ${m.top + innerH / 2})`}>{unit || 'DOS'}</text>
      {/* legend */}
      <g transform={`translate(${m.left + 12}, ${m.top + 12})`}>
        <line x1="0" y1="0" x2="22" y2="0" stroke="var(--brand, #2c66c0)" strokeWidth="1.5" />
        <text x="26" y="3" fontSize="10" fill="var(--ink-2, #444)">agent (pred)</text>
        <line x1="130" y1="0" x2="152" y2="0" stroke="var(--ok, #1c8c3c)" strokeWidth="1.5" strokeDasharray="4,2" />
        <text x="156" y="3" fontSize="10" fill="var(--ink-2, #444)">GT (ref)</text>
        {scoring_window && <>
          <rect x="260" y="-5" width="22" height="10" fill="rgba(245, 166, 35, 0.10)" stroke="rgba(245, 166, 35, 0.5)" strokeDasharray="2,2" />
          <text x="286" y="3" fontSize="10" fill="var(--ink-2, #444)">F1 scoring window</text>
        </>}
      </g>
    </svg>
    </div>
    <div className="curve-summary">
      <div><span className="curve-k">points</span><span className="curve-v">{summary.n_pred} pred · {summary.n_gt} gt</span></div>
      <div>
        <span className="curve-k">F1 score</span>
        <span className="curve-v">{fmt(score)} <span className="curve-tol">(P {fmt(precision, 3)} · R {fmt(recall, 3)})</span></span>
      </div>
      <div>
        <span className="curve-k">area</span>
        <span className="curve-v">A∩ {fmt(A_intersect, 3)} / A_pred {fmt(A_pred, 3)} / A_gt {fmt(A_gt, 3)}</span>
      </div>
      <div><span className="curve-k">peak shift</span><span className="curve-v">{peakShiftStr} eV</span></div>
      <div><span className="curve-k">peak dos ratio</span><span className="curve-v">{summary.peak_dos_ratio.toFixed(3)}</span></div>
    </div>
  </div>;
}

Object.assign(window, { CurveCompareRow, CurveCompare });
