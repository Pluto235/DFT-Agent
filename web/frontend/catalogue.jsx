// Catalogue tab + Preview center column

function Catalogue({ state, setState, lang, onOpenBatch }) {
  const tasks = state.tasks;
  const runs = state.runs || [];
  const cfg = state.config;
  const [tierFilter, setTierFilter] = useState('all');
  const [typeFilter, setTypeFilter] = useState('all');
  const [scoreFilter, setScoreFilter] = useState('all');
  const [modelFilter, setModelFilter] = useState('all');   // 'all' = any-model best；否则该 model best
  const [sort, setSort] = useState('recent');
  const [search, setSearch] = useState('');

  const selectMode = !!state.selectMode;
  const selectedIds = state.selectedTaskIds || [];
  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);

  // 代表性子集（v1.1：快速档 12 / 标准档 27 / 完整档 142）— 一次拉取本地缓存
  const [subsets, setSubsets] = useState([]);
  useEffect(() => {
    api.getSubsets()
      .then(d => setSubsets(d.subsets || []))
      .catch(() => setSubsets([]));
  }, []);

  // 一键选中某子集（覆盖当前选择；只选当前题库存在的题，避免幽灵 id）
  const selectSubset = (subsetKey) => {
    const sub = subsets.find(s => s.key === subsetKey);
    if (!sub) return;
    const taskIdsInLib = new Set(tasks.map(t => t.task_id));
    const ids = (sub.task_ids || []).filter(id => taskIdsInLib.has(id));
    setState(s => ({ ...s, selectedTaskIds: ids }));
  };

  const setSelectMode = (v) => setState(s => ({ ...s, selectMode: v, selectedTaskIds: v ? (s.selectedTaskIds || []) : [] }));
  const toggleSelect = (id) => setState(s => {
    const cur = new Set(s.selectedTaskIds || []);
    if (cur.has(id)) cur.delete(id); else cur.add(id);
    return { ...s, selectedTaskIds: Array.from(cur) };
  });
  const clearSelection = () => setState(s => ({ ...s, selectedTaskIds: [] }));

  // 动态枚举 tier / task_type — M3/M4 加入时自动出现在 dropdown，不需要 backend / 前端改硬编码。
  const tiersSeen = useMemo(() => {
    const s = new Set();
    tasks.forEach(t => { if (t.milestone) s.add(t.milestone); });
    return Array.from(s).sort();
  }, [tasks]);
  const taskTypesSeen = useMemo(() => {
    const s = new Set();
    tasks.forEach(t => { if (t.task_type) s.add(t.task_type); });
    return Array.from(s).sort();
  }, [tasks]);

  // 客户端聚合：每 task 在当前 modelFilter 下的 best done score + 最新 run status。
  // 'all' = any-model best；具体 model = 该 model 的 best done score。
  // pool 为空（无该 model run）→ score=null status=null（"—"）。
  // pool 有 run 但全 fail/cancelled → score=null status=最新一条状态（红 pill / 灰 pill）。
  // pool 有 done → 取 max(total_score) + status='done'。
  // bench-only：runs 里 task_id==null 的 custom 行不会匹配（filter 自然排除）。
  const enrichedTasks = useMemo(() => {
    const runsByTask = new Map();
    for (const r of runs) {
      if (!r.task_id) continue;
      let arr = runsByTask.get(r.task_id);
      if (!arr) { arr = []; runsByTask.set(r.task_id, arr); }
      arr.push(r);
    }
    return tasks.map(t => {
      const xs = runsByTask.get(t.task_id);
      if (!xs || xs.length === 0) {
        // runs 还没 loaded 或 task 没跑过：用后端 derived last_score 兜底（仅 'all' 模式）
        if (modelFilter === 'all' && t.last_score != null) {
          return { ...t, last_status: 'done' };
        }
        return { ...t, last_score: null, last_status: null };
      }
      const pool = modelFilter === 'all' ? xs : xs.filter(r => r.model === modelFilter);
      if (pool.length === 0) return { ...t, last_score: null, last_status: null };
      const done = pool.filter(r => r.status === 'done' && r.total_score != null);
      if (done.length) {
        return { ...t, last_score: Math.max(...done.map(r => r.total_score)), last_status: 'done' };
      }
      // 无 done：取最新一条的 status（按 created_at DESC）
      const latest = pool.slice().sort((a,b) => (b.created_at||'').localeCompare(a.created_at||''))[0];
      return { ...t, last_score: null, last_status: latest?.status ?? null };
    });
  }, [tasks, runs, modelFilter]);

  const filtered = useMemo(() => {
    let r = enrichedTasks.filter(x => {
      if (tierFilter !== 'all' && x.milestone !== tierFilter) return false;
      if (typeFilter !== 'all' && x.task_type !== typeFilter) return false;
      if (scoreFilter === 'high' && (x.last_score == null || x.last_score < 0.8)) return false;
      if (scoreFilter === 'low'  && (x.last_score == null || x.last_score >= 0.8)) return false;
      if (scoreFilter === 'none' && x.last_score != null) return false;
      if (search) {
        const q = search.toLowerCase();
        if (!x.material_formula.toLowerCase().includes(q)
          && !x.task_id.toLowerCase().includes(q)) return false;
      }
      return true;
    });
    if (sort === 'recent') r.sort((a,b) => (b.last_run_at||'').localeCompare(a.last_run_at||''));
    else if (sort === 'score_desc') r.sort((a,b) => (b.last_score ?? -1) - (a.last_score ?? -1));
    else if (sort === 'score_asc')  r.sort((a,b) => (a.last_score ?? 9) - (b.last_score ?? 9));
    else if (sort === 'id') r.sort((a,b) => a.task_id.localeCompare(b.task_id));
    return r;
  }, [enrichedTasks, tierFilter, typeFilter, scoreFilter, sort, search]);

  return <div className="col" style={{ width: 360 }}>
    <div className="col-header">
      <h2>{t(lang, 'col.catalogue')}</h2>
      <span className="kbd mono">{filtered.length}/{tasks.length}</span>
      <span style={{ flex: 1 }} />
      <button className={`chip select-toggle ${selectMode ? 'active' : ''}`}
        onClick={() => setSelectMode(!selectMode)}>
        {selectMode ? t(lang, 'cat.select_mode.exit') : t(lang, 'cat.select_mode.enter')}
      </button>
    </div>
    {selectMode && <div className="cat-toolbar batch-bar">
      <div className="batch-actions">
        <span className="batch-count">{t(lang, 'cat.batch.selected_count', { n: selectedIds.length })}</span>
        {/* 代表性子集快捷选：覆盖当前选择，一键加载 fast / standard / full 三档之一。
            子集清单见 doc/reports/bench/report_representative_subset.md (v1.1) */}
        {subsets.length > 0 && <div className="subset-quick-picks" style={{ display: 'inline-flex', gap: 4, marginRight: 8 }}>
          <span style={{ fontSize: 11, color: 'var(--muted)', alignSelf: 'center', marginRight: 4 }}>
            {t(lang, 'cat.batch.subset.label')}
          </span>
          {subsets.map(sub => (
            <button key={sub.key} className="btn ghost sm"
              title={sub.description}
              onClick={() => selectSubset(sub.key)}>
              {lang === 'zh' ? sub.name : sub.name_en} ({sub.count})
            </button>
          ))}
        </div>}
        {/* All 选当前 filtered 全部（受下方 search/tier/type/score 协同限定）；
            再点把 filtered 内的取消（不动已过滤掉的预选）。 */}
        <button className="btn ghost sm"
          onClick={() => {
            const ids = filtered.map(t => t.task_id);
            const allOn = ids.length > 0 && ids.every(id => selectedSet.has(id));
            setState(s => {
              const cur = new Set(s.selectedTaskIds || []);
              if (allOn) ids.forEach(id => cur.delete(id));
              else ids.forEach(id => cur.add(id));
              return { ...s, selectedTaskIds: Array.from(cur) };
            });
          }}>
          {filtered.length > 0 && filtered.every(t => selectedSet.has(t.task_id))
            ? `Deselect (${filtered.length})` : `All (${filtered.length})`}
        </button>
        <button className="btn primary sm" disabled={!selectedIds.length}
          onClick={() => onOpenBatch(selectedIds)}>
          {t(lang, 'cat.batch.add_to_queue', { n: selectedIds.length })}
        </button>
        <button className="btn ghost sm" disabled={!selectedIds.length} onClick={clearSelection}>
          {t(lang, 'cat.batch.clear')}
        </button>
        <span style={{ flex: 1 }} />
        <button className="btn ghost sm" onClick={() => setSelectMode(false)}>
          {t(lang, 'cat.select_mode.exit')}
        </button>
      </div>
    </div>}
    <div className="cat-toolbar">
      <div className="search">
        <span className="icon">⌕</span>
        <input value={search} onChange={e => setSearch(e.target.value)}
          placeholder={t(lang, 'cat.search.placeholder')} />
      </div>
      {/* 5 个 FilterPill 平铺一行；M3/M4 加进来后选项变多但 pill 大小不变。 */}
      <div className="chips">
        <FilterPill label="tier" value={tierFilter}
          options={['all', ...tiersSeen]}
          onChange={setTierFilter} />
        <FilterPill label="task" value={typeFilter}
          options={['all', ...taskTypesSeen]}
          onChange={setTypeFilter} />
        <FilterPill label="score" value={scoreFilter}
          options={[
            { value: 'all', label: 'any' },
            { value: 'high', label: '≥0.8' },
            { value: 'low', label: '<0.8' },
            { value: 'none', label: '—' },
          ]}
          onChange={setScoreFilter} />
        <FilterPill label="sort" value={sort}
          options={[
            { value: 'recent', label: t(lang, 'cat.sort.recent') },
            { value: 'score_desc', label: t(lang, 'cat.sort.score_desc') },
            { value: 'score_asc', label: t(lang, 'cat.sort.score_asc') },
            { value: 'id', label: t(lang, 'cat.sort.id') },
          ]}
          onChange={setSort} />
        {/* model：'all' = 跨 model 最佳分；选 specific model = 该 model 最佳分 */}
        <FilterPill label="score by" value={modelFilter}
          options={[
            { value: 'all', label: 'all best' },
            ...(cfg?.available_models || []).filter(m => m.available !== false)
              .map(m => ({ value: m.id, label: m.id })),
          ]}
          onChange={setModelFilter} />
      </div>
    </div>
    <div className="col-scroll">
      <div className="cat-list">
        {filtered.map(task => {
          const isChecked = selectedSet.has(task.task_id);
          // polymorph 标识：material.name 首 token（"α-Sn" / "β-Sn" / "coesite" / "α-quartz"）
          // 跟 formula 不同时才显示，避免单形材料（"Si" / "Si"）冗余
          const variantLabel = (() => {
            const n = task.material_name;
            if (!n) return null;
            const head = n.split(/\s+/)[0];
            return head && head !== task.material_formula ? head : null;
          })();
          return <div key={task.task_id}
            className={`cat-row ${state.selectedTaskId === task.task_id ? 'selected' : ''} ${selectMode ? 'select-mode' : ''} ${isChecked ? 'checked' : ''}`}
            // .cat-row 默认 grid-template-columns: 1fr auto（两列：l + r）。selectMode
            // 时多一个 .row-check 子元素 → 第三个直接占下一行，导致每行内容换行。
            // 这里 inline 改 3 列：checkbox（auto）+ 主内容（1fr）+ score/when（auto）。
            style={selectMode ? { gridTemplateColumns: 'auto 1fr auto' } : undefined}
            onClick={() => {
              if (selectMode) { toggleSelect(task.task_id); return; }
              // 选 task 时挑 viewedRunId：优先该 task 最近一次「有结果」的 run（done/failed），
              // 这样切到 Result 标签能直接看到历史结果而不是 'Result not ready'；
              // 没完成 run 才 fallback 到最新（譬如 task 第一次跑、还在 running）。
              const taskRuns = state.runs
                .filter(r => r.task_id === task.task_id)
                .sort((a, b) => b.created_at.localeCompare(a.created_at));
              const finished = taskRuns.find(r => r.status === 'done' || r.status === 'failed');
              const pick = finished || taskRuns[0];
              setState(s => ({ ...s, selectedTaskId: task.task_id, tab: 'catalogue',
                centerTab: 'preview', viewedRunId: pick?.run_id || null }));
            }}>
            {selectMode && <span className={`row-check ${isChecked ? 'on' : ''}`}>
              {isChecked ? '✓' : ''}
            </span>}
            <div className="l">
              <div className="l1">
                <span className="formula">{task.material_formula}</span>
                {variantLabel && <span className="variant-pill" title={task.material_name}>{variantLabel}</span>}
                <span className="id mono">{task.task_id.split('_').slice(2).join('_')}</span>
              </div>
              <div className="l2">
                <span className="tt-chip">{task.milestone}·{task.task_type}·{task.task_subtype}</span>
                <span>{task.last_run_count} {t(lang, 'cat.runs')}</span>
              </div>
            </div>
            <div className="r">
              <ScoreMini score={task.last_score} status={task.last_status} />
              <span className="when">{fmtRel(task.last_run_at, lang)}</span>
            </div>
          </div>;
        })}
        {!filtered.length && <div className="empty-state">
          <div className="h">{t(lang, 'cat.empty')}</div>
        </div>}
      </div>
    </div>
  </div>;
}

function Preview({ task, setState, lang }) {
  if (!task) return <div className="empty-state"><div className="h">No task selected</div></div>;
  return <div className="doc">
    <div className="preview-head">
      <div className="lhs">
        <div className="meta-row">
          <span className="tt-chip">{task.milestone}·{task.task_type}·{task.task_subtype}</span>
          {!task.network_allowed && <span className="badge no-net">{t(lang, 'preview.no_network')}</span>}
          {(() => {
            const gtClass = normalizeGtQuality(task.gt_quality);
            const badgeCls =
                gtClass === 'self_run_converged' ? 'gt-good'
              : gtClass === 'tetra_rescued'      ? 'gt-rescued'
              : gtClass === 'tetra_failed'       ? 'gt-tetra-fail'
              : gtClass === 'marginal'           ? 'gt-marginal'
              :                                    'gt-keep';
            return <span className={`badge ${badgeCls}`} title={task.gt_quality}>
              {t(lang, `preview.gt_quality.${gtClass}`)}
            </span>;
          })()}
        </div>
        <h1 className="preview-title">{task.material_formula}</h1>
        <div className="sub">
          <span>{task.material_name}</span>
          <span>schema {task.schema_version}</span>
          <span>{task.task_id}</span>
        </div>
      </div>
      <button className="btn primary"
        onClick={() => setState(s => ({ ...s, tab: 'run', centerTab: 'config', configTaskId: task.task_id }))}>
        {t(lang, 'preview.head.start')} →
      </button>
    </div>

    <div className="sec">
      <h3 className="sec-title">{t(lang, 'preview.sec.prompt')} <span className="count">· {task.rendered_prompt.length} chars</span></h3>
      <div className="prompt">{task.rendered_prompt}</div>
    </div>

    <div className="sec-grid-2">
      <div className="sec">
        <h3 className="sec-title">{t(lang, 'preview.sec.material')}</h3>
        <div className="kv">
          <div className="k">{t(lang, 'preview.field.formula')}</div><div className="v">{task.material.f}</div>
          <div className="k">{t(lang, 'preview.field.name')}</div><div className="v" style={{ fontFamily: 'var(--sans)' }}>{task.material.n}</div>
          <div className="k">{t(lang, 'preview.field.system')}</div><div className="v">{task.material.sys}</div>
          <div className="k">{t(lang, 'preview.field.lattice_a')}</div><div className="v tnum">{task.material.a.toFixed(3)}</div>
          <div className="k">{t(lang, 'preview.field.family')}</div><div className="v" style={{ fontFamily: 'var(--sans)' }}>{task.material.fam}</div>
          <div className="k">{t(lang, 'preview.field.spacegroup')}</div><div className="v tnum">#{task.material.sg}</div>
        </div>
      </div>
      <div className="sec">
        <h3 className="sec-title">{t(lang, 'preview.sec.files')} <span className="count">· {task.initial_files.length}</span></h3>
        <div className="flist">
          {task.initial_files.map(f =>
            <div key={f.name} className="f-row">
              <span className="name">{f.name}</span>
              <span className="desc">{f.desc}</span>
              <span className="size">{fmtBytes(f.size)}</span>
            </div>)}
        </div>
      </div>
    </div>

    <div className="sec">
      <h3 className="sec-title">{t(lang, 'preview.sec.schema')} <span className="count">· {task.extract_fields.length} fields</span></h3>
      <div className="field-table">
        <div className="h">field</div><div className="h">type</div><div className="h">desc</div><div className="h" style={{ textAlign: 'right' }}>weight</div>
        {task.extract_fields.map(f => <React.Fragment key={f.name}>
          <div style={{ paddingLeft: 8 }}>{f.name}</div>
          <div>{f.type}</div>
          <div className="desc">{f.desc}</div>
          <div className="w tnum">{(f.weight*100).toFixed(1)}%</div>
        </React.Fragment>)}
      </div>
    </div>

    <div className="sec">
      <h3 className="sec-title">{t(lang, 'preview.sec.rubric')}</h3>
      <div>
        {task.extract_fields.map(f =>
          <div key={f.name} className="rubric-row">
            <span className="name">{f.name}</span>
            <span className="desc">{f.desc}</span>
            <span className="w tnum">{(f.weight*100).toFixed(1)}%</span>
            <span className="bar"><div style={{ width: `${f.weight*200}%` }} /></span>
          </div>)}
      </div>
    </div>

    {task.convergence_evidence && <div className="sec">
      <ConvergenceEvidence evi={task.convergence_evidence} lang={lang} />
    </div>}
    {!task.convergence_evidence && (() => {
      const gtClass = normalizeGtQuality(task.gt_quality);
      return gtClass === 'keep_mp' || gtClass === 'tetra_rescued' || gtClass === 'tetra_failed';
    })() && <div className="sec">
      <h3 className="sec-title">{t(lang, 'preview.sec.evidence')}</h3>
      <div className="evidence-placeholder">
        {t(lang, `preview.evidence.${task.gt_quality}`)}
      </div>
    </div>}
  </div>;
}

// ConvergenceEvidence 入口分发：T1 / T2 schema 完全不同（T1 = ecutwfc_scan +
// kmesh_scan dict 各两轮 sweep；T2 = scan_points list of 8 + classification 块 +
// canonical_point_key + dos_params）。按数据 shape 嗅探（evi.scan_points 是 list
// = T2 信号）。0501 fresh repo Phase 7 时只 T1 case 上手，T2 未跟进 → T2 题
// preview 折叠面板空白，0505 拆双分支修复（详见 todo/web/task-preview-display-fix.md）。
function ConvergenceEvidence({ evi, lang }) {
  if (Array.isArray(evi?.scan_points)) {
    return <ConvergenceEvidenceT2 evi={evi} lang={lang} />;
  }
  return <ConvergenceEvidenceT1 evi={evi} lang={lang} />;
}

function ConvergenceEvidenceT1({ evi, lang }) {
  const [open, setOpen] = useState(false);
  // backend 实际 schema：ecutwfc_scan / kmesh_scan = { values[_ry], delta_E_meV_per_atom,
  // delta_P_kbar, ... }（不是 sweep[].total_energy）。原 prototype mock 字段名是猜的，
  // 真实数据点开就 r.total_energy.toFixed undefined 整页崩。这里改成消费真实 schema：
  // 列 = 网格 / 收敛标准（ΔE/atom + ΔP）+ verdict（按 threshold 判）。
  const ecut = evi.ecutwfc_scan;
  const kmesh = evi.kmesh_scan;
  const thrE = evi.convergence_threshold?.delta_E_meV_per_atom ?? 0.5;
  const thrP = evi.convergence_threshold?.delta_P_kbar ?? 0.5;
  const fmtKmesh = (k) => Array.isArray(k) ? k.join('×') : String(k);
  const renderScan = (label, gridLabel, values, scan) => {
    if (!scan || !Array.isArray(values)) return null;
    const dE = scan.delta_E_meV_per_atom || [];
    const dP = scan.delta_P_kbar || [];
    return <>
      <div style={{ margin: '14px 0 12px', fontFamily: 'var(--sans)', color: 'var(--ink-3)', fontSize: 11 }}>{label}</div>
      <div className="evi-table">
        <div className="h">{gridLabel}</div>
        <div className="h">ΔE / atom (meV)</div>
        <div className="h">ΔP (kbar)</div>
        <div className="h">verdict</div>
        {values.map((v, i) => {
          const e = dE[i], p = dP[i];
          const ok = (e == null || e < thrE) && (p == null || Math.abs(p) < thrP);
          return <React.Fragment key={i}>
            <div className="tnum">{Array.isArray(v) ? fmtKmesh(v) : v}</div>
            <div className="tnum">{e == null ? '—' : e.toFixed(3)}</div>
            <div className="tnum">{p == null ? '—' : p.toFixed(3)}</div>
            <div style={{ color: ok ? 'var(--ok)' : 'var(--ink-3)' }}>{ok ? 'converged' : 'underconverged'}</div>
          </React.Fragment>;
        })}
      </div>
    </>;
  };
  return <div className="collapsible">
    <div className="collapsible-head" onClick={() => setOpen(!open)}>
      <span style={{ fontWeight: 600 }}>{t(lang, 'preview.sec.evidence')}</span>
      <span className="mono" style={{ color: 'var(--ink-3)' }}>{open ? '−' : '+'}</span>
    </div>
    {open && <div className="collapsible-body">
      {evi.methodology && <div style={{ marginBottom: 8, fontSize: 12, color: 'var(--ink-3)', lineHeight: 1.5 }}>{evi.methodology}</div>}
      {renderScan(`ecutwfc sweep · kmesh fixed ${fmtKmesh(ecut?.kmesh_fixed)}`, 'ecutwfc (Ry)', ecut?.values_ry, ecut)}
      {renderScan(`k-mesh sweep · ecutwfc fixed ${kmesh?.ecutwfc_fixed_ry} Ry`, 'kmesh', kmesh?.values, kmesh)}
      {evi.converged_params && <div style={{ marginTop: 12, fontSize: 11, color: 'var(--ink-3)' }}>
        converged: ecutwfc {evi.converged_params.ecutwfc_ry} Ry · kmesh {fmtKmesh(evi.converged_params.kmesh)}
      </div>}
    </div>}
  </div>;
}

function ConvergenceEvidenceT2({ evi, lang }) {
  const [open, setOpen] = useState(false);
  // T2 schema：scan_points = list of 8 dict（key/round/ecutwfc_ry/kmesh/band_gap_ev/
  // dos_e_fermi/vbm_k_label/cbm_k_label/gap_type）+ classification 块（含 round1/round2
  // 末两点 Δ）+ canonical_point_key（GT 来源） + canonical_kmesh + dos_params。
  // 渲染：8 点表格（canonical_point_key 那行高亮）+ classification 文字 + 末两点 Δ + 阈值。
  const points = evi.scan_points || [];
  const cls = evi.classification || {};
  const canonical = evi.canonical_point_key;
  const canonicalKmesh = evi.canonical_kmesh;
  const fmtKmesh = (k) => Array.isArray(k) ? k.join('×') : String(k);
  const fmtNum = (v, d = 4) => (typeof v === 'number' ? v.toFixed(d) : '—');
  const verdictColor = ({
    converged: 'var(--ok)', marginal: 'var(--warn, #c98a3b)', not_converged: 'var(--ink-3)',
  })[cls.classification] || 'var(--ink-3)';
  return <div className="collapsible">
    <div className="collapsible-head" onClick={() => setOpen(!open)}>
      <span style={{ fontWeight: 600 }}>{t(lang, 'preview.sec.evidence')}</span>
      <span className="mono" style={{ color: 'var(--ink-3)' }}>{open ? '−' : '+'}</span>
    </div>
    {open && <div className="collapsible-body">
      {/* Header: classification verdict + GT 来源 + wall_time */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'baseline', marginBottom: 10, fontSize: 12 }}>
        <span style={{ color: verdictColor, fontWeight: 600 }}>{cls.classification || '—'}</span>
        {canonical && <span style={{ color: 'var(--ink-3)' }}>GT @ <span className="mono">{canonical}</span></span>}
        {Array.isArray(canonicalKmesh) && <span style={{ color: 'var(--ink-3)' }}>kmesh <span className="mono">{fmtKmesh(canonicalKmesh)}</span></span>}
        {typeof evi.wall_time_sec === 'number' && evi.wall_time_sec > 0 &&
          <span style={{ color: 'var(--ink-3)' }}>wall {evi.wall_time_sec.toFixed(0)}s</span>}
      </div>
      {cls.reason && <div style={{ fontSize: 11, color: 'var(--ink-3)', marginBottom: 10 }}>{cls.reason}</div>}

      {/* 8 点 scan 表格 */}
      <div style={{ margin: '6px 0 8px', fontFamily: 'var(--sans)', color: 'var(--ink-3)', fontSize: 11 }}>
        scan points (round 1 = ecutwfc sweep · round 2 = kmesh sweep)
      </div>
      <div className="evi-table" style={{ gridTemplateColumns: 'repeat(7, auto)' }}>
        <div className="h">scan_key</div>
        <div className="h">round</div>
        <div className="h">ecutwfc (Ry)</div>
        <div className="h">kmesh</div>
        <div className="h">band_gap (eV)</div>
        <div className="h">dos_e_fermi (eV)</div>
        <div className="h">gap_type</div>
        {points.map((pt) => {
          const isCanonical = pt.key === canonical;
          const cellStyle = isCanonical
            ? { fontWeight: 600, color: 'var(--ok)' }
            : {};
          return <React.Fragment key={pt.key}>
            <div className="tnum" style={cellStyle}>
              {isCanonical && <span style={{ marginRight: 4 }}>★</span>}
              {pt.key}
            </div>
            <div className="tnum" style={cellStyle}>{pt.round}</div>
            <div className="tnum" style={cellStyle}>{fmtNum(pt.ecutwfc_ry, 0)}</div>
            <div className="tnum" style={cellStyle}>{fmtKmesh(pt.kmesh)}</div>
            <div className="tnum" style={cellStyle}>{fmtNum(pt.band_gap_ev, 3)}</div>
            <div className="tnum" style={cellStyle}>{fmtNum(pt.dos_e_fermi, 3)}</div>
            <div style={cellStyle}>{pt.gap_type || '—'}</div>
          </React.Fragment>;
        })}
      </div>

      {/* 末两点 Δ 块（classification round1/round2） */}
      {(cls.round1_ecutwfc || cls.round2_kmesh) && <>
        <div style={{ margin: '14px 0 6px', fontFamily: 'var(--sans)', color: 'var(--ink-3)', fontSize: 11 }}>
          末两点 Δ（评判收敛）
        </div>
        <div className="evi-table" style={{ gridTemplateColumns: 'repeat(4, auto)' }}>
          <div className="h">轮次</div>
          <div className="h">Δ band_gap (eV)</div>
          <div className="h">Δ Ef (eV)</div>
          <div className="h">末两点</div>
          {cls.round1_ecutwfc && <>
            <div>Round 1 (ecutwfc)</div>
            <div className="tnum">{fmtNum(cls.round1_ecutwfc.delta_band_gap_ev, 4)}</div>
            <div className="tnum">{fmtNum(cls.round1_ecutwfc.delta_e_fermi_ev, 4)}</div>
            <div className="mono" style={{ fontSize: 11, color: 'var(--ink-3)' }}>
              {(cls.round1_ecutwfc.last_two_keys || []).join(' → ') || '—'}
            </div>
          </>}
          {cls.round2_kmesh && <>
            <div>Round 2 (kmesh)</div>
            <div className="tnum">{fmtNum(cls.round2_kmesh.delta_band_gap_ev, 4)}</div>
            <div className="tnum">{fmtNum(cls.round2_kmesh.delta_e_fermi_ev, 4)}</div>
            <div className="mono" style={{ fontSize: 11, color: 'var(--ink-3)' }}>
              {(cls.round2_kmesh.last_two_keys || []).join(' → ') || '—'}
            </div>
          </>}
        </div>
        {cls.thresholds && <div style={{ marginTop: 8, fontSize: 11, color: 'var(--ink-3)' }}>
          阈值：band_gap_thr={cls.thresholds.band_gap_thr_ev} eV · e_fermi_thr={cls.thresholds.e_fermi_thr_ev} eV
          {cls.thresholds.marginal_factor && <> · marginal_factor={cls.thresholds.marginal_factor}</>}
        </div>}
      </>}

      {/* DOS params */}
      {evi.dos_params && <div style={{ marginTop: 12, fontSize: 11, color: 'var(--ink-3)' }}>
        DOS params: σ={evi.dos_params.sigma_ev} eV · dE={evi.dos_params.de_ev} eV
      </div>}
    </div>}
  </div>;
}

Object.assign(window, { Catalogue, Preview });
