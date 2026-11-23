// Mock data + i18n for the DFT-Agent v3 prototype
// All shapes mirror backend-api-contract.md schemas.

const I18N = {
  en: {
    'app.brand': 'DFT-Agent Bench',
    'tab.catalogue': 'Catalogue',
    'tab.compose': 'Compose',
    'tab.run': 'Launch',
    'tab.history': 'Runs',
    'tab.leaderboard': 'Leaderboard',
    'tab.admin': 'Admin',
    'leaderboard.title': 'Leaderboard',
    'leaderboard.subtitle': 'Compare agents × models across material / task groups (public)',
    'leaderboard.loading': 'Loading leaderboard…',
    'leaderboard.empty': 'No bench runs yet — once finished runs land, the matrix populates here.',
    'leaderboard.error.fetch': 'Failed to load leaderboard',
    'leaderboard.metric': 'Metric',
    'leaderboard.metric.score': 'score',
    'leaderboard.metric.wall': 'wall',
    'leaderboard.metric.qe': 'QE',
    'leaderboard.metric.turn': 'turns',
    'leaderboard.metric.cost': 'cost',
    'leaderboard.agg': 'Aggregate',
    'leaderboard.agg.avg': 'avg',
    'leaderboard.agg.best': 'best',
    'leaderboard.milestone': 'material',
    'leaderboard.tasktype': 'task',
    'leaderboard.date': 'Date',
    'leaderboard.date.today': 'today',
    'leaderboard.date.week': 'week',
    'leaderboard.date.month': 'month',
    'leaderboard.date.all': 'all',
    'leaderboard.owner': 'Owner',
    'leaderboard.owner.all': 'all',
    'leaderboard.owner.mine': 'mine',
    'leaderboard.compareall': 'Compare all',
    'leaderboard.compareall.hint': 'show all 5 metric matrices stacked',
    'leaderboard.cell.click_hint': 'click a cell to see its runs',
    'leaderboard.drill.title': 'Runs in this cell',
    'leaderboard.drill.empty': 'No runs in this cell',
    'leaderboard.drill.loading': 'Loading runs…',
    'leaderboard.drill.col.when': 'when',
    'leaderboard.drill.col.task': 'task',
    'leaderboard.drill.col.score': 'score',
    'leaderboard.drill.col.wall': 'wall',
    'leaderboard.drill.col.qe': 'QE',
    'leaderboard.drill.col.turn': 'turns',
    'leaderboard.drill.col.cost': 'cost',
    'leaderboard.drill.col.status': 'status',
    'leaderboard.drill.col.owner': 'owner',
    'leaderboard.drill.close': 'Close',
    'leaderboard.drill.go_to_result': 'Click row → open result',
    'leaderboard.drill.rerun_fails': 'Rerun fails ({n})',
    'leaderboard.drill.rerun_fails.tip': 'Re-queue {n} task(s) without any successful run',
    'leaderboard.drill.rerun_fails.empty': 'No fail-only tasks in this cell',
    'leaderboard.drill.exclude_unstable': 'exclude unstable',
    'leaderboard.drill.exclude_unstable.tip': 'Skip tasks with metadata.is_stable=false (dev-only experimental tasks)',
    'leaderboard.row.agent': 'agent',
    'leaderboard.row.model': 'model',
    'leaderboard.cell.empty': '—',
    'leaderboard.cell.n': 'n',
    'topbar.agent': 'agent',
    'centertab.preview': 'Preview',
    'centertab.result': 'Result',
    'centertab.config': 'Config',
    'centertab.live': 'Live',
    'launch.back': 'Back to Runs',
    'col.catalogue': 'Tasks',
    'cat.search.placeholder': 'Search formula or task id…',
    'cat.filter.tier': 'Material tier',
    'cat.filter.tasktype': 'Task type',
    'cat.filter.score': 'Score band',
    'cat.sort.recent': 'recent',
    'cat.sort.score_desc': 'score ↓',
    'cat.sort.score_asc': 'score ↑',
    'cat.sort.id': 'id',
    'cat.empty': 'No tasks match these filters',
    'cat.runs': 'runs',
    'cat.never': 'never',
    'preview.head.start': 'Start run',
    'preview.sec.prompt': 'Agent prompt',
    'preview.sec.material': 'Material',
    'preview.sec.files': 'Initial files',
    'preview.sec.schema': 'Extract schema',
    'preview.sec.rubric': 'Scoring rubric',
    'preview.sec.evidence': 'GT evidence (convergence sweep)',
    'preview.field.formula': 'formula',
    'preview.field.name': 'name',
    'preview.field.system': 'crystal system',
    'preview.field.lattice_a': 'lattice a (Å)',
    'preview.field.family': 'family',
    'preview.field.spacegroup': 'space group',
    'preview.no_network': 'no network',
    'preview.gt_quality.self_run_converged': 'GT self-run converged',
    'preview.gt_quality.marginal': 'GT marginal',
    'preview.gt_quality.keep_mp': 'GT from Materials Project',
    'preview.gt_quality.tetra_rescued': 'GT tetrahedra rescued',
    'preview.gt_quality.tetra_failed': 'GT tetrahedra not converged',
    'preview.gt_quality.unknown': 'GT quality unknown',
    'preview.evidence.self_run_failed_kept_mp': 'Self-run GT generation failed (QE error). GT preserved from Materials Project; no convergence sweep available.',
    'preview.evidence.self_run_not_converged_kept_mp': 'Self-run GT did not meet convergence threshold. GT preserved from Materials Project; no convergence sweep available.',
    'preview.evidence.self_run_tetra_rescued': 'Original 8-point convergence test failed under fixed-occupations; 2026-05-06 tetrahedra DOS rerun obtained a valid Fermi energy now used as GT.',
    'preview.evidence.self_run_tetra_failed': '2026-05-06 tetrahedra DOS rerun did not converge (NSCF c_bands). GT not reliable; question marked unstable.',
    'config.model': 'Model',
    'config.agent': 'Agent',
    'config.max_turns': 'Max turns',
    'config.timeout': 'Timeout',
    'config.parallelism': 'Parallelism',
    'config.solver_path': 'Solver path',
    'config.runs_dir': 'Runs dir',
    'config.seed': 'Seed',
    'config.bench_dir': 'Bench dir',
    'config.schema_version': 'Schema version',
    'config.enhance_model': 'Enhance model',
    'config.launch': 'Launch run',
    'cost_preview.label': 'Estimated cost',
    'cost_preview.history': 'Based on {n} past runs of this task / agent / model.',
    'cost_preview.heuristic': 'Rough estimate (no history yet for this combination).',
    'cost_preview.loading': 'Calculating…',
    'cost_preview.batch.total': 'Estimated total cost: ≈ {total} for {n} tasks.',
    'cost_preview.batch.breakdown': '{n_history} tasks have history (≥3 past runs); {n_heuristic} use heuristic estimate.',
    'history.search.placeholder': 'search run_id / task / formula / model…',
    'run.status.queued': 'Queued',
    'run.status.running': 'Running',
    'run.status.evaluating': 'Evaluating',
    'run.status.done': 'Done',
    'run.status.failed': 'Failed',
    'run.status.cancelled': 'Cancelled',
    'run.elapsed': 'elapsed',
    'run.turn_progress': 'turn',
    'run.currently': 'currently',
    'run.cancel': 'Abort run',
    'run.view_result': 'View result',
    'result.score': 'score',
    'result.status.pass': 'passing',
    'result.status.partial': 'partial',
    'result.status.fail': 'failing',
    'result.meta.task': 'task',
    'result.meta.model': 'model',
    'result.meta.agent': 'agent',
    'result.meta.wall': 'wall time',
    'result.meta.turns': 'turns',
    'result.meta.qe_calls': 'QE calls',
    'result.meta.qe_wall': 'QE GPU time',
    'result.meta.audit': 'audit',
    'result.diff.field': 'field',
    'result.diff.pred': 'predicted',
    'result.diff.gt': 'ground truth',
    'result.diff.score': 'score',
    'result.diff.visual': '',
    'result.audit': 'Audit flags',
    'result.summary': 'Run summary',
    'result.summary.input_tokens': 'input tokens',
    'result.summary.output_tokens': 'output tokens',
    'result.summary.wall': 'wall time',
    'result.summary.cost': 'cost (USD)',
    'result.rerun': 'Re-run',
    'result.download_bundle': 'Download bundle',
    'result.download_bundle_tip': 'tar.gz of workdir + GT + report — share with collaborators for offline review',
    'result.custom.badge': 'Custom run · no GT',
    'result.custom.no_submit': 'Agent did not submit a final answer',
    'result.custom.transcript': 'Transcript',
    'result.custom.final_answer': 'Agent final answer',
    'result.custom.spec': 'Original task spec',
    'result.failure_analysis': 'AI failure analysis',
    'result.failure_analysis_btn': 'Analyze with Claude 4.7',
    'result.failure_analysis_hint': 'Sends task spec + last bash output + last few turns to Claude for diagnosis. ~5s.',
    'result.failure_analysis_loading': 'Analyzing with Claude…',
    'result.failure_analysis_redo': 'Re-analyze',
    'result.failure_analysis_disclaimer': 'AI-generated diagnosis; verify before acting.',
    'result.harness_interrupted_banner': 'Harness was interrupted before grading. Showing solver-side outputs (no scoring).',
    'result.task_outputs': 'Solver-reported outputs',
    'live.waiting_llm': 'waiting for agent\'s next response…',
    // QE progress hint（transcript in-flight bash turn 下挂的灰色小字）
    'qe_progress.iter': 'iter {n}/{max}',
    'qe_progress.elapsed': 'elapsed {x}',
    'qe_progress.eta': 'ETA ~{x}',
    'qe_progress.last_activity': 'last activity {x}s ago',
    'qe_progress.bfgs_step': 'BFGS step {n}',
    'qe_progress.waiting': 'waiting for first iter…',
    'history.stats.avg': 'avg',
    'history.col.cost': 'cost',
    'history.col.when': 'when',
    'history.col.who': 'who',
    'history.col.mode': 'mode',
    'history.col.model': 'model',
    'history.col.score': 'score',
    'history.col.wall': 'wall',
    'history.col.qe': 'QE',
    'history.col.turn': 'turns',
    'history.col.status': 'status',
    'history.scope.today': 'today',
    'history.scope.week': 'week',
    'history.scope.all': 'all',
    'history.mode.bench': 'bench',
    'history.mode.custom': 'custom',
    'history.model.all': 'all',
    'history.milestone.all': 'M:all',
    'history.milestone.title': 'Filter by material group (M1 / M2 / M3)',
    'history.tasktype.all': 'T:all',
    'history.tasktype.title': 'Filter by task type (T1 / T2 / T3)',
    'compose.head.title': 'Compose a custom task',
    'compose.head.sub': 'Describe a material and what you want the agent to compute. No GT, no scoring — just the agent\u2019s transcript and final answer.',
    'compose.input.upload': 'Upload',
    'compose.input.describe': 'Describe',
    'compose.input.pick': 'Pick from bench',
    'compose.dropzone.h': 'Drop CIF or POSCAR file',
    'compose.dropzone.sub': 'or click to browse · max 1 MB · .cif .poscar .vasp',
    'compose.text.label': 'Material description',
    'compose.text.placeholder': 'e.g. silicon diamond, bulk fcc Cu, MoS2 monolayer',
    'compose.pick.label': 'Reuse the material from a bench task',
    'compose.task_type.label': 'Task type',
    'compose.task_type.placeholder': 'Select a task type…',
    'compose.prompt.label': 'Free-form prompt',
    'compose.prompt.placeholder': 'Describe what you want the agent to compute…',
    'compose.enhance.button': 'Enhance with AI',
    'compose.enhance.adopt': 'Adopt suggestion',
    'compose.enhance.discard': 'Discard',
    'compose.enhance.refine': 'Refine and re-enhance',
    'compose.enhance.original': 'Original',
    'compose.enhance.enhanced': 'Enhanced',
    'compose.launch': 'Launch custom run',
    'runstrip.label': 'Active runs',
    'tip.turn': 'One step in the agent loop: a single Claude API call that may produce text and call tools.',
    'tip.tool_use': 'A tool the agent invoked (bash, write_file, read_file, or submit).',
    'tip.submit': 'The terminal tool the agent calls to declare its final answer; ends the run.',
    'tip.audit': 'Boolean flags the harness writes alongside the score (network use, QE invoked, etc).',
    'tip.transcript': 'The full assistant/user/tool turn log written by the solver while the run is in flight.',
    'lang.toggle': 'Lang',
    // Iter-4: Catalogue multi-select
    'cat.select_mode.enter': 'Select',
    'cat.select_mode.exit': 'Cancel select',
    'cat.batch.selected_count': '{n} selected',
    'cat.batch.add_to_queue': 'Add to batch ({n})',
    'cat.batch.clear': 'Clear',
    'cat.batch.subset.label': 'Subset:',
    'lb.subset.label': 'Subset',
    'lb.subset.all': 'All tasks',
    // Iter-4: Runs tab
    'runs.max_parallel.label': 'Max parallel',
    'runs.queued_count': 'queued',
    'runs.running_count': 'running',
    'runs.queue_position': '#{n} of {total}',
    'runs.cancel_btn': 'Cancel',
    'runs.status_filter.all': 'all',
    'runs.status_filter.queued': 'queued',
    'runs.status_filter.running': 'running',
    'runs.status_filter.done': 'done',
    'runs.status_filter.failed': 'failed',
    'runs.status_filter.cancelled': 'cancelled',
    'max_concurrent_reduced_toast': 'max_concurrent_runs reduced; running runs continue, newly accepted will queue',
    // Iter-4: ComposeBatchDialog
    'batch_dialog.title': 'Launch batch ({n} runs)',
    'batch_dialog.tasks_label': 'Selected tasks',
    'batch_dialog.shared_config': 'Shared config (applies to all)',
    'batch_dialog.cancel': 'Cancel',
    'batch_dialog.launch': 'Launch {n} runs',
    'batch_submitting': 'Submitting {n} run(s)…',
    'batch_result_title': 'Batch launch result',
    'batch_result_summary_all_success': '{n} run(s) queued',
    'batch_result_summary_partial': '{success} queued, {failed} failed',
    'batch_result_summary_all_failed': 'All {n} runs failed to submit',
    'batch_result_failed_title': 'Failed items',
    'batch_result_retry_failed_item': 'Retry',
    'batch_result_retry_all_failed': 'Retry all failed',
    'batch_result_close': 'Close',
    'toast.batch_queued': '{n} run(s) queued',
    'admin.title': 'Cluster admin',
    'admin.subtitle': 'GPU / disk / scheduler / usage — dev role only',
    'admin.section.cluster': 'Cluster status',
    'admin.section.scheduler_control': 'Scheduler control',
    'admin.section.recycle': 'Recycle running runs',
    'admin.section.gpu_policy': 'GPU enable policy',
    'admin.section.qe_gpu_mode': 'QE GPU queueing mode',
    'admin.qe_gpu_mode.backend_pin': 'backend_pin (default, safe)',
    'admin.qe_gpu_mode.wrapper_flock': 'wrapper_flock (high concurrency, requires deployed wrapper)',
    'admin.qe_gpu_mode.hint': 'backend_pin = solver holds one GPU for whole lifetime (default). wrapper_flock = wrapper grabs a GPU per pw.x call via flock — needs flock-aware wrapper deployed (see deploy/qe-gpu-wrap.sh.example).',
    'admin.qe_gpu_mode.confirm_flock': 'Switch to wrapper_flock?\n\nThis only works if you have already deployed a flock-aware wrapper (see deploy/qe-gpu-wrap.sh.example).\n\nNew runs will use the new mode. Existing running runs are unaffected. You can switch back to backend_pin anytime — it is always safe.\n\nProceed?',
    'admin.qe_gpu_mode.saved': 'Saved',
    'admin.section.usage': 'Usage stats',
    'admin.scheduler.pause_btn': '⏸ Pause',
    'admin.scheduler.resume_btn': '▶ Resume',
    'admin.scheduler.pause_hint': 'Pause scheduler — running runs continue, queue holds until resumed',
    'admin.scheduler.resume_hint': 'Resume scheduler — queued runs will start as capacity opens',
    'admin.max_parallel.label': 'Max parallel',
    'admin.recycle.hint': 'Pauses scheduler, then re-enqueues every running run with identical hyperparameters, kills + deletes the originals (not cancel — the row is gone).',
    'admin.recycle.btn': 'Pause + re-enqueue + delete running',
    'admin.recycle.confirm': 'This will pause the scheduler, re-enqueue every running run, then KILL and DELETE the originals. Continue?',
    'admin.recycle.done': '{n} runs recycled',
    'admin.section.usage_by_model': 'By model',
    'admin.section.usage_by_owner': 'By owner',
    'admin.col.gpu': 'GPU',
    'admin.col.cpu': 'CPU',
    'admin.col.memory': 'Memory',
    'admin.cpu.cores': 'cores',
    'admin.cpu.load': 'load',
    'admin.cpu.freq': 'freq',
    'admin.cpu.per_core': 'per-core util',
    'admin.mem.used': 'used',
    'admin.mem.available': 'available',
    'admin.mem.swap': 'swap',
    'admin.col.util': 'util',
    'admin.col.mem': 'mem',
    'admin.col.temp': 'temp',
    'admin.col.procs': 'compute apps',
    'admin.col.scheduler': 'Scheduler',
    'admin.col.queued': 'queued',
    'admin.col.live': 'live',
    'admin.col.paused': 'paused',
    'admin.col.disk': 'Workspace disk',
    'admin.col.runs': 'runs',
    'admin.col.cost': 'cost (USD)',
    'admin.col.input_tokens': 'input',
    'admin.col.output_tokens': 'output',
    'admin.col.wall': 'wall',
    'admin.col.qe_wall': 'QE wall',
    'admin.col.owner': 'owner',
    'admin.col.model': 'model',
    'admin.gpu_policy.hint': 'Disabled GPUs are skipped on next promote. Already-running runs are not affected.',
    'admin.gpu_policy.save': 'Save policy',
    'admin.gpu_policy.saved': 'Saved',
    'admin.gpu_policy.empty_warn': 'All GPUs disabled — scheduler will not promote any new run.',
    'admin.usage.since': 'Since',
    'admin.usage.since.7d': 'last 7 days',
    'admin.usage.since.30d': 'last 30 days',
    'admin.usage.since.all': 'all time',
    'admin.usage.refresh': 'Refresh',
    'admin.usage.total': 'Total',
    'admin.usage.bucket.label': 'Bucket',
    'admin.usage.bucket.day': 'Day',
    'admin.usage.bucket.week': 'Week',
    'admin.usage.bucket.month': 'Month',
    'admin.usage.chart.title': 'Cost & runs over time',
    'admin.usage.chart.runs': 'runs',
    'admin.usage.chart.cost': 'cost (USD)',
    'admin.usage.chart.empty': 'no data in this window',
    'admin.usage.chart.dense_day': 'too many days — consider switching to Week',
    'admin.usage.chart.dense_week': 'too many weeks — consider switching to Month',
    'admin.app.ours': 'ours',
    'admin.app.others': 'others',
    'admin.refreshed': 'auto-refresh 2s',
    'admin.empty.compute_apps': 'no compute apps',
    'admin.empty.usage': 'no runs in this window',
    'admin.error.fetch': 'failed to load admin data',
    'admin.error.forbidden': 'admin endpoints require dev role — log in with a dev token',
    // F3 Step 5: tokens management（dev_role only）
    'tab.tokens': 'Tokens',
    'tokens.title': 'Token Management',
    'tokens.subtitle': 'Create / edit / delete access tokens — dev role only',
    'tokens.add_btn': 'Add Token',
    'tokens.refresh_btn': 'Refresh',
    'tokens.loading': 'Loading tokens…',
    'tokens.empty': 'No tokens configured.',
    'tokens.forbidden': 'Token management requires dev role — log in with a dev token.',
    'tokens.error.fetch': 'Failed to load tokens',
    'tokens.col.display_name': 'Display Name',
    'tokens.col.owner': 'Owner',
    'tokens.col.token_id': 'Token ID',
    'tokens.col.role': 'Role',
    'tokens.col.capabilities': 'Capabilities',
    'tokens.col.actions': 'Actions',
    'tokens.action.edit': 'Edit',
    'tokens.action.delete': 'Delete',
    'tokens.dev_role_badge': 'dev',
    'tokens.normal_badge': 'normal',
    'tokens.cap_more': '+{n} more',
    'tokens.cap_none': '— none —',
    'tokens.dev_role_banner': 'dev_role tokens bypass all capabilities; edit tokens.yaml to revoke.',
    // create modal
    'tokens.create.title': 'Add New Token',
    'tokens.create.field.owner': 'Owner',
    'tokens.create.field.owner.placeholder': 'unique identifier (e.g. alice, bob)',
    'tokens.create.field.display_name': 'Display Name',
    'tokens.create.field.display_name.placeholder': 'human-readable label',
    'tokens.create.field.capabilities': 'Capabilities',
    'tokens.create.submit': 'Create token',
    'tokens.create.cancel': 'Cancel',
    // edit modal
    'tokens.edit.title': 'Edit Token',
    'tokens.edit.field.display_name': 'Display Name',
    'tokens.edit.field.capabilities': 'Capabilities',
    'tokens.edit.save': 'Save',
    'tokens.edit.cancel': 'Cancel',
    // delete confirm
    'tokens.delete.title': 'Delete Token',
    'tokens.delete.confirm': 'Delete token for {owner} ({display_name})? This cannot be undone.',
    'tokens.delete.submit': 'Delete',
    'tokens.delete.cancel': 'Cancel',
    // plaintext one-time toast
    'tokens.plaintext.title': 'New token created',
    'tokens.plaintext.warning': 'Token created. Copy it now — it cannot be retrieved later.',
    'tokens.plaintext.copy_btn': 'Copy',
    'tokens.plaintext.copied': 'Copied!',
    'tokens.plaintext.dismiss_btn': "I've copied it",
    // capability grid
    'tokens.caps.select_all': 'Select all',
    'tokens.caps.select_none': 'Select none',
    'tokens.caps.group.visibility': 'Visibility',
    'tokens.caps.group.submit': 'Submit',
    'tokens.caps.group.manage': 'Manage',
    'tokens.caps.group.admin': 'Admin',
    // errors
    'tokens.error.unknown_cap': 'Unknown capability: {cap}',
    'tokens.error.self_delete': 'Cannot delete your own token',
    'tokens.error.last_dev': 'Cannot delete the last dev_role token',
    'tokens.error.owner_taken': 'Owner already used; owners must be unique',
    'tokens.error.empty_owner': 'Owner cannot be empty',
    'tokens.error.empty_name': 'Display name cannot be empty',
    'tokens.error.create': 'Failed to create token',
    'tokens.error.update': 'Failed to update token',
    'tokens.error.delete': 'Failed to delete token',
  },
  zh: {
    'app.brand': 'DFT-Agent 题库',
    'tab.leaderboard': '排行榜',
    'leaderboard.title': '排行榜',
    'leaderboard.subtitle': '在不同材料组 / 任务组上对比 agent × model 的表现（公榜）',
    'leaderboard.loading': '加载中…',
    'leaderboard.empty': '尚无 bench run；完成的 run 会在此聚合。',
    'leaderboard.error.fetch': '加载失败',
    'leaderboard.metric': '指标',
    'leaderboard.metric.score': '分数',
    'leaderboard.metric.wall': '总用时',
    'leaderboard.metric.qe': 'QE 次数',
    'leaderboard.metric.turn': '轮数',
    'leaderboard.metric.cost': '花费',
    'leaderboard.agg': '聚合',
    'leaderboard.agg.avg': 'avg',
    'leaderboard.agg.best': 'best',
    'leaderboard.milestone': '材料',
    'leaderboard.tasktype': '任务',
    'leaderboard.date': '时间',
    'leaderboard.date.today': '今天',
    'leaderboard.date.week': '本周',
    'leaderboard.date.month': '本月',
    'leaderboard.date.all': '全部',
    'leaderboard.owner': '提交者',
    'leaderboard.owner.all': '全部',
    'leaderboard.owner.mine': '我的',
    'leaderboard.compareall': '全 metric 对比',
    'leaderboard.compareall.hint': '一次展开 5 个 metric 的矩阵',
    'leaderboard.cell.click_hint': '点击 cell 查看该组 run 列表',
    'leaderboard.drill.title': '该格 run 列表',
    'leaderboard.drill.empty': '该 cell 暂无 run',
    'leaderboard.drill.loading': '加载中…',
    'leaderboard.drill.col.when': '时间',
    'leaderboard.drill.col.task': '题目',
    'leaderboard.drill.col.score': '分数',
    'leaderboard.drill.col.wall': '总用时',
    'leaderboard.drill.col.qe': 'QE',
    'leaderboard.drill.col.turn': '轮数',
    'leaderboard.drill.col.cost': '花费',
    'leaderboard.drill.col.status': '状态',
    'leaderboard.drill.col.owner': '提交者',
    'leaderboard.drill.close': '关闭',
    'leaderboard.drill.go_to_result': '点击行 → 打开结果',
    'leaderboard.drill.rerun_fails': '重跑 fail ({n})',
    'leaderboard.drill.rerun_fails.tip': '将 {n} 个无成功记录的题目作为新批次重新入队',
    'leaderboard.drill.rerun_fails.empty': '该 cell 没有 fail-only 任务',
    'leaderboard.drill.exclude_unstable': '排除 unstable',
    'leaderboard.drill.exclude_unstable.tip': '跳过 metadata.is_stable=false 的题目（开发中实验性任务）',
    'leaderboard.row.agent': 'agent',
    'leaderboard.row.model': 'model',
    'leaderboard.cell.empty': '—',
    'leaderboard.cell.n': 'n',
    'tab.catalogue': '题库',
    'topbar.agent': 'agent',
    'tab.compose': '自定义题目',
    'tab.run': '启动',
    'tab.history': '运行',
    'tab.admin': '管理',
    'centertab.preview': '预览',
    'centertab.result': '结果',
    'centertab.config': '配置',
    'centertab.live': '实况',
    'launch.back': '返回运行列表',
    'col.catalogue': '题目',
    'cat.search.placeholder': '搜索化学式或 task id…',
    'cat.filter.tier': '材料层级',
    'cat.filter.tasktype': '任务类型',
    'cat.filter.score': '分数区间',
    'cat.sort.recent': '最近',
    'cat.sort.score_desc': '分数 ↓',
    'cat.sort.score_asc': '分数 ↑',
    'cat.sort.id': 'id',
    'cat.empty': '没有匹配的题目',
    'cat.runs': '次',
    'cat.never': '从未运行',
    'preview.head.start': '开始运行',
    'preview.sec.prompt': 'Agent prompt',
    'preview.sec.material': '材料',
    'preview.sec.files': '初始文件',
    'preview.sec.schema': '抽取字段',
    'preview.sec.rubric': '评分细则',
    'preview.sec.evidence': 'GT 证据（收敛性扫描）',
    'preview.field.formula': '化学式',
    'preview.field.name': '名称',
    'preview.field.system': '晶系',
    'preview.field.lattice_a': '晶格常数 a (Å)',
    'preview.field.family': '族',
    'preview.field.spacegroup': '空间群',
    'preview.no_network': '禁网络',
    'preview.gt_quality.self_run_converged': 'GT 自跑已收敛',
    'preview.gt_quality.marginal': 'GT 边缘',
    'preview.gt_quality.keep_mp': 'GT 来自 Materials Project',
    'preview.gt_quality.tetra_rescued': 'GT tetrahedra 抢救',
    'preview.gt_quality.tetra_failed': 'GT tetrahedra 未收敛',
    'preview.gt_quality.unknown': 'GT 质量未知',
    'preview.evidence.self_run_failed_kept_mp': '自跑 GT 失败（QE 报错），保留 Materials Project 数据，无收敛扫描可展示。',
    'preview.evidence.self_run_not_converged_kept_mp': '自跑 GT 未达收敛阈值，保留 Materials Project 数据，无收敛扫描可展示。',
    'preview.evidence.self_run_tetra_rescued': '原 8 点收敛测试在 fixed-occupations 下失败；2026-05-06 在 dos NSCF 改 tetrahedra 重跑后拿到有效 Fermi 能，作为新 GT。',
    'preview.evidence.self_run_tetra_failed': '2026-05-06 tetrahedra DOS rerun NSCF c_bands 不收敛。GT 不可靠，题目标 unstable 隐藏。',
    'config.model': '模型',
    'config.agent': 'Agent',
    'config.max_turns': '最大轮数',
    'config.timeout': '超时',
    'config.parallelism': '并行',
    'config.solver_path': 'Solver 路径',
    'config.runs_dir': 'Runs 目录',
    'config.seed': '随机种子',
    'config.bench_dir': 'Bench 目录',
    'config.schema_version': 'Schema 版本',
    'config.enhance_model': '增强模型',
    'config.launch': '启动运行',
    'cost_preview.label': '预估花费',
    'cost_preview.history': '基于该 task / agent / model 组合的历史 {n} 次运行均值。',
    'cost_preview.heuristic': '粗略估算（该组合还没有历史数据）。',
    'cost_preview.loading': '计算中…',
    'cost_preview.batch.total': '预估总花费：约 {total}（共 {n} 个题目）。',
    'cost_preview.batch.breakdown': '其中 {n_history} 题有历史（≥3 次运行），{n_heuristic} 题走启发式估算。',
    'history.search.placeholder': '搜索 run_id / 任务 / 材料 / 模型…',
    'run.status.queued': '排队中',
    'run.status.running': '运行中',
    'run.status.evaluating': '评分中',
    'run.status.done': '完成',
    'run.status.failed': '失败',
    'run.status.cancelled': '已取消',
    'run.elapsed': '已用时',
    'run.turn_progress': '轮',
    'run.currently': '当前',
    'run.cancel': '中止运行',
    'run.view_result': '查看结果',
    'result.score': '总分',
    'result.status.pass': '通过',
    'result.status.partial': '部分通过',
    'result.status.fail': '失败',
    'result.meta.task': '题目',
    'result.meta.model': '模型',
    'result.meta.agent': 'Agent',
    'result.meta.wall': '挂钟时间',
    'result.meta.turns': '总轮数',
    'result.meta.qe_calls': 'QE 调用次数',
    'result.meta.qe_wall': 'QE 累计耗时',
    'result.meta.audit': '审计',
    'result.diff.field': '字段',
    'result.diff.pred': '预测值',
    'result.diff.gt': 'GT',
    'result.diff.score': '分数',
    'result.diff.visual': '',
    'result.audit': '审计标志',
    'result.summary': '运行摘要',
    'result.summary.input_tokens': '输入 tokens',
    'result.summary.output_tokens': '输出 tokens',
    'result.summary.wall': '总用时',
    'result.summary.cost': '成本（USD）',
    'result.rerun': '重新运行',
    'result.download_bundle': '下载归档',
    'result.download_bundle_tip': 'tar.gz：workdir 关键文件 + GT + report；离线发给协作者 review 用',
    'result.custom.badge': '自定义运行 · 无 GT',
    'result.custom.no_submit': 'Agent 未提交最终答案',
    'result.custom.transcript': '完整对话',
    'result.custom.final_answer': 'Agent 最终答案',
    'result.custom.spec': '原始任务描述',
    'result.failure_analysis': 'AI 故障分析',
    'result.failure_analysis_btn': '调用 Claude 4.7 分析',
    'result.failure_analysis_hint': '把任务描述 + 末尾 bash 输出 + 末尾几轮对话发给 Claude 诊断。约 5 秒。',
    'result.failure_analysis_loading': 'Claude 分析中…',
    'result.failure_analysis_redo': '重新分析',
    'result.failure_analysis_disclaimer': 'AI 生成诊断，仅供参考。',
    'result.harness_interrupted_banner': 'Harness 在评分前被中断。显示 solver 自报的产物（无评分）。',
    'result.task_outputs': 'Solver 自报产物',
    'live.waiting_llm': '等待 agent 下一轮回复…',
    // QE 实时进度提示（transcript in-flight bash turn 下挂的灰色小字）
    'qe_progress.iter': '迭代 {n}/{max}',
    'qe_progress.elapsed': '已跑 {x}',
    'qe_progress.eta': '预估剩余 ~{x}',
    'qe_progress.last_activity': '{x}s 前有活动',
    'qe_progress.bfgs_step': 'BFGS step {n}',
    'qe_progress.waiting': '等首次迭代…',
    'history.stats.avg': '平均',
    'history.col.cost': '花费',
    'history.col.when': '时间',
    'history.col.who': '题目',
    'history.col.mode': '模式',
    'history.col.model': '模型',
    'history.col.score': '分数',
    'history.col.wall': '总用时',
    'history.col.qe': 'QE',
    'history.col.turn': '轮数',
    'history.col.status': '状态',
    'history.scope.today': '今天',
    'history.scope.week': '本周',
    'history.scope.all': '全部',
    'history.mode.bench': 'bench',
    'history.mode.custom': 'custom',
    'history.model.all': '全部',
    'history.milestone.all': 'M:全部',
    'history.milestone.title': '按材料分组筛选（M1 / M2 / M3）',
    'history.tasktype.all': 'T:全部',
    'history.tasktype.title': '按任务类型筛选（T1 / T2 / T3）',
    'compose.head.title': '自定义题目',
    'compose.head.sub': '描述一个材料和你想让 agent 算的内容。没有 GT，不打分 — 只展示 agent 的对话和最终答案。',
    'compose.input.upload': '上传文件',
    'compose.input.describe': '文本描述',
    'compose.input.pick': '选 bench 材料',
    'compose.dropzone.h': '拖入 CIF 或 POSCAR 文件',
    'compose.dropzone.sub': '或点击选择 · 最大 1 MB · .cif .poscar .vasp',
    'compose.text.label': '材料描述',
    'compose.text.placeholder': '例如：silicon diamond / bulk fcc Cu / MoS2 monolayer',
    'compose.pick.label': '复用 bench 题目的材料',
    'compose.task_type.label': '任务类型',
    'compose.task_type.placeholder': '选择任务类型…',
    'compose.prompt.label': '自由提示词',
    'compose.prompt.placeholder': '描述你想让 agent 算什么…',
    'compose.enhance.button': '用 AI 改写',
    'compose.enhance.adopt': '采用',
    'compose.enhance.discard': '丢弃',
    'compose.enhance.refine': '修改后重写',
    'compose.enhance.original': '原始',
    'compose.enhance.enhanced': '改写后',
    'compose.launch': '提交跑',
    'runstrip.label': '活跃运行',
    'tip.turn': 'Agent 循环的一步：一次 Claude API 调用，可能产生文本和工具调用。',
    'tip.tool_use': 'Agent 调用的工具（bash / write_file / read_file / submit）。',
    'tip.submit': 'Agent 用以提交最终答案的终结工具，调用后 run 结束。',
    'tip.audit': 'harness 在打分之外写入的布尔标志（是否联网、是否调用了 QE 等）。',
    'tip.transcript': 'Solver 在运行时写入的完整 assistant/user/tool 对话日志。',
    'lang.toggle': '语言',
    // Iter-4: Catalogue multi-select
    'cat.select_mode.enter': '多选',
    'cat.select_mode.exit': '退出多选',
    'cat.batch.selected_count': '已选 {n} 项',
    'cat.batch.add_to_queue': '加入队列 ({n})',
    'cat.batch.clear': '清空',
    'cat.batch.subset.label': '子集：',
    'lb.subset.label': '子集',
    'lb.subset.all': '全部',
    // Iter-4: Runs tab
    'runs.max_parallel.label': '最大并行',
    'runs.queued_count': '排队',
    'runs.running_count': '运行中',
    'runs.queue_position': '#{n} / 共 {total}',
    'runs.cancel_btn': '取消',
    'runs.status_filter.all': '全部',
    'runs.status_filter.queued': '排队',
    'runs.status_filter.running': '运行中',
    'runs.status_filter.done': '完成',
    'runs.status_filter.failed': '失败',
    'runs.status_filter.cancelled': '已取消',
    'max_concurrent_reduced_toast': '最大并行已下调；正在运行的跑批继续，新接受的将进入队列',
    // Iter-4: ComposeBatchDialog
    'batch_dialog.title': '提交跑批（{n} 个运行）',
    'batch_dialog.tasks_label': '已选任务',
    'batch_dialog.shared_config': '共享配置（应用于所有任务）',
    'batch_dialog.cancel': '取消',
    'batch_dialog.launch': '提交 {n} 个跑批',
    'batch_submitting': '正在提交 {n} 个跑批…',
    'batch_result_title': '批量提交结果',
    'batch_result_summary_all_success': '{n} 个跑批已入队',
    'batch_result_summary_partial': '{success} 个已入队，{failed} 个失败',
    'batch_result_summary_all_failed': '{n} 个跑批全部提交失败',
    'batch_result_failed_title': '失败项',
    'batch_result_retry_failed_item': '重试',
    'batch_result_retry_all_failed': '重试全部失败项',
    'batch_result_close': '关闭',
    'toast.batch_queued': '{n} 个跑批已入队',
    'admin.title': '集群管理',
    'admin.subtitle': 'GPU / 磁盘 / 调度器 / 用量 — 仅 dev 可见',
    'admin.section.cluster': '集群状态',
    'admin.section.scheduler_control': '调度器控制',
    'admin.section.recycle': '回收当前运行中任务',
    'admin.section.gpu_policy': 'GPU 启用策略',
    'admin.section.qe_gpu_mode': 'QE GPU 排队模式',
    'admin.qe_gpu_mode.backend_pin': 'backend_pin（默认，安全）',
    'admin.qe_gpu_mode.wrapper_flock': 'wrapper_flock（高并发，需先部署 wrapper）',
    'admin.qe_gpu_mode.hint': 'backend_pin：spawn 时 pin 一张卡，整 solver 生命周期持有（默认）。wrapper_flock：wrapper 在 pw.x 调用级 flock 抢卡 —— 需先部署支持 flock 的 wrapper（见 deploy/qe-gpu-wrap.sh.example）。',
    'admin.qe_gpu_mode.confirm_flock': '切换到 wrapper_flock？\n\n这只在你已部署 flock-aware wrapper 后才工作（见 deploy/qe-gpu-wrap.sh.example）。\n\n新启动的 run 会用新模式；已 running 的 run 不受影响。切回 backend_pin 永远安全。\n\n继续？',
    'admin.qe_gpu_mode.saved': '已保存',
    'admin.scheduler.pause_btn': '⏸ 暂停',
    'admin.scheduler.resume_btn': '▶ 恢复',
    'admin.scheduler.pause_hint': '暂停调度器 —— 已 running 任务继续，queue 等待 resume',
    'admin.scheduler.resume_hint': '恢复调度器 —— queued 任务按容量自动 promote',
    'admin.max_parallel.label': '最大并行',
    'admin.recycle.hint': '一键暂停 scheduler，把每条 running 任务以相同超参重新入队，然后 kill 并删除原 run（不是 cancel —— DB 行直接抹除）。',
    'admin.recycle.btn': '暂停 + 重入队 + 删除当前运行中任务',
    'admin.recycle.confirm': '将暂停 scheduler、把所有 running 任务重新入队，然后 KILL 并 DELETE 原任务。继续？',
    'admin.recycle.done': '已回收 {n} 个任务',
    'admin.section.usage': '用量统计',
    'admin.section.usage_by_model': '按模型',
    'admin.section.usage_by_owner': '按用户',
    'admin.col.gpu': 'GPU',
    'admin.col.cpu': 'CPU',
    'admin.col.memory': '内存',
    'admin.cpu.cores': '核数',
    'admin.cpu.load': '负载',
    'admin.cpu.freq': '主频',
    'admin.cpu.per_core': '每核占用',
    'admin.mem.used': '已用',
    'admin.mem.available': '可用',
    'admin.mem.swap': '交换分区',
    'admin.col.util': '使用率',
    'admin.col.mem': '显存',
    'admin.col.temp': '温度',
    'admin.col.procs': '计算进程',
    'admin.col.scheduler': '调度器',
    'admin.col.queued': '排队',
    'admin.col.live': '运行',
    'admin.col.paused': '暂停',
    'admin.col.disk': '工作区磁盘',
    'admin.col.runs': '运行数',
    'admin.col.cost': '花费 (USD)',
    'admin.col.input_tokens': '输入',
    'admin.col.output_tokens': '输出',
    'admin.col.wall': '挂钟',
    'admin.col.qe_wall': 'QE 挂钟',
    'admin.col.owner': '用户',
    'admin.col.model': '模型',
    'admin.gpu_policy.hint': '禁用的 GPU 会在下次 promote 时被跳过；已 running 的任务不受影响。',
    'admin.gpu_policy.save': '保存策略',
    'admin.gpu_policy.saved': '已保存',
    'admin.gpu_policy.empty_warn': '所有 GPU 都被禁用 —— 调度器不会 promote 新任务。',
    'admin.usage.since': '时间窗',
    'admin.usage.since.7d': '近 7 天',
    'admin.usage.since.30d': '近 30 天',
    'admin.usage.since.all': '全部',
    'admin.usage.refresh': '刷新',
    'admin.usage.total': '总计',
    'admin.usage.bucket.label': '粒度',
    'admin.usage.bucket.day': '日',
    'admin.usage.bucket.week': '周',
    'admin.usage.bucket.month': '月',
    'admin.usage.chart.title': '花费与运行数随时间变化',
    'admin.usage.chart.runs': '运行数',
    'admin.usage.chart.cost': '花费 (USD)',
    'admin.usage.chart.empty': '此时间窗内无数据',
    'admin.usage.chart.dense_day': '日数过多，建议切换到周',
    'admin.usage.chart.dense_week': '周数过多，建议切换到月',
    'admin.app.ours': '我们',
    'admin.app.others': '他人',
    'admin.refreshed': '自动刷新 2 秒',
    'admin.empty.compute_apps': '无计算进程',
    'admin.empty.usage': '此时间窗内无任务',
    'admin.error.fetch': '加载管理数据失败',
    'admin.error.forbidden': 'admin 端点仅 dev 角色可访问 —— 请用 dev token 登录',
    // F3 Step 5: tokens 管理（仅 dev_role）
    'tab.tokens': 'Token',
    'tokens.title': 'Token 管理',
    'tokens.subtitle': '创建 / 编辑 / 删除访问 token —— 仅 dev 角色可见',
    'tokens.add_btn': '添加 Token',
    'tokens.refresh_btn': '刷新',
    'tokens.loading': '加载中…',
    'tokens.empty': '尚未配置任何 token。',
    'tokens.forbidden': 'Token 管理仅 dev 角色可访问 —— 请用 dev token 登录。',
    'tokens.error.fetch': '加载 token 列表失败',
    'tokens.col.display_name': '显示名',
    'tokens.col.owner': '用户',
    'tokens.col.token_id': 'Token ID',
    'tokens.col.role': '角色',
    'tokens.col.capabilities': '权限',
    'tokens.col.actions': '操作',
    'tokens.action.edit': '编辑',
    'tokens.action.delete': '删除',
    'tokens.dev_role_badge': 'dev',
    'tokens.normal_badge': 'normal',
    'tokens.cap_more': '+{n} 项',
    'tokens.cap_none': '— 无 —',
    'tokens.dev_role_banner': 'dev_role token bypass 所有权限，需编辑 tokens.yaml 才能撤销。',
    // 创建 modal
    'tokens.create.title': '新增 Token',
    'tokens.create.field.owner': '用户标识',
    'tokens.create.field.owner.placeholder': '唯一标识（如 alice / bob）',
    'tokens.create.field.display_name': '显示名',
    'tokens.create.field.display_name.placeholder': 'UI 上展示的名称',
    'tokens.create.field.capabilities': '权限',
    'tokens.create.submit': '创建 token',
    'tokens.create.cancel': '取消',
    // 编辑 modal
    'tokens.edit.title': '编辑 Token',
    'tokens.edit.field.display_name': '显示名',
    'tokens.edit.field.capabilities': '权限',
    'tokens.edit.save': '保存',
    'tokens.edit.cancel': '取消',
    // 删除确认
    'tokens.delete.title': '删除 Token',
    'tokens.delete.confirm': '删除 {owner}（{display_name}）的 token？此操作不可撤销。',
    'tokens.delete.submit': '删除',
    'tokens.delete.cancel': '取消',
    // 明文 token 一次性 toast
    'tokens.plaintext.title': 'Token 已创建',
    'tokens.plaintext.warning': 'Token 已创建。请立即复制 —— 之后无法再获取。',
    'tokens.plaintext.copy_btn': '复制',
    'tokens.plaintext.copied': '已复制！',
    'tokens.plaintext.dismiss_btn': '我已复制',
    // 权限 grid
    'tokens.caps.select_all': '全选',
    'tokens.caps.select_none': '清空',
    'tokens.caps.group.visibility': '可见性',
    'tokens.caps.group.submit': '提交',
    'tokens.caps.group.manage': '管理',
    'tokens.caps.group.admin': '管理员',
    // 错误
    'tokens.error.unknown_cap': '未知权限：{cap}',
    'tokens.error.self_delete': '不能删除自己的 token',
    'tokens.error.last_dev': '不能删除最后一个 dev_role token',
    'tokens.error.owner_taken': '该用户标识已被占用；owner 必须唯一',
    'tokens.error.empty_owner': '用户标识不能为空',
    'tokens.error.empty_name': '显示名不能为空',
    'tokens.error.create': '创建 token 失败',
    'tokens.error.update': '更新 token 失败',
    'tokens.error.delete': '删除 token 失败',
  },
};

// ============================================================================
// API client — talks to backend FastAPI (docs/03-api-contract.md)
// ----------------------------------------------------------------------------
// 三大宪法 §2：后端适配前端 schema；本 client 直接拼请求，shape 与 backend 严格对齐。
// 使用方式：app.jsx 通过 `await api.<method>(...)` 拿数据；mock 已删除。
// 调试：浏览器 console 可以 `await window.__api.getConfig()`。
// ============================================================================

const API_BASE = '';  // The browser only sends its token to this origin.

// 内部辅助：把 query params 拼成 ?k=v&...（None / undefined 跳过）
function _qs(params) {
  if (!params) return '';
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === '') continue;
    // 数组值用 append 表达多值（譬如 task_scope=M1_T1&task_scope=M2_T2）；
    // 标量走 set。空数组和空字符串一样跳过
    if (Array.isArray(v)) {
      for (const item of v) {
        if (item !== null && item !== undefined && item !== '') u.append(k, String(item));
      }
    } else {
      u.set(k, String(v));
    }
  }
  const s = u.toString();
  return s ? '?' + s : '';
}

/**
 * 统一 fetch 包装：处理 JSON 序列化 / error_code 解析 / 网络错误。
 *
 * - 成功（2xx）：返回解析后 JSON
 * - 失败（4xx/5xx）：reject `{status, code, message, raw}`
 * - 网络错误：reject `{status: 0, code: 'network_error', message, raw: null}`
 *
 * code 提取规则（与 backend main.unknown_exception_handler / FastAPI HTTPException 对齐）：
 * - 500 unknown_exception_handler 返 `{error_code, message}` → code = error_code
 * - 4xx FastAPI HTTPException 返 `{detail: '...'}` → code = '<status>_<inferred>'，message = detail
 * - 422 Pydantic 返 `{detail: [{...}]}` → code = 'validation_error'，message = JSON.stringify(detail)
 */
async function apiFetch(path, opts = {}) {
  const url = API_BASE + path;
  const init = {
    method: opts.method || 'GET',
    headers: { ...(opts.headers || {}) },
    // VS Code Simple Browser 对没有 Cache-Control 的 GET 偶尔会发 stale
    // 响应（pollRuns 拿到旧 status='running'，UI 卡住不切 failed）。强制 no-store
    // 确保每次都打到 backend；GET 都是轻量 query，性能影响可忽略。
    cache: 'no-store',
  };
  if (opts.body !== undefined) {
    if (opts.isFormData) {
      // FormData 自带 boundary；不要手 set Content-Type
      init.body = opts.body;
    } else {
      init.headers['Content-Type'] = init.headers['Content-Type'] || 'application/json';
      init.body = typeof opts.body === 'string' ? opts.body : JSON.stringify(opts.body);
    }
  }
  // Auth：sessionStorage AUTH_TOKEN 存在就附 Bearer。opts.skipAuth=true 用于登录前的 status / check 调用。
  if (!opts.skipAuth) {
    try {
      const token = window.sessionStorage.getItem('AUTH_TOKEN');
      if (token) init.headers['Authorization'] = `Bearer ${token}`;
    } catch (_e) { /* 隐私模式禁 localStorage */ }
  }
  let resp;
  try {
    resp = await fetch(url, init);
  } catch (e) {
    return Promise.reject({
      status: 0, code: 'network_error',
      message: e?.message || 'network error', raw: null,
    });
  }
  // 401：token 失效（被 server 端 rotate 或被踢）。清掉本地 + dispatch 全局事件，
  // app.jsx 监听 → 切回 splash。不直接 reload 防丢失页面状态。
  if (resp.status === 401 && !opts.skipAuth) {
    try { window.sessionStorage.removeItem('AUTH_TOKEN'); } catch (_e) {}
    try { window.dispatchEvent(new Event('auth:logout')); } catch (_e) {}
  }
  let raw = null;
  const ct = resp.headers.get('content-type') || '';
  try {
    raw = ct.includes('application/json') ? await resp.json() : await resp.text();
  } catch (_e) {
    raw = null;
  }
  if (resp.ok) return raw;
  // 失败：解析 error code
  let code, message;
  if (raw && typeof raw === 'object') {
    if (raw.error_code) {                            // 500 unknown_exception_handler
      code = raw.error_code;
      message = raw.message || resp.statusText;
    } else if (Array.isArray(raw.detail)) {          // 422 pydantic
      code = 'validation_error';
      message = JSON.stringify(raw.detail);
    } else if (typeof raw.detail === 'string') {     // 4xx HTTPException
      code = String(resp.status);
      message = raw.detail;
    } else {
      code = String(resp.status);
      message = resp.statusText;
    }
  } else {
    code = String(resp.status);
    message = (typeof raw === 'string' ? raw : '') || resp.statusText;
  }
  return Promise.reject({ status: resp.status, code, message, raw });
}

/**
 * 二进制下载（tar.gz / zip 等），与 apiFetch 同款 token 注入但不解析 JSON。
 * 拿到 blob 后触发浏览器下载（创建 a 元素 click + revokeObjectURL）。
 *
 * 失败 reject `{status, message}`；401 同 apiFetch 派发 auth:logout。
 */
async function apiDownload(path, filename) {
  const url = API_BASE + path;
  const init = { method: 'GET', headers: {}, cache: 'no-store' };
  try {
    const token = window.sessionStorage.getItem('AUTH_TOKEN');
    if (token) init.headers['Authorization'] = `Bearer ${token}`;
  } catch (_e) { /* 隐私模式禁 localStorage */ }
  let resp;
  try {
    resp = await fetch(url, init);
  } catch (e) {
    return Promise.reject({
      status: 0, code: 'network_error',
      message: e?.message || 'network error',
    });
  }
  if (resp.status === 401) {
    try { window.sessionStorage.removeItem('AUTH_TOKEN'); } catch (_e) {}
    try { window.dispatchEvent(new Event('auth:logout')); } catch (_e) {}
    return Promise.reject({ status: 401, code: 'unauthorized', message: 'token expired' });
  }
  if (!resp.ok) {
    let msg = resp.statusText;
    try {
      const det = await resp.json();
      msg = det.detail || msg;
    } catch (_e) { /* 不是 JSON 也无所谓 */ }
    return Promise.reject({ status: resp.status, code: String(resp.status), message: msg });
  }
  const blob = await resp.blob();
  const objUrl = window.URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = objUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => window.URL.revokeObjectURL(objUrl), 1000);
}

// ============================================================================
// 14 endpoints — 1:1 映射到 backend/api/{config,tasks,runs,compose}.py
// ============================================================================
const api = {
  // —— health ——
  // 富 health：harness_ok / solver_ok / bench_questions_ok / tasks_loaded
  getHealth: () => apiFetch('/health'),

  // —— auth ——
  // GET status：判断是否启用 auth + 当前 token 是否有效
  getAuthStatus: () => apiFetch('/api/auth/status'),
  // POST check：splash 登录表单校验 token（不依赖 localStorage，明文 body 提交一次）
  checkToken: (token) =>
    apiFetch('/api/auth/check', { method: 'POST', body: { token }, skipAuth: true }),

  // —— config ——
  getConfig: () => apiFetch('/api/config'),
  // 任一字段都允许 omit；后端只更新传入的字段。
  updateConfig: (patch) =>
    apiFetch('/api/config', {
      method: 'PUT',
      body: patch,
    }),

  // —— subsets ——
  // 代表性子集预设清单（v1.1：快速档 12 / 标准档 27 / 完整档 142）
  // 返 { version, date, subsets: [{key, name, name_en, description, task_ids, count}] }
  getSubsets: () => apiFetch('/api/subsets'),

  // —— tasks ——
  // params: { milestone, task_type, task_subtype, formula_q, sort, limit, offset }
  listTasks: (params) => apiFetch('/api/tasks' + _qs(params)),
  getTask: (taskId) => apiFetch('/api/tasks/' + encodeURIComponent(taskId)),
  getTaskAgentView: (taskId) =>
    apiFetch('/api/tasks/' + encodeURIComponent(taskId) + '/agent_view'),

  // —— runs ——
  // params: { source, status, mode, task_id, limit, offset }
  listRuns: (params) => apiFetch('/api/runs' + _qs(params)),
  getRun: (runId) => apiFetch('/api/runs/' + encodeURIComponent(runId)),
  getTranscript: (runId, since_turn) =>
    apiFetch('/api/runs/' + encodeURIComponent(runId) + '/transcript' +
      _qs({ since_turn: since_turn || 0 })),
  getResult: (runId) =>
    apiFetch('/api/runs/' + encodeURIComponent(runId) + '/result'),

  // QE 实时进度（transcript live ETA, 2026-05-08）
  // 返 {in_flight, binary, calc_name, iter_n, iter_max, elapsed_sec, eta_sec,
  //     last_activity_sec, phase, reason}；in_flight=false 时前端不渲染
  // 但仍 polling，下一次 QE 调用启动会自动切到 in_flight=true
  getQeProgress: (runId) =>
    apiFetch('/api/runs/' + encodeURIComponent(runId) + '/qe_progress'),

  // 下载 run 的 case bundle（tar.gz：workdir 关键文件 + GT + report + auto README）
  // 公开 endpoint，与 curve_field 同政策；返回触发浏览器下载，无 JSON 返回
  downloadCaseBundle: (runId) =>
    apiDownload(
      '/api/runs/' + encodeURIComponent(runId) + '/case_bundle',
      runId + '.tar.gz',
    ),

  // 失败分析：GET 拿 cache（没有则 204/null）；POST 触发分析
  getFailureAnalysis: (runId) =>
    apiFetch('/api/runs/' + encodeURIComponent(runId) + '/failure_analysis'),
  analyzeFailure: (runId, force) =>
    apiFetch('/api/runs/' + encodeURIComponent(runId) + '/failure_analysis' +
      (force ? '?force=1' : ''), { method: 'POST', body: {} }),

  // bench: { custom: false, task_id, model, max_turns, solver_path?, solver_args? }
  // custom: { custom: true, prompt, material, task_type, model, max_turns, max_output_tokens, ... }
  startRun: (req) => apiFetch('/api/runs', { method: 'POST', body: req }),

  // { specs: [{task_id, model?, max_turns?}], model, max_turns }
  startBatch: (req) => apiFetch('/api/runs:batch', { method: 'POST', body: req }),

  // F2 cost preview（pre-launch USD estimate）
  // single: { task_id, agent_id, model_id, max_turns } → { est_usd, source, n_samples }
  previewCost: (req) =>
    apiFetch('/api/runs:preview-cost', { method: 'POST', body: req }),
  // batch: { task_ids: [...], agent_id, model_id, max_turns } → { per_task: [...], total_est_usd, n_with_history, n_heuristic }
  previewCostBatch: (req) =>
    apiFetch('/api/runs:preview-cost-batch', { method: 'POST', body: req }),

  cancelRun: (runId, reason) =>
    apiFetch('/api/runs/' + encodeURIComponent(runId) + ':cancel',
      { method: 'POST', body: { reason: reason || null } }),

  // 批量删 runs（active 自动 cancel + DELETE row + rmtree run_dir）
  deleteRuns: (runIds) =>
    apiFetch('/api/runs:delete_batch',
      { method: 'POST', body: { run_ids: runIds } }),

  // 一键取消所有 queued + running + evaluating（不删 row，保留为 cancelled）
  cancelActive: () =>
    apiFetch('/api/runs:cancel_active', { method: 'POST', body: {} }),

  // —— scheduler pause / resume（软暂停：停止 promote queue→running，running 不动） ——
  getSchedulerState: () => apiFetch('/api/runs:scheduler_state'),
  pauseScheduler: () => apiFetch('/api/runs:pause', { method: 'POST', body: {} }),
  resumeScheduler: () => apiFetch('/api/runs:resume', { method: 'POST', body: {} }),

  // —— compose ——
  // { user_description, task_type, material_hint? }
  enhancePrompt: (req) =>
    apiFetch('/api/compose:enhance', { method: 'POST', body: req }),

  // file: File | Blob (FormData append 'file')
  uploadMaterial: (file) => {
    const fd = new FormData();
    fd.append('file', file);
    return apiFetch('/api/compose:upload', {
      method: 'POST', body: fd, isFormData: true,
    });
  },

  // —— leaderboard（公榜，登录用户都可见）——
  // params: { date_from, date_to, owner, agent_id, task_scope: ['M1_T1',...], aggregation: 'avg'|'best' }
  getLeaderboard: (params) => apiFetch('/api/leaderboard' + _qs(params)),
  // drill-down：某 (agent_id, model) cell 下的 run 列表（同 leaderboard 的 scope/owner/date filter）
  getLeaderboardRuns: (params) => apiFetch('/api/leaderboard/runs' + _qs(params)),

  // —— admin（dev only；非 dev 调用 → 403）——
  // GPU/disk/scheduler 实时快照（前端 5s 刷新；后端有 2.5s cache）
  getClusterState: () => apiFetch('/api/admin/cluster_state'),
  // since: ISO Z 字符串可选（'2026-05-01T00:00:00Z' 等）；空 → 全表
  // bucket: 'day' | 'week' | 'month' 可选 → 后端按该粒度返回 by_period 时间序列
  getUsageStats: (since, bucket) => apiFetch('/api/admin/usage_stats' + _qs({ since, bucket })),
  getGpuPolicy: () => apiFetch('/api/admin/gpu_policy'),
  // body: { enabled: [0, 1, ...] }
  setGpuPolicy: (enabled) =>
    apiFetch('/api/admin/gpu_policy', { method: 'POST', body: { enabled } }),
  // QE GPU 排队模式（backend_pin / wrapper_flock）
  getQeGpuMode: () => apiFetch('/api/admin/qe_gpu_mode'),
  setQeGpuMode: (mode) =>
    apiFetch('/api/admin/qe_gpu_mode', { method: 'POST', body: { mode } }),
  // 一键 pause + 把 running runs 用相同超参重新入队 + kill+delete 旧 run
  recycleRunning: () =>
    apiFetch('/api/admin/runs:recycle_running', { method: 'POST', body: {} }),

  // —— tokens 管理（F3 Step 5；dev_role only；非 dev 直接 403）——
  // GET registry：capability 全集 + 分组 + 中文描述 → UI 渲染 checkbox grid
  getTokensRegistry: () => apiFetch('/api/tokens/registry'),
  // GET list：返回 [{token_id, token_masked, owner, display_name, is_dev_role, capabilities}, ...]
  listTokens: () => apiFetch('/api/tokens'),
  // POST create：body {owner, display_name, capabilities?}；返回 {token, token_id, ...}
  // 明文 token 仅创建时返回**一次**——UI 必须立即提示用户复制；关闭后无法找回
  createToken: (body) =>
    apiFetch('/api/tokens', { method: 'POST', body }),
  // PATCH update：body {capabilities?, display_name?}；is_dev_role **不能**通过 API 改
  updateToken: (tokenId, patch) =>
    apiFetch('/api/tokens/' + encodeURIComponent(tokenId),
      { method: 'PATCH', body: patch }),
  // DELETE：自删 / 最后一个 dev_role token → 400
  deleteToken: (tokenId) =>
    apiFetch('/api/tokens/' + encodeURIComponent(tokenId),
      { method: 'DELETE' }),
};

// ============================================================================
// Window 暴露：仅 I18N + __api（dev 调试）
// ============================================================================
window.I18N = I18N;
window.__api = api;
window.__API_BASE = API_BASE;
