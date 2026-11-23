// Compose tab — custom task input + enhance preview

function Compose({ state, setState, lang, onLaunch, user }) {
  const cfg = state.config;
  const tasks = state.tasks;
  const _firstAvail = cfg.available_models.find(m => m.available !== false) || cfg.available_models[0];
  const [inputMode, setInputMode] = useState('describe');
  const [textDesc, setTextDesc] = useState('');
  const [pickedId, setPickedId] = useState(null);
  const [pickerSearch, setPickerSearch] = useState('');
  const [uploadedFile, setUploadedFile] = useState(null);
  const [uploadParse, setUploadParse] = useState(null);
  const [taskType, setTaskType] = useState('');
  const [prompt, setPrompt] = useState('');
  const [enhancing, setEnhancing] = useState(false);
  const [enhancePreview, setEnhancePreview] = useState(null);
  const [priority, setPriority] = useState('normal');
  // launch settings — 跟 bench RunConfig 对齐。max_output_tokens 0505 移除 UI
  // 暴露：API 必填但用户不需调（v2 agent 在 solver 端硬编码 16384 不依赖 web 参数）；
  // 这里直接用 schema 默认 8192，详见 todo/web/run-defaults-ssot.md D3 决策。
  const [model, setModel] = useState(_firstAvail?.id || '');
  const [agentId, setAgentId] = useState(state.runConfig.agentId || (cfg.agents?.[0]?.id) || '');
  const [qeBackend, setQeBackend] = useState(state.runConfig.qeBackend || 'gpu');
  const [customModel, setCustomModel] = useState('');
  const [modelSearch, setModelSearch] = useState('');
  const [maxTurns, setMaxTurns] = useState(cfg.custom_run_max_turns_default || 100);
  const [solverTimeoutSec, setSolverTimeoutSec] = useState(cfg.solver_timeout_default || 7200);
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
    if (m.available === false) return;
    setModel(m.id); setCustomModel('');
  };
  // 紧急优先级 gate：has submit.urgent_priority cap（dev_role 自动 bypass）
  const canUrgent = hasCapability(user, CAP_SUBMIT_URGENT_PRIORITY);

  const dragRef = useRef(false);
  const onFile = (file) => {
    setUploadedFile(file);
    setTimeout(() => {
      setUploadParse({
        formula: file.name.includes('Si') ? 'Si' : (file.name.includes('MoS2') ? 'MoS2' : 'Unknown'),
        lattice_system: 'cubic',
        space_group: 227,
        atoms_count: 2,
      });
    }, 300);
  };

  const materialReady = (inputMode === 'upload' && uploadParse)
    || (inputMode === 'describe' && textDesc.trim().length > 0)
    || (inputMode === 'pick' && pickedId);
  const ready = materialReady && taskType && prompt.trim().length >= 20;

  const doEnhance = () => {
    setEnhancing(true);
    setTimeout(() => {
      const enhanced = `# Task: ${cfg.task_types.find(x => x.id === taskType)?.label_en} of ${textDesc || pickedId || uploadParse?.formula || 'the material'}

## Material
${uploadParse ? `- formula: ${uploadParse.formula}\n- crystal system: ${uploadParse.lattice_system}\n- space group: ${uploadParse.space_group}` : (textDesc ? `- description: ${textDesc}` : '- (from bench library)')}

## Computational requirements
- ecutwfc ≥ 50 Ry; ecutrho ≥ 400 Ry
- k-mesh ≥ 8×8×8 (Monkhorst–Pack)
- forc_conv_thr ≤ 1.0e-3 Ry/Bohr
- press_conv_thr ≤ 0.5 kbar

## Expected output
${prompt}

## Submission
Call the \`submit\` tool with the extracted fields above. Use atomic units where applicable.`;
      setEnhancePreview({ original: prompt, enhanced, model: cfg.enhance_model, summary: 'Added explicit convergence thresholds, k-mesh, and submission instructions.' });
      setEnhancing(false);
    }, 900);
  };

  const filteredPick = useMemo(() => {
    if (!pickerSearch) return tasks.slice(0, 12);
    const q = pickerSearch.toLowerCase();
    return tasks.filter(x => x.material_formula.toLowerCase().includes(q) || x.task_id.toLowerCase().includes(q)).slice(0, 30);
  }, [tasks, pickerSearch]);

  return <div className="doc">
    <div style={{ marginBottom: 28, paddingBottom: 16, borderBottom: '1px solid var(--rule)' }}>
      <h1 className="preview-title">{t(lang, 'compose.head.title')}</h1>
      <div style={{ color: 'var(--ink-3)', fontSize: 13, marginTop: 6, maxWidth: 720, lineHeight: 1.5 }}>{t(lang, 'compose.head.sub')}</div>
    </div>

    <div className="sec">
      <h3 className="sec-title">1. material</h3>
      <div className="input-mode-seg" style={{ marginBottom: 14 }}>
        <button className={inputMode==='upload'?'active':''} onClick={() => setInputMode('upload')}>{t(lang, 'compose.input.upload')}</button>
        <button className={inputMode==='describe'?'active':''} onClick={() => setInputMode('describe')}>{t(lang, 'compose.input.describe')}</button>
        <button className={inputMode==='pick'?'active':''} onClick={() => setInputMode('pick')}>{t(lang, 'compose.input.pick')}</button>
      </div>

      {inputMode === 'upload' && <div>
        <label className="dropzone"
          onDragOver={e => { e.preventDefault(); dragRef.current = true; }}
          onDrop={e => { e.preventDefault(); if (e.dataTransfer.files[0]) onFile(e.dataTransfer.files[0]); }}>
          <div className="h">{uploadedFile ? uploadedFile.name : t(lang, 'compose.dropzone.h')}</div>
          <div className="sub">{t(lang, 'compose.dropzone.sub')}</div>
          <input type="file" accept=".cif,.poscar,.vasp" style={{ display: 'none' }}
            onChange={e => { if (e.target.files[0]) onFile(e.target.files[0]); }} />
        </label>
        {uploadParse && <div className="parsed-summary">
          <div className="cell"><div className="k">formula</div><div className="v">{uploadParse.formula}</div></div>
          <div className="cell"><div className="k">system</div><div className="v">{uploadParse.lattice_system}</div></div>
          <div className="cell"><div className="k">space group</div><div className="v">#{uploadParse.space_group}</div></div>
          <div className="cell"><div className="k">atoms</div><div className="v">{uploadParse.atoms_count}</div></div>
        </div>}
      </div>}

      {inputMode === 'describe' && <div>
        <input className="field" placeholder={t(lang, 'compose.text.placeholder')}
          value={textDesc} onChange={e => setTextDesc(e.target.value)} />
        <div className="field-meta"><span>{t(lang, 'compose.text.label')}</span><span className="tnum">{textDesc.length} chars</span></div>
      </div>}

      {inputMode === 'pick' && <div>
        <div className="search" style={{ marginBottom: 8 }}>
          <span className="icon">⌕</span>
          <input value={pickerSearch} onChange={e => setPickerSearch(e.target.value)} placeholder={t(lang, 'cat.search.placeholder')} />
        </div>
        <div className="bench-picker-list">
          {filteredPick.map(task =>
            <div key={task.task_id} className={`row ${pickedId === task.task_id ? 'selected' : ''}`}
              onClick={() => setPickedId(task.task_id)}>
              <div>
                <span className="formula">{task.material_formula}</span>{' '}
                <span style={{ color: 'var(--ink-3)', fontSize: 11 }}>{task.material_name}</span>
              </div>
              <span className="id">{task.task_id}</span>
            </div>)}
        </div>
      </div>}
    </div>

    <div className="sec">
      <h3 className="sec-title">2. task type</h3>
      <select className="field" value={taskType} onChange={e => setTaskType(e.target.value)}>
        <option value="">{t(lang, 'compose.task_type.placeholder')}</option>
        {cfg.task_types.filter(x => x.supported_in_custom).map(tt =>
          <option key={tt.id} value={tt.id}>{tt[lang === 'zh' ? 'label_zh' : 'label_en']} — {tt.description}</option>)}
      </select>
    </div>

    <div className="sec">
      <h3 className="sec-title">3. prompt</h3>
      <textarea className="field" rows={6} value={prompt} onChange={e => setPrompt(e.target.value)}
        placeholder={t(lang, 'compose.prompt.placeholder')} />
      <div className="field-meta">
        <span style={{ color: prompt.length < 20 ? 'var(--err)' : 'var(--ink-3)' }}>
          {prompt.length < 20 ? `${20 - prompt.length} more chars before enhance is enabled` : 'ready for enhance'}
        </span>
        <span className="tnum">{prompt.length} chars</span>
      </div>
      <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
        <button className="btn" disabled={prompt.length < 20 || enhancing} onClick={doEnhance}>
          {enhancing && <span className="spinner" />} {t(lang, 'compose.enhance.button')}
        </button>
      </div>
    </div>

    <div className="sec">
      <h3 className="sec-title">4. launch settings</h3>
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
                  {m.available === false && <span className="disabled-pill">off</span>}
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
                  {ms.every(m => m.available === false) && <span className="disabled-pill">off</span>}
                </div>
                {ms.map(m =>
                  <div key={m.id}
                    className={`model-list-row ${(model === m.id && !customModel) ? 'selected' : ''} ${m.available === false ? 'disabled' : ''}`}
                    onClick={() => _selectModel(m)}>
                    <div className="row1">
                      <span className="name mono">{m.id}</span>
                      <span className={`tier tier-${m.tier}`}>{m.tier}</span>
                      {m.available === false && <span className="disabled-pill">off</span>}
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
              <span>1</span><span>default {cfg.custom_run_max_turns_default || 200}</span><span>{cfg.max_turns_max}</span>
            </div>
          </div>
        </div>

        <div className="sec">
          <h3 className="sec-title">solver wall-time (h)</h3>
          <div className="slider-row">
            <div className="slider-val tnum">{(solverTimeoutSec / 3600).toFixed(1)} h</div>
            {/* 0505 user 决策：粒度 0.5h（step=1800s）；min 0.5h 起步避免拖到 5min 误操作 */}
            <input type="range" className="slider-input" min={1800} max={86400} step={1800} value={solverTimeoutSec}
              onChange={e => setSolverTimeoutSec(parseInt(e.target.value))} />
            <div className="mono" style={{ fontSize: 11, color: 'var(--ink-3)', display: 'flex', justifyContent: 'space-between' }}>
              <span>0.5 h</span><span>default {(cfg.solver_timeout_default / 3600).toFixed(1)} h</span><span>24 h</span>
            </div>
          </div>
        </div>

        <div className="sec">
          <h3 className="sec-title">优先级</h3>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <PrioritySelect value={priority} onChange={setPriority} isDev={canUrgent} />
            <span style={{ fontSize: 11, color: 'var(--ink-3)' }}>
              紧急 → 优先 → 普通；同级按入队时间。{!canUrgent && <span> 紧急需 submit.urgent_priority 权限。</span>}
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

      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 16 }}>
        <button className="btn primary" disabled={!ready}
          onClick={() => onLaunch({
            mode: 'custom', prompt,
            material: { source: inputMode, formula_text: textDesc, bench_task_id: pickedId, parsed_summary: uploadParse },
            task_type: taskType,
            model: effectiveModel,
            agent_id: agentId || undefined,
            max_turns: maxTurns,
            solver_timeout_sec: solverTimeoutSec,
            priority,
            qe_backend: qeBackend,
          })}>{t(lang, 'compose.launch')} →</button>
      </div>
    </div>

    {enhancePreview && <div className="enhance-preview">
      <div className="enhance-head">
        <span>enhance preview</span>
        <span className="enhance-meta">{enhancePreview.summary}</span>
      </div>
      <div className="enhance-cols">
        <div className="enhance-col original">
          <div className="h">{t(lang, 'compose.enhance.original')}</div>
          <DiffSpan tokens={diffWords(enhancePreview.original, enhancePreview.enhanced).removed} />
        </div>
        <div className="enhance-col enhanced">
          <div className="h">{t(lang, 'compose.enhance.enhanced')}</div>
          <DiffSpan tokens={diffWords(enhancePreview.original, enhancePreview.enhanced).added} />
        </div>
      </div>
      <div className="enhance-actions">
        <button className="btn primary sm" onClick={() => { setPrompt(enhancePreview.enhanced); setEnhancePreview(null); }}>{t(lang, 'compose.enhance.adopt')}</button>
        <button className="btn sm" onClick={() => setEnhancePreview(null)}>{t(lang, 'compose.enhance.discard')}</button>
        <button className="btn ghost sm" onClick={doEnhance} disabled={enhancing}>{enhancing && <span className="spinner" />} {t(lang, 'compose.enhance.refine')}</button>
        <span className="enhance-meta">enhanced by {enhancePreview.model}</span>
      </div>
    </div>}
  </div>;
}

Object.assign(window, { Compose });
