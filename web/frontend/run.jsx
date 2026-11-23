// Run tab — Config + Live (transcript)

function RunConfig({ task, state, setState, lang, onLaunch, user }) {
  const cfg = state.config;
  const _firstAvail = cfg.available_models.find(m => m.available !== false) || cfg.available_models[0];
  const [model, setModel] = useState(state.runConfig.model || _firstAvail.id);
  const [agentId, setAgentId] = useState(state.runConfig.agentId || (cfg.agents?.[0]?.id) || '');
  const [maxTurns, setMaxTurns] = useState(state.runConfig.maxTurns || cfg.max_turns_default);
  const [solverTimeoutSec, setSolverTimeoutSec] = useState(state.runConfig.solverTimeoutSec || 7200);
  const [priority, setPriority] = useState(state.runConfig.priority || 'normal');
  const [qeBackend, setQeBackend] = useState(state.runConfig.qeBackend || 'gpu');
  // 紧急优先级 gate：has submit.urgent_priority cap（dev_role 自动 bypass）
  const canUrgent = hasCapability(user, CAP_SUBMIT_URGENT_PRIORITY);
  // F2: cost preview — { est_usd, source, n_samples } | null
  const [costPreview, setCostPreview] = useState(null);
  const [costLoading, setCostLoading] = useState(false);
  const [customModel, setCustomModel] = useState('');
  const [modelSearch, setModelSearch] = useState('');
  const useDropdown = cfg.available_models.length > 6;
  const isKnown = cfg.available_models.some(m => m.id === model);
  const effectiveModel = customModel.trim() || model;
  const filteredModels = useDropdown
    ? cfg.available_models.filter(m =>
        !modelSearch || m.id.toLowerCase().includes(modelSearch.toLowerCase())
          || (m.provider || '').toLowerCase().includes(modelSearch.toLowerCase())
          || m.tier.toLowerCase().includes(modelSearch.toLowerCase())
          || (m.desc || '').toLowerCase().includes(modelSearch.toLowerCase()))
    : cfg.available_models;
  // 按 provider 分组，preserve insertion order (Map 按插入顺序遍历)
  const groupedModels = useMemo(() => {
    const g = new Map();
    for (const m of filteredModels) {
      const p = m.provider || 'unknown';
      if (!g.has(p)) g.set(p, []);
      g.get(p).push(m);
    }
    return Array.from(g.entries());
  }, [filteredModels]);
  const _PROVIDER_LABEL = {
    anthropic: 'Anthropic', openai: 'OpenAI', google: 'Google',
    deepseek: 'DeepSeek', moonshot: 'Moonshot Kimi', xai: 'xAI', mistral: 'Mistral',
    openrouter: 'OpenRouter (open weights)', ollama: 'Local Ollama',
  };
  const _selectModel = (m) => {
    if (m.available === false) return;  // disabled — 静默不响应
    setModel(m.id); setCustomModel('');
  };

  // F2: debounced cost preview。watch 4-tuple (task_id, agent_id, model_id, max_turns)，
  // 500ms idle 后调 POST /api/runs:preview-cost。effect dep 含 effectiveModel 不是 model
  // —— customModel 优先，与实际启动 launch 时一致。任一字段缺 → 不发请求 + 清空展示。
  useEffect(() => {
    if (!task?.task_id || !agentId || !effectiveModel || !maxTurns) {
      setCostPreview(null);
      setCostLoading(false);
      return;
    }
    setCostLoading(true);
    const timer = setTimeout(() => {
      window.__api.previewCost({
        task_id: task.task_id,
        agent_id: agentId,
        model_id: effectiveModel,
        max_turns: maxTurns,
      }).then(out => {
        setCostPreview(out);
        setCostLoading(false);
      }).catch(_e => {
        // backend 报错（比如 custom model 价格未配 → 返 0.0 也不会抛）；
        // 网络 / 401 等真错才到这里——清掉展示避免显 stale 值。
        setCostPreview(null);
        setCostLoading(false);
      });
    }, 500);
    return () => clearTimeout(timer);
  }, [task?.task_id, agentId, effectiveModel, maxTurns]);

  if (!task) return <div className="empty-state"><div className="h">Pick a task first</div>
    <button className="btn" onClick={() => setState(s => ({ ...s, tab: 'catalogue', centerTab: 'preview' }))}>Browse catalogue</button></div>;
  return <div className="doc">
    <div className="preview-head">
      <div className="lhs">
        <div className="meta-row">
          <span className="tt-chip">{task.milestone}·{task.task_type}·{task.task_subtype}</span>
        </div>
        <h1 className="preview-title">{task.material_formula}</h1>
        <div className="sub"><span>{task.task_id}</span></div>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 6 }}>
        <CostPreviewRow loading={costLoading} preview={costPreview} lang={lang} />
        <button className="btn primary" onClick={() => onLaunch(task, effectiveModel, maxTurns, solverTimeoutSec, priority, agentId || undefined)}>{t(lang, 'config.launch')} →</button>
      </div>
    </div>

    <div className="config-grid">
      <div className="sec">
        <h3 className="sec-title">{t(lang, 'config.model')}</h3>
        {!useDropdown && <div className="model-cards">
          {filteredModels.map(m =>
            <div key={m.id}
              className={`model-card ${(model === m.id && !customModel) ? 'selected' : ''} ${m.available === false ? 'disabled' : ''}`}
              onClick={() => _selectModel(m)}
              title={m.available === false ? `${m.provider}: not enabled in this deployment (no API key / not smoke-verified)` : ''}>
              <div className="row1">
                <span className="name">{m.id}</span>
                <span className={`tier tier-${m.tier}`}>{m.tier}</span>
                {m.available === false && <span className="disabled-pill" title="not enabled in this deployment">off</span>}
              </div>
              <div className="desc">{m.desc}</div>
            </div>)}
        </div>}
        {useDropdown && <div className="model-dropdown">
          <input className="model-search" placeholder={`Search ${cfg.available_models.length} models · provider / id / tier…`}
            value={modelSearch} onChange={e => setModelSearch(e.target.value)} />
          <div className="model-list">
            {groupedModels.map(([provider, ms]) => <React.Fragment key={provider}>
              <div className="model-group-header">
                <span className="provider-name">{_PROVIDER_LABEL[provider] || provider}</span>
                <span className="provider-count mono">{ms.length}</span>
                {ms.every(m => m.available === false) && <span className="disabled-pill" title="provider not enabled in this deployment">off</span>}
              </div>
              {ms.map(m =>
                <div key={m.id}
                  className={`model-list-row ${(model === m.id && !customModel) ? 'selected' : ''} ${m.available === false ? 'disabled' : ''}`}
                  onClick={() => _selectModel(m)}
                  title={m.available === false ? `${provider}: not enabled in this deployment; pick an available model.` : ''}>
                  <div className="row1">
                    <span className="name mono">{m.id}</span>
                    <span className={`tier tier-${m.tier}`}>{m.tier}</span>
                    {m.available === false && <span className="disabled-pill" title="not enabled in this deployment">off</span>}
                  </div>
                  <div className="desc">{m.desc}</div>
                </div>)}
            </React.Fragment>)}
            {!filteredModels.length && <div className="empty-state sm"><div className="sub">no matches</div></div>}
          </div>
        </div>}
        <div className="custom-model-row">
          <label className="lbl">Custom model id</label>
          <input className="custom-model-input mono" placeholder="e.g. mistral-large-3, internal-finetune-v2"
            value={customModel} onChange={e => setCustomModel(e.target.value)} />
          {customModel.trim() && <div className="warn-line">⚠ Backend will reject if not registered in <span className="mono">solver/models.yaml</span></div>}
          {!customModel.trim() && !isKnown && <div className="warn-line">⚠ Selected id not in available_models — backend will reject if not registered</div>}
        </div>
      </div>
      <div className="sec">
        <h3 className="sec-title">{t(lang, 'config.agent')}</h3>
        <select className="agent-select" value={agentId}
                onChange={e => { setAgentId(e.target.value); setState(s => ({ ...s, runConfig: { ...s.runConfig, agentId: e.target.value } })); }}>
          {(cfg.agents || []).map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        {(() => {
          const a = (cfg.agents || []).find(x => x.id === agentId);
          return a?.description ? <div className="agent-desc">{a.description}</div> : null;
        })()}
      </div>
      <div className="sec">
        <h3 className="sec-title">{t(lang, 'config.max_turns')}</h3>
        <div className="slider-row">
          <div className="slider-val tnum">{maxTurns}</div>
          <input type="range" className="slider-input" min={1} max={cfg.max_turns_max} value={maxTurns}
            onChange={e => setMaxTurns(parseInt(e.target.value))} />
          <div className="mono" style={{ fontSize: 11, color: 'var(--ink-3)', display: 'flex', justifyContent: 'space-between' }}>
            <span>1</span><span>default {cfg.max_turns_default}</span><span>{cfg.max_turns_max}</span>
          </div>
        </div>
      </div>
      <div className="sec">
        <h3 className="sec-title">solver wall-time (h)</h3>
        <div className="slider-row">
          <div className="slider-val tnum">{(solverTimeoutSec / 3600).toFixed(1)} h</div>
          {/* step=300 (5 min) — 24h 范围下足够细，避免 1px = 几小时 的鲁棒性问题 */}
          <input type="range" className="slider-input" min={300} max={86400} step={300} value={solverTimeoutSec}
            onChange={e => setSolverTimeoutSec(parseInt(e.target.value))} />
          <div className="mono" style={{ fontSize: 11, color: 'var(--ink-3)', display: 'flex', justifyContent: 'space-between' }}>
            <span>5 min</span><span>default 6 h</span><span>24 h</span>
          </div>
        </div>
      </div>
      <div className="sec">
        <h3 className="sec-title">优先级</h3>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <PrioritySelect value={priority} onChange={setPriority} isDev={canUrgent} />
          <span style={{ fontSize: 11, color: 'var(--ink-3)' }}>
            queued 行排序：紧急 → 优先 → 普通；同级按入队时间。
            {!canUrgent && <span> 紧急需 submit.urgent_priority 权限。</span>}
          </span>
        </div>
      </div>
      <div className="sec">
        <h3 className="sec-title">QE Backend</h3>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <input type="radio" name="qe-backend" value="gpu"
              checked={qeBackend === 'gpu'}
              onChange={() => { setQeBackend('gpu'); setState(s => ({ ...s, runConfig: { ...s.runConfig, qeBackend: 'gpu' } })); }} />
            <span>GPU <span style={{ color: 'var(--ink-3)', fontSize: 11 }}>(default · podman container)</span></span>
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <input type="radio" name="qe-backend" value="cpu"
              checked={qeBackend === 'cpu'}
              onChange={() => { setQeBackend('cpu'); setState(s => ({ ...s, runConfig: { ...s.runConfig, qeBackend: 'cpu' } })); }} />
            <span>CPU <span style={{ color: 'var(--ink-3)', fontSize: 11 }}>(native QE 7.4 · serial by default · single-task ~5-20× slower; batch competitive)</span></span>
          </label>
        </div>
      </div>
    </div>

    <div className="sec">
      <h3 className="sec-title">read-only environment</h3>
      <div className="config-readonly">
        <div className="cell"><div className="lbl">seed</div><div className="val">42</div></div>
        <div className="cell"><div className="lbl">{t(lang, 'config.schema_version')}</div><div className="val">{cfg.schema_version_supported}</div></div>
        <div className="cell"><div className="lbl">{t(lang, 'config.enhance_model')}</div><div className="val">{cfg.enhance_model}</div></div>
        <div className="cell"><div className="lbl">{t(lang, 'config.solver_path')}</div><div className="val">{cfg.agent_solver_path}</div></div>
        <div className="cell"><div className="lbl">{t(lang, 'config.runs_dir')}</div><div className="val">{cfg.demo_runs_dir}</div></div>
        <div className="cell"><div className="lbl">{t(lang, 'config.bench_dir')}</div><div className="val">{cfg.bench_questions_dir}</div></div>
      </div>
    </div>
  </div>;
}

// Format wall offset 为 "+Nm Ns" / "+Ns" 形式角标，类似聊天软件相对时间戳
function fmtWallOffset(sec) {
  if (sec == null) return '';
  if (sec < 60) return `+${Math.round(sec)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec - m * 60);
  if (m < 60) return s ? `+${m}m ${s}s` : `+${m}m`;
  const h = Math.floor(m / 60);
  const mr = m - h * 60;
  return mr ? `+${h}h ${mr}m` : `+${h}h`;
}

// 该 turn 自身耗时（不带 + 前缀）：'0s' / 'Nm Ns' / 'Nh Nm'。null → '—'
function fmtTurnDuration(sec) {
  if (sec == null) return '—';
  if (sec < 1) return '<1s';
  if (sec < 60) return `${Math.round(sec)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec - m * 60);
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const mr = m - h * 60;
  return mr ? `${h}h ${mr}m` : `${h}h`;
}

// 检测该 turn 是否有 bash 调用未完成（in-flight 或被 wall 超时 kill）。
// 信号：bash tool_call 数 > bash tool_result 数（adapter 只对在 bash.log 里有 metadata
// 的 bash 调用 append result，所以两者差就是未完成的 bash）。
// running 状态时 = 还在跑（QE 进行中等）；failed/cancelled 时 = 被中途 kill 没回来。
function turnHasInflightBash(turn) {
  const bashCalls = (turn.tool_calls || []).filter(tc => tc.tool_name === 'bash').length;
  const bashResults = (turn.tool_results || []).length;
  return bashCalls > bashResults;
}

// QE binary 列表 + MPI launcher：与 backend qe_stats.py 同步白名单。
// 判定逻辑：cmd 用 shell 操作符（;/&&/||/|/&/\n）拆段，每段提取主命令 token
// （跳过 env var FOO=bar 和前导 time/nohup/exec/sudo），主命令 basename 是 QE → 算；
// 是 mpirun/mpiexec/srun 且 args 含 QE basename → 算。排除 which/cat/head/grep 误判。
const QE_BINS = ['pw.x', 'dos.x', 'pp.x', 'projwfc.x', 'bands.x', 'ph.x'];
const MPI_LAUNCHERS = ['mpirun', 'mpiexec', 'srun'];
const PRE_TOKENS = ['time', 'nohup', 'exec', 'sudo'];
// 查询性 flag：QE binary 见到这些就只是打印 version/help，不是实际计算
const QUERY_FLAGS = ['--version', '--help', '-version', '-help', '-V', '-h'];

function _segmentInvokesQE(seg) {
  const tokens = seg.trim().split(/\s+/).filter(Boolean);
  let main = null;
  const args = [];
  for (const tok of tokens) {
    if (main === null && tok.includes('=') && !tok.startsWith('-')) {
      const head = tok.split('=', 1)[0];
      if (head && /^[A-Za-z][A-Za-z0-9_]*$/.test(head)) continue;
    }
    if (main === null && PRE_TOKENS.includes(tok)) continue;
    if (main === null) main = tok;
    else args.push(tok);
  }
  if (main === null) return false;
  const mainBase = main.split('/').pop();
  if (QE_BINS.includes(mainBase)) {
    if (args.some(a => QUERY_FLAGS.includes(a))) return false;
    return true;
  }
  if (MPI_LAUNCHERS.includes(mainBase)) {
    for (let i = 0; i < args.length; i++) {
      if (QE_BINS.includes(args[i].split('/').pop())) {
        const qeArgs = args.slice(i + 1);
        if (qeArgs.some(qa => QUERY_FLAGS.includes(qa))) return false;
        return true;
      }
    }
  }
  return false;
}

function _isQeInvocation(cmd) {
  return cmd.split(/&&|\|\||;|\||&|\n/).some(_segmentInvokesQE);
}

function turnHasQE(turn) {
  if (!turn.tool_calls?.length) return false;
  return turn.tool_calls.some(tc =>
    tc.tool_name === 'bash' && _isQeInvocation(tc.input?.command || '')
  );
}

// ── QE detection ────────────────────────────────────────────────────────
// 'tools' = backend trace（workdir/qe_calls.jsonl）告诉我们这个 turn 实际启动了
// N 个 QE binary（来自 qe_tools.run_pw / run_dos / run_bands / run_pp）。
// 'raw'   = bash cmd 文本里能直接看到 pw.x / mpirun pw.x / dos.x（v1 时代路径，
// v2 prompt 禁用——出现就标红警示）。
// 同 turn 同时有 raw 和 tools → 'tools' 优先（trace 是 ground truth；raw 多半是
// `grep pw.x .out` 这种查询命令，不是真启动）。
function turnQeKind(turn) {
  if (turn?.qe_call_count > 0) return 'tools';
  if (!turn?.tool_calls?.length) return null;
  for (const tc of turn.tool_calls) {
    if (tc.tool_name !== 'bash') continue;
    const cmd = tc.input?.command || '';
    if (_isQeInvocation(cmd)) return 'raw';
  }
  return null;
}

// 聊天式气泡：user 在右（task prompt），agent 在左（回复）；tool_use / tool_result 折成子卡片
// qeKind: null | 'raw' | 'tools'
//   raw   = agent 直跑 bash pw.x / mpirun pw.x（违反 v2 prompt，标红警示）
//   tools = agent 走 qe_tools.run_pw 路径（符合 v2 prompt，标绿）；qeCount 给确切启动数
// hasQE prop 保留兼容（true → 等价 qeKind='raw'，老调用无 break）
function ChatBubble({ role, lang, children, time, timeTitle, header, tokens, hasQE, qeKind, qeCount }) {
  const isUser = role === 'user';
  const kind = qeKind || (hasQE ? 'raw' : null);
  const tagText = kind === 'tools'
    ? (qeCount > 0 ? `QE-tools ×${qeCount}` : 'QE-tools')
    : 'QE';
  return <div className={`chat-row ${isUser ? 'user' : 'agent'}`}>
    <div className={`chat-bubble ${kind ? `has-qe qe-${kind}` : ''}`}>
      {header && <div className="chat-header">{header}{kind && <span className={`chat-qe-tag qe-${kind}`}>{tagText}</span>}</div>}
      <div className="chat-body">{children}</div>
      <div className="chat-foot">
        {tokens && <span className="chat-tokens tnum">{tokens}</span>}
        {time && <span className="chat-time tnum" title={timeTitle || undefined}>{time}</span>}
      </div>
    </div>
  </div>;
}

function TranscriptTurn({ turn, lang, runStatus, defaultOpen=false }) {
  // 聊天式重构：每个 assistant turn 一个气泡（agent 侧）；text + tool_calls + tool_results
  // 都 inline 展开（不再折叠 turn-head/body 两层），用 sub-card 表达 tool_use / tool_result。
  const tokens = turn.usage ? `${turn.usage.input_tokens}↓ ${turn.usage.output_tokens}↑` : '';
  // 主时间标 = 本 turn cmd 实际跑时长（cmd_wall_sec），便于直接看每个 turn 占多久；
  // hover title 给从 run 起的偏移（wall_offset_sec）。无 bash 的 turn 显示 '—'。
  // 例外：bash in-flight（cmd 没回来 → 没 metadata → cmd_wall_sec=0）且 run 仍 running
  //      → 显 spinner，避免误导成 "<1s"（QE 还在算，不是 1 秒内跑完）。
  //      run 已终态（failed/cancelled/done）但 bash 没完成 → 用 killed-fallback 估时正常显。
  const inflight = runStatus === 'running' && turnHasInflightBash(turn);
  const timeNode = inflight
    ? <span className="chat-time-running"><span className="spinner" /> running…</span>
    : fmtTurnDuration(turn.cmd_wall_sec);
  const offsetTitle = turn.wall_offset_sec != null
    ? `at ${fmtWallOffset(turn.wall_offset_sec)} from run start`
    : '';
  const qeKind = turnQeKind(turn);
  const header = <span className="chat-turn-n">turn {turn.turn}{turn.stop_reason ? ` · ${turn.stop_reason}` : ''}</span>;
  return <ChatBubble role="agent" lang={lang} time={timeNode} timeTitle={offsetTitle} header={header} tokens={tokens} qeKind={qeKind} qeCount={turn.qe_call_count || 0}>
    {turn.text_blocks?.map((tb, i) => <div key={`t${i}`} className="chat-text">{tb}</div>)}
    {turn.tool_calls?.map((tc, i) => {
      const tr = turn.tool_results?.[i];
      return <ToolCallCard key={`tc${i}`} call={tc} result={tr} lang={lang} />;
    })}
  </ChatBubble>;
}

function ToolCallCard({ call, result, lang }) {
  const [open, setOpen] = useState(false);
  const summary = call.tool_name === 'bash' ? call.input.command
    : call.tool_name === 'write_file' ? `→ ${call.input.path}`
    : call.tool_name === 'submit' ? 'submit final answer'
    : JSON.stringify(call.input).slice(0, 60);
  return <div className="tool-card">
    <div className="tool-head" onClick={() => setOpen(!open)}>
      <span className="tool-toggle">{open ? '▾' : '▸'}</span>
      <span className="toolname">{call.tool_name}</span>
      <span className="tool-summary">{summary}</span>
    </div>
    {open && <div className="tool-body">
      <pre className="tool-input">{call.tool_name === 'submit' ? JSON.stringify(call.input, null, 2) :
        (call.input.command || (call.input.path
          ? `${call.input.path}\n${'-'.repeat(40)}\n${(call.input.content||'').slice(0, 400)}`
          : JSON.stringify(call.input, null, 2)))}</pre>
      {result && <div className="tool-result-block">
        <div className="lbl">tool_result · {result.tool_id}</div>
        <pre className="tool-stdout">{result.stdout_tail || '(empty)'}</pre>
      </div>}
    </div>}
  </div>;
}

// QE 实时进度灰色小字（transcript in-flight bash turn 下挂 / 或 transcript 区域底部）
// progress 来自 /api/runs/{id}/qe_progress；只在 in_flight=true 时被父组件 mount。
//   - SCF（pw.x）：`⏳ QE pw.x [scf] · iter N/M · elapsed M:SS · ETA ~M:SS`（ETA 仅 eta_sec 非空）
//   - dos/bands/pp：`⏳ QE dos.x · elapsed M:SS · last activity Xs ago`（无 iter）
//   - pw.x 但还没第一次迭代：`⏳ QE pw.x · elapsed M:SS · waiting for first iter…`
// 样式：灰色小字 12px，与 v3 prototype 风格一致；padding-left 16 与 transcript turn 缩进对齐
function QeProgressHint({ lang, progress }) {
  if (!progress || !progress.in_flight) return null;
  const { binary, calc_name, iter_n, iter_max, elapsed_sec, eta_sec, last_activity_sec } = progress;
  const segments = [];
  // 第一段：binary + 可选 [calc_name]
  const head = calc_name && calc_name !== binary
    ? `QE ${binary} [${calc_name}]`
    : `QE ${binary}`;
  segments.push(head);
  // SCF iter 段（只有 pw.x 才有）
  if (binary === 'pw.x') {
    if (iter_n != null && iter_max != null) {
      segments.push(t(lang, 'qe_progress.iter', { n: iter_n, max: iter_max }));
    } else {
      segments.push(t(lang, 'qe_progress.waiting'));
    }
  }
  // elapsed
  if (elapsed_sec != null) {
    segments.push(t(lang, 'qe_progress.elapsed', { x: fmtSec(elapsed_sec) }));
  }
  // ETA（仅 pw.x + ≥3 iter 时非空）
  if (eta_sec != null) {
    segments.push(t(lang, 'qe_progress.eta', { x: fmtSec(eta_sec) }));
  }
  // dos/bands/pp 没 iter 也没 ETA → 显 last activity 提供 "还在跑" 信号
  if (binary !== 'pw.x' && last_activity_sec != null) {
    segments.push(t(lang, 'qe_progress.last_activity', {
      x: Math.round(last_activity_sec),
    }));
  }
  return <div className="qe-progress-hint" style={{
    color: 'var(--ink-3)', fontSize: 12, paddingLeft: 16, marginTop: 4,
  }}>
    <span>⏳ {segments.join(' · ')}</span>
  </div>;
}


function RunLive({ run, task, transcript, state, setState, lang, onCancel, onViewResult, customSummary, onRetry, availableAgents = [] }) {
  if (!run) return <div className="empty-state"><div className="h">No active run</div>
    <button className="btn" onClick={() => setState(s => ({ ...s, tab: 'catalogue' }))}>Browse catalogue</button></div>;
  // Defensive: filter out any sparse/undefined entries that could appear during animation race conditions.
  const safeTranscript = (transcript || []).filter(t => t && typeof t === 'object');
  const elapsed = (run.status === 'running' || run.status === 'evaluating')
    ? Math.floor((Date.now() - new Date(run.started_at || run.created_at).getTime()) / 1000)
    : (run.wall_time_sec || 0);
  const pct = run.max_turns ? Math.min(100, (run.current_turn / run.max_turns) * 100) : 0;
  const lastToolUse = useMemo(() => {
    for (let i = safeTranscript.length - 1; i >= 0; i--) {
      const tcs = safeTranscript[i]?.tool_calls || [];
      if (tcs.length) {
        const tc = tcs[tcs.length-1];
        const inp = tc.input;
        return `${tc.tool_name}: ${tc.tool_name === 'bash' ? inp.command : (inp.path || JSON.stringify(inp).slice(0,60))}`;
      }
    }
    return '—';
  }, [safeTranscript]);

  // ---- 聊天风滚动 ----
  // 滚动容器是外层 .center-body（overflow-y: auto），不在 RunLive 渲染树里 → 用 querySelector
  // 拿引用。逻辑：
  //   1. 监听 scroll 事件维护 isAtBottomRef（< 80px = 当作 "在底部"）
  //   2. safeTranscript.length 变化（新 turn 出现）时，若此前在底部 → 平滑滚到底
  //   3. 不在底部时显示右下角 ↓ 按钮，点击跳到底
  const isAtBottomRef = useRef(true);
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  useEffect(() => {
    const container = document.querySelector('.center-body');
    if (!container) return;
    const NEAR_BOTTOM_PX = 80;
    const handler = () => {
      const dist = container.scrollHeight - container.scrollTop - container.clientHeight;
      const atBottom = dist < NEAR_BOTTOM_PX;
      isAtBottomRef.current = atBottom;
      setShowScrollBtn(!atBottom);
    };
    container.addEventListener('scroll', handler, { passive: true });
    handler();  // 初始一次（mount 时可能已经滚到中间）
    return () => container.removeEventListener('scroll', handler);
  }, []);
  useEffect(() => {
    if (!isAtBottomRef.current) return;
    const container = document.querySelector('.center-body');
    if (!container) return;
    // RAF 等下一帧 layout 完成再 scroll，避免新 turn 高度还没算就跳
    requestAnimationFrame(() => {
      container.scrollTo({ top: container.scrollHeight, behavior: 'smooth' });
    });
  }, [safeTranscript.length]);
  const scrollToBottom = () => {
    const c = document.querySelector('.center-body');
    if (c) c.scrollTo({ top: c.scrollHeight, behavior: 'smooth' });
  };

  // Auto-flip to Result subtab 1s after status transitions running/evaluating → done.
  // Track the last status we observed so a freshly-mounted RunLive viewing an
  // already-done run does NOT trigger the jump (otherwise clicking "Live" on a
  // finished run from the Runs tab would yank you straight back to Result).
  const lastStatusRef = useRef(run.status);
  useEffect(() => {
    const prev = lastStatusRef.current;
    lastStatusRef.current = run.status;
    if (run.status !== 'done') return;
    if (prev !== 'running' && prev !== 'evaluating') return; // only on real transition
    const id = setTimeout(() => {
      setState(s => (s.centerTab !== 'result' && s.tab === 'run')
        ? { ...s, centerTab: 'result', viewedRunId: run.run_id } : s);
    }, 1000);
    return () => clearTimeout(id);
  }, [run.status, run.run_id]);

  // QE 实时进度 polling（2026-05-08）：run.status='running' 时每 5s 拉
  // /api/runs/{id}/qe_progress；in_flight=false 时不渲染但 polling 继续
  // （下一次 QE call 启动会自动切到 in_flight=true）。
  // 5s cadence 与 transcript 1s polling 错开，QE iter 通常 1-3s/次，5s 够细。
  const [qeProgress, setQeProgress] = useState(null);
  useEffect(() => {
    if (run.status !== 'running') {
      setQeProgress(null);
      return;
    }
    let alive = true;
    let timer = null;
    const tick = async () => {
      if (!alive) return;
      try {
        const data = await window.__api.getQeProgress(run.run_id);
        if (alive) setQeProgress(data);
      } catch (_e) { /* 容错：网络瞬断不打 toast */ }
      if (alive) timer = setTimeout(tick, 5000);
    };
    tick();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [run.run_id, run.status]);

  const formula = run.mode === 'custom' ? '(custom)' : (task ? task.material_formula : '');
  return <div className="doc">
    <div className="run-hero">
      <div className="lhs">
        <div className="meta-row">
          <StatusBadge status={run.status} lang={lang} />
          <span className={`badge ${run.mode}`}>{run.mode}</span>
          {run.mode === 'custom' && <span className="badge warn">{t(lang, 'result.custom.badge')}</span>}
          {run.qe_backend && <span className={`badge ${run.qe_backend === 'cpu' ? 'warn' : ''}`}>{run.qe_backend.toUpperCase()}</span>}
        </div>
        <h1 className="preview-title">{run.mode === 'custom' ? (customSummary || 'custom run') : formula}</h1>
        <div className="sub">
          <span>model {run.model}</span>
          {(() => {
            const a = run.agent_id && availableAgents.find(x => x.id === run.agent_id);
            const name = a?.name || availableAgents[0]?.name;
            return name ? <span>agent {name}</span> : null;
          })()}
          <span>started {fmtRel(run.started_at || run.created_at, lang)}</span>
          <span className="mono">{run.run_id}</span>
        </div>
      </div>
      <div className="right-col">
        <div className="timing-big tnum">{fmtElapsed(elapsed)}</div>
        {run.status === 'running' && <button className="btn danger sm" onClick={() => onCancel(run)}>{t(lang, 'run.cancel')}</button>}
        {run.status === 'evaluating' && <span className="evaluating-pill"><span className="pulse" /> evaluator running…</span>}
        {run.status === 'done' && <button className="btn primary sm" onClick={() => onViewResult(run)}>{t(lang, 'run.view_result')} →</button>}
        {run.status === 'failed' && onRetry && <button className="btn primary sm" onClick={() => onRetry(run)}>Retry</button>}
        {run.status === 'cancelled' && onRetry && <button className="btn sm" onClick={() => onRetry(run)}>Re-run</button>}
      </div>
    </div>

    {run.status === 'done' && <div className="completion-banner ok">
      <span className="ic">✓</span>
      <span className="msg">Run completed — switching to Result…</span>
      <button className="btn primary sm" onClick={() => onViewResult(run)}>{t(lang, 'run.view_result')} →</button>
    </div>}
    {run.status === 'failed' && <div className="completion-banner err">
      <span className="ic">✕</span>
      <span className="msg">Run failed{run.error ? ` — ${run.error}` : ''}</span>
      {onRetry && <button className="btn primary sm" onClick={() => onRetry(run)}>Retry</button>}
    </div>}
    {run.status === 'cancelled' && <div className="completion-banner warn">
      <span className="ic">⊘</span>
      <span className="msg">Cancelled by user{run.error ? ` — ${run.error}` : ''}</span>
      {onRetry && <button className="btn sm" onClick={() => onRetry(run)}>Re-run</button>}
    </div>}

    <div className="progress-row">
      <div className="progress-bar"><div style={{ width: `${pct}%` }} /></div>
      <div className="progress-text">{run.current_turn} / {run.max_turns} {t(lang, 'run.turn_progress')}</div>
    </div>

    {run.status === 'running' && <div className="currently">
      <span className="lbl">{t(lang, 'run.currently')}</span>
      <span className="pulse" />
      <span className="cmd">{lastToolUse}</span>
    </div>}

    {run.error && run.status !== 'failed' && run.status !== 'cancelled' && <div className="error-block"><span className="lbl">error</span><span className="mono">{run.error}</span></div>}

    <div className="sec">
      <h3 className="sec-title"><Tip tipKey="tip.transcript" lang={lang}>transcript</Tip> <span className="count">· {safeTranscript.length} turns</span></h3>
      <div className="transcript chat">
        {/* 第一条用户气泡：放给 agent 的初始 prompt（聊天软件风格的"用户首发消息"） */}
        {(() => {
          const initialPrompt = run.mode === 'custom'
            ? (run.custom_spec?.prompt || customSummary || '')
            : (task?.rendered_prompt || '');
          if (!initialPrompt) return null;
          return <ChatBubble role="user" lang={lang} time={fmtWallOffset(0)}
            header={<span className="chat-turn-n">{run.mode === 'custom' ? 'custom prompt' : 'task prompt'}</span>}>
            <div className="chat-text">{initialPrompt}</div>
          </ChatBubble>;
        })()}
        {safeTranscript.map((turn, i) => <TranscriptTurn key={i} turn={turn} lang={lang} runStatus={run.status} />)}
        {/* QE 实时进度灰色小字（2026-05-08）：transcript 区域下方挂一行，
            in_flight 时由 backend /api/runs/{id}/qe_progress 提供。
            display 在 .transcript.chat 内、turn map 之后，与 waiting-llm-hint 同区域 */}
        {run.status === 'running' && qeProgress?.in_flight && (
          <QeProgressHint lang={lang} progress={qeProgress} />
        )}
        {/* run 仍 running 且最后一个 turn 没 inflight bash（bash 都跑完了 / 该 turn 是
            纯 text 或 write_file 等同步 tool）→ 等 LLM 出下一 turn。inflight 时已有
            turn 内 spinner，不重复显示。terminal 状态（done/failed/cancelled）也不显。 */}
        {run.status === 'running' && safeTranscript.length > 0 &&
         !turnHasInflightBash(safeTranscript[safeTranscript.length - 1]) && (
          <div className="waiting-llm-hint">
            <span className="spinner" />
            <span>{t(lang, 'live.waiting_llm')}</span>
          </div>
        )}
        {!safeTranscript.length && <div className="empty-state"><div className="h">Waiting for first turn…</div></div>}
      </div>
    </div>
    {showScrollBtn && (
      <button className="scroll-to-bottom-btn" onClick={scrollToBottom} title="Scroll to bottom" aria-label="Scroll to bottom">
        ↓
      </button>
    )}
  </div>;
}

function CustomConfigView({ run, lang, onRerun }) {
  const spec = run.custom_spec || {};
  const mat = spec.material || {};
  const matLabel = mat.parsed_summary?.formula
    || mat.formula_text
    || mat.bench_task_id
    || '(custom material)';
  const matSource = mat.source === 'upload' ? 'uploaded structure'
    : mat.source === 'describe' ? 'described in text'
    : mat.source === 'pick' ? 'picked from bench library'
    : mat.source || 'unknown';
  return <div className="doc">
    <div className="preview-head">
      <div className="lhs">
        <div className="meta-row">
          <span className="badge custom">custom</span>
          {spec.task_type && <span className="tt-chip">{spec.task_type}</span>}
        </div>
        <h1 className="preview-title">{matLabel}</h1>
        <div className="sub mono" style={{ color: 'var(--ink-3)', fontSize: 11 }}>{run.run_id}</div>
      </div>
      <button className="btn" onClick={() => onRerun(run)}>{t(lang, 'result.rerun')} →</button>
    </div>

    <div className="sec">
      <h3 className="sec-title">submitted prompt <span className="count">· {(spec.prompt || '').length} chars</span></h3>
      <div className="prompt" style={{ maxHeight: 'none' }}>{spec.prompt || <em style={{ color: 'var(--ink-3)' }}>(no prompt captured)</em>}</div>
    </div>

    <div className="config-grid">
      <div className="sec">
        <h3 className="sec-title">material</h3>
        <div className="config-readonly">
          <div className="cell"><div className="lbl">source</div><div className="val">{matSource}</div></div>
          {mat.parsed_summary?.formula && <div className="cell"><div className="lbl">formula</div><div className="val mono">{mat.parsed_summary.formula}</div></div>}
          {mat.parsed_summary?.lattice_system && <div className="cell"><div className="lbl">lattice</div><div className="val">{mat.parsed_summary.lattice_system}</div></div>}
          {mat.parsed_summary?.space_group && <div className="cell"><div className="lbl">space group</div><div className="val">{mat.parsed_summary.space_group}</div></div>}
          {mat.formula_text && <div className="cell"><div className="lbl">description</div><div className="val">{mat.formula_text}</div></div>}
          {mat.bench_task_id && <div className="cell"><div className="lbl">bench task</div><div className="val mono">{mat.bench_task_id}</div></div>}
        </div>
      </div>
      <div className="sec">
        <h3 className="sec-title">launch</h3>
        <div className="config-readonly">
          <div className="cell"><div className="lbl">model</div><div className="val mono">{run.model}</div></div>
          <div className="cell"><div className="lbl">max turns</div><div className="val tnum">{run.max_turns}</div></div>
          <div className="cell"><div className="lbl">task type</div><div className="val mono">{spec.task_type || '—'}</div></div>
          <div className="cell"><div className="lbl">created</div><div className="val mono">{run.created_at?.slice(0, 19)?.replace('T', ' ') || '—'}</div></div>
        </div>
      </div>
    </div>

    <div className="sec">
      <h3 className="sec-title">read-only environment <span className="count">· custom mode</span></h3>
      <div className="config-readonly">
        <div className="cell"><div className="lbl">timeout</div><div className="val">3600 s</div></div>
        <div className="cell"><div className="lbl">seed</div><div className="val">42</div></div>
        <div className="cell"><div className="lbl">scoring</div><div className="val">none (no GT)</div></div>
        <div className="cell"><div className="lbl">audit flags</div><div className="val">none (custom mode)</div></div>
      </div>
    </div>
  </div>;
}

Object.assign(window, { RunConfig, RunLive, TranscriptTurn, CustomConfigView });
