// ComposeBatchDialog — modal for launching N selected tasks with shared config

function ComposeBatchDialog({ dlg, tasks, models, cfg, lang, onClose, onLaunch, onRetry, onChange, user }) {
  const taskMap = useMemo(() => Object.fromEntries(tasks.map(t => [t.task_id, t])), [tasks]);
  // Normalize models: accept array of strings or objects {id, tier, desc}
  const modelOpts = useMemo(() => (models || []).map(m => typeof m === 'string' ? { id: m } : m), [models]);
  const phase = dlg.phase || 'form';
  const N = dlg.selectedTaskIds.length;
  const result = dlg.result;

  // F2 cost preview —— form 阶段 watch 4-tuple，500ms idle 后调批量 endpoint。
  // 切到 submitting / result 阶段不再 polling（state 切换会触发 cleanup → 自动停）。
  const [costPreview, setCostPreview] = useState(null);
  const [costLoading, setCostLoading] = useState(false);
  useEffect(() => {
    if (phase !== 'form') {
      setCostPreview(null); setCostLoading(false);
      return;
    }
    const aid = dlg.agentId || cfg.agents?.[0]?.id;
    const mid = dlg.model;
    const turns = dlg.maxTurns || cfg.max_turns_default;
    if (!aid || !mid || !turns || N === 0) {
      setCostPreview(null); setCostLoading(false);
      return;
    }
    setCostLoading(true);
    const timer = setTimeout(() => {
      window.__api.previewCostBatch({
        task_ids: dlg.selectedTaskIds,
        agent_id: aid,
        model_id: mid,
        max_turns: turns,
      }).then(out => {
        setCostPreview(out);
        setCostLoading(false);
      }).catch(_e => {
        setCostPreview(null);
        setCostLoading(false);
      });
    }, 500);
    return () => clearTimeout(timer);
  }, [phase, dlg.selectedTaskIds, dlg.agentId, dlg.model, dlg.maxTurns, cfg.agents, cfg.max_turns_default, N]);

  // ESC to close (when allowed)
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') {
        if (phase === 'form' || phase === 'result') onClose();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [phase, onClose]);

  const renderForm = () => <>
    <div className="batch-dlg-head">
      <div className="batch-dlg-title">{t(lang, 'batch_dialog.title', { n: N })}</div>
      <button className="icon-btn" onClick={onClose} aria-label="close">×</button>
    </div>
    <div className="batch-dlg-body">
      <div className="batch-dlg-section">
        <div className="batch-dlg-label">{t(lang, 'batch_dialog.tasks_label')} <span className="count mono">· {N}</span></div>
        <div className="batch-task-list">
          {dlg.selectedTaskIds.map(tid => {
            const tsk = taskMap[tid];
            return <div key={tid} className="batch-task-row">
              <span className="formula">{tsk?.material_formula || '?'}</span>
              <span className="tt-chip">{tsk ? `${tsk.milestone}·${tsk.task_type}·${tsk.task_subtype}` : '—'}</span>
              <span className="id mono">{tid}</span>
            </div>;
          })}
        </div>
      </div>

      <div className="batch-dlg-section">
        <div className="batch-dlg-label">{t(lang, 'batch_dialog.shared_config')}</div>
        <div className="batch-config-grid">
          <label className="batch-field">
            <span className="k">model</span>
            <select value={dlg.model} onChange={e => onChange({ model: e.target.value })}>
              {modelOpts.map(m => <option key={m.id} value={m.id}>{m.id}</option>)}
            </select>
          </label>
          <label className="batch-field">
            <span className="k">agent</span>
            <select value={dlg.agentId || ''} onChange={e => onChange({ agentId: e.target.value })}>
              {(cfg.agents || []).map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select>
          </label>
          <label className="batch-field">
            <span className="k">max_turns</span>
            <input type="number" min={1} max={200} value={dlg.maxTurns}
              onChange={e => onChange({ maxTurns: Math.max(1, Math.min(200, +e.target.value || cfg.max_turns_default)) })} />
          </label>
          <label className="batch-field">
            <span className="k">wall-time (h)</span>
            <input type="number" min={0.5} max={24} step={0.5}
              value={((dlg.solverTimeoutSec ?? cfg.solver_timeout_default) / 3600).toFixed(1)}
              onChange={e => {
                const h = Math.max(0.5, Math.min(24, +e.target.value || (cfg.solver_timeout_default / 3600)));
                onChange({ solverTimeoutSec: Math.round(h * 3600) });
              }} />
          </label>
          <label className="batch-field">
            <span className="k">priority</span>
            <PrioritySelect value={dlg.priority || 'normal'}
              onChange={(v) => onChange({ priority: v })}
              isDev={hasCapability(user, CAP_SUBMIT_URGENT_PRIORITY)} size="sm" />
          </label>
          <label className="batch-field">
            <span className="k">QE backend</span>
            <select value={dlg.qeBackend || 'gpu'} onChange={e => onChange({ qeBackend: e.target.value })}>
              <option value="gpu">GPU (default)</option>
              <option value="cpu">CPU (native build · serial default · single-task slower)</option>
            </select>
          </label>
        </div>
      </div>
      <div className="batch-dlg-section">
        <CostPreviewBatchTotal
          loading={costLoading}
          preview={costPreview}
          taskCount={N}
          lang={lang} />
      </div>
    </div>
    <div className="batch-dlg-foot">
      <button className="btn ghost" onClick={onClose}>{t(lang, 'batch_dialog.cancel')}</button>
      <button className="btn primary" onClick={onLaunch}>{t(lang, 'batch_dialog.launch', { n: N })}</button>
    </div>
  </>;

  const renderSubmitting = () => <>
    <div className="batch-dlg-head">
      <div className="batch-dlg-title">{t(lang, 'batch_submitting', { n: N })}</div>
    </div>
    <div className="batch-dlg-body" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: 180 }}>
      <div className="spinner" />
    </div>
  </>;

  const renderResult = () => {
    if (!result) return null;
    const { accepted = [], failed = [] } = result;
    const allFail = accepted.length === 0;
    const partial = accepted.length > 0 && failed.length > 0;
    let summary;
    if (allFail) summary = t(lang, 'batch_result_summary_all_failed', { n: failed.length });
    else if (partial) summary = t(lang, 'batch_result_summary_partial', { success: accepted.length, failed: failed.length });
    else summary = t(lang, 'batch_result_summary_all_success', { n: accepted.length });

    return <>
      <div className="batch-dlg-head">
        <div className="batch-dlg-title">{t(lang, 'batch_result_title')}</div>
        <button className="icon-btn" onClick={onClose} aria-label="close">×</button>
      </div>
      <div className="batch-dlg-body">
        <div className={`batch-result-summary ${allFail ? 'err' : partial ? 'warn' : 'ok'}`}>
          <div className="counts">
            <span className="ok-count">{accepted.length} ✓</span>
            <span className="sep">·</span>
            <span className="fail-count">{failed.length} ✗</span>
          </div>
          <div className="msg">{summary}</div>
        </div>

        {failed.length > 0 && <div className="batch-dlg-section">
          <div className="batch-dlg-label">
            {t(lang, 'batch_result_failed_title')} <span className="count mono">· {failed.length}</span>
            <span style={{ flex: 1 }} />
            <button className="btn ghost sm" onClick={() => onRetry(failed)}>
              {t(lang, 'batch_result_retry_all_failed')}
            </button>
          </div>
          <div className="batch-failed-list">
            {failed.map((f) => <div key={f.spec_index} className="batch-failed-row">
              <div className="lhs">
                <span className="id mono">{f.spec.task_id}</span>
                <span className={`err-code ${f.error_code === 'task_not_found' ? 'soft' : 'hard'}`}>{f.error_code}</span>
                <span className="err-msg">{f.error_msg}</span>
              </div>
              <button className="btn ghost xs" onClick={() => onRetry([f])}>
                {t(lang, 'batch_result_retry_failed_item')}
              </button>
            </div>)}
          </div>
        </div>}

        {accepted.length > 0 && <div className="batch-dlg-section">
          <div className="batch-dlg-label">
            queued <span className="count mono">· {accepted.length}</span>
          </div>
          <div className="batch-accepted-list">
            {accepted.slice(0, 8).map(a => <div key={a.run_id} className="batch-accepted-row">
              <span className="id mono">{a.task_id}</span>
              <span className="qpos mono">#{a.queue_position}</span>
            </div>)}
            {accepted.length > 8 && <div className="batch-accepted-row more">+ {accepted.length - 8} more</div>}
          </div>
        </div>}
      </div>
      <div className="batch-dlg-foot">
        <button className="btn primary" onClick={onClose}>{t(lang, 'batch_result_close')}</button>
      </div>
    </>;
  };

  return <div className="batch-dlg-overlay" onClick={(e) => { if (e.target === e.currentTarget && phase !== 'submitting') onClose(); }}>
    <div className="batch-dlg" role="dialog" aria-modal="true" onClick={e => e.stopPropagation()}>
      {phase === 'form' && renderForm()}
      {phase === 'submitting' && renderSubmitting()}
      {phase === 'result' && renderResult()}
    </div>
  </div>;
}

Object.assign(window, { ComposeBatchDialog });
