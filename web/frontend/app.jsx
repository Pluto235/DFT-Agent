// Main App — TopBar, RunStrip, navigation, run lifecycle, Tweaks panel

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "showTooltips": true,
  "playbackSpeed": 1,
  "dataScale": 172,
  "forceRunState": "auto",
  "resultMode": "auto",
  "batchFailureRate": "random"
}/*EDITMODE-END*/;

// UserPill：显 display_name + role badge（dev 用 accent，normal 用 muted）。
// 点击展开下拉，含 logout 按钮。多租户 only：disabled mode 下 user.owner='localhost' 也照样渲染，
// 让用户知道自己当前在以谁的身份操作。
function UserPill({ user }) {
  const [open, setOpen] = useState(false);
  if (!user) return null;
  const isDev = isDevRole(user);
  // F3 Step 1+ 后端不再返回 user.role；这里把 dev_role 翻成显示用 label
  // （后端 capability gate 才是真正的权限来源；该 label 只是 UI 提示）。
  const roleLabel = isDev ? 'dev' : 'normal';
  const onLogout = () => {
    try { window.sessionStorage.removeItem('AUTH_TOKEN'); } catch (_e) {}
    try { window.dispatchEvent(new Event('auth:logout')); } catch (_e) {}
  };
  return <div className="user-pill" style={{ position: 'relative', marginLeft: 8 }}>
    <button onClick={() => setOpen(o => !o)} style={{
      display: 'flex', alignItems: 'center', gap: 6, padding: '4px 10px',
      border: '1px solid var(--rule)', borderRadius: 6, background: 'transparent',
      color: 'var(--ink)', cursor: 'pointer', fontFamily: 'var(--sans)', fontSize: 12,
    }} title={`signed in as ${user.owner} (${roleLabel})`}>
      <span style={{ maxWidth: 120, overflow: 'hidden', textOverflow: 'ellipsis',
                     whiteSpace: 'nowrap' }}>{user.display_name || user.owner}</span>
      <span className="role-badge" style={{
        padding: '1px 6px', borderRadius: 8, fontSize: 10, fontWeight: 600,
        textTransform: 'uppercase', letterSpacing: 0.5,
        background: isDev ? 'var(--accent)' : 'var(--paper-3)',
        color: isDev ? 'var(--paper)' : 'var(--ink-2)',
      }}>{roleLabel}</span>
      <span style={{ color: 'var(--ink-3)', fontSize: 10 }}>{open ? '▲' : '▼'}</span>
    </button>
    {open && <div style={{
      position: 'absolute', right: 0, top: 'calc(100% + 6px)',
      minWidth: 200, padding: 12, zIndex: 100,
      background: 'var(--paper)', border: '1px solid var(--rule)', borderRadius: 8,
      boxShadow: '0 8px 28px rgba(0,0,0,0.18)',
      fontFamily: 'var(--sans)', fontSize: 12, color: 'var(--ink-2)',
    }}>
      <div style={{ marginBottom: 8 }}>
        <div style={{ color: 'var(--ink-3)', fontSize: 10, textTransform: 'uppercase' }}>owner</div>
        <div className="mono" style={{ color: 'var(--ink)' }}>{user.owner}</div>
      </div>
      <div style={{ marginBottom: 10 }}>
        <div style={{ color: 'var(--ink-3)', fontSize: 10, textTransform: 'uppercase' }}>role</div>
        <div className="mono" style={{ color: 'var(--ink)' }}>{roleLabel}{isDev ? ' (sees dev tasks)' : ''}</div>
      </div>
      <button onClick={onLogout} style={{
        width: '100%', padding: '6px 10px', border: '1px solid var(--rule)',
        borderRadius: 6, background: 'transparent', color: 'var(--err)',
        cursor: 'pointer', fontFamily: 'var(--sans)', fontSize: 12,
      }}>Sign out</button>
    </div>}
  </div>;
}


function TopBar({ state, setState, lang, setLang, theme, setTheme, openTweaks, onCleanupToggle, user }) {
  const cfg = state.config;
  const settingsOpen = state.settingsOpen;
  // health 驱动 LED：null = backend unreachable（红）；任一关键 path 缺失 = degraded（黄）；
  // 全 ok = 正常（绿，用 var(--ok)）。文字也随 health 切换：
  //   - 正常：'{visible_count} tasks'（按当前角色 is_stable 过滤后的可见数）
  //   - degraded：'harness/solver missing'
  //   - 不可达：'backend down'
  const h = state.health;
  // /api/tasks 已按 role 过滤（normal 仅见 is_stable=true，dev 见全部）；
  // state.tasks 加载前 fallback 到 health.tasks_loaded（=cache 全集）以避免空状态闪 0。
  const visibleTasksLoaded = Array.isArray(state.tasks) && state.tasks.length > 0;
  const visibleTaskCount = visibleTasksLoaded ? state.tasks.length : (h?.tasks_loaded ?? 0);
  let ledColor = 'var(--err)', apiText = 'backend down', apiTitle = 'backend /health unreachable';
  if (h) {
    const allOk = h.harness_ok && h.solver_ok && h.bench_questions_ok && h.anthropic_api_key_present;
    if (allOk) {
      ledColor = 'var(--ok)';
      apiText = `${visibleTaskCount} tasks`;
      const roleHint = isDevRole(user)
        ? 'dev role: 全部题目（含 is_stable=false）'
        : 'normal role: 仅 is_stable=true';
      apiTitle = `harness ${h.harness_path}\nsolver ${h.solver_path}\nquestions ${h.bench_questions_dir}\n\nvisible: ${visibleTaskCount} (${roleHint})\nbench cache: ${h.tasks_loaded}`;
    } else {
      ledColor = '#d97706'; // amber
      const missing = [];
      if (!h.harness_ok) missing.push('harness');
      if (!h.solver_ok) missing.push('solver');
      if (!h.bench_questions_ok) missing.push('questions');
      if (!h.anthropic_api_key_present) missing.push('ANTHROPIC_API_KEY');
      apiText = `missing: ${missing.join(', ')}`;
      apiTitle = `degraded — ${missing.length} component(s) not ready`;
    }
  }
  return <div className="topbar">
    <div className="brand">
      <img className="brand-logo" src="assets/favicon.svg" alt="DFT-Agent Bench" />
      <span className="brand-divider" />
      <span className="brand-product">{t(lang, 'app.brand')}</span>
      <span className="v">v0.1</span>
    </div>
    <div className="tabs">
      {(() => {
        const baseTabs = ['catalogue', 'compose', 'history', 'leaderboard'];
        // admin tab 可见性：只要持有任意 admin.* cap（或 is_dev_role）就显示——
        // 否则 dev 给 normal 授单个 admin sub-cap（如 admin.view_cluster）后，
        // normal 在前端找不到入口（user 0508 反馈修）。每个 sub-panel 内部还要按
        // 自己 cap render；后端每个 endpoint 也 enforce，前端只是隐藏 / 显示入口。
        // tokens tab 仅 is_dev_role 可见 —— 权限管理本身不在 cap 系统里。
        const isDev = isDevRole(user);
        const userCaps = user?.capabilities || [];
        const hasAnyAdminCap = isDev || userCaps.some(c => c.startsWith('admin.'));
        const tabs = [...baseTabs];
        if (hasAnyAdminCap) tabs.push('admin');
        if (isDev) tabs.push('tokens');
        return tabs.map(tab =>
          <button key={tab} className={`tab ${state.tab === tab || (tab === 'history' && state.tab === 'run') ? 'active' : ''}`}
            onClick={() => setState(s => ({ ...s, tab }))}>{t(lang, `tab.${tab}`)}</button>);
      })()}
    </div>
    <div className="spacer" />
    <div className="api-status" title={apiTitle}>
      <span className="led" style={{ background: ledColor }} />
      <span>{apiText}</span>
    </div>
    <button className="theme-toggle"
      onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
      title={theme === 'dark' ? 'Switch to light' : 'Switch to dark'}
      aria-label="Toggle theme">
      {theme === 'dark'
        ? <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="8" cy="8" r="3"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3.05 3.05l1.41 1.41M11.54 11.54l1.41 1.41M3.05 12.95l1.41-1.41M11.54 4.46l1.41-1.41"/></svg>
        : <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M13.5 9.5A5.5 5.5 0 0 1 6.5 2.5a5.5 5.5 0 1 0 7 7z"/></svg>
      }
    </button>
    <div className="lang-chip">
      <button className={lang === 'en' ? 'active' : ''} onClick={() => setLang('en')}>EN</button>
      <button className={lang === 'zh' ? 'active' : ''} onClick={() => setLang('zh')}>中</button>
    </div>
    <UserPill user={user} />
    <div className="settings-menu" style={{ position: 'relative' }}>
      <button className="settings-toggle" onClick={() => setState(s => ({ ...s, settingsOpen: !s.settingsOpen }))}
        style={{
          marginLeft: 8, padding: '4px 10px',
          border: '1px solid var(--rule)', borderRadius: 6, background: 'transparent',
          color: 'var(--ink-2)', cursor: 'pointer', fontFamily: 'var(--sans)', fontSize: 12,
        }}
        title="Settings">⚙ {settingsOpen ? '▲' : '▼'}</button>
      {settingsOpen && <div className="settings-dropdown" style={{
        position: 'absolute', right: 0, top: 'calc(100% + 6px)',
        minWidth: 320, maxWidth: 360, padding: 14, zIndex: 100,
        background: 'var(--paper)', border: '1px solid var(--rule)', borderRadius: 8,
        boxShadow: '0 8px 28px rgba(0,0,0,0.18)',
        fontFamily: 'var(--sans)', fontSize: 12, color: 'var(--ink-2)',
      }}>
        <div style={{ fontWeight: 600, marginBottom: 10, color: 'var(--ink)' }}>Settings</div>
        <label style={{ display: 'flex', alignItems: 'flex-start', gap: 10, cursor: 'pointer' }}>
          <input type="checkbox" checked={!!cfg.cleanup_intermediates_on_finish}
            onChange={(e) => onCleanupToggle(e.target.checked)} style={{ marginTop: 2 }} />
          <span style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 500 }}>Auto-cleanup intermediates</div>
            <div style={{ color: 'var(--ink-3)', marginTop: 2, lineHeight: 1.45 }}>
              Delete <span className="mono">workdir/tmp*</span> after each run (saves ~50–250 MB per QE run).
            </div>
            <div style={{ color: 'var(--ink-3)', marginTop: 4, lineHeight: 1.45 }}>
              Keeps: <span className="mono">.in</span>, <span className="mono">.out</span>, <span className="mono">transcript.json</span>, <span className="mono">report.json</span>.
            </div>
          </span>
        </label>
      </div>}
    </div>
  </div>;
}

function RunStrip({ runs, state, setState, lang, currentOwner = 'localhost' }) {
  const active = runs.filter(r => r.status === 'running' || r.status === 'evaluating');
  if (!active.length) return null;
  return <div className="runstrip">
    <span className="runstrip-label">{t(lang, 'runstrip.label')}</span>
    {active.map(r => {
      const elapsed = Math.floor((Date.now() - new Date(r.started_at || r.created_at).getTime()) / 1000);
      const pct = r.max_turns ? Math.min(100, (r.current_turn / r.max_turns) * 100) : 0;
      const formula = r.task_id ? r.task_id.split('_')[2] : 'custom';
      const ownerIsSelf = r.owner === currentOwner;
      const pmeta = priorityMeta(r.priority || 'normal');
      const stripExtra = (r.priority && r.priority !== 'normal')
        ? { borderLeft: `3px solid ${pmeta.color}` } : {};
      return <div key={r.run_id} className={`runstrip-card ${state.activeRun?.run_id === r.run_id ? 'live' : ''}`}
        style={stripExtra}
        onClick={() => setState(s => {
          // Don't take foreground ownership — that resets the run's progress to 0
          // and restarts the transcript animation. Only set viewedRunId so the user
          // can watch the existing progress; if the run is the current activeRun
          // (just-launched from this session), keep it as activeRun so the
          // foreground animator stays attached.
          const isActive = s.activeRun?.run_id === r.run_id;
          return { ...s, tab: 'run', centerTab: 'live',
            viewedRunId: r.run_id,
            activeRun: isActive ? s.activeRun : null };
        })}>
        <div className="row1">
          <div className="who">
            <span className="formula">{formula}</span>
            {(() => {
              // task_id 过长时（>30 字符）截断显示，鼠标 hover title 看完整 id
              const tid = r.task_id || 'custom';
              const shown = tid.length > 30 ? tid.slice(0, 30) + '…' : tid;
              return <span className="id" title={tid}>{shown}</span>;
            })()}
          </div>
          {r.owner && <span className="owner-chip"
            title={ownerIsSelf ? `owned by you (${r.owner})` : `owned by ${r.owner}`}
            style={{ padding: '0 5px', borderRadius: 3,
                     background: ownerIsSelf ? 'var(--paper-3)' : 'rgba(var(--accent-rgb,99,102,241),0.12)',
                     color: ownerIsSelf ? 'var(--ink-3)' : 'var(--accent)',
                     fontSize: 9, fontWeight: 500 }}>
            by {r.owner}
          </span>}
          {r.priority && r.priority !== 'normal' && <span title={pmeta.label}
            style={{ padding: '0 5px', borderRadius: 3,
                     background: pmeta.bg, color: pmeta.color,
                     fontSize: 9, fontWeight: 600 }}>{pmeta.label}</span>}
          <StatusBadge status={r.status} lang={lang} />
        </div>
        <div className="row2">
          <span className="tnum">{r.current_turn}/{r.max_turns}</span>
          <span style={{ flex: 1, height: 3, background: 'var(--paper-3)', borderRadius: 2, overflow: 'hidden' }}>
            <span style={{ display: 'block', height: '100%', width: `${pct}%`, background: 'var(--accent)' }} />
          </span>
          <span className="tnum">{fmtElapsed(elapsed)}</span>
        </div>
      </div>;
    })}
  </div>;
}

function CenterTabs({ state, setState, lang }) {
  // Display center tabs based on the active main tab
  let tabs = [];
  if (state.tab === 'catalogue') {
    tabs = [{ id: 'preview', label: t(lang, 'centertab.preview') }];
    if (state.viewedRunId) tabs.push({ id: 'result', label: t(lang, 'centertab.result') });
  } else if (state.tab === 'run') {
    tabs = [{ id: 'config', label: t(lang, 'centertab.config') }, { id: 'live', label: t(lang, 'centertab.live') }];
    const finishedStatuses = ['done', 'failed', 'cancelled'];
    const showResult = state.centerTab === 'result'
      || finishedStatuses.includes(state.activeRun?.status)
      || (state.viewedRunId && state.runs.find(r => r.run_id === state.viewedRunId && finishedStatuses.includes(r.status)));
    if (showResult) tabs.push({ id: 'result', label: t(lang, 'centertab.result') });
  } else {
    return null;
  }
  if (tabs.length <= 1 && state.tab === 'catalogue') return null;
  const showBack = state.tab === 'run';
  return <div className="center-tabs">
    {showBack && <button className="center-back" title={t(lang, 'launch.back')}
      onClick={() => setState(s => ({ ...s, tab: 'history' }))}
      aria-label="Back to Runs">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M15 18l-6-6 6-6"/>
      </svg>
    </button>}
    {tabs.map(tab =>
      <button key={tab.id} className={`center-tab ${state.centerTab === tab.id ? 'active' : ''}`}
        onClick={() => setState(s => ({ ...s, centerTab: tab.id }))}>{tab.label}</button>)}
  </div>;
}

// Re-stamp queue_position (1-indexed by created_at) and queue_total on every
// `queued` run. Call any time the queue composition changes — promotion,
// cancellation, batch enqueue. Returns a new runs array.
function restampQueue(runs) {
  const queued = runs.filter(r => r.status === 'queued')
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
  const total = queued.length;
  const positions = new Map(queued.map((r, i) => [r.run_id, i + 1]));
  return runs.map(r => positions.has(r.run_id)
    ? { ...r, queue_position: positions.get(r.run_id), queue_total: total }
    : r);
}

// Placeholder config until /api/config 回来 — keeps TopBar / RunStrip / Statusbar 先不崩
const _CFG_PLACEHOLDER = {
  bench_questions_dir: '…',
  bench_runs_dir: '…',
  demo_runs_dir: '…',
  custom_task_dir: '…',
  agent_solver_path: '…',
  agent_solver_args: '',
  available_models: [
    { id: 'claude-sonnet-4-6', tier: 'frontier', desc: '' },
    { id: 'claude-opus-4-7',   tier: 'frontier', desc: '' },
    { id: 'claude-haiku-4-5',  tier: 'standard', desc: '' },
  ],
  enhance_model: 'claude-haiku-4-5',
  max_turns_default: 100,
  custom_run_max_turns_default: 100,
  max_turns_max: 200,
  solver_timeout_default: 7200,
  max_concurrent_runs: 2,
  max_concurrent_runs_limit: 8,
  schema_version_supported: '1.3',
  task_types: [],
  agents: [],
};

// ============================================================================
// Auth gate — wraps MainApp with a splash login if backend has AUTH_TOKEN set.
// auth_status=disabled → straight to MainApp（VS Code 端口转发 / dev 场景）
// auth_status=enabled && !token  → render <Splash>
// auth_status=enabled && token   → render <MainApp>
// 401 from any /api/* → data.js 派发 'auth:logout' 事件 → 切回 splash
// ============================================================================
function Splash({ onLogin, initialError }) {
  const [token, setToken] = useState('');
  const [error, setError] = useState(initialError || null);
  const [submitting, setSubmitting] = useState(false);
  const submit = async (e) => {
    e?.preventDefault();
    if (!token.trim()) { setError('Token cannot be empty'); return; }
    setSubmitting(true); setError(null);
    try {
      await window.__api.checkToken(token.trim());
      onLogin(token.trim());
    } catch (e) {
      setError(e?.status === 401 ? 'Invalid token' : (e?.message || 'auth check failed'));
      setSubmitting(false);
    }
  };
  return <div className="splash">
    <form className="splash-card" onSubmit={submit}>
      <img className="splash-logo" src="assets/favicon.svg" alt="DFT-Agent Bench" width="64" height="64" />
      <h1 className="splash-title">DFT-Agent Bench</h1>
      <div className="splash-sub">Restricted access · paste your token to continue</div>
      <input type="password" className="splash-input mono" autoFocus
        placeholder="access token" value={token}
        onChange={(e) => { setToken(e.target.value); if (error) setError(null); }}
        disabled={submitting} />
      {error && <div className="splash-err">{error}</div>}
      <button type="submit" className="splash-submit" disabled={submitting || !token.trim()}>
        {submitting ? 'Verifying…' : 'Enter'}
      </button>
      <div className="splash-foot mono">v0.1 · solo demo · do not share token</div>
    </form>
  </div>;
}

function App() {
  // user: { owner, role, display_name } | null（disabled mode 下后端会返 _LOCALHOST_USER）
  const [authState, setAuthState] = useState({
    checking: true, authed: false, enabled: true, user: null,
  });
  useEffect(() => {
    let alive = true;
    const check = async () => {
      try {
        const st = await window.__api.getAuthStatus();
        if (!alive) return;
        if (!st.enabled) {
          setAuthState({ checking: false, authed: true, enabled: false, user: st.user || null });
        } else {
          setAuthState({
            checking: false, authed: !!st.authenticated, enabled: true,
            user: st.authenticated ? (st.user || null) : null,
          });
        }
      } catch (_e) {
        if (alive) setAuthState({ checking: false, authed: false, enabled: true, user: null });
      }
    };
    check();
    const onLogout = () => setAuthState(s => ({ ...s, authed: false, user: null }));
    window.addEventListener('auth:logout', onLogout);
    return () => { alive = false; window.removeEventListener('auth:logout', onLogout); };
  }, []);

  if (authState.checking) {
    return <div className="splash"><div className="splash-card">
      <div className="splash-sub" style={{ marginTop: 12 }}>Loading…</div>
    </div></div>;
  }
  if (!authState.authed) {
    return <Splash onLogin={async (t) => {
      window.sessionStorage.setItem('AUTH_TOKEN', t);
      // 登录后再拉一次 status 取 user 信息（checkToken 不返 user）
      let user = null;
      try {
        const st = await window.__api.getAuthStatus();
        user = st.user || null;
      } catch (_e) {}
      setAuthState({ checking: false, authed: true, enabled: true, user });
    }} />;
  }
  return <MainApp user={authState.user} />;
}

function MainApp({ user }) {
  const [lang, setLang] = useState('zh');
  const [theme, setTheme] = useState(() => {
    const saved = localStorage.getItem('wdi-theme');
    if (saved === 'light' || saved === 'dark') return saved;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  });
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('wdi-theme', theme);
  }, [theme]);
  const [tweaks, setTweak] = useTweaks(TWEAK_DEFAULTS);
  // URL routing：刷新页面时从 ?tab=&centerTab=&run=&task= 恢复视图，
  // 否则用户每次刷新都被踢回 catalogue（demo 体验差）。
  const _initFromUrl = (() => {
    try {
      const p = new URLSearchParams(window.location.search);
      return {
        tab: p.get('tab') || 'catalogue',
        centerTab: p.get('centerTab') || 'preview',
        viewedRunId: p.get('run') || null,
        selectedTaskId: p.get('task') || 'M1_T1_Si_vc_relax',
        // Runs 界面 filter / search / sort 状态（提升自 result-history.jsx 内部 useState）
        // 默认值见下方 useState；URL 没参数时回到默认（沿用 viewedRunId 模式）
        runsScope: p.get('runsScope') || 'all',
        runsMode: p.get('runsMode') || 'all',
        runsModel: p.get('runsModel') || 'all',
        runsMilestone: p.get('runsMilestone') || 'all',
        runsTaskType: p.get('runsTaskType') || 'all',
        runsSubset: p.get('runsSubset') || 'all',
        runsStatusFilter: p.get('runsStatus') || 'all',
        runsSearch: p.get('runsSearch') || '',
        runsSortCol: p.get('runsSortCol') || null,
        runsSortDir: p.get('runsSortDir') || 'desc',
      };
    } catch (_e) {
      return {};
    }
  })();
  const [state, setState] = useState({
    tab: _initFromUrl.tab || 'catalogue',
    centerTab: _initFromUrl.centerTab || 'preview',
    selectedTaskId: _initFromUrl.selectedTaskId || 'M1_T1_Si_vc_relax',
    activeRun: null,
    viewedRunId: _initFromUrl.viewedRunId || null,
    configTaskId: null,
    config: _CFG_PLACEHOLDER,
    tasks: [],
    runs: [],
    results: {},  // run_id -> RunResult（pollResult 终态后填）
    transcripts: {}, // run_id -> { turns: [...], current_turn, since_turn }（pollTranscript 累积）
    runConfig: { model: 'claude-sonnet-4-6', maxTurns: _CFG_PLACEHOLDER.max_turns_default, agentId: null, qeBackend: 'gpu' },
    // Iter-4
    selectMode: false,
    selectedTaskIds: [],
    maxParallel: _CFG_PLACEHOLDER.max_concurrent_runs,
    runsStatusFilter: _initFromUrl.runsStatusFilter || 'all',
    // Runs 界面 filter / search / sort 提升到顶层（详情页返回不丢、刷新 / 分享 URL 都保留）
    runsScope: _initFromUrl.runsScope || 'all',
    runsMode: _initFromUrl.runsMode || 'all',
    runsModel: _initFromUrl.runsModel || 'all',
    runsMilestone: _initFromUrl.runsMilestone || 'all',
    runsTaskType: _initFromUrl.runsTaskType || 'all',
    runsSubset: _initFromUrl.runsSubset || 'all',
    runsSearch: _initFromUrl.runsSearch || '',
    runsSortCol: _initFromUrl.runsSortCol || null,
    runsSortDir: _initFromUrl.runsSortDir || 'desc',
    runsSelectMode: false,
    selectedRunIds: [],
    settingsOpen: false,
    health: null, // { ok, harness_ok, solver_ok, bench_questions_ok, tasks_loaded, anthropic_api_key_present } | null = backend unreachable
    schedulerPaused: false, // 软暂停：scheduler 不再 promote queue→running；通过 /api/runs:scheduler_state 轮询同步
    batchDialog: null, // { selectedTaskIds, model, maxTurns, phase: 'form'|'submitting'|'result', result?: {accepted, failed} }
    toast: null,       // { msg, kind: 'ok'|'warn'|'err' }
  });

  // Toast auto-dismiss
  useEffect(() => {
    if (!state.toast) return;
    const id = setTimeout(() => setState(s => ({ ...s, toast: null })), 3500);
    return () => clearTimeout(id);
  }, [state.toast?.msg, state.toast?.id]);

  // === mount: 一次性载入 config + tasks ===
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const cfg = await window.__api.getConfig();
        if (!alive) return;
        setState(s => ({ ...s, config: cfg, maxParallel: cfg.max_concurrent_runs,
          // runConfig.agentId 没设过 → 从 cfg.agents[0].id 兜底；用户改过则保留
          runConfig: { ...s.runConfig, agentId: s.runConfig.agentId || cfg.agents?.[0]?.id || null } }));
      } catch (e) {
        if (alive) setState(s => ({ ...s, toast: {
          msg: `config load failed: ${e.message}`, kind: 'err', id: Date.now() } }));
      }
      try {
        const tasks = await window.__api.listTasks({ limit: 1000 });
        if (alive) setState(s => ({ ...s, tasks }));
      } catch (e) {
        if (alive) setState(s => ({ ...s, toast: {
          msg: `tasks load failed: ${e.message}`, kind: 'err', id: Date.now() } }));
      }
    })();
    return () => { alive = false; };
  }, []);

  // === health 轮询（每 5s）：驱动 TopBar LED + Statusbar 'harness ready'/'solver' 等动态指示 ===
  useEffect(() => {
    let alive = true;
    let timer = null;
    const tick = async () => {
      if (!alive) return;
      try {
        const h = await window.__api.getHealth();
        if (alive) setState(s => ({ ...s, health: h }));
      } catch (_e) {
        // backend unreachable → health=null（指示红灯）；不打 toast 避免噪音
        if (alive) setState(s => s.health === null ? s : ({ ...s, health: null }));
      }
      if (alive) timer = setTimeout(tick, 5000);
    };
    tick();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, []);

  // === scheduler state 轮询（每 3s）：保持 paused 状态与多 tab/外部 PUT 同步 ===
  useEffect(() => {
    let alive = true;
    let timer = null;
    const tick = async () => {
      if (!alive) return;
      try {
        const st = await window.__api.getSchedulerState();
        if (alive) setState(s => s.schedulerPaused === !!st.paused ? s : ({ ...s, schedulerPaused: !!st.paused }));
      } catch (_e) {
        // backend 不通；health 已经会显示红灯，这里默静默
      }
      if (alive) timer = setTimeout(tick, 3000);
    };
    tick();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, []);

  // === URL 同步：tab/centerTab/viewedRun/selectedTask + Runs filter/search/sort
  // 变化时 replaceState（不入 history）。默认值不写入 URL 避免噪音。 ===
  useEffect(() => {
    try {
      const p = new URLSearchParams();
      if (state.tab && state.tab !== 'catalogue') p.set('tab', state.tab);
      if (state.centerTab && state.centerTab !== 'preview') p.set('centerTab', state.centerTab);
      if (state.viewedRunId) p.set('run', state.viewedRunId);
      if (state.selectedTaskId && state.selectedTaskId !== 'M1_T1_Si_vc_relax') p.set('task', state.selectedTaskId);
      // Runs 界面 filter / search / sort（命名前缀 runs 沿用 runsStatus 风格）
      if (state.runsScope && state.runsScope !== 'all') p.set('runsScope', state.runsScope);
      if (state.runsMode && state.runsMode !== 'all') p.set('runsMode', state.runsMode);
      if (state.runsModel && state.runsModel !== 'all') p.set('runsModel', state.runsModel);
      if (state.runsMilestone && state.runsMilestone !== 'all') p.set('runsMilestone', state.runsMilestone);
      if (state.runsTaskType && state.runsTaskType !== 'all') p.set('runsTaskType', state.runsTaskType);
      if (state.runsSubset && state.runsSubset !== 'all') p.set('runsSubset', state.runsSubset);
      if (state.runsStatusFilter && state.runsStatusFilter !== 'all') p.set('runsStatus', state.runsStatusFilter);
      if (state.runsSearch) p.set('runsSearch', state.runsSearch);
      if (state.runsSortCol) p.set('runsSortCol', state.runsSortCol);
      if (state.runsSortDir && state.runsSortDir !== 'desc') p.set('runsSortDir', state.runsSortDir);
      const qs = p.toString();
      const url = window.location.pathname + (qs ? '?' + qs : '');
      window.history.replaceState(null, '', url);
    } catch (_e) { /* ignore */ }
  }, [state.tab, state.centerTab, state.viewedRunId, state.selectedTaskId,
      state.runsScope, state.runsMode, state.runsModel, state.runsMilestone,
      state.runsTaskType, state.runsSubset, state.runsStatusFilter, state.runsSearch,
      state.runsSortCol, state.runsSortDir]);

  // === pollRuns：2s 全列表 → setRuns；focused 视图（state.activeRun running）1s 加快 ===
  useEffect(() => {
    let alive = true;
    let timer = null;
    const focused = state.activeRun && (
      state.activeRun.status === 'running' || state.activeRun.status === 'evaluating');
    const periodMs = focused ? 1000 : 2000;
    const tick = async () => {
      if (!alive) return;
      try {
        const runs = await window.__api.listRuns({ limit: 1000 });
        if (!alive) return;
        setState(s => {
          // 同步 activeRun 的最新状态（如果它还在 runs[] 中），用于驱动 status 切换
          const active = s.activeRun
            ? (runs.find(r => r.run_id === s.activeRun.run_id) || s.activeRun)
            : null;
          return { ...s, runs, activeRun: active };
        });
      } catch (_e) {
        // 网络错容忍：保持现有 runs；不打 toast 以免 polling 噪声
      }
      if (alive) timer = setTimeout(tick, periodMs);
    };
    tick();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [state.activeRun?.run_id, state.activeRun?.status]);

  // active-run 转录条：从 backend pollTranscript 累加
  const [activeTranscript, setActiveTranscript] = useState([]);

  // === pollTranscript：以 viewedRunId 为锚（不是 activeRun），用户从 RunStrip 点
  // 切换其他 run 时，transcript 也跟着切；running/evaluating 时 1s 拉增量，终态
  // 时拉一次完整作为静态 snapshot。
  useEffect(() => {
    const id = state.viewedRunId;
    if (!id) { setActiveTranscript([]); return; }
    const r = state.runs.find(x => x.run_id === id);
    if (!r) return;
    let alive = true;
    let timer = null;
    let cursor = 0;
    setActiveTranscript([]);  // 切到新 run 重置
    const isLive = r.status === 'running' || r.status === 'evaluating';
    const tick = async () => {
      if (!alive) return;
      try {
        // run 仍 live 时多拉一条（since_turn = cursor - 1），让最新已知 turn 的
        // tool_results / cmd_wall_sec 随 bash.log 落 metadata 而刷新；否则 in-flight
        // 那条会一直停在 "running…" 直到用户 F5。终态 run 用 cursor 直接，无需重拉。
        const fetchSince = isLive ? Math.max(0, cursor - 1) : cursor;
        const snap = await window.__api.getTranscript(id, fetchSince);
        if (!alive) return;
        if (snap.turns && snap.turns.length) {
          // merge by turn number：相同 turn 用新 snapshot 覆盖（刷新 tool_results 等），
          // 新 turn append。Map 保插入顺序，对原有 turn 做 set 不影响位置。
          setActiveTranscript(prev => {
            const byTurn = new Map(prev.map(t => [t.turn, t]));
            for (const t of snap.turns) byTurn.set(t.turn, t);
            return Array.from(byTurn.values()).sort((a, b) => a.turn - b.turn);
          });
          cursor = snap.current_turn;
          // 同步 activeRun.current_turn 给进度条（仅 viewed 等于 active 时）
          setState(s => s.activeRun && s.activeRun.run_id === id
            ? { ...s, activeRun: { ...s.activeRun, current_turn: cursor } }
            : s);
        }
      } catch (_e) { /* 容错 */ }
      if (alive && isLive) timer = setTimeout(tick, 1000);
    };
    tick();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [state.viewedRunId, state.runs.find(r => r.run_id === state.viewedRunId)?.status]);

  // === viewedRunId fallback：placeholder run 被 scheduler promote 删除后，自动切到
  // 最新 running run（同一 launch 的 real_id），让 UI 不卡在 "run not found"。
  useEffect(() => {
    if (!state.viewedRunId) return;
    const found = state.runs.find(r => r.run_id === state.viewedRunId);
    if (found) return;  // 还在
    // viewedRunId 找不到（多半是 placeholder 已被 spawn 后 delete）→ 找最近 running
    const latest = [...state.runs]
      .filter(r => r.status === 'running' || r.status === 'evaluating')
      .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))[0];
    if (latest) {
      setState(s => ({ ...s, viewedRunId: latest.run_id, activeRun: latest }));
    }
  }, [state.viewedRunId, state.runs]);

  // === pollResult：activeRun 进终态时一次性拉 result 缓存到 state.results[run_id] ===
  useEffect(() => {
    const r = state.activeRun;
    if (!r) return;
    if (!['done', 'failed', 'cancelled'].includes(r.status)) return;
    if (state.results[r.run_id]) return;  // 已缓
    let alive = true;
    (async () => {
      try {
        const res = await window.__api.getResult(r.run_id);
        if (alive) setState(s => ({ ...s, results: { ...s.results, [r.run_id]: res } }));
      } catch (_e) { /* result 还没写好 / 不存在 — 不打 toast，下次 viewResult 再拉 */ }
    })();
    return () => { alive = false; };
  }, [state.activeRun?.run_id, state.activeRun?.status]);

  // === 切换 viewedRunId 但非 activeRun（譬如点 RunStrip 看其他 run）→ 拉 result 一次 ===
  useEffect(() => {
    const id = state.viewedRunId;
    if (!id || (state.activeRun && state.activeRun.run_id === id)) return;
    if (state.results[id]) return;
    const run = state.runs.find(x => x.run_id === id);
    if (!run || !['done', 'failed', 'cancelled'].includes(run.status)) return;
    let alive = true;
    (async () => {
      try {
        const res = await window.__api.getResult(id);
        if (alive) setState(s => ({ ...s, results: { ...s.results, [id]: res } }));
      } catch (_e) { /* 容错 */ }
    })();
    return () => { alive = false; };
  }, [state.viewedRunId, state.runs]);

  // force timer re-render every second for live elapsed/run-strip
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  // wire up Tweaks toggle — handled by <TweaksPanel> from tweaks-panel.jsx

  // apply tweaks (only tooltip toggle has a global CSS effect)
  useEffect(() => {
    document.documentElement.classList.toggle('hide-tooltips', !tweaks.showTooltips);
  }, [tweaks.showTooltips]);

  // forceRunState dev simulator + queue scheduler simulation 已删（原 mock 路径）；
  // 真实状态由 backend SQLite + scheduler 写，pollRuns 反映；tweaks.forceRunState 不再生效。

  // solverTimeoutSec 没传时走 cfg.solver_timeout_default（_CFG_PLACEHOLDER 默认 7200 = 2h）。
  // 之前默认 1800 (30min) 导致 catalog 直接 Start run 路径在大体系上常超时，已造成事故；
  // 0505 SSOT 整合后从 backend cfg 读，详见 todo/web/run-defaults-ssot.md。
  const launchBenchRun = async (task, model, maxTurns, solverTimeoutSec, priority = 'normal', agentId = undefined) => {
    if (solverTimeoutSec == null) solverTimeoutSec = state.config.solver_timeout_default;
    try {
      // agentId === undefined → 用当前 runConfig.agentId（catalogue 启动路径默认）
      // agentId === null → 老 row migration 前没 agent_id，也回退当前 default
      // 其他真值 → 显式覆盖（rerun 路径透传原 run.agent_id）
      const resolvedAgentId = (agentId === undefined || agentId === null)
        ? (state.runConfig.agentId || undefined)
        : agentId;
      const resolvedQeBackend = state.runConfig.qeBackend || 'gpu';
      const resp = await window.__api.startRun({
        custom: false, task_id: task.task_id, model, max_turns: maxTurns,
        solver_timeout_sec: solverTimeoutSec,
        agent_id: resolvedAgentId,
        priority,
        qe_backend: resolvedQeBackend,
      });
      const newRun = {
        run_id: resp.run_id, mode: resp.mode, task_id: resp.task_id,
        model: resp.model, status: resp.status, source: 'demo',
        created_at: resp.created_at, started_at: resp.created_at,
        current_turn: resp.current_turn || 0, max_turns: resp.max_turns,
        run_dir: resp.run_dir,
        queue_position: resp.queue_position, queue_total: resp.queue_total,
        owner: resp.owner, priority: resp.priority,
      };
      // placeholder 永远 status=queued；判 "会立刻跑还是排队" 看现有 live 数 + cap：
      // queue_total <= 空 slot → scheduler 下个 tick 内会 promote 这条 → 直接进 Live；
      // 否则真要排队 → 跳到 Runs 列表（history tab）让用户看队列上下文。
      // viewedRunId fallback effect 会在 placeholder 被删时把 activeRun/viewedRunId
      // 切到 latest running run（real_id），所以 Live 路径不会卡 "run not found"。
      const live = state.runs.filter(
        r => r.status === 'running' || r.status === 'evaluating'
      ).length;
      const slots = (state.maxParallel || 0) - live;
      const willRun = (resp.queue_total || 1) <= slots;
      setState(s => {
        const existing = s.runs.find(x => x.run_id === newRun.run_id);
        const runs = existing ? s.runs.map(x => x.run_id === newRun.run_id ? newRun : x)
                              : [newRun, ...s.runs];
        return willRun
          ? { ...s, activeRun: newRun, runs, tab: 'run', centerTab: 'live',
              viewedRunId: newRun.run_id,
              toast: { msg: `launching ${task.task_id}…`,
                       kind: 'ok', id: Date.now() } }
          : { ...s, runs, viewedRunId: newRun.run_id, tab: 'history',
              runsStatusFilter: 'all',
              toast: { msg: `queued #${resp.queue_position}/${resp.queue_total}`,
                       kind: 'ok', id: Date.now() } };
      });
    } catch (e) {
      setState(s => ({ ...s, toast: {
        msg: `start failed: ${e.message}`, kind: 'err', id: Date.now() } }));
    }
  };

  const launchCustomRun = async (spec) => {
    try {
      const resp = await window.__api.startRun({
        custom: true,
        prompt: spec.prompt,
        material: spec.material,
        task_type: spec.task_type,
        model: spec.model || state.config.available_models[0]?.id || 'claude-sonnet-4-6',
        max_turns: spec.max_turns || state.config.custom_run_max_turns_default || 100,
        // max_output_tokens 不再前端传——backend Pydantic schemas/run.py 默认 8192；
        // 0505 移除 UI 暴露，详见 todo run-defaults-ssot D3
        solver_timeout_sec: spec.solver_timeout_sec || state.config.solver_timeout_default,
        agent_id: spec.agent_id || state.runConfig.agentId || undefined,
        priority: spec.priority || 'normal',
        qe_backend: spec.qe_backend || state.runConfig.qeBackend || 'gpu',
      });
      const newRun = {
        run_id: resp.run_id, mode: resp.mode, task_id: null,
        custom_task_summary: spec.prompt.slice(0, 60),
        custom_spec: { ...spec, material: spec.material, task_type: spec.task_type },
        model: resp.model, status: resp.status, source: 'demo',
        created_at: resp.created_at, started_at: resp.created_at,
        current_turn: resp.current_turn || 0, max_turns: resp.max_turns,
        run_dir: resp.run_dir,
        queue_position: resp.queue_position, queue_total: resp.queue_total,
        owner: resp.owner, priority: resp.priority,
      };
      // 同 launchBenchRun：cap 有空就直接 Live，否则进 Runs 列表
      const live = state.runs.filter(
        r => r.status === 'running' || r.status === 'evaluating'
      ).length;
      const slots = (state.maxParallel || 0) - live;
      const willRun = (resp.queue_total || 1) <= slots;
      setState(s => {
        const existing = s.runs.find(x => x.run_id === newRun.run_id);
        const runs = existing ? s.runs.map(x => x.run_id === newRun.run_id ? newRun : x)
                              : [newRun, ...s.runs];
        return willRun
          ? { ...s, activeRun: newRun, runs, tab: 'run', centerTab: 'live',
              viewedRunId: newRun.run_id,
              toast: { msg: `launching custom run…`, kind: 'ok', id: Date.now() } }
          : { ...s, runs, viewedRunId: newRun.run_id, tab: 'history',
              runsStatusFilter: 'all',
              toast: { msg: `queued #${resp.queue_position}/${resp.queue_total}`,
                       kind: 'ok', id: Date.now() } };
      });
    } catch (e) {
      setState(s => ({ ...s, toast: {
        msg: `start failed: ${e.message}`, kind: 'err', id: Date.now() } }));
    }
  };

  const cancelRun = async (run) => {
    try {
      await window.__api.cancelRun(run.run_id, 'user abort');
      // 乐观更新：状态切到 cancelled；下次 pollRuns 会以 backend 真实值校对
      setState(s => {
        const cancelled = { ...run, status: 'cancelled',
          finished_at: new Date().toISOString(),
          error: 'cancelled by user' };
        return { ...s, activeRun: s.activeRun?.run_id === run.run_id ? cancelled : s.activeRun,
          runs: s.runs.map(x => x.run_id === run.run_id ? cancelled : x) };
      });
    } catch (e) {
      setState(s => ({ ...s, toast: {
        msg: `cancel failed: ${e.message}`, kind: 'err', id: Date.now() } }));
    }
  };

  const viewResult = (run) => setState(s => ({ ...s, centerTab: 'result' }));
  // Re-run：尽可能保留原 run 的配置（task_id / model / max_turns / solver_timeout_sec /
  // priority / agent_id 全部从 RunListItem 透传）。老 row（migration 前）某些字段缺失时
  // 用 cfg 默认（max_turns / timeout / priority / agent_id 走当前 default）。
  const rerun = (run) => {
    const task = state.tasks.find(t => t.task_id === run.task_id);
    if (!task) return;
    launchBenchRun(
      task,
      run.model,
      run.max_turns ?? state.config.max_turns_default,
      run.solver_timeout_sec ?? state.config.solver_timeout_default,
      run.priority || 'normal',
      run.agent_id ?? null,
    );
  };

  // ============== Iter-4: batch launch + queue handlers ==============

  const openBatchDialog = (taskIds) => {
    setState(s => ({
      ...s,
      batchDialog: {
        selectedTaskIds: [...taskIds],
        model: s.runConfig.model || 'claude-sonnet-4-6',
        agentId: s.runConfig.agentId || s.config.agents?.[0]?.id || null,
        maxTurns: s.runConfig.maxTurns || s.config.max_turns_default,
        solverTimeoutSec: s.runConfig.solverTimeoutSec || s.config.solver_timeout_default,
        priority: 'normal',
        qeBackend: s.runConfig.qeBackend || 'gpu',
        phase: 'form',
        result: null,
      },
    }));
  };
  const closeBatchDialog = () => setState(s => ({ ...s, batchDialog: null }));

  // 真 batch submit：调 api.startBatch；后台 pollRuns 会把新 run 拉进 state.runs
  const submitBatch = async (taskIds) => {
    setState(s => ({ ...s, batchDialog: { ...s.batchDialog, phase: 'submitting' } }));
    try {
      const dlg = state.batchDialog || {};
      const result = await window.__api.startBatch({
        specs: taskIds.map(tid => ({ task_id: tid })),
        model: dlg.model || state.config.available_models[0]?.id || 'claude-sonnet-4-6',
        agent_id: dlg.agentId || state.runConfig.agentId || undefined,
        max_turns: dlg.maxTurns || state.config.max_turns_default || 50,
        solver_timeout_sec: dlg.solverTimeoutSec || state.config.solver_timeout_default,
        priority: dlg.priority || 'normal',
        qe_backend: dlg.qeBackend || state.runConfig.qeBackend || 'gpu',
      });
      return result;  // {accepted, failed}（backend 返 BatchSubmitResponse 同 shape）
    } catch (e) {
      // 网络 / 顶层校验失败 → 视为整批 failed
      const fakeFailed = taskIds.map((tid, idx) => ({
        spec_index: idx, spec: { task_id: tid },
        error_code: e.code || 'unknown', error_msg: e.message || 'submit failed',
      }));
      return { accepted: [], failed: fakeFailed };
    }
  };

  const handleBatchLaunch = async () => {
    const dlg = state.batchDialog;
    if (!dlg) return;
    const result = await submitBatch(dlg.selectedTaskIds);
    if (result.failed.length === 0) {
      // All success: close + toast + jump to Runs (filter='all' 让用户能看到刚 queue
      // 的几条 + 已 running 的，跟 single launch 跳转一致；previous 默认 'queued' 会
      // 隐藏其它行，反而显得"任务消失了")
      setState(s => ({
        ...s,
        batchDialog: null,
        selectMode: false, selectedTaskIds: [],
        tab: 'history', runsStatusFilter: 'all',
        toast: { msg: t(lang, 'toast.batch_queued', { n: result.accepted.length }), kind: 'ok', id: Date.now() },
      }));
    } else {
      // Partial or all fail: keep dialog open, show result
      setState(s => ({ ...s, batchDialog: { ...s.batchDialog, phase: 'result', result } }));
    }
  };

  const handleRetryFailedSpecs = async (failedSubset) => {
    const taskIds = failedSubset.map(f => f.spec.task_id);
    const result = await submitBatch(taskIds);
    setState(s => {
      const dlg = s.batchDialog;
      if (!dlg) return s;
      // Remove successfully retried failed entries; re-add new failures
      const retriedSpecIndexes = new Set(failedSubset.map(f => f.spec_index));
      const stillFailed = (dlg.result?.failed || []).filter(f => !retriedSpecIndexes.has(f.spec_index));
      // Map new failures to use stable distinct keys (keep their fresh meta)
      const newFailed = result.failed.map((f, j) => ({ ...f, spec_index: 1000 + Date.now() % 100000 + j, spec: { task_id: taskIds[f.spec_index] } }));
      const mergedFailed = [...stillFailed, ...newFailed];
      const mergedAccepted = [...(dlg.result?.accepted || []), ...result.accepted];
      return { ...s, batchDialog: { ...dlg, result: { accepted: mergedAccepted, failed: mergedFailed } } };
    });
  };

  // Cancel from list: queued / running / evaluating 都走同一 backend cancel endpoint
  const cancelRunFromList = (run) => {
    cancelRun(run);
  };

  // Max-parallel stepper：写 backend config_kv（PUT /api/config）；本地乐观更新
  const setMaxParallel = async (n) => {
    const limit = state.config.max_concurrent_runs_limit || 8;
    const clamped = Math.max(1, Math.min(limit, n));
    const reduced = clamped < state.maxParallel;
    setState(s => ({
      ...s, maxParallel: clamped,
      toast: reduced ? { msg: t(lang, 'max_concurrent_reduced_toast'), kind: 'warn', id: Date.now() } : s.toast,
    }));
    try {
      const cfg = await window.__api.updateConfig({ max_concurrent_runs: clamped });
      setState(s => ({ ...s, config: cfg, maxParallel: cfg.max_concurrent_runs }));
    } catch (e) {
      setState(s => ({ ...s, toast: {
        msg: `update config failed: ${e.message}`, kind: 'err', id: Date.now() } }));
    }
  };

  // Cleanup toggle (TopBar settings dropdown)：写 backend config_kv → 落到 monitor task
  // 终态分支决定是否 cleanup_run_dir。乐观更新本地 config 副本。
  const setCleanupIntermediates = async (on) => {
    setState(s => ({ ...s, config: { ...s.config, cleanup_intermediates_on_finish: !!on } }));
    try {
      const cfg = await window.__api.updateConfig({ cleanup_intermediates_on_finish: !!on });
      setState(s => ({ ...s, config: cfg }));
    } catch (e) {
      setState(s => ({ ...s, toast: {
        msg: `update config failed: ${e.message}`, kind: 'err', id: Date.now() } }));
    }
  };

  // 软暂停 toggle：optimistic update 立即翻 paused，调 backend pause/resume 后用真值覆盖。
  // backend 失败回滚到原值并 toast。
  const toggleSchedulerPause = async () => {
    const wasPaused = state.schedulerPaused;
    setState(s => ({ ...s, schedulerPaused: !wasPaused }));
    try {
      const fn = wasPaused ? window.__api.resumeScheduler : window.__api.pauseScheduler;
      const resp = await fn();
      setState(s => ({
        ...s,
        schedulerPaused: !!resp.paused,
        toast: {
          msg: resp.paused
            ? `Scheduler paused (${resp.queued} queued held, ${resp.live} still running)`
            : `Scheduler resumed (${resp.queued} queued will start)`,
          kind: 'ok', id: Date.now(),
        },
      }));
    } catch (e) {
      setState(s => ({
        ...s,
        schedulerPaused: wasPaused,  // 回滚
        toast: { msg: `${wasPaused ? 'resume' : 'pause'} failed: ${e.message}`, kind: 'err', id: Date.now() },
      }));
    }
  };

  // 一键 cancel 所有 queued/running/evaluating；不删 row（保留为 cancelled），
  // 用户事后还能 Delete 二次清理。
  const cancelAllActive = async () => {
    try {
      const resp = await window.__api.cancelActive();
      setState(s => ({
        ...s,
        toast: {
          msg: resp.failed.length
            ? `cancelled ${resp.cancelled.length}, ${resp.failed.length} failed`
            : `cancelled ${resp.cancelled.length} active runs`,
          kind: resp.failed.length ? 'warn' : 'ok', id: Date.now(),
        },
      }));
    } catch (e) {
      setState(s => ({ ...s, toast: {
        msg: `cancel active failed: ${e.message}`, kind: 'err', id: Date.now() } }));
    }
  };

  // Bulk delete from Runs select-mode：cancel active + delete row + rmtree run_dir。
  const deleteSelectedRuns = async (runIds) => {
    if (!runIds?.length) return;
    try {
      const resp = await window.__api.deleteRuns(runIds);
      setState(s => ({
        ...s,
        runs: s.runs.filter(r => !resp.deleted.includes(r.run_id)),
        runsSelectMode: false,
        selectedRunIds: [],
        toast: {
          msg: resp.failed.length
            ? `deleted ${resp.deleted.length}, ${resp.failed.length} failed`
            : `deleted ${resp.deleted.length} runs`,
          kind: resp.failed.length ? 'warn' : 'ok', id: Date.now(),
        },
      }));
    } catch (e) {
      setState(s => ({ ...s, toast: {
        msg: `bulk delete failed: ${e.message}`, kind: 'err', id: Date.now() } }));
    }
  };

  // Apply mock-data-scale tweak: slice the visible task list
  const visibleTasks = useMemo(() => {
    // Re-derive per-task aggregates from the live runs list so catalogue stays in
    // sync when the queue scheduler promotes runs (e.g. MoS2 shows its real score
    // after auto-completion instead of the stale "—" from initial mock data).
    const byTask = {};
    for (const r of state.runs) {
      if (!r.task_id) continue;
      const slot = byTask[r.task_id] || (byTask[r.task_id] = { runs: 0, lastDone: null, lastAny: null });
      slot.runs++;
      if (!slot.lastAny || (r.created_at || '') > (slot.lastAny.created_at || '')) slot.lastAny = r;
      if (r.status === 'done' && r.total_score != null) {
        if (!slot.lastDone || (r.finished_at || r.created_at || '') > (slot.lastDone.finished_at || slot.lastDone.created_at || '')) slot.lastDone = r;
      }
    }
    return state.tasks.slice(0, tweaks.dataScale).map(task => {
      const agg = byTask[task.task_id];
      if (!agg) return task;
      return {
        ...task,
        last_score: agg.lastDone?.total_score ?? task.last_score ?? null,
        last_run_at: agg.lastAny?.created_at ?? task.last_run_at,
        last_run_count: agg.runs,
      };
    });
  }, [state.tasks, state.runs, tweaks.dataScale]);
  const stateForTabs = { ...state, tasks: visibleTasks };

  function AppTweaksPanel() {
    return <TweaksPanel title="Tweaks">
      <TweakSection label="Run state" />
      <TweakSelect label="Force active run state"
        value={tweaks.forceRunState}
        options={[
          { value: 'auto', label: 'auto (live)' },
          { value: 'queued', label: 'queued' },
          { value: 'running', label: 'running' },
          { value: 'done', label: 'done' },
          { value: 'failed', label: 'failed' },
          { value: 'cancelled', label: 'cancelled' },
        ]}
        onChange={(v) => setTweak('forceRunState', v)} />
      <TweakRadio label="Transcript playback"
        value={tweaks.playbackSpeed}
        options={[{ value: 1, label: '1×' }, { value: 2, label: '2×' }, { value: 4, label: '4×' }]}
        onChange={(v) => setTweak('playbackSpeed', v)} />

      <TweakSection label="Mock data" />
      <TweakRadio label="Catalogue size"
        value={tweaks.dataScale}
        options={[{ value: 8, label: '8' }, { value: 30, label: '30' }, { value: 172, label: '172' }]}
        onChange={(v) => setTweak('dataScale', v)} />
      <TweakSelect label="Batch failure rate"
        value={tweaks.batchFailureRate}
        options={[
          { value: 'all_success', label: 'none — all accepted' },
          { value: 'random', label: 'random (0–2 fail)' },
          { value: 'partial', label: 'partial (~⅓ fail)' },
          { value: 'all_fail', label: 'all fail' },
        ]}
        onChange={(v) => setTweak('batchFailureRate', v)} />

      <TweakSection label="View" />
      <TweakRadio label="Result panel mode"
        value={tweaks.resultMode}
        options={[
          { value: 'auto', label: 'auto' },
          { value: 'bench', label: 'bench' },
          { value: 'custom', label: 'custom' },
        ]}
        onChange={(v) => setTweak('resultMode', v)} />
      <TweakToggle label="Jargon tooltips"
        value={tweaks.showTooltips}
        onChange={(v) => setTweak('showTooltips', v)} />
    </TweaksPanel>;
  }
  const viewedRun = state.runs.find(r => r.run_id === state.viewedRunId) || state.activeRun;
  // Resolve which bench task drives the Config view. Priority:
  //   1. explicit configTaskId (set by "Open Config →" buttons)
  //   2. the task of the currently-viewed run, when it's a bench run
  //      (so clicking RunStrip → Config shows that run's task, not stale Si)
  //   3. selectedTaskId fallback (user landed in catalogue first)
  const configTaskCandidateId = state.configTaskId
    || (viewedRun?.mode === 'bench' ? viewedRun.task_id : null)
    || state.selectedTaskId;
  const configTask = state.tasks.find(t => t.task_id === configTaskCandidateId);
  const selectedTask = state.tasks.find(t => t.task_id === state.selectedTaskId);

  // Choose result data for currently viewed run; null if backend hasn't returned result yet
  const resultData = useMemo(() => {
    if (!viewedRun) return null;
    const cached = state.results[viewedRun.run_id];
    if (cached) {
      // backend RunResult union：BenchRunResult.mode === 'bench'，CustomRunResult.mode === 'custom'
      if (cached.mode === 'bench') {
        return { kind: 'bench', report: cached };
      }
      if (cached.mode === 'custom') {
        return {
          kind: 'custom',
          result: cached,
          // 终态后 transcript 仍可读 activeTranscript（pollTranscript 累计）；空则 result 内部 transcript_summary 兜底
          transcript: activeTranscript,
        };
      }
    }
    // 未缓存且非终态 → 不展示（renderCenter 走 empty state）
    return null;
  }, [viewedRun, state.results, activeTranscript]);

  const renderCenter = () => {
    if (state.tab === 'history') return <History runs={state.runs} tasks={visibleTasks} setState={setState} lang={lang}
      maxParallel={state.maxParallel}
      statusFilter={state.runsStatusFilter}
      setStatusFilter={(v) => setState(s => ({ ...s, runsStatusFilter: v }))}
      scope={state.runsScope}
      setScope={(v) => setState(s => ({ ...s, runsScope: v }))}
      mode={state.runsMode}
      setMode={(v) => setState(s => ({ ...s, runsMode: v }))}
      model={state.runsModel}
      setModel={(v) => setState(s => ({ ...s, runsModel: v }))}
      milestone={state.runsMilestone}
      setMilestone={(v) => setState(s => ({ ...s, runsMilestone: v }))}
      taskType={state.runsTaskType}
      setTaskType={(v) => setState(s => ({ ...s, runsTaskType: v }))}
      subset={state.runsSubset}
      setSubset={(v) => setState(s => ({ ...s, runsSubset: v }))}
      searchText={state.runsSearch}
      setSearchText={(v) => setState(s => ({ ...s, runsSearch: v }))}
      sortCol={state.runsSortCol}
      setSortCol={(v) => setState(s => ({ ...s, runsSortCol: v }))}
      sortDir={state.runsSortDir}
      setSortDir={(v) => setState(s => ({ ...s, runsSortDir: v }))}
      onCancel={cancelRunFromList}
      selectMode={state.runsSelectMode}
      selectedRunIds={state.selectedRunIds}
      onToggleSelectMode={() => setState(s => ({ ...s, runsSelectMode: !s.runsSelectMode, selectedRunIds: [] }))}
      onToggleRunSelect={(runId) => setState(s => ({
        ...s, selectedRunIds: s.selectedRunIds.includes(runId)
          ? s.selectedRunIds.filter(id => id !== runId)
          : [...s.selectedRunIds, runId],
      }))}
      onSelectAll={(runIds) => setState(s => ({ ...s, selectedRunIds: runIds }))}
      onDeleteSelected={() => deleteSelectedRuns(state.selectedRunIds)}
      onCancelActive={cancelAllActive}
      paused={state.schedulerPaused}
      availableModels={state.config.available_models || []}
      availableAgents={state.config.agents || []}
      currentOwner={user?.owner || 'localhost'} />;
    if (state.tab === 'compose') return <div className="center"><div className="center-body"><Compose state={state} setState={setState} lang={lang} onLaunch={launchCustomRun} user={user} /></div></div>;
    if (state.tab === 'leaderboard') return <div className="center"><div className="center-body"><Leaderboard lang={lang} user={user} setState={setState} tasks={state.tasks} onOpenBatch={openBatchDialog} /></div></div>;
    if (state.tab === 'admin') return <div className="center"><div className="center-body"><AdminPage lang={lang} user={user}
      paused={state.schedulerPaused}
      onTogglePause={toggleSchedulerPause}
      maxParallel={state.maxParallel}
      maxParallelLimit={state.config.max_concurrent_runs_limit || 8}
      setMaxParallel={setMaxParallel} /></div></div>;
    // F3 Step 5：tokens 管理（dev_role only）。组件内部还有 isDevRole 二次守卫，
    // 即便 URL 直拼 ?tab=tokens 也只会渲染 forbidden 占位（不发请求）。
    if (state.tab === 'tokens') return <div className="center"><div className="center-body"><TokensPage user={user} lang={lang} /></div></div>;
    if (state.tab === 'catalogue') {
      if (state.centerTab === 'result' && viewedRun && resultData) {
        const taskForResult = state.tasks.find(t => t.task_id === viewedRun.task_id);
        const effKind = tweaks.resultMode === 'auto' ? resultData.kind : tweaks.resultMode;
        // run.agent_id 优先；DB 还没存 agent_id 的老 row 落到 cfg.agents[0].name（当前唯一 agent）
        const agentName = (() => {
          const aid = viewedRun.agent_id;
          const found = aid && state.config.agents?.find(a => a.id === aid);
          return found?.name || state.config.agents?.[0]?.name || null;
        })();
        return <div className="center">
          <CenterTabs state={state} setState={setState} lang={lang} />
          <div className="center-body">
            {effKind === 'bench'
              ? <ResultBench run={viewedRun} task={taskForResult || state.tasks[0]}
                  report={resultData.report} lang={lang} onRerun={rerun} agentName={agentName} />
              : <ResultCustom run={viewedRun}
                  result={resultData.result}
                  transcript={resultData.transcript || []} lang={lang} agentName={agentName} />}
          </div>
        </div>;
      }
      if (state.centerTab === 'result' && (!viewedRun || !resultData)) {
        const noRunYet = !viewedRun;
        return <div className="center">
          <CenterTabs state={state} setState={setState} lang={lang} />
          <div className="center-body">
            <div className="empty-state">
              <div className="h">{noRunYet ? 'No runs for this task yet' : 'Result not ready'}</div>
              <div className="sub">
                {noRunYet
                  ? <>Launch this task from <strong>Config</strong> to generate a run and see its scored Result.</>
                  : <>This run is <strong>{viewedRun.status}</strong> — Result will populate once it finishes.</>}
              </div>
              {noRunYet
                ? <button className="btn primary" onClick={() => setState(s => ({ ...s, tab: 'run', centerTab: 'config', configTaskId: selectedTask?.task_id }))}>Open Config →</button>
                : <button className="btn" onClick={() => setState(s => ({ ...s, tab: 'run', centerTab: 'live', viewedRunId: viewedRun.run_id }))}>View Live →</button>}
            </div>
          </div>
        </div>;
      }
      return <div className="center">
        <CenterTabs state={state} setState={setState} lang={lang} />
        <div className="center-body"><Preview task={selectedTask} setState={setState} lang={lang} /></div>
      </div>;
    }
    if (state.tab === 'run') {
      return <div className="center">
        <CenterTabs state={state} setState={setState} lang={lang} />
        <div className="center-body">
          {state.centerTab === 'config' && (() => {
            // Custom (compose) runs don't have a bench task — show the custom spec
            // that produced this run, so clicking Config doesn't accidentally show
            // some unrelated bench task's config.
            const customRun = (state.activeRun?.mode === 'custom' && state.activeRun)
              || (viewedRun?.mode === 'custom' && viewedRun);
            if (customRun) return <CustomConfigView run={customRun} lang={lang} onRerun={rerun} />;
            return <RunConfig task={configTask} state={state} setState={setState} lang={lang} onLaunch={launchBenchRun} user={user} />;
          })()}
          {state.centerTab === 'live' && (() => {
            const run = state.activeRun || viewedRun;
            // RunLive 内 line 128 `if (!run) return` 早 return + 后续 useRef/useEffect
            // 违反 React Rules of Hooks（hook count 跨 render 变化 → 整个 app crash）。
            // run undefined 时此处直接渲染占位，绕过 RunLive 的 hook order 问题。
            if (!run) {
              return <div className="empty-state">
                <div className="h">No active run</div>
                <div className="sub">Pick a run from the Runs tab or launch a new one.</div>
              </div>;
            }
            const transcript = activeTranscript;
            return <RunLive run={run} task={state.tasks.find(t => t.task_id === run?.task_id)}
              transcript={transcript}
              state={state} setState={setState} lang={lang} onCancel={cancelRun} onViewResult={viewResult} onRetry={rerun}
              customSummary={state.activeRun?.custom_task_summary}
              availableAgents={state.config.agents || []} />;
          })()}
          {state.centerTab === 'result' && viewedRun && resultData && (() => {
            const effKind = tweaks.resultMode === 'auto' ? resultData.kind : tweaks.resultMode;
            const agentName = (() => {
              const aid = viewedRun.agent_id;
              const found = aid && state.config.agents?.find(a => a.id === aid);
              return found?.name || state.config.agents?.[0]?.name || null;
            })();
            return effKind === 'bench'
              ? <ResultBench run={viewedRun} task={state.tasks.find(t => t.task_id === viewedRun.task_id) || state.tasks[0]}
                  report={resultData.report} lang={lang} onRerun={rerun} agentName={agentName} />
              : <ResultCustom run={viewedRun}
                  result={resultData.result}
                  transcript={resultData.transcript || []} lang={lang} agentName={agentName} />;
          })()}
          {state.centerTab === 'result' && (!viewedRun || !resultData) && (
            <div className="empty-state">
              <div className="h">{viewedRun ? 'Result not ready' : 'No run selected'}</div>
              <div className="sub">
                {viewedRun
                  ? <>This run is <strong>{viewedRun.status}</strong> — Result will populate once it finishes.</>
                  : <>Launch a run to see its scored Result.</>}
              </div>
              {viewedRun && (viewedRun.status === 'running' || viewedRun.status === 'evaluating') &&
                <button className="btn" onClick={() => setState(s => ({ ...s, centerTab: 'live' }))}>View Live →</button>}
            </div>
          )}
        </div>
      </div>;
    }
  };

  return <div className="app" data-screen-label="DFT-Agent Bench">
    <TopBar state={state} setState={setState} lang={lang} setLang={setLang} theme={theme} setTheme={setTheme} onCleanupToggle={setCleanupIntermediates} user={user} />
    <RunStrip runs={state.runs} state={state} setState={setState} lang={lang} currentOwner={user?.owner || 'localhost'} />
    <div className={`workspace ${state.tab === 'catalogue' ? 'two-col' : 'one-col'}`}>
      {(state.tab === 'catalogue') && <Catalogue state={stateForTabs} setState={setState} lang={lang} onOpenBatch={openBatchDialog} />}
      {renderCenter()}
    </div>
    <div className="statusbar">
      {(() => {
        const h = state.health;
        // harness 段：health 不通 = 红 backend down；harness path 缺 = 红 missing；
        // anthropic_api_key 缺 = 黄 key missing；其他 = 绿 ready
        let dot = 'var(--ok)', text = 'harness ready', title = '';
        if (!h) { dot = 'var(--err)'; text = 'backend down'; title = '/health 请求失败'; }
        else if (!h.harness_ok) { dot = 'var(--err)'; text = `harness missing: ${h.harness_path}`; title = h.harness_path; }
        else if (!h.solver_ok) { dot = 'var(--err)'; text = `solver missing: ${h.solver_path}`; title = h.solver_path; }
        else if (!h.anthropic_api_key_present) { dot = '#d97706'; text = 'ANTHROPIC_API_KEY missing'; title = 'set in env or config.toml'; }
        else { title = `${h.harness_path}\n${h.solver_path}`; }
        return <span className="seg" title={title}>
          <span style={{ width: 6, height: 6, borderRadius: 3, background: dot, display: 'inline-block', marginRight: 4 }} />
          {text}
        </span>;
      })()}
      <span className="seg">runs dir · {state.config.demo_runs_dir}</span>
      <span className="seg">schema {state.config.schema_version_supported}</span>
      <span style={{ flex: 1 }} />
      <span className="seg">{state.runs.length} runs · {state.runs.filter(r => r.status === 'running' || r.status === 'evaluating').length} live · {state.runs.filter(r => r.status === 'queued').length} queued</span>
    </div>
    <AppTweaksPanel />
    {state.batchDialog && <ComposeBatchDialog
      dlg={state.batchDialog}
      tasks={state.tasks}
      models={state.config.available_models || ['claude-sonnet-4-6', 'claude-opus-4-7', 'claude-haiku-4-5']}
      cfg={state.config}
      lang={lang}
      user={user}
      onClose={closeBatchDialog}
      onLaunch={handleBatchLaunch}
      onRetry={handleRetryFailedSpecs}
      onChange={(patch) => setState(s => ({ ...s, batchDialog: { ...s.batchDialog, ...patch } }))} />}
    {state.toast && <div className={`global-toast ${state.toast.kind || 'ok'}`}>
      <span className="dot" />
      <span className="msg">{state.toast.msg}</span>
    </div>}
  </div>;
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
