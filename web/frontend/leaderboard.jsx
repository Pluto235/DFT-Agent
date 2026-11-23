/* eslint-disable no-undef */
// Leaderboard：(agent × model) 主矩阵 + (M_T) chip 多选 filter +
// metric / aggregation seg + heatmap 着色 + compare-all 5-metric 并列 +
// date scope + owner ('all'|'mine') filter。
// 后端在 /api/leaderboard，公榜（任何登录用户都可见）。

const { useState, useEffect, useMemo } = React;

const _METRICS = ['score', 'wall', 'qe', 'turn', 'cost'];
const _AGGREGATIONS = ['avg', 'best'];
// 这些 metric 越低越好（排序方向反 / heatmap 反极）
const _LOWER_BETTER = new Set(['wall', 'qe', 'turn', 'cost']);
const _DATE_PRESETS = ['today', 'week', 'month', 'all'];
const _OWNER_FILTERS = ['all', 'mine'];

// ============================================================================
// 取值 + 格式化
// ============================================================================

function pickCellValue(metrics, metric, _aggregation) {
  // backend 端已根据 aggregation 计算（per-task → 跨 task 平均），前端总取 m.avg。
  if (!metrics) return null;
  const m = metrics[metric];
  if (!m) return null;
  return m.avg;
}

function fmtCellValue(metric, value) {
  if (value === null || value === undefined) return '—';
  if (metric === 'score') return Number(value).toFixed(2);
  if (metric === 'wall') return typeof fmtSec === 'function' ? fmtSec(value) : `${value.toFixed(0)}s`;
  if (metric === 'qe') return Number(value).toFixed(1);
  if (metric === 'turn') return Number(value).toFixed(1);
  if (metric === 'cost') return typeof fmtUsd === 'function' ? fmtUsd(value) : `$${value.toFixed(2)}`;
  return String(value);
}

// ============================================================================
// 数据 → 矩阵
// ============================================================================

// data.rows: list[{agent_id, agent_name, model, metrics}]
// → { agents: [{id, name}], models: [model_id], cellAt(agent_id, model): metrics | null }
function pivotToMatrix(rows) {
  const agentMap = new Map();
  const modelSet = new Set();
  const cellByKey = new Map();
  for (const r of rows) {
    if (!agentMap.has(r.agent_id)) agentMap.set(r.agent_id, r.agent_name);
    modelSet.add(r.model);
    cellByKey.set(`${r.agent_id}::${r.model}`, r.metrics);
  }
  const agents = Array.from(agentMap, ([id, name]) => ({ id, name }));
  const models = Array.from(modelSet).sort();
  const cellAt = (aid, m) => cellByKey.get(`${aid}::${m}`) || null;
  return { agents, models, cellAt };
}

// row 在某 metric 下的"行均值"用于排序；缺值不计
function rowAvgForMetric(matrix, agentId, metric, aggregation) {
  let sum = 0, n = 0;
  for (const m of matrix.models) {
    const cell = matrix.cellAt(agentId, m);
    const v = pickCellValue(cell, metric, aggregation);
    if (v !== null && v !== undefined && Number.isFinite(v)) {
      sum += v; n += 1;
    }
  }
  return n > 0 ? sum / n : null;
}

// ============================================================================
// Heatmap 着色
// ============================================================================

// 收集矩阵里所有 (agent, model) cell 在某 metric 下的有效值，用于归一化 range
function collectValues(matrix, metric, aggregation) {
  const out = [];
  for (const a of matrix.agents) {
    for (const m of matrix.models) {
      const v = pickCellValue(matrix.cellAt(a.id, m), metric, aggregation);
      if (v !== null && v !== undefined && Number.isFinite(v)) out.push(v);
    }
  }
  return out;
}

// score 用固定 0..1 范围（数据全 0.95 时仍显高分绿）；其他 metric 用本表 min..max
function rangeFor(metric, values) {
  if (metric === 'score') return { min: 0, max: 1 };
  if (values.length < 2) return null; // 单值无法归一化 → 不染色
  return { min: Math.min(...values), max: Math.max(...values) };
}

// hue 0=红 / 120=绿。lower-better metric 反极：低值 → 绿（hue 120）。
function cellColor(value, metric, range) {
  if (value == null || !Number.isFinite(value) || !range) return null;
  const span = range.max - range.min;
  if (span === 0) return null;
  let t = (value - range.min) / span; // 0..1
  t = Math.max(0, Math.min(1, t));
  if (_LOWER_BETTER.has(metric)) t = 1 - t;
  const hue = t * 120;
  return `hsla(${hue}, 65%, 50%, 0.18)`;
}

// ============================================================================
// 单矩阵渲染（compare-all 模式下被复用）
// ============================================================================

function MatrixTable({ matrix, metric, aggregation, lang, compact, onCellClick, nTasksInScope }) {
  const values = collectValues(matrix, metric, aggregation);
  const range = rangeFor(metric, values);
  const lowerBetter = _LOWER_BETTER.has(metric);

  const rowAvgs = new Map();
  for (const a of matrix.agents) {
    rowAvgs.set(a.id, rowAvgForMetric(matrix, a.id, metric, aggregation));
  }
  const sortedAgents = [...matrix.agents].sort((a, b) => {
    const va = rowAvgs.get(a.id), vb = rowAvgs.get(b.id);
    if (va == null && vb == null) return a.id.localeCompare(b.id);
    if (va == null) return 1;
    if (vb == null) return -1;
    return lowerBetter ? va - vb : vb - va;
  });

  return <div className={`lb-matrix-wrap ${compact ? 'compact' : ''}`}>
    {compact && <div className="lb-matrix-cap">
      <span className="lb-matrix-cap-metric">{t(lang, `leaderboard.metric.${metric}`)}</span>
      <span className="lb-matrix-cap-hint">
        {lowerBetter ? '↓ lower better' : '↑ higher better'}
      </span>
    </div>}
    <table className="lb-matrix">
      <thead>
        <tr>
          <th className="lb-row-head-h">
            <span>{t(lang, 'leaderboard.row.agent')}</span>
            <span className="lb-mute">/ {t(lang, 'leaderboard.row.model')} →</span>
          </th>
          {matrix.models.map(m => (
            <th key={m} className="lb-col-head mono" title={m}>{m}</th>
          ))}
          <th className="lb-col-head lb-col-total" title="row average across all models">
            {t(lang, 'leaderboard.agg.avg')}
          </th>
        </tr>
      </thead>
      <tbody>
        {sortedAgents.map(agent => (
          <tr key={agent.id}>
            <th className="lb-row-head">
              <span className="lb-agent-name">{agent.name}</span>
              <span className="lb-agent-id mono">{agent.id}</span>
            </th>
            {matrix.models.map(model => {
              const cell = matrix.cellAt(agent.id, model);
              const v = pickCellValue(cell, metric, aggregation);
              const bg = cellColor(v, metric, range);
              // task 维度计数（与 metric 无关）：attempted = 跑过的 task 种类，
              // succeeded = 至少 1 次 done 的 task 数，failed_only = 全 fail 的
              const ts = cell?.task_stats || { attempted: 0, succeeded: 0, failed_only: 0 };
              const total = nTasksInScope || 0;
              const tip = cell ? [
                `${agent.name} · ${model}`,
                `${metric}: ${fmtCellValue(metric, v)}`,
                `attempted: ${ts.attempted}/${total} tasks`,
                `  ✓ succeeded (≥1 done): ${ts.succeeded}`,
                `  ✗ all failed: ${ts.failed_only}`,
                `  · untried: ${Math.max(0, total - ts.attempted)}`,
                metric === 'score' && cell.score
                  ? `score pool: n_runs=${cell.score.n_total} (done=${cell.score.n_done}, failed-zero=${cell.score.n_failed_zero})`
                  : (cell[metric] ? `${metric} pool: n_tasks=${cell[metric].n_tasks}` : ''),
              ].filter(Boolean).join('\n') : `${agent.name} · ${model}: no runs`;
              const clickable = !!cell && !!onCellClick;
              return (
                <td key={model}
                    className={`lb-cell ${cell ? '' : 'lb-cell-empty'} ${clickable ? 'clickable' : ''}`}
                    style={bg ? { backgroundColor: bg } : undefined}
                    title={tip + (clickable ? '\n\nclick to see runs' : '')}
                    onClick={clickable ? () => onCellClick(agent, model) : undefined}>
                  <div className="lb-cell-val tnum">{fmtCellValue(metric, v)}</div>
                  {cell && total > 0 && <div className="lb-cell-n mono">
                    <span className="lb-cell-cov">{ts.attempted}/{total}</span>
                    {ts.succeeded > 0 && <span className="lb-cell-ok"> ✓{ts.succeeded}</span>}
                    {ts.failed_only > 0 && <span className="lb-cell-fail"> ✗{ts.failed_only}</span>}
                  </div>}
                </td>
              );
            })}
            <td className="lb-cell lb-cell-total tnum"
                title={`row avg across ${matrix.models.length} models`}>
              <div className="lb-cell-val">{fmtCellValue(metric, rowAvgs.get(agent.id))}</div>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  </div>;
}

// ============================================================================
// Drill-down modal：单 (agent, model) cell 下的 run 列表
// ============================================================================

function DrillDown({ agent, model, filterParams, lang, tasks, user, onClose, onPickRun, onOpenBatch }) {
  const [runs, setRuns] = useState(null);
  const [error, setError] = useState(null);
  const [excludeUnstable, setExcludeUnstable] = useState(false);
  // 见到 dev_tasks（is_stable=false 的题目）的能力 → 才显示 exclude_unstable + rerun_fails
  const canSeeDevTasks = hasCapability(user, CAP_VIEW_DEV_TASKS);

  useEffect(() => {
    let cancelled = false;
    setRuns(null); setError(null);
    (async () => {
      try {
        const params = { ...filterParams, agent_id: agent.id, model };
        const d = await window.__api.getLeaderboardRuns(params);
        if (!cancelled) setRuns(d.runs || []);
      } catch (e) {
        if (!cancelled) setError(e.message || String(e));
      }
    })();
    return () => { cancelled = true; };
  }, [agent.id, model, JSON.stringify(filterParams)]);

  // ESC 关闭
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const taskMap = useMemo(
    () => Object.fromEntries((tasks || []).map(t => [t.task_id, t])),
    [tasks]
  );

  // fail = task_id 在该 cell 出现过但无任何 done run（与 backend leaderboard
  // n_tasks_failed_only 同义）。excludeUnstable=true 时丢掉 is_stable=false 任务。
  const failTaskIds = useMemo(() => {
    if (!Array.isArray(runs)) return [];
    const byTask = new Map();
    for (const r of runs) {
      if (!r.task_id) continue;
      if (!byTask.has(r.task_id)) byTask.set(r.task_id, []);
      byTask.get(r.task_id).push(r);
    }
    const ids = [];
    for (const [tid, rs] of byTask.entries()) {
      if (rs.some(r => r.status === 'done')) continue;
      if (excludeUnstable && taskMap[tid]?.is_stable === false) continue;
      ids.push(tid);
    }
    return ids;
  }, [runs, excludeUnstable, taskMap]);

  const onClickRerunFails = () => {
    if (failTaskIds.length === 0) return;
    if (typeof onOpenBatch !== 'function') return;
    onOpenBatch(failTaskIds);
    onClose();
  };

  const fmtWhen = (iso) => {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      const m = (d.getMonth() + 1).toString().padStart(2, '0');
      const day = d.getDate().toString().padStart(2, '0');
      const h = d.getHours().toString().padStart(2, '0');
      const mn = d.getMinutes().toString().padStart(2, '0');
      return `${m}-${day} ${h}:${mn}`;
    } catch (_e) { return iso.slice(0, 16); }
  };

  return <div className="lb-drill-overlay" onClick={onClose}>
    <div className="lb-drill-modal" onClick={e => e.stopPropagation()}>
      <div className="lb-drill-head">
        <div className="lb-drill-head-l">
          <h3>{t(lang, 'leaderboard.drill.title')}</h3>
          <div className="lb-drill-cap">
            <span className="lb-drill-cap-agent">{agent.name}</span>
            <span className="lb-drill-cap-sep">·</span>
            <span className="lb-drill-cap-model mono">{model}</span>
            {filterParams.milestone && <>
              <span className="lb-drill-cap-sep">·</span>
              <span className="lb-drill-cap-scope">{filterParams.milestone}</span>
            </>}
            {filterParams.task_type && <>
              <span className="lb-drill-cap-sep">·</span>
              <span className="lb-drill-cap-scope">{filterParams.task_type}</span>
            </>}
            {filterParams.owner && <>
              <span className="lb-drill-cap-sep">·</span>
              <span className="lb-drill-cap-owner">owner={filterParams.owner}</span>
            </>}
          </div>
          <div className="lb-drill-hint">{t(lang, 'leaderboard.drill.go_to_result')}</div>
        </div>
        <div className="lb-drill-head-r">
          {canSeeDevTasks && <>
            <label className="lb-drill-unstable-toggle"
                   title={t(lang, 'leaderboard.drill.exclude_unstable.tip')}>
              <input type="checkbox" checked={excludeUnstable}
                     onChange={(e) => setExcludeUnstable(e.target.checked)} />
              <span>{t(lang, 'leaderboard.drill.exclude_unstable')}</span>
            </label>
            <button className="btn sm lb-drill-rerun-fails"
                    onClick={onClickRerunFails}
                    disabled={failTaskIds.length === 0}
                    title={failTaskIds.length === 0
                      ? t(lang, 'leaderboard.drill.rerun_fails.empty')
                      : t(lang, 'leaderboard.drill.rerun_fails.tip', { n: failTaskIds.length })}>
              {t(lang, 'leaderboard.drill.rerun_fails', { n: failTaskIds.length })}
            </button>
          </>}
          <button className="lb-drill-close" onClick={onClose}
                  title={t(lang, 'leaderboard.drill.close')}>×</button>
        </div>
      </div>
      {error && <div className="lb-err">{error}</div>}
      {runs === null && !error && <div className="lb-empty">{t(lang, 'leaderboard.drill.loading')}</div>}
      {runs !== null && runs.length === 0 && <div className="lb-empty">{t(lang, 'leaderboard.drill.empty')}</div>}
      {runs !== null && runs.length > 0 && <div className="lb-drill-table-wrap">
        <table className="lb-drill-table">
          <thead>
            <tr>
              <th>{t(lang, 'leaderboard.drill.col.when')}</th>
              <th>{t(lang, 'leaderboard.drill.col.task')}</th>
              <th>{t(lang, 'leaderboard.drill.col.score')}</th>
              <th>{t(lang, 'leaderboard.drill.col.wall')}</th>
              <th>{t(lang, 'leaderboard.drill.col.qe')}</th>
              <th>{t(lang, 'leaderboard.drill.col.turn')}</th>
              <th>{t(lang, 'leaderboard.drill.col.cost')}</th>
              <th>{t(lang, 'leaderboard.drill.col.status')}</th>
              <th>{t(lang, 'leaderboard.drill.col.owner')}</th>
            </tr>
          </thead>
          <tbody>
            {runs.map(r => {
              const task = taskMap[r.task_id];
              const formula = task?.material_formula || '';
              return <tr key={r.run_id} className="lb-drill-row" onClick={() => onPickRun(r)}>
                <td className="mono">{fmtWhen(r.created_at)}</td>
                <td>
                  <div className="lb-drill-task-formula">{formula || r.task_scope}</div>
                  <div className="lb-drill-task-id mono">{r.task_id}</div>
                </td>
                <td className="tnum">{r.total_score == null ? '—' : Number(r.total_score).toFixed(2)}</td>
                <td className="tnum">{fmtCellValue('wall', r.wall_time_sec)}</td>
                <td className="tnum">{r.qe_call_count == null ? '—' : r.qe_call_count}</td>
                <td className="tnum">{r.current_turn == null ? '—' : r.current_turn}</td>
                <td className="tnum">{fmtCellValue('cost', r.cost_usd)}</td>
                <td><span className={`badge status-${r.status}`}>{r.status}</span></td>
                <td className="mono">{r.owner || '—'}</td>
              </tr>;
            })}
          </tbody>
        </table>
      </div>}
    </div>
  </div>;
}

// ============================================================================
// Date / owner filter helpers
// ============================================================================

function dateFromPreset(preset) {
  if (preset === 'all' || !preset) return null;
  const days = preset === 'today' ? 1 : preset === 'week' ? 7 : preset === 'month' ? 30 : 0;
  if (!days) return null;
  return new Date(Date.now() - days * 86400_000).toISOString();
}

// ============================================================================
// 顶层 Leaderboard
// ============================================================================

function Leaderboard({ lang, user, setState, tasks, onOpenBatch }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [milestone, setMilestone] = useState('all');   // 'all' | 'M1' | 'M2' | ...
  const [taskType, setTaskType] = useState('all');     // 'all' | 'T1' | 'T2' | ...
  const [subsetView, setSubsetView] = useState('all'); // 'all' | 'fast' | 'standard' | 'full'
  const [aggregation, setAggregation] = useState('best');
  const [metric, setMetric] = useState('score');
  const [dateScope, setDateScope] = useState('all');
  const [ownerFilter, setOwnerFilter] = useState('all'); // 'all' | 'mine'
  const [compareAll, setCompareAll] = useState(false);
  const [drilldown, setDrilldown] = useState(null); // {agent, model} | null

  // 代表性子集清单（v1.1）— 一次拉取本地缓存；用于视图切换按钮组
  const [subsets, setSubsets] = useState([]);
  useEffect(() => {
    window.__api.getSubsets()
      .then(d => setSubsets(d.subsets || []))
      .catch(() => setSubsets([]));
  }, []);

  const myOwner = user?.owner || 'localhost';

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        const params = { aggregation };
        if (milestone !== 'all') params.milestone = milestone;
        if (taskType !== 'all') params.task_type = taskType;
        if (subsetView !== 'all') params.subset = subsetView;
        const dateFrom = dateFromPreset(dateScope);
        if (dateFrom) params.date_from = dateFrom;
        if (ownerFilter === 'mine') params.owner = myOwner;
        const d = await window.__api.getLeaderboard(params);
        if (!cancelled) { setData(d); setLoading(false); }
      } catch (e) {
        if (!cancelled) { setError(e.message || String(e)); setLoading(false); }
      }
    })();
    return () => { cancelled = true; };
  }, [aggregation, milestone, taskType, subsetView, dateScope, ownerFilter, myOwner]);

  // 子集视图副标题（"标准档排行榜（27 道）"等）
  const activeSubset = subsetView !== 'all' ? subsets.find(s => s.key === subsetView) : null;
  const subsetSubtitle = activeSubset
    ? `${lang === 'zh' ? activeSubset.name : activeSubset.name_en} · ${activeSubset.count} 题`
    : null;

  const head = <div className="lb-head">
    <h2>{t(lang, 'leaderboard.title')}{subsetSubtitle && <span style={{
      marginLeft: 8, fontSize: '0.65em', color: 'var(--muted)', fontWeight: 'normal'
    }}>· {subsetSubtitle}</span>}</h2>
    <div className="lb-sub">{t(lang, 'leaderboard.subtitle')}</div>
  </div>;

  // 子集视图切换按钮组（与 segmented control 同款）：[全部] [快速档 12] [标准档 27] [完整档 142]
  const subsetSwitch = subsets.length > 0 && <div className="lb-tb-group">
    <span className="lb-tb-lbl">{t(lang, 'lb.subset.label')}</span>
    <div className="seg">
      <button className={subsetView === 'all' ? 'active' : ''}
        onClick={() => setSubsetView('all')}>
        {t(lang, 'lb.subset.all')}
      </button>
      {subsets.map(sub => (
        <button key={sub.key} className={subsetView === sub.key ? 'active' : ''}
          onClick={() => setSubsetView(sub.key)}
          title={sub.description}>
          {lang === 'zh' ? sub.name : sub.name_en} ({sub.count})
        </button>
      ))}
    </div>
  </div>;

  const toolbar = data && <div className="lb-toolbar">
    {/* metric switcher — compare-all 模式下静音禁选 */}
    <div className="lb-tb-group">
      <span className="lb-tb-lbl">{t(lang, 'leaderboard.metric')}</span>
      <div className={`seg ${compareAll ? 'disabled' : ''}`}>
        {_METRICS.map(m => <button key={m}
          className={metric === m ? 'active' : ''}
          disabled={compareAll}
          onClick={() => setMetric(m)}>
          {t(lang, `leaderboard.metric.${m}`)}
        </button>)}
      </div>
    </div>
    {/* aggregation */}
    <div className="lb-tb-group">
      <span className="lb-tb-lbl">{t(lang, 'leaderboard.agg')}</span>
      <div className="seg">
        {_AGGREGATIONS.map(a => <button key={a}
          className={aggregation === a ? 'active' : ''}
          onClick={() => setAggregation(a)}>
          {t(lang, `leaderboard.agg.${a}`)}
        </button>)}
      </div>
    </div>
    {/* date scope */}
    <div className="lb-tb-group">
      <span className="lb-tb-lbl">{t(lang, 'leaderboard.date')}</span>
      <div className="seg">
        {_DATE_PRESETS.map(d => <button key={d}
          className={dateScope === d ? 'active' : ''}
          onClick={() => setDateScope(d)}>
          {t(lang, `leaderboard.date.${d}`)}
        </button>)}
      </div>
    </div>
    {/* owner — 公榜默认 all；'mine' 只看自己 */}
    <div className="lb-tb-group">
      <span className="lb-tb-lbl">{t(lang, 'leaderboard.owner')}</span>
      <div className="seg">
        {_OWNER_FILTERS.map(o => <button key={o}
          className={ownerFilter === o ? 'active' : ''}
          onClick={() => setOwnerFilter(o)}
          title={o === 'mine' ? `owner = ${myOwner}` : 'all owners (public)'}>
          {t(lang, `leaderboard.owner.${o}`)}
        </button>)}
      </div>
    </div>
    {/* compare-all toggle */}
    <div className="lb-tb-group">
      <button className={`lb-compare-toggle ${compareAll ? 'active' : ''}`}
        onClick={() => setCompareAll(v => !v)}
        title={t(lang, 'leaderboard.compareall.hint')}>
        {compareAll ? '✓ ' : ''}{t(lang, 'leaderboard.compareall')}
      </button>
    </div>
    {/* milestone / task_type FilterPill — 与 catalogue 同款 dropdown，扩展性好 */}
    <div className="lb-tb-group">
      <FilterPill label={t(lang, 'leaderboard.milestone')}
        value={milestone}
        options={['all', ...(data.milestones_available || [])]}
        onChange={setMilestone} />
    </div>
    <div className="lb-tb-group">
      <FilterPill label={t(lang, 'leaderboard.tasktype')}
        value={taskType}
        options={['all', ...(data.task_types_available || [])]}
        onChange={setTaskType} />
    </div>
    {/* 子集视图切换：[全部] [快速档 12] [标准档 27] [完整档 142]，与 milestone/task_type AND 组合 */}
    {subsetSwitch}
  </div>;

  // ──── 三态 ────
  if (loading && !data) return <div className="lb-page">{head}
    <div className="lb-empty">{t(lang, 'leaderboard.loading')}</div>
  </div>;
  if (error) return <div className="lb-page">{head}
    <div className="lb-err">{t(lang, 'leaderboard.error.fetch')}: {error}</div>
  </div>;
  if (!data || data.rows.length === 0) return <div className="lb-page">{head}
    {toolbar}
    <div className="lb-empty">{t(lang, 'leaderboard.empty')}</div>
  </div>;

  // ──── 矩阵 ────
  const matrix = pivotToMatrix(data.rows);
  if (matrix.agents.length === 0 || matrix.models.length === 0) {
    return <div className="lb-page">{head}{toolbar}
      <div className="lb-empty">{t(lang, 'leaderboard.empty')}</div>
    </div>;
  }

  const onCellClick = (agent, model) => setDrilldown({ agent, model });

  const nTasksInScope = data.n_tasks_in_scope || 0;
  const matrixView = compareAll
    ? <div className="lb-compare-stack">
        {_METRICS.map(m => <MatrixTable key={m}
          matrix={matrix} metric={m} aggregation={aggregation}
          lang={lang} compact={true} onCellClick={onCellClick}
          nTasksInScope={nTasksInScope} />)}
      </div>
    : <MatrixTable matrix={matrix} metric={metric} aggregation={aggregation}
                   lang={lang} compact={false} onCellClick={onCellClick}
                   nTasksInScope={nTasksInScope} />;

  // drill-down 用与主矩阵同样的 filter（除了 aggregation 不传——drill-down 列原始 run）
  const drillFilters = {};
  if (milestone !== 'all') drillFilters.milestone = milestone;
  if (taskType !== 'all') drillFilters.task_type = taskType;
  const dateFrom = dateFromPreset(dateScope);
  if (dateFrom) drillFilters.date_from = dateFrom;
  if (ownerFilter === 'mine') drillFilters.owner = myOwner;

  const goToRun = (run) => {
    if (typeof setState === 'function') {
      setState(s => ({
        ...s,
        tab: 'run', centerTab: 'result',
        viewedRunId: run.run_id,
        activeRun: null,
      }));
    }
    setDrilldown(null);
  };

  return <div className="lb-page">
    {head}
    {toolbar}
    {matrixView}
    <div className="lb-foot-hint">
      {matrix.agents.length} agents × {matrix.models.length} models ·
      {' '}material: {data.filters_applied.milestone || 'all'} ·
      {' '}task: {data.filters_applied.task_type || 'all'} ·
      {' '}aggregation: {data.filters_applied.aggregation}
      {data.filters_applied.date_from && <> · since {data.filters_applied.date_from.slice(0, 10)}</>}
      {data.filters_applied.owner && <> · owner: {data.filters_applied.owner}</>}
      <span className="lb-foot-hint-tip">· {t(lang, 'leaderboard.cell.click_hint')}</span>
    </div>
    {drilldown && <DrillDown
      agent={drilldown.agent}
      model={drilldown.model}
      filterParams={drillFilters}
      lang={lang}
      tasks={tasks}
      user={user}
      onClose={() => setDrilldown(null)}
      onPickRun={goToRun}
      onOpenBatch={onOpenBatch}
    />}
  </div>;
}

window.Leaderboard = Leaderboard;
