// Result views (bench + custom) and History tab

// FailureAnalysis：failed run 的 AI 故障分析按钮 + 内容。lazy 触发，cache 到 run_dir。
// mount 时 GET 拿 cache；按钮点击 POST 触发分析；force=true 覆盖重算。
function FailureAnalysis({ runId, lang }) {
  const [content, setContent] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [hasCache, setHasCache] = useState(false);

  useEffect(() => {
    let cancelled = false;
    window.__api.getFailureAnalysis(runId)
      .then(d => {
        if (cancelled) return;
        if (d && d.cached && d.content) {
          setContent(d.content);
          setHasCache(true);
        }
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [runId]);

  const trigger = async (force = false) => {
    setLoading(true);
    setError(null);
    try {
      const d = await window.__api.analyzeFailure(runId, force);
      setContent(d.content);
      setHasCache(true);
    } catch (e) {
      setError(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  };

  return <div className="sec">
    <h3 className="sec-title">
      {t(lang, 'result.failure_analysis')}
      {hasCache && <span className="count">· cached · claude-opus-4-7</span>}
    </h3>
    {!content && !loading && <div className="failure-analysis-empty">
      <button className="btn primary sm" onClick={() => trigger(false)} disabled={loading}>
        {t(lang, 'result.failure_analysis_btn')}
      </button>
      <span className="hint">{t(lang, 'result.failure_analysis_hint')}</span>
    </div>}
    {loading && <div className="failure-analysis-loading">
      <span className="spinner" />
      {t(lang, 'result.failure_analysis_loading')}
    </div>}
    {content && <>
      <div className="failure-analysis-content">{content}</div>
      <div style={{ marginTop: 8, display: 'flex', gap: 8, alignItems: 'center' }}>
        <button className="btn ghost xs" onClick={() => trigger(true)} disabled={loading}>
          {t(lang, 'result.failure_analysis_redo')}
        </button>
        <span className="hint" style={{ fontSize: 11, color: 'var(--ink-4)' }}>
          {t(lang, 'result.failure_analysis_disclaimer')}
        </span>
      </div>
    </>}
    {error && <div className="failure-analysis-error">⚠ {error}</div>}
  </div>;
}

// 下载 run 的 case bundle（tar.gz：workdir 关键 + GT + report + auto README）。
// 公开 endpoint，与 curve_field 同政策；点击触发浏览器下载，无需进 detail 页。
function DownloadBundleBtn({ runId, lang }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const onClick = async () => {
    if (loading) return;
    setLoading(true);
    setError(null);
    try {
      await window.__api.downloadCaseBundle(runId);
    } catch (e) {
      setError(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  };
  return <button
    className="btn ghost sm"
    onClick={onClick}
    disabled={loading}
    title={error ? `failed: ${error}` : t(lang, 'result.download_bundle_tip')}
  >
    {loading ? '⏳ ' : '⬇ '}{t(lang, 'result.download_bundle')}
  </button>;
}

function ResultBench({ run, task, report, lang, onRerun, agentName }) {
  const sc = report.total_score;
  const isFailed = sc == null;
  const status = isFailed ? 'failed' : scoreClass(sc);
  const fields = Object.keys(report.per_field_scores || {});
  const auditEntries = Object.entries(report.audit_flags || {});
  const auditPass = auditEntries.filter(([,v]) => v === true).length;
  return <div className="doc">
    <div className="meta-row" style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
      <div className="meta-row">
        <span className="badge bench">bench</span>
        <span className="tt-chip">{task?.milestone}·{task?.task_type}·{task?.task_subtype}</span>
        {isFailed && <span className="tt-chip" style={{ color: 'var(--err)', borderColor: 'var(--err)' }}>{run.status || 'failed'}</span>}
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        <DownloadBundleBtn runId={run.run_id} lang={lang} />
        <button className="btn primary sm" onClick={() => onRerun(run)}>{t(lang, 'result.rerun')}</button>
      </div>
    </div>
    <h1 className="preview-title" style={{ marginBottom: 8 }}>{task?.material_formula}</h1>
    <div className="sub mono" style={{ color: 'var(--ink-3)', fontSize: 11, marginBottom: 8 }}>{run.run_id}</div>

    <div className="score-hero">
      <div>
        <div className="score-big tnum">{isFailed ? '—' : sc.toFixed(3)}<span className="denom">/ 1.000</span></div>
        <div className={`score-status ${status}`}>{isFailed ? 'no score · run failed' : t(lang, `result.status.${status}`)}</div>
      </div>
      <div>
        <div className="score-meta">
          <div className="k">{t(lang, 'result.meta.task')}</div><div className="v">{report.task_id}</div>
          <div className="k">{t(lang, 'result.meta.model')}</div><div className="v">{report.model}</div>
          {agentName && <><div className="k">{t(lang, 'result.meta.agent')}</div><div className="v">{agentName}</div></>}
          <div className="k">{t(lang, 'result.meta.wall')}</div><div className="v tnum">{fmtSec(report.wall_time_sec)}</div>
          <div className="k">{t(lang, 'result.meta.turns')}</div><div className="v tnum">{run.current_turn}</div>
          <div className="k">{t(lang, 'result.meta.qe_calls')}</div>
          <div className="v tnum">{run.qe_call_count != null ? run.qe_call_count : '—'}</div>
          <div className="k">{t(lang, 'result.meta.qe_wall')}</div>
          <div className="v tnum" title="max(self-reported WALL across *.out summary lines, bash-cmd wall sum) + killed-cmd estimate. self-reported is exact compute time; bash-cmd captures all invocations including agent re-runs that overwrote earlier .out.">
            {run.qe_wall_sec != null ? fmtSec(run.qe_wall_sec) : '—'}
            {(() => {
              // 在 self vs bash 明显不一致时（diff > 30s 且比例 > 1.2x）显示括号补充
              // 提示：让用户察觉 agent 重跑覆盖 .out / 后台启动 等导致单一指标失真的情况。
              const s = run.qe_wall_self_reported_sec;
              const b = run.qe_wall_bash_cmd_sec;
              if (s == null || b == null || s <= 0 || b <= 0) return null;
              const diff = Math.abs(s - b);
              const ratio = Math.max(s, b) / Math.max(Math.min(s, b), 1e-9);
              if (diff < 30 || ratio < 1.2) return null;
              return <span style={{ marginLeft: 6, color: 'var(--ink-3)', fontSize: 11 }}
                title={`self-reported (sum across .out files): ${fmtSec(s)}\nbash-cmd wall (sum of QE bash invocations): ${fmtSec(b)}\n两路差距 ≥ 30s 且比例 ≥ 1.2× → 提示 agent 可能重跑覆盖 .out（bash 大），或后台启动 (.out 大)`}>
                (.out: {fmtSec(s)} / bash: {fmtSec(b)})
              </span>;
            })()}
          </div>
          <div className="k">{t(lang, 'result.meta.audit')}</div><div className="v tnum">{auditPass}/{auditEntries.length}</div>
        </div>
        {!isFailed && fields.length > 0 && <div className="field-bar-chart">
          {fields.map(f => {
            const s = report.per_field_scores[f];
            const cls = scoreFieldClass(s);
            // 高度按分数；s≈0 时设最低 6% 仍可见（保留 hover 区 + 显示分数差异）
            const h = Math.max(s * 100, 6);
            return <div key={f} className={`bar ${cls}`} style={{ height: `${h}%` }} title={`${f}: ${s.toFixed(2)}`} />;
          })}
        </div>}
      </div>
    </div>

    {report.harness_interrupted && <div className="harness-interrupted-banner">
      ⚠ {t(lang, 'result.harness_interrupted_banner')}
    </div>}

    {isFailed && run.error && <div className="sec">
      <h3 className="sec-title" style={{ color: 'var(--err)' }}>error</h3>
      <div className="prompt" style={{ maxHeight: 'none', borderColor: 'var(--err)', color: 'var(--err)' }}>{run.error}</div>
    </div>}

    {report.harness_interrupted && Object.keys(report.task_outputs || {}).length > 0 && <div className="sec">
      <h3 className="sec-title">{t(lang, 'result.task_outputs')}</h3>
      <div className="task-outputs-grid">
        {Object.entries(report.task_outputs).map(([k, v]) =>
          <React.Fragment key={k}>
            <div className="k mono">{k}</div>
            <div className="v mono">{String(v)}</div>
          </React.Fragment>)}
      </div>
    </div>}

    {isFailed && <FailureAnalysis runId={run.run_id} lang={lang} />}

    {!isFailed && fields.length > 0 && <div className="sec">
      <h3 className="sec-title">field-level scoring <span className="count">· {fields.length} fields</span></h3>
      <div className="diff-table with-gt">
        <div className="h">{t(lang, 'result.diff.field')}</div>
        <div className="h">{t(lang, 'result.diff.pred')}</div>
        <div className="h">{t(lang, 'result.diff.gt')}</div>
        <div className="h" style={{ textAlign: 'right' }}>{t(lang, 'result.diff.score')}</div>
        <div className="h"></div>
        {fields.map(f => {
          const s = report.per_field_scores[f];
          const v = report.extracted_fields?.[f];
          // GT 结构：{value, unit, tolerance, rule}（数值字段）或 {value: 'direct'}（枚举字段）
          // 缺失（老 run / API 没返）显 '—'
          const gtEntry = report.ground_truth?.[f];
          const gtVal = gtEntry?.value;
          const gtUnit = gtEntry?.unit;
          const gtTol = gtEntry?.tolerance;
          // curve_f1 字段（T2 dos_curve 等）走 CurveCompareRow expand-to-viz 渲染，
          // 替代 fmtPred 的 <dict, N keys> 简化（信息全丢）。
          // 2026-05-17：rule 从 curve_l2 切到 curve_f1（F1/Dice area-overlap）
          if (gtEntry?.rule === 'curve_f1') {
            return <CurveCompareRow key={f} runId={run.run_id} fieldName={f}
              score={s} unit={gtUnit} tolerance={gtTol ?? 0.1} lang={lang} />;
          }
          return <React.Fragment key={f}>
            <div className="field">{f}</div>
            <div className="pred">{fmtPred(v)}</div>
            <div className="gt"
              title={gtTol != null ? `tolerance: ${gtTol}${gtEntry?.rule ? ` (${gtEntry.rule})` : ''}` : undefined}>
              {gtVal == null
                ? <span style={{ color: 'var(--ink-4)' }}>—</span>
                : <>{fmtPred(gtVal)}{gtUnit ? <span style={{ color: 'var(--ink-3)', marginLeft: 4, fontSize: 11 }}>{gtUnit}</span> : null}</>}
            </div>
            <div className="score-cell">{s.toFixed(2)}</div>
            <div className="visual"><span className="bar"><div className={scoreFieldClass(s)} style={{ width: `${s*100}%` }} /></span></div>
          </React.Fragment>;
        })}
      </div>
    </div>}

    {auditEntries.length > 0 && <div className="sec">
      <h3 className="sec-title"><Tip tipKey="tip.audit" lang={lang}>{t(lang, 'result.audit')}</Tip> <span className="count">· {auditPass}/{auditEntries.length} pass</span></h3>
      <div className="audit-grid">
        {auditEntries.map(([k, v]) =>
          <div key={k} className="audit-row">
            <span className={`audit-dot ${v ? 'pass' : 'fail'}`} />
            <div>
              <div className="name">{k}</div>
              <div className="reason">{v ? 'ok' : 'flag triggered'}</div>
            </div>
          </div>)}
      </div>
    </div>}

    <div className="sec">
      <h3 className="sec-title">{t(lang, 'result.summary')}</h3>
      <div className="summary-cells">
        <div className="cell"><div className="v tnum">{fmtTokens(report.input_tokens_total)}</div><div className="k">{t(lang, 'result.summary.input_tokens')}</div></div>
        <div className="cell"><div className="v tnum">{fmtTokens(report.output_tokens_total)}</div><div className="k">{t(lang, 'result.summary.output_tokens')}</div></div>
        <div className="cell"><div className="v tnum">{fmtUsd(report.cost_usd)}</div><div className="k">{t(lang, 'result.summary.cost')}</div></div>
        <div className="cell"><div className="v tnum">{fmtSec(report.wall_time_sec)}</div><div className="k">{t(lang, 'result.summary.wall')}</div></div>
      </div>
    </div>

    {report.notes && <div className="sec">
      <h3 className="sec-title">solver notes</h3>
      <div className="prompt" style={{ maxHeight: 'none' }}>{report.notes}</div>
    </div>}
  </div>;
}

function ResultCustom({ run, result, transcript, lang, agentName }) {
  return <div className="doc">
    <div className="meta-row" style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
      <div className="meta-row">
        <span className="badge custom">custom</span>
        <span className="badge warn">{t(lang, 'result.custom.badge')}</span>
      </div>
      <DownloadBundleBtn runId={run.run_id} lang={lang} />
    </div>
    <h1 className="preview-title" style={{ marginBottom: 8, fontSize: 22 }}>
      {result.custom_task_spec?.material?.formula_text || result.custom_task_spec?.prompt?.slice(0, 60) || run.run_id}
    </h1>
    <div className="sub mono" style={{ color: 'var(--ink-3)', fontSize: 11, marginBottom: 24 }}>
      <span>{run.run_id}</span> · <span>{run.model}</span>
      {agentName && <> · <span>{agentName}</span></>}
      {' · '}<span className="tnum">{fmtSec(run.wall_time_sec)}</span>
    </div>

    <div className="sec">
      <h3 className="sec-title">{t(lang, 'result.custom.spec')}</h3>
      <div className="prompt">{result.custom_task_spec?.prompt}</div>
    </div>

    <div className="sec">
      <h3 className="sec-title">{t(lang, 'result.custom.final_answer')}</h3>
      {result.final_answer ? <div className="final-answer">
        <div className="fa-kv">
          {Object.entries(result.final_answer).map(([k, v]) =>
            <React.Fragment key={k}>
              <div className="k">{k}</div>
              <div className="v">{fmtPred(v)}</div>
            </React.Fragment>)}
        </div>
      </div> : <div className="error-block">{t(lang, 'result.custom.no_submit')}</div>}
    </div>

    {(run.status === 'failed' || run.status === 'cancelled' || !result.final_answer) && (
      <FailureAnalysis runId={run.run_id} lang={lang} />
    )}

    <div className="sec">
      <h3 className="sec-title">{t(lang, 'result.summary')}</h3>
      <div className="summary-cells">
        <div className="cell"><div className="v tnum">{fmtTokens(result.input_tokens_total)}</div><div className="k">{t(lang, 'result.summary.input_tokens')}</div></div>
        <div className="cell"><div className="v tnum">{fmtTokens(result.output_tokens_total)}</div><div className="k">{t(lang, 'result.summary.output_tokens')}</div></div>
        <div className="cell"><div className="v tnum">{fmtUsd(result.cost_usd)}</div><div className="k">{t(lang, 'result.summary.cost')}</div></div>
        <div className="cell"><div className="v tnum">{fmtSec(run.wall_time_sec)}</div><div className="k">{t(lang, 'result.meta.wall')}</div></div>
      </div>
    </div>

    <div className="sec">
      <h3 className="sec-title">{t(lang, 'result.custom.transcript')} <span className="count">· {transcript.length} turns</span></h3>
      <div className="transcript">
        {transcript.map((turn, i) => <TranscriptTurn key={i} turn={turn} lang={lang} />)}
      </div>
    </div>
  </div>;
}

// 表头点击排序的列。未命中显示淡的 ↕ 提示可点；命中时高亮 ▲/▼。
function SortableHeader({ col, label, align, sortCol, sortDir, onClick, children }) {
  const isActive = sortCol === col;
  const arrow = isActive ? (sortDir === 'asc' ? '▲' : '▼') : '↕';
  const justify = align === 'right' ? 'flex-end' : 'flex-start';
  return <div className={`h sortable ${isActive ? 'active' : ''}`}
    style={{ justifyContent: justify }}
    onClick={() => onClick(col)}>
    <span>{label}</span>
    <span className="sort-arrow">{arrow}</span>
    {children}
  </div>;
}

// 统一的列头 dropdown filter — 跟 SortableHeader 同样大小，点击展开内联菜单。
// active=true 时整格 accent 背景，菜单内被选项打 ✓。点击外部关闭。
function FilterDropdownHeader({ label, value, options, onChange, children }) {
  const [open, setOpen] = useState(false);
  const isActive = value !== 'all';
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);
  return <div ref={ref} className={`h filterable ${isActive ? 'active' : ''} ${open ? 'open' : ''}`}
    onClick={() => setOpen(!open)}>
    <span>{label}</span>
    <span className="filter-arrow">▾</span>
    {children}
    {open && <div className="filter-menu" onClick={e => e.stopPropagation()}>
      {options.map(opt =>
        <div key={opt} className={`filter-menu-item ${value === opt ? 'selected' : ''}`}
          onClick={() => { onChange(opt); setOpen(false); }}>
          <span className="check">{value === opt ? '✓' : ''}</span>
          <span>{opt}</span>
        </div>)}
    </div>}
  </div>;
}

// 末列设置入口：齿轮 + dropdown，承载列宽复位等表级动作。
// 复用 .filterable / .filter-menu 样式，菜单 anchor 从右对齐避免溢出表外。
function SettingsHeader({ onResetCols }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);
  return <div ref={ref} className={`h filterable settings-header ${open ? 'open' : ''}`}
    title="表设置"
    onClick={() => setOpen(!open)}>
    <span style={{ fontSize: 14, lineHeight: 1 }}>⚙</span>
    {open && <div className="filter-menu history-settings-menu" onClick={e => e.stopPropagation()}>
      <div className="filter-menu-item"
        onClick={() => { onResetCols(); setOpen(false); }}>
        <span>恢复默认列宽</span>
      </div>
    </div>}
  </div>;
}

// 列定义：显示顺序固定，宽度可拖。who 列默认 'flex'（占剩余空间），拖动后转为 px 值。
// 默认宽度按 248 runs 真实数据 p95 内容宽 + 24px cell padding + 8px buffer 得出
// （/tmp/measure_history_cols.py 的近似估算），再 round 到 5/10 的倍数；
// 同时确保 ≥ 表头 i18n label（含中文）+ 排序/筛选箭头宽度。
const _HISTORY_COLS = ['when', 'who', 'mode', 'model', 'agent', 'owner', 'prio', 'score', 'wall', 'qe', 'turn', 'cost', 'status', 'action'];
const _HISTORY_COL_DEFAULT_WIDTHS = {
  when: 90,        // '15m ago' / '#19/20'(queued)
  who: 'flex',     // formula + id 上下叠放；flex 占剩余宽度（minmax 200 floor）
  mode: 80,        // 'bench' / 'custom' badge
  model: 150,      // 'claude-opus-4-7' / 'deepseek-v4-pro' (mono)
  agent: 110,      // 'v1-stable' / future agent names
  owner: 80,       // 'localhost' / 'alice' / 'bob' (mono 11px)
  prio: 70,
  score: 70,
  wall: 70,
  qe: 60,
  turn: 60,
  cost: 70,
  status: 70,
  action: 70,
};
const _HISTORY_COL_MIN_WIDTH = 40;
// 默认列宽 schema 改了就 bump key — 让老 localStorage 失效，避免用户继续看到旧默认
const _HISTORY_COL_LS_KEY = 'history-col-widths-v8';

function _loadColWidths() {
  try {
    const saved = JSON.parse(window.localStorage.getItem(_HISTORY_COL_LS_KEY) || '{}');
    return { ..._HISTORY_COL_DEFAULT_WIDTHS, ...saved };
  } catch (_e) { return { ..._HISTORY_COL_DEFAULT_WIDTHS }; }
}

function _gridTemplate(colW, selectMode) {
  const tpl = _HISTORY_COLS.map(c => {
    const v = colW[c];
    // who 列 flex floor 200px：长 task_id（如 M2_T1_LiAlSi2O6_spodumene_vc_relax）
    // p95 ~ 217px；窗口窄到 1280 时也尽量不裁；过窄了仍走 ellipsis 收尾
    if (v === 'flex') return 'minmax(200px, 1fr)';
    return typeof v === 'number' ? `${v}px` : v;
  }).join(' ');
  return selectMode ? `28px ${tpl}` : tpl;
}

function History({ runs, tasks, setState, lang, maxParallel, statusFilter, setStatusFilter,
                   scope, setScope, mode, setMode, model, setModel, milestone, setMilestone,
                   taskType, setTaskType, subset, setSubset, searchText, setSearchText, sortCol, setSortCol,
                   sortDir, setSortDir,
                   onCancel, selectMode, selectedRunIds, onToggleSelectMode, onToggleRunSelect, onSelectAll, onDeleteSelected, onCancelActive, paused, availableModels = [], availableAgents = [], currentOwner = 'localhost' }) {
  // 0505：scheduler pause/resume + max-parallel stepper 已迁到 admin 面板（dev only）；
  // 这里只剩只读展示——paused banner（无 resume 按钮）+ paused 状态 chip + max parallel
  // 当前值。dev 与 normal 在本页面看到的 UI 完全一致（dev 走 admin tab 操作）。
  // 列宽 state + localStorage 持久化（用户拖完页面刷新仍生效）。
  const [colW, setColW] = useState(_loadColWidths);
  useEffect(() => {
    try { window.localStorage.setItem(_HISTORY_COL_LS_KEY, JSON.stringify(colW)); } catch (_e) {}
  }, [colW]);
  // 代表性子集清单（v1.1：fast 12 / standard 27 / full 142）— 一次拉取本地缓存
  // 与 leaderboard.jsx / catalogue.jsx 复用同 endpoint，用于 subset segmented filter。
  // API fail → []，chip 整组不渲染（与其它两 tab fallback 一致）。
  const [subsets, setSubsets] = useState([]);
  useEffect(() => {
    window.__api.getSubsets()
      .then(d => setSubsets(d.subsets || []))
      .catch(() => setSubsets([]));
  }, []);
  const subsetTaskIds = useMemo(() => {
    const m = {};
    for (const s of subsets) m[s.key] = new Set(s.task_ids || []);
    return m;
  }, [subsets]);
  const startResize = (col) => (e) => {
    e.preventDefault(); e.stopPropagation();
    const startX = e.clientX;
    // who 列从 flex 转 px：拿当前实际渲染宽（offsetWidth）作为起点
    const headerEl = e.currentTarget.parentElement;
    const startW = typeof colW[col] === 'number' ? colW[col]
      : (headerEl?.offsetWidth || 120);
    const onMove = (mv) => {
      const next = Math.max(_HISTORY_COL_MIN_WIDTH, startW + (mv.clientX - startX));
      setColW(prev => prev[col] === next ? prev : { ...prev, [col]: next });
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  };
  const resetColWidths = () => {
    setColW({ ..._HISTORY_COL_DEFAULT_WIDTHS });
    try { window.localStorage.removeItem(_HISTORY_COL_LS_KEY); } catch (_e) {}
  };
  const gridTpl = _gridTemplate(colW, selectMode);
  // resizer 元素挂在 .h 内右边缘；点击时触发 mousedown listener
  const resizer = (col) => <span className="col-resizer"
    onMouseDown={startResize(col)} onClick={(e) => e.stopPropagation()}
    title="拖动调整列宽 · 双击复位"
    onDoubleClick={(e) => { e.stopPropagation(); setColW(p => ({ ...p, [col]: _HISTORY_COL_DEFAULT_WIDTHS[col] })); }} />;
  // 0506：scope / mode / model / milestone / taskType / searchText / sortCol / sortDir
  // + statusFilter 全部从 props 接（提升到 app.jsx 顶层 + URL replaceState 同步），
  // 这样进 run 详情返回 / 浏览器刷新 / 分享 URL 时 filter 都能保留。
  // model filter 按具体 model id 过滤（'all' / 'claude-opus-4-7' / 'kimi-k2-turbo' / ...）；
  // milestone / taskType filter：bench task_id 是 `M{n}_T{m}_<material>_<task>` 格式，
  // custom 模式 task_id 不匹配此前缀，选具体 M/T 值时会被自动过滤掉（合理 UX）；
  // 列排序：sortCol ∈ {null, 'when', 'who', 'score', 'wall', 'qe', 'turn', 'cost'}，
  // sortDir ∈ {'asc', 'desc'}；null = 默认行为（created_at desc + queued/running 顶部）。
  const taskMap = useMemo(() => Object.fromEntries(tasks.map(t => [t.task_id, t])), [tasks]);
  // dropdown 列出 (runs 实际出现的 ∪ 平台注册的 availableModels) 的并集，
  // 这样：① 平台支持但未跑过的 model 也可见；② custom-input 没注册但跑过的也可见。
  const modelsAll = useMemo(() => {
    const set = new Set();
    runs.forEach(r => { if (r.model) set.add(r.model); });
    availableModels.forEach(m => { if (m && m.id) set.add(m.id); });
    return Array.from(set).sort();
  }, [runs, availableModels]);
  // milestone / task-type 可选值：从 tasks ∪ runs 的 task_id `M{n}_T{m}_` 前缀解析；
  // 没有数据时 seg 只渲染 'all' 一项时被隐藏（避免出现单 button 的无意义 seg）
  const { milestonesAvailable, taskTypesAvailable } = useMemo(() => {
    const ms = new Set(), ts = new Set();
    const scan = (tid) => {
      const m = (tid || '').match(/^M(\d+)_T(\d+)_/);
      if (m) { ms.add('M' + m[1]); ts.add('T' + m[2]); }
    };
    tasks.forEach(t => scan(t.task_id));
    runs.forEach(r => scan(r.task_id));
    return {
      milestonesAvailable: Array.from(ms).sort(),
      taskTypesAvailable: Array.from(ts).sort(),
    };
  }, [tasks, runs]);

  // 列点击排序的 toggle：同列再点 → asc/desc 切换；新列默认 desc（用户最常想看 top）
  const toggleSort = (col) => {
    if (sortCol === col) {
      setSortDir(sortDir === 'asc' ? 'desc' : 'asc');
    } else {
      setSortCol(col);
      setSortDir('desc');
    }
  };

  const filtered = useMemo(() => {
    let r = [...runs];
    // 先 baseline 排：created_at desc。下面 sortCol 命中时会被覆盖。
    r.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
    if (scope !== 'all') {
      const cutoff = Date.now() - (scope === 'today' ? 1 : 7) * 86400 * 1000;
      r = r.filter(x => x.status === 'queued' || x.status === 'running' || new Date(x.created_at).getTime() > cutoff);
    }
    if (mode !== 'all') r = r.filter(x => x.mode === mode);
    if (model !== 'all') r = r.filter(x => x.model === model);
    // milestone / taskType：从 task_id 前缀 `M{n}_T{m}_` 解析。custom 模式不匹配 → 选具体 M/T 时被排除
    if (milestone !== 'all') r = r.filter(x => (x.task_id || '').startsWith(milestone + '_'));
    if (taskType !== 'all') r = r.filter(x => /^M\d+_/.test(x.task_id || '') && (x.task_id || '').includes('_' + taskType + '_'));
    // subset filter：bench 代表性子集 (fast/standard/full)。task_ids 不在 subset 内的 run（含
    // custom mode）全部被排除，与 milestone/taskType 行为一致 — "在 task 维度上看 run"。
    if (subset !== 'all' && subsetTaskIds[subset]) r = r.filter(x => subsetTaskIds[subset].has(x.task_id));
    if (statusFilter && statusFilter !== 'all') r = r.filter(x => x.status === statusFilter);
    // 文字搜索：run_id / task_id / formula / custom_task_summary / model 任一命中即留
    const q = searchText.trim().toLowerCase();
    if (q) {
      r = r.filter(x => {
        const formula = taskMap[x.task_id]?.material_formula || '';
        return (
          (x.run_id || '').toLowerCase().includes(q) ||
          (x.task_id || '').toLowerCase().includes(q) ||
          (x.model || '').toLowerCase().includes(q) ||
          formula.toLowerCase().includes(q) ||
          (x.custom_task_summary || '').toLowerCase().includes(q)
        );
      });
    }
    // 列排序：所有行（含 queued/running）一起参与；缺值（譬如 queued 行的 wall/cost/score）
    // 用 -Infinity 占位，desc 时沉底、asc 时顶起 — 用户期望排序对全表生效。
    if (sortCol) {
      const sign = sortDir === 'asc' ? 1 : -1;
      const NEG_INF = -Infinity;
      const pick = (x) => {
        switch (sortCol) {
          case 'when': return x.created_at || '';
          // who 排序按 task_id 字典序：bench 行自然按 milestone→task→material→task 分组，
          // custom 行（task_id null/`custom-…`）会沉到一端。空值用空字符串占位。
          case 'who': return x.task_id || '';
          case 'score': return x.total_score == null ? NEG_INF : x.total_score;
          case 'wall': return x.wall_time_sec == null ? NEG_INF : x.wall_time_sec;
          case 'qe': return x.qe_call_count == null ? NEG_INF : x.qe_call_count;
          case 'turn': return x.current_turn == null ? NEG_INF : x.current_turn;
          case 'cost': return x.cost_usd == null ? NEG_INF : x.cost_usd;
          default: return 0;
        }
      };
      r.sort((a, b) => {
        const va = pick(a), vb = pick(b);
        if (va < vb) return -1 * sign;
        if (va > vb) return 1 * sign;
        return 0;
      });
    }
    return r;
  }, [runs, scope, mode, model, milestone, taskType, subset, subsetTaskIds, statusFilter, searchText, sortCol, sortDir, taskMap]);
  const stats = useMemo(() => {
    const done = filtered.filter(x => x.status === 'done' && x.total_score != null);
    const avg = done.length ? done.reduce((a, x) => a + x.total_score, 0) / done.length : null;
    const failed = filtered.filter(x => x.status === 'failed').length;
    const customN = filtered.filter(x => x.mode === 'custom').length;
    const queued = runs.filter(x => x.status === 'queued').length;
    const running = runs.filter(x => x.status === 'running').length;
    // 平均资源消耗：覆盖 done + failed + cancelled（终态），按非 null 取均值——
    // queued/running 没数据；done 单独取分母会让 cost / 轮数失真（fail 也烧 token）。
    const term = filtered.filter(x => ['done', 'failed', 'cancelled'].includes(x.status));
    const avgOf = (key) => {
      const xs = term.filter(x => x[key] != null);
      return xs.length ? xs.reduce((a, x) => a + x[key], 0) / xs.length : null;
    };
    const avgQe = avgOf('qe_call_count');
    const avgWall = avgOf('wall_time_sec');
    const avgCost = avgOf('cost_usd');
    const avgTurn = avgOf('current_turn');
    return { n: filtered.length, avg, avgQe, avgWall, avgCost, avgTurn,
             failed, customN, queued, running };
  }, [filtered, runs]);

  return <div className="center">
    {paused && <div className="scheduler-paused-banner">
      <span className="dot" />
      <span className="msg">
        Scheduler paused by dev — running runs will finish, queue is held
        {stats.queued > 0 && <> · <span className="mono">{stats.queued}</span> waiting</>}
        {stats.running > 0 && <> · <span className="mono">{stats.running}</span> still running</>}
      </span>
    </div>}
    <div className="history-toolbar">
      <div className="history-counts">
        <div className="hc-group">
          <div className="hc-stat">
            <span className="hc-lbl">runs</span>
            <span className="hc-val">{stats.n}</span>
          </div>
          <div className="hc-stat">
            <span className="hc-lbl">failed</span>
            <span className="hc-val">{stats.failed}</span>
          </div>
          <div className="hc-stat">
            <span className="hc-lbl">custom</span>
            <span className="hc-val">{stats.customN}</span>
          </div>
        </div>
        <div className="hc-sep" />
        <div className="hc-stat">
          <span className="hc-lbl">{t(lang, 'history.stats.avg')} {t(lang, 'history.col.score')}</span>
          <span className="hc-val tnum">{stats.avg != null ? stats.avg.toFixed(2) : '—'}</span>
        </div>
        <div className="hc-sep" />
        <div className="hc-stat">
          <span className="hc-lbl">{t(lang, 'history.stats.avg')} {t(lang, 'history.col.qe')}</span>
          <span className="hc-val tnum">{stats.avgQe != null ? stats.avgQe.toFixed(1) : '—'}</span>
        </div>
        <div className="hc-sep" />
        <div className="hc-stat">
          <span className="hc-lbl">{t(lang, 'history.stats.avg')} {t(lang, 'history.col.wall')}</span>
          <span className="hc-val tnum">{stats.avgWall != null ? fmtSec(stats.avgWall) : '—'}</span>
        </div>
        <div className="hc-sep" />
        <div className="hc-stat">
          <span className="hc-lbl">{t(lang, 'history.stats.avg')} {t(lang, 'history.col.cost')}</span>
          <span className="hc-val tnum">{stats.avgCost != null ? fmtUsd(stats.avgCost) : '—'}</span>
        </div>
        <div className="hc-sep" />
        <div className="hc-stat">
          <span className="hc-lbl">{t(lang, 'history.stats.avg')} {t(lang, 'history.col.turn')}</span>
          <span className="hc-val tnum">{stats.avgTurn != null ? stats.avgTurn.toFixed(1) : '—'}</span>
        </div>
      </div>
      <div className="queue-status-pills">
        <span className="qpill running"><span className="dot" />{stats.running} {t(lang, 'runs.running_count')}</span>
        <span className="qpill queued"><span className="dot" />{stats.queued} {t(lang, 'runs.queued_count')}</span>
        {/* 只读 max-parallel chip（修改入口已迁到 admin 面板，dev only） */}
        <span className="qpill"
          title={`每次同时最多 promote ${maxParallel} 个 run。修改请到 admin 面板。`}
          style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
          <span style={{ color: 'var(--ink-3)' }}>{t(lang, 'runs.max_parallel.label')}</span>
          <span className="mono" style={{ fontWeight: 600 }}>{maxParallel}</span>
        </span>
        {/* scheduler 状态 chip（dev 与 normal 看相同；'dev only' 提示告诉用户去 admin 面板操作） */}
        <span
          title={paused
            ? 'Scheduler 已被 dev 暂停；queued 任务等待恢复。'
            : 'Scheduler 正常运行；queued 任务按容量自动 promote。'}
          style={{ display: 'inline-flex', alignItems: 'center',
                   padding: '3px 8px', borderRadius: 99, fontSize: 11,
                   lineHeight: 1, fontFamily: 'var(--mono)',
                   background: paused ? 'rgba(220,38,38,0.12)' : 'var(--paper-3)',
                   color: paused ? 'var(--err)' : 'var(--ink-3)',
                   fontWeight: paused ? 600 : 400 }}>
          {paused ? '⏸ scheduler paused (dev only)' : '▶ scheduler running'}
        </span>
      </div>
    </div>
    <div className="history-toolbar">
      <div className="seg">
        {['today','week','all'].map(v => <button key={v} className={scope===v?'active':''} onClick={() => setScope(v)}>{t(lang, `history.scope.${v}`)}</button>)}
      </div>
      {/* mode / provider seg 已移到列头（ModeFilterHeader / ModelFilterHeader）— toolbar 不再重复展示。 */}
      {/* milestone / task-type 过滤：只在 tasks/runs 实际包含 M/T 前缀时渲染（避免空 seg） */}
      {milestonesAvailable.length > 0 && <div className="seg" title={t(lang, 'history.milestone.title')}>
        {['all', ...milestonesAvailable].map(v =>
          <button key={v} className={milestone===v?'active':''} onClick={() => setMilestone(v)}>
            {v === 'all' ? t(lang, 'history.milestone.all') : v}
          </button>)}
      </div>}
      {taskTypesAvailable.length > 0 && <div className="seg" title={t(lang, 'history.tasktype.title')}>
        {['all', ...taskTypesAvailable].map(v =>
          <button key={v} className={taskType===v?'active':''} onClick={() => setTaskType(v)}>
            {v === 'all' ? t(lang, 'history.tasktype.all') : v}
          </button>)}
      </div>}
      {/* subset filter: bench 代表性子集（fast 12 / standard 27 / full 142）。chip 显示
          的 count 是子集任务总数，与 leaderboard / catalogue chip 同语义。API fail 时
          subsets=[] → 整组不渲染。 */}
      {subsets.length > 0 && <div className="seg" title={lang === 'zh' ? '按代表性子集过滤' : 'filter by representative subset'}>
        <button className={subset === 'all' ? 'active' : ''} onClick={() => setSubset('all')}>
          {lang === 'zh' ? '全部' : 'all'}
        </button>
        {subsets.map(sub =>
          <button key={sub.key} className={subset === sub.key ? 'active' : ''}
            onClick={() => setSubset(sub.key)}
            title={sub.description}>
            {(lang === 'zh' ? sub.name : sub.name_en)} ({sub.count})
          </button>)}
      </div>}
      <div className="seg">
        {['all','queued','running','done','failed','cancelled'].map(v =>
          <button key={v} className={statusFilter===v?'active':''} onClick={() => setStatusFilter(v)}>
            {t(lang, `runs.status_filter.${v}`)}
          </button>)}
      </div>
      <div className="history-search">
        <input
          type="text"
          className="history-search-input"
          placeholder={t(lang, 'history.search.placeholder')}
          value={searchText}
          onChange={e => setSearchText(e.target.value)}
          spellCheck={false}
        />
        {searchText && <button className="history-search-clear" onClick={() => setSearchText('')} title="clear">×</button>}
      </div>
      {onToggleSelectMode && <div className="seg" style={{ marginLeft: 'auto' }}>
        {!selectMode && <button onClick={onToggleSelectMode}>Select</button>}
        {selectMode && <>
          <button onClick={() => onSelectAll(filtered.map(r => r.run_id))}>
            All ({filtered.length})
          </button>
          <button onClick={() => onSelectAll([])}>None</button>
          <button onClick={onToggleSelectMode}>Exit</button>
          {/* Cancel Active：不依赖选择，直接对所有 queued+running+evaluating 触发
              cancel；保留 row（status=cancelled），用户事后还能 Delete。 */}
          <button
            disabled={!stats.queued && !stats.running}
            onClick={() => {
              const n = stats.queued + stats.running;
              if (window.confirm(`Cancel ${n} active run(s)? Queued runs will be removed from queue; running runs will be SIGTERM'd (5s grace then SIGKILL). DB rows are kept as 'cancelled'.`)) {
                if (onCancelActive) onCancelActive();
              }
            }}>
            Cancel Active ({stats.queued + stats.running})
          </button>
          <button
            disabled={!selectedRunIds?.length}
            onClick={() => {
              if (window.confirm(`Delete ${selectedRunIds.length} run(s)? Active runs will be cancelled. run_dir on disk will be removed. This cannot be undone.`)) {
                onDeleteSelected();
              }
            }}
            style={{ color: selectedRunIds?.length ? 'var(--err)' : undefined }}>
            Delete ({selectedRunIds?.length || 0})
          </button>
        </>}
      </div>}
    </div>
    <div className="center-body">
      <div className="history-header" style={{ gridTemplateColumns: gridTpl }}>
        {selectMode && <div className="h" style={{ padding: '8px 4px' }}>
          <input type="checkbox"
            checked={filtered.length > 0 && selectedRunIds?.length === filtered.length}
            ref={el => { if (el) el.indeterminate = selectedRunIds?.length > 0 && selectedRunIds.length < filtered.length; }}
            onChange={(e) => onSelectAll(e.target.checked ? filtered.map(r => r.run_id) : [])}
          />
        </div>}
        <SortableHeader col="when" label={t(lang, 'history.col.when')} sortCol={sortCol} sortDir={sortDir} onClick={toggleSort}>{resizer('when')}</SortableHeader>
        <SortableHeader col="who" label={t(lang, 'history.col.who')} sortCol={sortCol} sortDir={sortDir} onClick={toggleSort}>{resizer('who')}</SortableHeader>
        <FilterDropdownHeader label="mode" value={mode}
          options={['all', 'bench', 'custom']} onChange={setMode}>{resizer('mode')}</FilterDropdownHeader>
        <FilterDropdownHeader label="model" value={model}
          options={['all', ...modelsAll]} onChange={setModel}>{resizer('model')}</FilterDropdownHeader>
        <div className="h">agent{resizer('agent')}</div>
        <div className="h">owner{resizer('owner')}</div>
        <div className="h" title="priority: 紧急 / 优先 / 普通">prio{resizer('prio')}</div>
        <SortableHeader col="score" label={t(lang, 'history.col.score')} sortCol={sortCol} sortDir={sortDir} onClick={toggleSort}>{resizer('score')}</SortableHeader>
        <SortableHeader col="wall" label={t(lang, 'history.col.wall')} sortCol={sortCol} sortDir={sortDir} onClick={toggleSort}>{resizer('wall')}</SortableHeader>
        <SortableHeader col="qe" label={t(lang, 'history.col.qe')} sortCol={sortCol} sortDir={sortDir} onClick={toggleSort}>{resizer('qe')}</SortableHeader>
        <SortableHeader col="turn" label={t(lang, 'history.col.turn')} sortCol={sortCol} sortDir={sortDir} onClick={toggleSort}>{resizer('turn')}</SortableHeader>
        <SortableHeader col="cost" label="cost" sortCol={sortCol} sortDir={sortDir} onClick={toggleSort}>{resizer('cost')}</SortableHeader>
        <div className="h">{t(lang, 'history.col.status')}{resizer('status')}</div>
        <SettingsHeader onResetCols={resetColWidths} />
      </div>
      <div className="history-list">
        {filtered.map(r => {
          const task = r.task_id ? taskMap[r.task_id] : null;
          const cancellable = r.status === 'queued' || r.status === 'running';
          const isSelected = selectMode && selectedRunIds?.includes(r.run_id);
          const pmeta = priorityMeta(r.priority || 'normal');
          const prioCls = (r.priority === 'high' || r.priority === 'urgent') ? `is-priority-${r.priority}` : '';
          return <div key={r.run_id} className={`history-row ${r.status === 'queued' ? 'is-queued' : ''} ${r.status === 'running' ? 'is-running' : ''} ${prioCls} ${isSelected ? 'is-selected' : ''}`}
            style={{ gridTemplateColumns: gridTpl }}
            onClick={() => {
              if (selectMode) { onToggleRunSelect(r.run_id); return; }
              // Unified routing: finished runs → run/result; in-flight or queued → run/live.
              // Bench result needs selectedTaskId so the result page can resolve the task.
              if (r.status === 'done' || r.status === 'failed' || r.status === 'cancelled') {
                setState(s => ({ ...s, tab: 'run', centerTab: 'result', activeRun: null,
                  viewedRunId: r.run_id,
                  selectedTaskId: r.task_id || s.selectedTaskId }));
              } else {
                // queued / running / evaluating → Live；queued 行 RunLive 仅显示
                // StatusBadge=queued，promote 后 viewedRunId fallback (app.jsx) 自动
                // 切到 real_id，transcript 跟着流出。
                setState(s => ({ ...s, tab: 'run', centerTab: 'live', activeRun: r, viewedRunId: r.run_id }));
              }
            }}>
            {selectMode && <div style={{ padding: '8px 4px' }}>
              <input type="checkbox" checked={isSelected || false} readOnly />
            </div>}
            <div className="when">
              {r.status === 'queued'
                ? <span className="qpos mono" title={t(lang, 'runs.queue_position', { n: r.queue_position, total: r.queue_total })}>
                    #{r.queue_position}/{r.queue_total}
                  </span>
                : fmtRel(r.created_at, lang)}
            </div>
            <div className="who">
              <span className="formula">{task ? task.material_formula : (r.custom_task_summary?.slice(0, 32) + '…' || 'custom')}</span>
              <span className="id">{r.task_id || 'custom'}</span>
            </div>
            <div><span className={`badge ${r.mode}`}>{r.mode}</span></div>
            <div className="model">{r.model}</div>
            <div className="agent-col" style={{ color: 'var(--ink-2)' }}
              title={(() => {
                const a = r.agent_id && availableAgents.find(x => x.id === r.agent_id);
                return a?.description || a?.name || r.agent_id || (availableAgents[0]?.name || '—');
              })()}>
              {(() => {
                const a = r.agent_id && availableAgents.find(x => x.id === r.agent_id);
                return a?.name || (availableAgents[0]?.name) || <span style={{ color: 'var(--ink-4)' }}>—</span>;
              })()}
            </div>
            <div className={`owner-col ${r.owner === currentOwner ? 'is-self' : ''}`}
              title={r.owner === currentOwner ? `owned by you (${r.owner})` : `owned by ${r.owner}`}>
              {r.owner || 'localhost'}
            </div>
            <div className="priority-col">
              <span title={pmeta.label}
                style={{ padding: '1px 6px', borderRadius: 3,
                         background: pmeta.bg, color: pmeta.color,
                         fontSize: 10, fontWeight: 600 }}>{pmeta.label}</span>
            </div>
            <div>{r.mode === 'custom' ? <span style={{ color: 'var(--ink-4)' }}>—</span> : <ScoreMini score={r.total_score} />}</div>
            <div className="wall tnum">{r.status === 'queued' ? <span style={{ color: 'var(--ink-4)' }}>—</span> : fmtSec(r.wall_time_sec)}</div>
            <div className="tnum"
              title={r.qe_wall_sec != null ? `QE wall: ${fmtSec(r.qe_wall_sec)}` : ''}>
              {r.status === 'queued' || r.qe_call_count == null
                ? <span style={{ color: 'var(--ink-4)' }}>—</span>
                : r.qe_call_count}
            </div>
            <div className="tnum"
              title={r.max_turns ? `max: ${r.max_turns}` : ''}>
              {r.status === 'queued' || !r.current_turn
                ? <span style={{ color: 'var(--ink-4)' }}>—</span>
                : r.current_turn}
            </div>
            <div className="tnum"
              title={r.input_tokens_total != null
                ? `${fmtTokens(r.input_tokens_total)} in / ${fmtTokens(r.output_tokens_total)} out`
                : ''}>{fmtUsd(r.cost_usd)}</div>
            <div><StatusBadge status={r.status} lang={lang} /></div>
            <div>
              {cancellable && <button className="btn ghost xs"
                onClick={(e) => { e.stopPropagation(); onCancel(r); }}>
                {t(lang, 'runs.cancel_btn')}
              </button>}
            </div>
          </div>;
        })}
        {!filtered.length && <div className="empty-state"><div className="h">no runs</div></div>}
      </div>
    </div>
  </div>;
}

Object.assign(window, { ResultBench, ResultCustom, History });
