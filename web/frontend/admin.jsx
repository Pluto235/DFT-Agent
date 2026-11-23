/* eslint-disable no-undef */
// Dev-only Admin dashboard：GPU / disk / scheduler 实时快照 + GPU 启用策略 +
// 用量统计（runs 表按 status / model / owner 聚合）。后端在 /api/admin/*；
// auth_middleware 已 gate 仅 dev role。tab 在 TopBar 也只对 dev role 显示。

const { useState, useEffect, useRef, useMemo } = React;

// 字节 → 人类可读（用 GB 因为磁盘统计 200+ GB 是常态，utils.fmtBytes 上限 MB 不够）
function fmtBytesLg(b) {
  if (b == null || !isFinite(b)) return '—';
  if (b < 1024) return `${b} B`;
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(1)} KB`;
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(1)} MB`;
  if (b < 1024 ** 4) return `${(b / 1024 ** 3).toFixed(1)} GB`;
  return `${(b / 1024 ** 4).toFixed(2)} TB`;
}

function fmtPct(v) {
  if (v == null) return '—';
  return `${v}%`;
}

// "since" preset 选项 → ISO Z 字符串（now-7d / now-30d / null=all）
function sinceFromPreset(preset) {
  if (preset === 'all') return null;
  const now = Date.now();
  const days = preset === '7d' ? 7 : preset === '30d' ? 30 : 7;
  return new Date(now - days * 86400_000).toISOString();
}

function ClusterPanel({ data, lang }) {
  if (!data) return <div className="admin-empty">{t(lang, 'admin.error.fetch')}</div>;
  const ourPids = new Set(data.our_active_pids || []);
  const gpus = data.gpus || [];
  const apps = data.compute_apps || [];
  const sched = data.scheduler || {};
  const disk = data.disk || {};
  const cpu = data.cpu || {};
  const mem = data.memory || {};

  return <div className="admin-panel">
    <div className="admin-panel-head">
      <h3>{t(lang, 'admin.section.cluster')}</h3>
      <span className="admin-panel-hint">{t(lang, 'admin.refreshed')}</span>
    </div>

    <div className="admin-cluster-grid">
     {/* Row 1 — GPU + CPU side-by-side */}
     <div className="admin-cluster-row primary">
      {/* GPU 卡列表 */}
      <div className="admin-card">
        <div className="admin-card-head">{t(lang, 'admin.col.gpu')}</div>
        {gpus.length === 0 && <div className="admin-card-empty">no GPUs detected</div>}
        {gpus.length > 0 && <table className="admin-table">
          <thead>
            <tr>
              <th>#</th>
              <th>name</th>
              <th>{t(lang, 'admin.col.util')}</th>
              <th>{t(lang, 'admin.col.mem')}</th>
              <th>{t(lang, 'admin.col.temp')}</th>
              <th>{t(lang, 'admin.col.procs')}</th>
            </tr>
          </thead>
          <tbody>
            {gpus.map(g => {
              const gpuApps = apps.filter(a => a.gpu_index === g.index);
              const ours = gpuApps.filter(a => ourPids.has(a.pid));
              const others = gpuApps.filter(a => !ourPids.has(a.pid));
              const memPct = g.memory_total_mib
                ? Math.round((g.memory_used_mib / g.memory_total_mib) * 100) : 0;
              return <tr key={g.index}>
                <td className="mono">{g.index}</td>
                <td className="mono" title={g.name}>{g.name.replace(/^NVIDIA /, '')}</td>
                <td>{fmtPct(g.util_pct)}</td>
                <td title={`${g.memory_used_mib} / ${g.memory_total_mib} MiB`}>
                  {(g.memory_used_mib / 1024).toFixed(1)} / {(g.memory_total_mib / 1024).toFixed(0)} GB
                  <span className="admin-mem-pct"> ({memPct}%)</span>
                </td>
                <td>{g.temp_c}°C</td>
                <td>
                  {gpuApps.length === 0 && <span className="admin-mute">{t(lang, 'admin.empty.compute_apps')}</span>}
                  {ours.length > 0 && <span className="admin-chip ours" title={ours.map(a => `pid ${a.pid} (${a.process_name}) ${a.used_memory_mib} MiB`).join('\n')}>
                    {t(lang, 'admin.app.ours')}: {ours.length}
                  </span>}
                  {others.length > 0 && <span className="admin-chip others" title={others.map(a => `pid ${a.pid} (${a.process_name}) ${a.used_memory_mib} MiB`).join('\n')}>
                    {t(lang, 'admin.app.others')}: {others.length}
                  </span>}
                </td>
              </tr>;
            })}
          </tbody>
        </table>}
      </div>

      {/* CPU 总览 + per-core 占用条带 */}
      {cpu.cores_logical && <div className="admin-card">
        <div className="admin-card-head">{t(lang, 'admin.col.cpu')}</div>
        <div className="admin-kv">
          <div><span>{t(lang, 'admin.cpu.cores')}</span><b>{cpu.cores_physical} phys / {cpu.cores_logical} logical</b></div>
          <div><span>{t(lang, 'admin.col.util')}</span><b className={cpu.util_pct >= 80 ? 'admin-warn' : ''}>{fmtPct(cpu.util_pct)}</b></div>
          <div><span>{t(lang, 'admin.cpu.load')}</span><b>{cpu.load_1m} / {cpu.load_5m} / {cpu.load_15m}</b></div>
          {cpu.freq_mhz && <div><span>{t(lang, 'admin.cpu.freq')}</span><b>{cpu.freq_mhz} MHz</b></div>}
        </div>
        {cpu.per_core_pct && cpu.per_core_pct.length > 0 && <div className="admin-cpu-cores" title={t(lang, 'admin.cpu.per_core')}>
          {cpu.per_core_pct.map((p, i) => {
            const cls = p >= 80 ? 'hot' : p >= 30 ? 'warm' : p >= 5 ? 'cool' : 'idle';
            return <span key={i} className={`admin-cpu-core ${cls}`}
              title={`core ${i}: ${p.toFixed(1)}%`}
              style={{ height: `${Math.max(2, Math.round(p * 0.24))}px` }} />;
          })}
        </div>}
      </div>}
     </div>

     {/* Row 2 — Memory + Scheduler + Disk equal width */}
     <div className="admin-cluster-row secondary">

      {/* 内存 + swap */}
      {mem.total_bytes && <div className="admin-card">
        <div className="admin-card-head">{t(lang, 'admin.col.memory')}</div>
        <div className="admin-kv">
          <div><span>{t(lang, 'admin.mem.used')}</span><b>{fmtBytesLg(mem.used_bytes)} / {fmtBytesLg(mem.total_bytes)} <span className="admin-mem-pct">({mem.used_pct}%)</span></b></div>
          <div><span>{t(lang, 'admin.mem.available')}</span><b>{fmtBytesLg(mem.available_bytes)}</b></div>
          {mem.total_bytes > 0 && <div className="admin-disk-bar" title={`${mem.used_pct}%`}>
            <span style={{ width: `${Math.round(mem.used_pct)}%` }} />
          </div>}
          {mem.swap_total_bytes > 0 && <div>
            <span>{t(lang, 'admin.mem.swap')}</span>
            <b className={mem.swap_used_pct >= 50 ? 'admin-warn' : ''}>
              {fmtBytesLg(mem.swap_used_bytes)} / {fmtBytesLg(mem.swap_total_bytes)} <span className="admin-mem-pct">({mem.swap_used_pct}%)</span>
            </b>
          </div>}
        </div>
      </div>}

      {/* Scheduler 状态 */}
      <div className="admin-card">
        <div className="admin-card-head">{t(lang, 'admin.col.scheduler')}</div>
        <div className="admin-kv">
          <div><span>{t(lang, 'admin.col.queued')}</span><b>{sched.queued ?? '—'}</b></div>
          <div><span>{t(lang, 'admin.col.live')}</span><b>{sched.live ?? '—'}</b></div>
          <div><span>{t(lang, 'admin.col.paused')}</span><b className={sched.paused ? 'admin-warn' : ''}>{sched.paused ? 'yes' : 'no'}</b></div>
        </div>
      </div>

      {/* 磁盘 */}
      <div className="admin-card">
        <div className="admin-card-head">{t(lang, 'admin.col.disk')}</div>
        <div className="admin-kv">
          <div><span>path</span><b className="mono">{disk.path || '—'}</b></div>
          <div><span>used</span><b>{fmtBytesLg(disk.used_bytes)}</b></div>
          <div><span>free</span><b>{fmtBytesLg(disk.free_bytes)}</b></div>
          <div><span>total</span><b>{fmtBytesLg(disk.total_bytes)}</b></div>
          {disk.total_bytes > 0 && <div className="admin-disk-bar">
            <span style={{ width: `${Math.round((disk.used_bytes / disk.total_bytes) * 100)}%` }} />
          </div>}
        </div>
      </div>
     </div>
    </div>
  </div>;
}

function QeGpuModePanel({ mode, onSave, lang }) {
  // mode: { mode: 'backend_pin' | 'wrapper_flock', available_modes: [...] } | null
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(null);
  const [err, setErr] = useState(null);

  if (!mode) return <div className="admin-empty">{t(lang, 'admin.error.fetch')}</div>;

  const set = async (next) => {
    if (next === mode.mode) return;
    if (next === 'wrapper_flock') {
      if (!window.confirm(t(lang, 'admin.qe_gpu_mode.confirm_flock'))) return;
    }
    setSaving(true); setErr(null);
    try {
      await window.__api.setQeGpuMode(next);
      setSavedAt(Date.now());
      onSave && onSave();
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      setSaving(false);
    }
  };

  return <div className="admin-panel">
    <div className="admin-panel-head">
      <h3>{t(lang, 'admin.section.qe_gpu_mode')}</h3>
    </div>
    <div className="admin-policy-row">
      {(mode.available_modes || ['backend_pin', 'wrapper_flock']).map(m => (
        <button key={m} className={`btn ${mode.mode === m ? 'primary' : 'ghost'}`}
          disabled={saving} onClick={() => set(m)}>
          {t(lang, `admin.qe_gpu_mode.${m}`)}
        </button>
      ))}
      {savedAt && <span className="admin-saved">{t(lang, 'admin.qe_gpu_mode.saved')}</span>}
    </div>
    <div className="admin-panel-hint">{t(lang, 'admin.qe_gpu_mode.hint')}</div>
    {err && <div className="admin-err">{err}</div>}
  </div>;
}


function GpuPolicyPanel({ policy, onSave, lang }) {
  // policy: { enabled, detected_total, all_indices } | null
  const [enabled, setEnabled] = useState(() => new Set(policy?.enabled || []));
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(null);
  const [err, setErr] = useState(null);

  // policy 从父级 fetch 拿到时同步本地 state（不在用户编辑期间覆盖——
  // saving=true 的几百毫秒里若父级 refresh 进来不要回滚）。
  useEffect(() => {
    if (!policy) return;
    if (saving) return;
    setEnabled(new Set(policy.enabled));
  }, [policy?.enabled?.join(',')]);  // depen on serialized list

  if (!policy) return <div className="admin-empty">{t(lang, 'admin.error.fetch')}</div>;
  const all = policy.all_indices || [];

  const toggle = (i) => {
    setEnabled(prev => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i); else next.add(i);
      return next;
    });
  };

  const save = async () => {
    setSaving(true); setErr(null);
    try {
      await window.__api.setGpuPolicy(Array.from(enabled).sort((a, b) => a - b));
      setSavedAt(Date.now());
      onSave && onSave();
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      setSaving(false);
    }
  };

  const dirty = (() => {
    const cur = new Set(policy.enabled);
    if (cur.size !== enabled.size) return true;
    for (const i of cur) if (!enabled.has(i)) return true;
    return false;
  })();

  return <div className="admin-panel">
    <div className="admin-panel-head">
      <h3>{t(lang, 'admin.section.gpu_policy')}</h3>
    </div>
    <div className="admin-policy-row">
      {all.length === 0 && <span className="admin-mute">no GPUs</span>}
      {all.map(i => <label key={i} className={`admin-gpu-toggle ${enabled.has(i) ? 'on' : 'off'}`}>
        <input type="checkbox" checked={enabled.has(i)} onChange={() => toggle(i)} />
        <span>GPU {i}</span>
      </label>)}
      <button className="btn primary" disabled={saving || !dirty} onClick={save}>
        {saving ? '…' : t(lang, 'admin.gpu_policy.save')}
      </button>
      {savedAt && !dirty && <span className="admin-saved">{t(lang, 'admin.gpu_policy.saved')}</span>}
    </div>
    <div className="admin-panel-hint">{t(lang, 'admin.gpu_policy.hint')}</div>
    {enabled.size === 0 && <div className="admin-warn-banner">{t(lang, 'admin.gpu_policy.empty_warn')}</div>}
    {err && <div className="admin-err">{err}</div>}
  </div>;
}

// SVG 双 Y 轴折线图：左轴 = run count（蓝实线）；右轴 = cost USD（橙实线）。
// X 轴 = period 字符串（升序）。pattern 抄 result-curve-compare.jsx 的 CurveCompare —
// 内联 polyline + 手工 ticks，不依赖任何 chart lib（fresh repo 无 package.json）。
//
// 输入：byPeriod = list[{period: str, n: int, cost_usd: float}]，已 ASC 排序。
// 空数据 → 渲染 empty 文案。
function UsagePeriodChart({ byPeriod, lang, bucket }) {
  if (!byPeriod || byPeriod.length === 0) {
    return <div className="admin-empty admin-usage-chart-empty">{t(lang, 'admin.usage.chart.empty')}</div>;
  }
  const width = 720, height = 280;
  const m = { top: 24, right: 56, bottom: 56, left: 56 };
  const innerW = width - m.left - m.right;
  const innerH = height - m.top - m.bottom;

  // Y 边界
  let nMax = 0, costMax = 0;
  for (const r of byPeriod) {
    if (r.n > nMax) nMax = r.n;
    if (r.cost_usd > costMax) costMax = r.cost_usd;
  }
  if (nMax <= 0) nMax = 1;
  if (costMax <= 0) costMax = 0.001;
  // 给顶部 10% 留白避免顶到 chart 边
  nMax = nMax * 1.10;
  costMax = costMax * 1.10;

  const N = byPeriod.length;
  // X：等距铺开；只 1 个点时居中
  const xAt = (i) => N === 1 ? m.left + innerW / 2 : m.left + (i / (N - 1)) * innerW;
  const yN = (n) => m.top + innerH - (n / nMax) * innerH;
  const yCost = (c) => m.top + innerH - (c / costMax) * innerH;

  // X tick: 当 N 大时 stride，最多展示 ~10 个 label
  const stride = Math.max(1, Math.ceil(N / 10));
  const xTicks = [];
  for (let i = 0; i < N; i += stride) xTicks.push(i);
  if (xTicks[xTicks.length - 1] !== N - 1) xTicks.push(N - 1);

  // Y ticks: 5 evenly spaced
  const nTicks = [];
  const costTicks = [];
  for (let i = 0; i <= 5; i++) {
    nTicks.push((nMax * i / 5));
    costTicks.push((costMax * i / 5));
  }

  // 折线点串
  const nPoints = byPeriod.map((r, i) => `${xAt(i).toFixed(1)},${yN(r.n).toFixed(1)}`).join(' ');
  const costPoints = byPeriod.map((r, i) => `${xAt(i).toFixed(1)},${yCost(r.cost_usd).toFixed(1)}`).join(' ');

  // 长 range UX 提示
  const dense = (bucket === 'day' && N > 90)
    ? t(lang, 'admin.usage.chart.dense_day')
    : (bucket === 'week' && N > 52)
    ? t(lang, 'admin.usage.chart.dense_week')
    : null;

  // 总和（chart 下方汇总）
  const sumN = byPeriod.reduce((a, r) => a + r.n, 0);
  const sumCost = byPeriod.reduce((a, r) => a + r.cost_usd, 0);

  return <div className="admin-usage-chart">
    {dense && <div className="admin-warn-banner admin-usage-chart-dense">{dense}</div>}
    <svg viewBox={`0 0 ${width} ${height}`} className="admin-usage-chart-svg" preserveAspectRatio="xMidYMid meet">
      {/* 双 Y 轴 */}
      <line x1={m.left} y1={m.top + innerH} x2={m.left + innerW} y2={m.top + innerH} stroke="var(--ink-4, #999)" strokeWidth="1" />
      <line x1={m.left} y1={m.top} x2={m.left} y2={m.top + innerH} stroke="var(--brand, #2c66c0)" strokeWidth="1" />
      <line x1={m.left + innerW} y1={m.top} x2={m.left + innerW} y2={m.top + innerH} stroke="var(--warn, #d97706)" strokeWidth="1" />
      {/* 左 Y ticks (run count) */}
      {nTicks.map((v, i) => <g key={`nt${i}`}>
        <line x1={m.left} y1={yN(v)} x2={m.left + innerW} y2={yN(v)} stroke="var(--ink-4, #ddd)" strokeOpacity="0.18" />
        <line x1={m.left - 4} y1={yN(v)} x2={m.left} y2={yN(v)} stroke="var(--ink-4, #999)" />
        <text x={m.left - 8} y={yN(v) + 3} textAnchor="end" fontSize="10" fill="var(--brand, #2c66c0)">{Math.round(v)}</text>
      </g>)}
      {/* 右 Y ticks (cost USD) */}
      {costTicks.map((v, i) => <g key={`ct${i}`}>
        <line x1={m.left + innerW} y1={yCost(v)} x2={m.left + innerW + 4} y2={yCost(v)} stroke="var(--ink-4, #999)" />
        <text x={m.left + innerW + 8} y={yCost(v) + 3} textAnchor="start" fontSize="10" fill="var(--warn, #d97706)">${v.toFixed(2)}</text>
      </g>)}
      {/* X ticks + label */}
      {xTicks.map(i => <g key={`xt${i}`}>
        <line x1={xAt(i)} y1={m.top + innerH} x2={xAt(i)} y2={m.top + innerH + 4} stroke="var(--ink-4, #999)" />
        {/* 旋转 label 避免拥挤 */}
        <text x={xAt(i)} y={m.top + innerH + 8}
          textAnchor="end" fontSize="10" fill="var(--ink-3, #666)"
          transform={`rotate(-45 ${xAt(i)} ${m.top + innerH + 8})`}>
          {byPeriod[i].period}
        </text>
      </g>)}
      {/* run count 折线（蓝实线 + 端点） */}
      <polyline points={nPoints} fill="none" stroke="var(--brand, #2c66c0)" strokeWidth="1.8" />
      {byPeriod.map((r, i) => <circle key={`np${i}`}
        cx={xAt(i)} cy={yN(r.n)} r="3" fill="var(--brand, #2c66c0)" stroke="white" strokeWidth="1">
        <title>{`${r.period} · ${r.n} runs · $${r.cost_usd.toFixed(4)}`}</title>
      </circle>)}
      {/* cost 折线（橙实线 + 端点） */}
      <polyline points={costPoints} fill="none" stroke="var(--warn, #d97706)" strokeWidth="1.8" />
      {byPeriod.map((r, i) => <circle key={`cp${i}`}
        cx={xAt(i)} cy={yCost(r.cost_usd)} r="3" fill="var(--warn, #d97706)" stroke="white" strokeWidth="1">
        <title>{`${r.period} · $${r.cost_usd.toFixed(4)} · ${r.n} runs`}</title>
      </circle>)}
      {/* legend */}
      <g transform={`translate(${m.left + 12}, ${m.top + 4})`}>
        <line x1="0" y1="0" x2="22" y2="0" stroke="var(--brand, #2c66c0)" strokeWidth="1.8" />
        <text x="26" y="3" fontSize="10" fill="var(--ink-2, #444)">{t(lang, 'admin.usage.chart.runs')}</text>
        <line x1="120" y1="0" x2="142" y2="0" stroke="var(--warn, #d97706)" strokeWidth="1.8" />
        <text x="146" y="3" fontSize="10" fill="var(--ink-2, #444)">{t(lang, 'admin.usage.chart.cost')}</text>
      </g>
    </svg>
    <div className="admin-usage-chart-summary">
      <span className="admin-mute">{byPeriod.length} {t(lang, bucket === 'day' ? 'admin.usage.bucket.day' : bucket === 'week' ? 'admin.usage.bucket.week' : 'admin.usage.bucket.month').toLowerCase()}</span>
      <span> · </span>
      <span>{sumN} {t(lang, 'admin.usage.chart.runs')}</span>
      <span> · </span>
      <span>${sumCost.toFixed(2)}</span>
    </div>
  </div>;
}

function UsagePanel({ data, since, setSince, bucket, setBucket, onRefresh, lang }) {
  if (!data) return <div className="admin-empty">{t(lang, 'admin.error.fetch')}</div>;
  const total = data.total || {};
  const byStatus = data.by_status || [];
  const byModel = data.by_model || [];
  const byOwner = data.by_owner || [];
  const byPeriod = data.by_period || [];

  // by_status → 直接 chip 列表（done/running/queued/failed/cancelled/evaluating）
  const statusOrder = ['done', 'running', 'evaluating', 'queued', 'failed', 'cancelled'];
  const statusMap = Object.fromEntries(byStatus.map(r => [r.status, r.n]));

  return <div className="admin-panel">
    <div className="admin-panel-head">
      <h3>{t(lang, 'admin.section.usage')}</h3>
      <div className="admin-usage-controls">
        <span className="admin-mute">{t(lang, 'admin.usage.since')}:</span>
        <select value={since} onChange={e => setSince(e.target.value)}>
          <option value="7d">{t(lang, 'admin.usage.since.7d')}</option>
          <option value="30d">{t(lang, 'admin.usage.since.30d')}</option>
          <option value="all">{t(lang, 'admin.usage.since.all')}</option>
        </select>
        <span className="admin-mute admin-usage-bucket-label">{t(lang, 'admin.usage.bucket.label')}:</span>
        <div className="admin-usage-bucket-group" role="group" aria-label="bucket">
          {['day', 'week', 'month'].map(b =>
            <button
              key={b}
              type="button"
              className={`admin-usage-bucket-btn ${bucket === b ? 'is-active' : ''}`}
              onClick={() => setBucket(b)}>
              {t(lang, `admin.usage.bucket.${b}`)}
            </button>
          )}
        </div>
        <button className="btn" onClick={onRefresh}>{t(lang, 'admin.usage.refresh')}</button>
      </div>
    </div>

    {/* 总计卡 + by_status */}
    <div className="admin-totals">
      <div className="admin-stat">
        <span className="admin-stat-label">{t(lang, 'admin.usage.total')} {t(lang, 'admin.col.runs')}</span>
        <span className="admin-stat-val">{total.n ?? 0}</span>
      </div>
      <div className="admin-stat">
        <span className="admin-stat-label">{t(lang, 'admin.col.cost')}</span>
        <span className="admin-stat-val">{fmtUsd(total.cost)}</span>
      </div>
      <div className="admin-stat">
        <span className="admin-stat-label">{t(lang, 'admin.col.input_tokens')}</span>
        <span className="admin-stat-val">{fmtTokens(total.input_tokens)}</span>
      </div>
      <div className="admin-stat">
        <span className="admin-stat-label">{t(lang, 'admin.col.output_tokens')}</span>
        <span className="admin-stat-val">{fmtTokens(total.output_tokens)}</span>
      </div>
      <div className="admin-stat">
        <span className="admin-stat-label">{t(lang, 'admin.col.wall')}</span>
        <span className="admin-stat-val">{fmtSec(total.wall)}</span>
      </div>
      <div className="admin-stat">
        <span className="admin-stat-label">{t(lang, 'admin.col.qe_wall')}</span>
        <span className="admin-stat-val">{fmtSec(total.qe_wall)}</span>
      </div>
    </div>

    {byStatus.length > 0 && <div className="admin-status-row">
      {statusOrder.filter(s => statusMap[s]).map(s =>
        <span key={s} className={`admin-status-chip status-${s}`}>{s}: {statusMap[s]}</span>)}
      {Object.keys(statusMap).filter(s => !statusOrder.includes(s)).map(s =>
        <span key={s} className="admin-status-chip">{s}: {statusMap[s]}</span>)}
    </div>}

    {/* 时间序列双线图（cost USD + run count）按 bucket 切桶 */}
    <div className="admin-subpanel admin-usage-chart-wrap">
      <h4>{t(lang, 'admin.usage.chart.title')}</h4>
      <UsagePeriodChart byPeriod={byPeriod} lang={lang} bucket={bucket} />
    </div>

    {byOwner.length > 0 && <div className="admin-subpanel">
      <h4>{t(lang, 'admin.section.usage_by_owner')}</h4>
      <table className="admin-table">
        <thead>
          <tr>
            <th>{t(lang, 'admin.col.owner')}</th>
            <th>{t(lang, 'admin.col.runs')}</th>
            <th>{t(lang, 'admin.col.cost')}</th>
            <th>{t(lang, 'admin.col.input_tokens')}</th>
            <th>{t(lang, 'admin.col.output_tokens')}</th>
            <th>{t(lang, 'admin.col.wall')}</th>
            <th>{t(lang, 'admin.col.qe_wall')}</th>
          </tr>
        </thead>
        <tbody>
          {byOwner.map(r => <tr key={r.owner}>
            <td className="mono">{r.owner}</td>
            <td>{r.n}</td>
            <td>{fmtUsd(r.cost)}</td>
            <td>{fmtTokens(r.input_tokens)}</td>
            <td>{fmtTokens(r.output_tokens)}</td>
            <td>{fmtSec(r.wall)}</td>
            <td>{fmtSec(r.qe_wall)}</td>
          </tr>)}
        </tbody>
      </table>
    </div>}

    {byModel.length > 0 && <div className="admin-subpanel">
      <h4>{t(lang, 'admin.section.usage_by_model')}</h4>
      <table className="admin-table">
        <thead>
          <tr>
            <th>{t(lang, 'admin.col.model')}</th>
            <th>{t(lang, 'admin.col.runs')}</th>
            <th>{t(lang, 'admin.col.cost')}</th>
            <th>{t(lang, 'admin.col.input_tokens')}</th>
            <th>{t(lang, 'admin.col.output_tokens')}</th>
            <th>$ / 1M in</th>
            <th>$ / 1M out</th>
          </tr>
        </thead>
        <tbody>
          {byModel.map(r => {
            // per-token unit cost (USD per 1M tokens) —— 通常前端配置里有 ModelInfo 价格表，
            // 但聚合数据已 collapse 成总额；这里直接 derive from 总额。
            // 注意：不区分 input/output 单价（聚合层面没法分，输入输出都按 cost 比例摊回），
            // 所以这两个列只是"总成本回算到 1M token"的 sanity check —— 用作 cost-per-token
            // 异常监测（某 model row $/1M 比正常高 → 该 model 可能价格变了或者用量倾斜）。
            const inMillion = (r.input_tokens || 0) / 1_000_000;
            const outMillion = (r.output_tokens || 0) / 1_000_000;
            const totalT = inMillion + outMillion;
            const perInM = totalT > 0 ? (r.cost * inMillion / totalT / inMillion) : 0;
            const perOutM = totalT > 0 ? (r.cost * outMillion / totalT / outMillion) : 0;
            return <tr key={r.model}>
              <td className="mono">{r.model}</td>
              <td>{r.n}</td>
              <td>{fmtUsd(r.cost)}</td>
              <td>{fmtTokens(r.input_tokens)}</td>
              <td>{fmtTokens(r.output_tokens)}</td>
              <td>{inMillion > 0 ? fmtUsd(perInM) : '—'}</td>
              <td>{outMillion > 0 ? fmtUsd(perOutM) : '—'}</td>
            </tr>;
          })}
        </tbody>
      </table>
    </div>}

    {byOwner.length === 0 && byModel.length === 0 && byStatus.length === 0
      && <div className="admin-empty">{t(lang, 'admin.empty.usage')}</div>}
  </div>;
}

// Scheduler 控制（0505 从 runs 界面迁来 — pause/resume + max-parallel 都是全局
// 资源控制，dev only）。Pause 立即生效（running 跑完，queue 不再 promote）；
// max-parallel 调小时已 running 不受影响，新 promote 受新上限。state + handler
// 由 AdminPage 从 app.jsx 拿（共用 app.jsx 的 3s 轮询，避免双 poller 浪费）。
//
// Layout：单行两控件，inline label 内嵌——pause group（status chip + button）
// 与 max-parallel group（label + stepper）并列。配色走 theme tokens，
// 白夜模式自动适配。详细 tooltip 在按钮 / stepper 的 title 属性里。
function SchedulerControlPanel({ paused, onTogglePause, maxParallel, maxParallelLimit, setMaxParallel, lang }) {
  return <div className="admin-panel">
    <div className="admin-panel-head">
      <h3>{t(lang, 'admin.section.scheduler_control')}</h3>
    </div>
    <div className="admin-scheduler-row">
      {/* Pause / Resume — 合并 status + action：dot 颜色显当前状态，按钮文字显
          点击后的动作（macOS play/pause 同款语义）。 */}
      <button
        className={`admin-scheduler-toggle ${paused ? 'is-paused' : 'is-running'}`}
        onClick={onTogglePause}
        title={paused
          ? t(lang, 'admin.scheduler.resume_hint')
          : t(lang, 'admin.scheduler.pause_hint')}>
        <span className="admin-scheduler-toggle-dot" />
        {paused ? t(lang, 'admin.scheduler.resume_btn') : t(lang, 'admin.scheduler.pause_btn')}
      </button>
      {/* Max parallel */}
      <div className="admin-scheduler-group">
        <span className="admin-scheduler-inline-label">{t(lang, 'admin.max_parallel.label')}</span>
        <div className="admin-stepper" title={`1 – ${maxParallelLimit}`}>
          <button className="admin-stepper-btn" onClick={() => setMaxParallel(maxParallel - 1)}
            disabled={maxParallel <= 1} aria-label="decrement">−</button>
          <span className="admin-stepper-val">{maxParallel}</span>
          <button className="admin-stepper-btn" onClick={() => setMaxParallel(maxParallel + 1)}
            disabled={maxParallel >= maxParallelLimit} aria-label="increment">+</button>
        </div>
      </div>
    </div>
  </div>;
}


// 一键 pause + 把 running runs 用相同超参重新入队 + kill+delete 旧 run
function RecycleRunningPanel({ lang }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);   // { recycled: [], failed: [], paused }
  const [err, setErr] = useState(null);

  const onClick = async () => {
    if (!window.confirm(t(lang, 'admin.recycle.confirm'))) return;
    setBusy(true); setErr(null); setResult(null);
    try {
      const r = await window.__api.recycleRunning();
      setResult(r);
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  return <div className="admin-panel">
    <div className="admin-panel-head">
      <h3>{t(lang, 'admin.section.recycle')}</h3>
    </div>
    <div className="admin-panel-hint">{t(lang, 'admin.recycle.hint')}</div>
    <div className="admin-recycle-actions">
      <button className="admin-recycle-btn" onClick={onClick} disabled={busy}>
        {busy ? '…' : t(lang, 'admin.recycle.btn')}
      </button>
      {err && <span className="admin-warn">{err}</span>}
      {result && <span className="admin-recycle-result">
        {t(lang, 'admin.recycle.done').replace('{n}', result.recycled.length)}
        {result.failed.length > 0 && <> · <span className="admin-warn">
          {result.failed.length} failed
        </span></>}
      </span>}
    </div>
    {result && result.recycled.length > 0 && <div className="admin-recycle-list">
      {result.recycled.map(x => <div key={x.old_run_id} className="admin-recycle-row mono">
        <span className="admin-recycle-old">{x.old_run_id}</span>
        <span className="admin-recycle-arrow">→</span>
        <span className="admin-recycle-new">{x.new_run_id}</span>
        {x.task_id && <span className="admin-recycle-task">· {x.task_id}</span>}
        {x.owner && <span className="admin-recycle-owner">· {x.owner}</span>}
      </div>)}
    </div>}
    {result && result.failed.length > 0 && <div className="admin-recycle-list">
      {result.failed.map(x => <div key={x.run_id} className="admin-recycle-row mono admin-warn">
        ✗ {x.run_id}: {x.error_msg}
      </div>)}
    </div>}
  </div>;
}


function AdminPage({ lang, user, paused, onTogglePause, maxParallel, maxParallelLimit, setMaxParallel }) {
  const [cluster, setCluster] = useState(null);
  const [policy, setPolicy] = useState(null);
  const [qeGpuMode, setQeGpuMode] = useState(null);
  const [usage, setUsage] = useState(null);
  const [sincePreset, setSincePreset] = useState('7d');
  const [bucket, setBucket] = useState('day');
  const [error, setError] = useState(null);

  // 每个 sub-panel 走对应 cap；is_dev_role 自动 bypass 一切（hasCapability 内部处理）。
  // 后端每个 endpoint 也 enforce，前端只是隐藏 + 不发请求（不浪费 403 round-trip）。
  const canViewCluster = hasCapability(user, CAP_ADMIN_VIEW_CLUSTER);
  const canManageGpuPolicy = hasCapability(user, CAP_ADMIN_MANAGE_GPU_POLICY);
  const canManageScheduler = hasCapability(user, CAP_ADMIN_MANAGE_SCHEDULER);
  const canManageRunsBulk = hasCapability(user, CAP_ADMIN_MANAGE_RUNS_BULK);
  const canViewUsage = hasCapability(user, CAP_ADMIN_VIEW_USAGE);
  const canViewAny = canViewCluster || canManageGpuPolicy || canManageScheduler
    || canManageRunsBulk || canViewUsage;

  // 2s 自动 refresh cluster_state（后端 1s cache 配套；psutil 采样 200ms 消 jitter）
  useEffect(() => {
    if (!canViewCluster) return;
    let cancelled = false;
    const fetch = async () => {
      try {
        const c = await window.__api.getClusterState();
        if (!cancelled) setCluster(c);
      } catch (e) {
        if (!cancelled) setError(e.message || String(e));
      }
    };
    fetch();
    const id = setInterval(fetch, 2000);
    return () => { cancelled = true; clearInterval(id); };
  }, [canViewCluster]);

  // policy 一次拉拽 + save 后重拉（save 已在 GpuPolicyPanel 里调 onSave 触发）
  const fetchPolicy = async () => {
    try {
      const p = await window.__api.getGpuPolicy();
      setPolicy(p);
    } catch (e) { setError(e.message || String(e)); }
  };
  useEffect(() => { if (canManageGpuPolicy) fetchPolicy(); }, [canManageGpuPolicy]);

  const fetchQeGpuMode = async () => {
    try {
      const m = await window.__api.getQeGpuMode();
      setQeGpuMode(m);
    } catch (e) { setError(e.message || String(e)); }
  };
  useEffect(() => { if (canManageGpuPolicy) fetchQeGpuMode(); }, [canManageGpuPolicy]);

  // usage 初始 + since/bucket 切换 + 手动 refresh
  const fetchUsage = async (preset, b) => {
    const since = sinceFromPreset(preset || sincePreset);
    try {
      const u = await window.__api.getUsageStats(since, b || bucket);
      setUsage(u);
    } catch (e) { setError(e.message || String(e)); }
  };
  useEffect(() => {
    if (canViewUsage) fetchUsage(sincePreset, bucket);
  }, [canViewUsage, sincePreset, bucket]);

  if (!canViewAny) {
    return <div className="admin-page">
      <div className="admin-empty">{t(lang, 'admin.error.forbidden')}</div>
    </div>;
  }

  return <div className="admin-page">
    <div className="admin-head">
      <h2>{t(lang, 'admin.title')}</h2>
      <div className="admin-sub">{t(lang, 'admin.subtitle')}</div>
    </div>
    {error && <div className="admin-err">{error}</div>}
    {canViewCluster && <ClusterPanel data={cluster} lang={lang} />}
    {canManageScheduler && <SchedulerControlPanel
      paused={paused}
      onTogglePause={onTogglePause}
      maxParallel={maxParallel}
      maxParallelLimit={maxParallelLimit}
      setMaxParallel={setMaxParallel}
      lang={lang} />}
    {canManageRunsBulk && <RecycleRunningPanel lang={lang} />}
    {canManageGpuPolicy && <GpuPolicyPanel policy={policy} onSave={fetchPolicy} lang={lang} />}
    {canManageGpuPolicy && <QeGpuModePanel mode={qeGpuMode} onSave={fetchQeGpuMode} lang={lang} />}
    {canViewUsage && <UsagePanel data={usage} since={sincePreset} setSince={setSincePreset}
      bucket={bucket} setBucket={setBucket}
      onRefresh={() => fetchUsage(sincePreset, bucket)} lang={lang} />}
  </div>;
}

window.AdminPage = AdminPage;
