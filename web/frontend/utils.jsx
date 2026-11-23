// Shared utilities and primitive components

const { useState, useEffect, useRef, useMemo, useCallback } = React;

function t(lang, key, vars) {
  const raw = (window.I18N[lang] && window.I18N[lang][key]) ?? window.I18N.en[key] ?? key;
  if (!vars) return raw;
  return raw.replace(/\{(\w+)\}/g, (_, k) => (vars[k] !== undefined ? vars[k] : `{${k}}`));
}

function fmtRel(iso, lang='en') {
  if (!iso) return lang === 'zh' ? '从未' : 'never';
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 1) return lang === 'zh' ? '刚刚' : 'just now';
  if (m < 60) return lang === 'zh' ? `${m} 分钟前` : `${m}m ago`;
  const h = Math.floor(m/60);
  if (h < 24) return lang === 'zh' ? `${h} 小时前` : `${h}h ago`;
  const d = Math.floor(h/24);
  if (d < 7) return lang === 'zh' ? `${d} 天前` : `${d}d ago`;
  const w = Math.floor(d/7);
  if (w < 4) return lang === 'zh' ? `${w} 周前` : `${w}w ago`;
  return lang === 'zh' ? `${Math.floor(d/30)} 月前` : `${Math.floor(d/30)}mo ago`;
}

function fmtSec(sec) {
  if (sec == null) return '—';
  if (sec < 60) return `${sec.toFixed(0)}s`;
  if (sec < 3600) {
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    return `${m}m ${s.toString().padStart(2,'0')}s`;
  }
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return `${h}h ${m.toString().padStart(2,'0')}m`;
}

function fmtElapsed(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m.toString().padStart(2,'0')}:${s.toString().padStart(2,'0')}`;
}

function fmtBytes(b) {
  if (b == null) return '';
  if (b < 1024) return `${b} B`;
  if (b < 1024*1024) return `${(b/1024).toFixed(1)} KB`;
  return `${(b/1024/1024).toFixed(2)} MB`;
}

// USD cost：< $0.01 显示 <0.01；< $1 显示 4 位小数（agent 跑大多在 $0.001-$0.5 区间，2 位精度不够）；
// >= $1 显示 2 位小数。null → "—"。
function fmtUsd(c) {
  if (c == null) return '—';
  if (c < 0.01) return '<$0.01';
  if (c < 1)    return `$${c.toFixed(4)}`;
  return `$${c.toFixed(2)}`;
}

// token 总数：< 1k 直接数；< 1M "12.3k"；>= 1M "1.2M"
function fmtTokens(n) {
  if (n == null) return '—';
  if (n < 1000)     return String(n);
  if (n < 1_000_000) return `${(n/1000).toFixed(1)}k`;
  return `${(n/1_000_000).toFixed(2)}M`;
}

function scoreClass(s) {
  if (s == null) return 'none';
  if (s >= 0.8) return 'pass';
  if (s >= 0.55) return 'partial';
  return 'fail';
}

function scoreFieldClass(s) {
  if (s == null) return 'none';
  if (s >= 0.9) return 'pass';
  if (s >= 0.7) return 'partial';
  return 'fail';
}

function StatusBadge({ status, lang }) {
  return <span className={`badge ${status}`}>{t(lang, `run.status.${status}`)}</span>;
}

// status='failed' / 'cancelled' 渲染独立 pill，区别 score==null（"—"）。
// 现有 .score-mini.fail 是低分（< 0.55）红色；run-failed/cancelled 走独立 .status-pill class
// 避免 class 名冲突。
function ScoreMini({ score, status }) {
  if (status === 'failed')    return <span className="status-pill failed">fail</span>;
  if (status === 'cancelled') return <span className="status-pill cancelled">cancel</span>;
  if (score == null)          return <span className="score-mini none">—</span>;
  return <span className={`score-mini ${scoreClass(score)}`}>{score.toFixed(2)}</span>;
}

// Renders a value the way the contract calls out: number → {val} {unit} / string → as-is / dict → <dict, N keys>
function fmtPred(v) {
  if (v == null) return '—';
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(4);
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'string') return v;
  if (Array.isArray(v)) return `[${v.length} items]`;
  if (typeof v === 'object') return `<dict, ${Object.keys(v).length} keys>`;
  return String(v);
}

// trivial word-level diff highlighter
function diffWords(a, b) {
  const aw = a.split(/(\s+)/);
  const bw = b.split(/(\s+)/);
  const aSet = new Set(aw.filter(x => x.trim()));
  const bSet = new Set(bw.filter(x => x.trim()));
  const removed = aw.map((w, i) => ({ w, type: w.trim() && !bSet.has(w) ? 'del' : 'eq' }));
  const added   = bw.map((w, i) => ({ w, type: w.trim() && !aSet.has(w) ? 'add' : 'eq' }));
  return { removed, added };
}

function DiffSpan({ tokens }) {
  return <>{tokens.map((tok, i) => {
    if (tok.type === 'del') return <span key={i} className="diff-del">{tok.w}</span>;
    if (tok.type === 'add') return <span key={i} className="diff-add">{tok.w}</span>;
    return <span key={i}>{tok.w}</span>;
  })}</>;
}

function Tip({ tipKey, lang, children }) {
  return <span className="has-tip" data-tip={t(lang, tipKey)}>{children}</span>;
}

// 归一 gt_quality：bench v1.3 + 0506 tetra rerun，实际有 8 种值
// （self_run_converged / marginal_convergence[_*pct] / self_run_failed_kept_mp /
// self_run_not_converged_kept_mp / self_run_tetra_rescued / self_run_tetra_failed）；
// 前端归 5 类视觉。
function normalizeGtQuality(q) {
  if (!q) return 'unknown';
  if (q === 'self_run_converged') return 'self_run_converged';
  if (q === 'self_run_tetra_rescued') return 'tetra_rescued';
  if (q === 'self_run_tetra_failed') return 'tetra_failed';
  if (q.startsWith('marginal')) return 'marginal';
  if (q.includes('kept_mp')) return 'keep_mp';
  return 'unknown';
}

// 通用 toolbar pill 形 dropdown — 比 column header 紧凑，专门给 toolbar 行用。
// options 支持 string[]（'all', 'M1', ...）或 {value, label}[]。
// value 与 options[0] 不同时视为 active（高亮 + accent border）。
// 点击 toggle 展开 inline menu，点选项 / 点外部 自动收。
function FilterPill({ label, value, options, onChange }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);
  const norm = (o) => typeof o === 'object' ? o : { value: o, label: o };
  const items = options.map(norm);
  const defaultValue = items[0]?.value;
  const isActive = value !== defaultValue;
  const current = items.find(o => o.value === value) || items[0];
  return <div ref={ref} className={`chip-dropdown ${isActive ? 'active' : ''} ${open ? 'open' : ''}`}
    onClick={() => setOpen(!open)}>
    {label && <span className="chip-label">{label}</span>}
    <span className="chip-value">{current?.label}</span>
    <span className="chip-arrow">▾</span>
    {open && <div className="filter-menu" onClick={e => e.stopPropagation()}>
      {items.map(opt =>
        <div key={opt.value}
          className={`filter-menu-item ${opt.value === value ? 'selected' : ''}`}
          onClick={() => { onChange(opt.value); setOpen(false); }}>
          <span className="check">{opt.value === value ? '✓' : ''}</span>
          <span>{opt.label}</span>
        </div>)}
    </div>}
  </div>;
}

// Priority 共享配置：UI label + 视觉颜色 + 后端 string。urgent 走 submit.urgent_priority capability。
const PRIORITY_OPTIONS = [
  { value: 'normal', label: '普通',  shortLabel: 'N', color: 'var(--ink-3)',     bg: 'var(--paper-3)' },
  { value: 'high',   label: '优先',  shortLabel: 'H', color: '#b45309',           bg: 'rgba(245,158,11,0.12)' },
  { value: 'urgent', label: '紧急',  shortLabel: 'U', color: 'var(--err)',        bg: 'rgba(220,38,38,0.12)', devOnly: true },
];

// isDev prop 名保留（语义一致）：true 表示用户可选 urgent
//   ——dev_role bypass 或 持有 submit.urgent_priority capability。
// 由调用方用 hasCapability(user, CAP_SUBMIT_URGENT_PRIORITY) 计算后传入。
function PrioritySelect({ value, onChange, isDev = false, size = 'md' }) {
  // segmented 3-button；无 urgent 权限时 urgent 按钮 disabled
  const opts = PRIORITY_OPTIONS;
  const sm = size === 'sm';
  return <div style={{ display: 'inline-flex', border: '1px solid var(--rule)',
                       borderRadius: 6, overflow: 'hidden' }}>
    {opts.map((o, i) => {
      const disabled = o.devOnly && !isDev;
      const selected = value === o.value;
      return <button key={o.value} type="button"
        disabled={disabled}
        onClick={() => !disabled && onChange(o.value)}
        title={disabled ? `${o.label}: 需 submit.urgent_priority 权限` : o.label}
        style={{
          padding: sm ? '2px 8px' : '4px 12px',
          border: 'none',
          borderLeft: i > 0 ? '1px solid var(--rule)' : 'none',
          background: selected ? o.bg : 'transparent',
          color: disabled ? 'var(--ink-4)' : (selected ? o.color : 'var(--ink-2)'),
          fontFamily: 'var(--sans)', fontSize: sm ? 11 : 12,
          fontWeight: selected ? 600 : 400,
          cursor: disabled ? 'not-allowed' : 'pointer',
          opacity: disabled ? 0.5 : 1,
        }}>{o.label}</button>;
    })}
  </div>;
}

function priorityMeta(value) {
  return PRIORITY_OPTIONS.find(o => o.value === value) || PRIORITY_OPTIONS[0];
}

// F2 cost preview 行 — RunConfig（单题）共用。
// preview = null 时若 loading 显 "Calculating…"，否则不渲染（不展示 stale 值）。
// preview.source: 'history' → "Based on N past runs"；'heuristic' → "Rough estimate"。
// 视觉：紧凑两行（est_usd 一行 + caveat 一行），right-aligned 跟 launch 按钮同栏。
function CostPreviewRow({ loading, preview, lang }) {
  if (!loading && !preview) return null;
  return <div style={{
    fontSize: 11, lineHeight: 1.4, color: 'var(--ink-3)',
    textAlign: 'right', minHeight: 30,
  }}>
    {loading
      ? <div className="mono">{t(lang, 'cost_preview.loading')}</div>
      : <>
        <div>
          <span style={{ color: 'var(--ink-2)' }}>{t(lang, 'cost_preview.label')}: </span>
          <span className="mono" style={{ color: 'var(--ink-1)', fontWeight: 600 }}>≈ {fmtUsd(preview.est_usd)}</span>
        </div>
        <div style={{ fontSize: 10 }}>
          {preview.source === 'history'
            ? t(lang, 'cost_preview.history', { n: preview.n_samples })
            : t(lang, 'cost_preview.heuristic')}
        </div>
      </>}
  </div>;
}

// F2 cost preview 批量 total —— ComposeBatchDialog 共用。
// preview = { per_task, total_est_usd, n_with_history, n_heuristic }
// 显示：总额 + breakdown（多少题有历史 / 多少题走启发式）。
function CostPreviewBatchTotal({ loading, preview, taskCount, lang }) {
  if (!loading && !preview) return null;
  return <div style={{
    fontSize: 12, lineHeight: 1.5, color: 'var(--ink-2)',
    padding: '8px 10px', background: 'var(--paper-3)',
    borderRadius: 6, marginTop: 8,
  }}>
    {loading
      ? <div className="mono" style={{ color: 'var(--ink-3)' }}>{t(lang, 'cost_preview.loading')}</div>
      : <>
        <div>{t(lang, 'cost_preview.batch.total', {
          total: fmtUsd(preview.total_est_usd),
          n: taskCount,
        })}</div>
        <div style={{ fontSize: 11, color: 'var(--ink-3)', marginTop: 2 }}>
          {t(lang, 'cost_preview.batch.breakdown', {
            n_history: preview.n_with_history,
            n_heuristic: preview.n_heuristic,
          })}
        </div>
      </>}
  </div>;
}

Object.assign(window, { t, fmtRel, fmtSec, fmtElapsed, fmtBytes, fmtUsd, fmtTokens,
  scoreClass, scoreFieldClass, StatusBadge, ScoreMini, fmtPred,
  diffWords, DiffSpan, Tip, normalizeGtQuality, FilterPill,
  PRIORITY_OPTIONS, PrioritySelect, priorityMeta,
  CostPreviewRow, CostPreviewBatchTotal,
  useState, useEffect, useRef, useMemo, useCallback });
