/* eslint-disable no-undef */
// tokens.jsx — F3 Step 5：Token 管理 UI（dev_role only）。
//
// 设计要点
// =======
// - 整页走 isDevRole(user) gate：URL 直拼 ?tab=tokens 也无济于事，组件内 early-return
//   <div>Forbidden</div>，且不发任何 API 请求（避免 timing leak + 无谓 403 round-trip）。
// - Add / Edit 共用 CapabilityCheckboxGrid 组件：4 个 group（visibility / submit /
//   manage / admin）按 backend `/api/tokens/registry` 返回的 groups dict 渲染。
// - is_dev_role token 编辑：grid disabled + 灰显 banner，提示需改 yaml。Backend
//   也不接受 PATCH is_dev_role，所以前端禁用纯属 UX 提示。
// - Add 流程：成功后展示 plaintext token banner + Copy 按钮 + 用户主动 dismiss
//   关闭，关闭后从内存丢弃。Backend create endpoint 仅返回一次明文。
// - Delete：confirm dialog；自删 / 末 dev token 时 backend 返 400，UI 转译错误为
//   actionable 中文文案（'不能删除自己的 token' / '不能删除最后一个 dev_role token'）。
//
// 加载顺序：index.html 中放在 admin.jsx 之后、app.jsx 之前；通过 window.TokensPage
// 暴露给 app.jsx 路由。

const { useState: useStateT, useEffect: useEffectT, useMemo: useMemoT, useRef: useRefT } = React;


// ============================================================================
// CapabilityCheckboxGrid — Add / Edit 共用的 capability checkbox grid。
// 4 group（visibility / submit / manage / admin）按 registry.groups 渲染；
// 每 group 有 select all / select none 快捷链接 + tooltip 显示 description。
// disabled=true 时整 grid 灰显，可选 disabledReason banner 一行提示。
// ============================================================================
function CapabilityCheckboxGrid({ registry, value, onChange, disabled = false, disabledReason = null, lang }) {
  // value 是当前选中 cap id 列表；onChange(newList) 由父级管 state。
  const valueSet = useMemoT(() => new Set(value || []), [value]);
  if (!registry) {
    return <div className="tokens-cap-grid-loading">{lang === 'zh' ? '加载权限列表…' : 'Loading capabilities…'}</div>;
  }
  const { groups = {}, descriptions = {} } = registry;
  const groupKeys = ['visibility', 'submit', 'manage', 'admin'];

  const toggle = (cap) => {
    if (disabled) return;
    const next = new Set(valueSet);
    if (next.has(cap)) next.delete(cap);
    else next.add(cap);
    onChange(Array.from(next));
  };
  const selectAllInGroup = (caps) => {
    if (disabled) return;
    const next = new Set(valueSet);
    caps.forEach(c => next.add(c));
    onChange(Array.from(next));
  };
  const selectNoneInGroup = (caps) => {
    if (disabled) return;
    const next = new Set(valueSet);
    caps.forEach(c => next.delete(c));
    onChange(Array.from(next));
  };

  return <div className={`tokens-cap-grid ${disabled ? 'is-disabled' : ''}`}>
    {disabled && disabledReason && <div className="tokens-cap-disabled-banner">{disabledReason}</div>}
    {groupKeys.map(gk => {
      const caps = groups[gk] || [];
      if (caps.length === 0) return null;
      const allSelected = caps.every(c => valueSet.has(c));
      const noneSelected = caps.every(c => !valueSet.has(c));
      return <div key={gk} className="tokens-cap-group">
        <div className="tokens-cap-group-head">
          <span className="tokens-cap-group-title">{t(lang, `tokens.caps.group.${gk}`)}</span>
          <span className="tokens-cap-group-actions">
            <button type="button" className="tokens-cap-link"
              disabled={disabled || allSelected}
              onClick={() => selectAllInGroup(caps)}>
              {t(lang, 'tokens.caps.select_all')}
            </button>
            <span className="tokens-cap-link-sep">·</span>
            <button type="button" className="tokens-cap-link"
              disabled={disabled || noneSelected}
              onClick={() => selectNoneInGroup(caps)}>
              {t(lang, 'tokens.caps.select_none')}
            </button>
          </span>
        </div>
        <div className="tokens-cap-group-body">
          {caps.map(cap => {
            const desc = descriptions[cap] || cap;
            const checked = valueSet.has(cap);
            return <label key={cap} className={`tokens-cap-item ${checked ? 'checked' : ''} ${disabled ? 'disabled' : ''}`}
              title={desc}>
              <input type="checkbox" checked={checked} disabled={disabled}
                onChange={() => toggle(cap)} />
              <span className="tokens-cap-name mono">{cap}</span>
              <span className="tokens-cap-desc">{desc}</span>
            </label>;
          })}
        </div>
      </div>;
    })}
  </div>;
}


// ============================================================================
// PlaintextTokenBanner — create 成功后展示的一次性 banner。
// monospace token 字符串 + Copy 按钮（复制成功临时显示 "Copied!"）+ 用户主动
// dismiss 关闭。dismiss 后从父级 state 删除（明文 token 不再保留）。
// ============================================================================
function PlaintextTokenBanner({ token, owner, displayName, onDismiss, lang }) {
  const [copied, setCopied] = useStateT(false);
  const copy = async () => {
    try {
      // 标准 clipboard API；https / localhost 才允许。fallback：select + execCommand。
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(token);
      } else {
        // legacy fallback
        const ta = document.createElement('textarea');
        ta.value = token;
        ta.setAttribute('readonly', '');
        ta.style.position = 'absolute'; ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (_e) {
      // 复制失败时仍允许用户手动选中——明文已经显示，UX 不阻塞
    }
  };
  return <div className="tokens-plaintext-overlay" role="dialog" aria-modal="true">
    <div className="tokens-plaintext-card">
      <div className="tokens-plaintext-head">
        <h3>{t(lang, 'tokens.plaintext.title')}</h3>
        <div className="tokens-plaintext-meta">
          <span className="mono">{owner}</span>
          <span className="sep">·</span>
          <span>{displayName}</span>
        </div>
      </div>
      <div className="tokens-plaintext-warn">{t(lang, 'tokens.plaintext.warning')}</div>
      <div className="tokens-plaintext-token-row">
        <code className="tokens-plaintext-token mono">{token}</code>
        <button type="button" className="btn primary tokens-plaintext-copy-btn"
          onClick={copy}>
          {copied ? t(lang, 'tokens.plaintext.copied') : t(lang, 'tokens.plaintext.copy_btn')}
        </button>
      </div>
      <div className="tokens-plaintext-foot">
        <button type="button" className="btn primary" onClick={onDismiss}>
          {t(lang, 'tokens.plaintext.dismiss_btn')}
        </button>
      </div>
    </div>
  </div>;
}


// ============================================================================
// TokenCreateModal — Add token 表单。
// state: { owner, display_name, capabilities }；提交后调 createToken endpoint。
// 成功后 onSuccess(plaintextToken, owner, display_name) 由父级展示 plaintext banner。
// ============================================================================
function TokenCreateModal({ registry, onClose, onSuccess, lang }) {
  const [owner, setOwner] = useStateT('');
  const [displayName, setDisplayName] = useStateT('');
  // 初始勾选 view.others_runs：等价当前 normal 行为（能看别人的 run）。
  // user 0508 决策："默认值设成都看得到"。dev 想限制某 token 只看自己 run 时
  // 在 grid 取消勾选这个 cap 即可——提交 [] 是真空，被后端尊重（不会再注入默认）。
  const [caps, setCaps] = useStateT([CAP_VIEW_OTHERS_RUNS]);
  const [submitting, setSubmitting] = useStateT(false);
  const [error, setError] = useStateT(null);

  // ESC 关闭（不在 submitting 时禁止）
  useEffectT(() => {
    const onKey = (e) => {
      if (e.key === 'Escape' && !submitting) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [submitting, onClose]);

  const submit = async (e) => {
    e?.preventDefault();
    setError(null);
    if (!owner.trim()) {
      setError(t(lang, 'tokens.error.empty_owner'));
      return;
    }
    if (!displayName.trim()) {
      setError(t(lang, 'tokens.error.empty_name'));
      return;
    }
    setSubmitting(true);
    try {
      const resp = await window.__api.createToken({
        owner: owner.trim(),
        display_name: displayName.trim(),
        capabilities: caps,
      });
      // 把明文 token + meta 抛给父级；父级关闭 modal 同时打开 PlaintextBanner
      onSuccess({
        token: resp.token,
        owner: resp.owner,
        display_name: resp.display_name,
      });
    } catch (e) {
      setSubmitting(false);
      // 转译已知 backend 错误为本地化提示
      const msg = e?.message || String(e);
      if (msg.includes('already used')) {
        setError(t(lang, 'tokens.error.owner_taken'));
      } else if (msg.includes('unknown capabilities')) {
        // 不展开具体 cap，统一提示 grid 校验问题
        setError(t(lang, 'tokens.error.unknown_cap', { cap: msg.replace(/^.*unknown capabilities:?\s*/, '') }));
      } else {
        setError(`${t(lang, 'tokens.error.create')}: ${msg}`);
      }
    }
  };

  return <div className="tokens-dlg-overlay" onClick={(e) => {
    if (e.target === e.currentTarget && !submitting) onClose();
  }}>
    <div className="tokens-dlg" role="dialog" aria-modal="true">
      <form onSubmit={submit}>
        <div className="tokens-dlg-head">
          <div className="tokens-dlg-title">{t(lang, 'tokens.create.title')}</div>
          <button type="button" className="icon-btn" onClick={onClose} disabled={submitting} aria-label="close">×</button>
        </div>
        <div className="tokens-dlg-body">
          <div className="tokens-dlg-section">
            <div className="tokens-form-grid">
              <label className="tokens-form-field">
                <span className="k">{t(lang, 'tokens.create.field.owner')} *</span>
                <input type="text" autoFocus value={owner} maxLength={64}
                  placeholder={t(lang, 'tokens.create.field.owner.placeholder')}
                  onChange={(e) => { setOwner(e.target.value); if (error) setError(null); }}
                  disabled={submitting} />
              </label>
              <label className="tokens-form-field">
                <span className="k">{t(lang, 'tokens.create.field.display_name')} *</span>
                <input type="text" value={displayName} maxLength={128}
                  placeholder={t(lang, 'tokens.create.field.display_name.placeholder')}
                  onChange={(e) => { setDisplayName(e.target.value); if (error) setError(null); }}
                  disabled={submitting} />
              </label>
            </div>
          </div>
          <div className="tokens-dlg-section">
            <div className="tokens-dlg-label">{t(lang, 'tokens.create.field.capabilities')}</div>
            <CapabilityCheckboxGrid registry={registry} value={caps} onChange={setCaps} lang={lang} />
          </div>
          {error && <div className="tokens-dlg-err">{error}</div>}
        </div>
        <div className="tokens-dlg-foot">
          <button type="button" className="btn ghost" onClick={onClose} disabled={submitting}>
            {t(lang, 'tokens.create.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={submitting}>
            {submitting ? '…' : t(lang, 'tokens.create.submit')}
          </button>
        </div>
      </form>
    </div>
  </div>;
}


// ============================================================================
// TokenEditModal — Edit display_name / capabilities。
// is_dev_role token：grid disabled + banner 提示。display_name 仍可编辑（backend
// 接受）；caps 不发出（PATCH 不送 capabilities 字段，backend 视作不变）。
// ============================================================================
function TokenEditModal({ tok, registry, onClose, onSaved, lang }) {
  const [displayName, setDisplayName] = useStateT(tok.display_name || '');
  const [caps, setCaps] = useStateT(tok.capabilities || []);
  const [submitting, setSubmitting] = useStateT(false);
  const [error, setError] = useStateT(null);

  useEffectT(() => {
    const onKey = (e) => { if (e.key === 'Escape' && !submitting) onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [submitting, onClose]);

  const submit = async (e) => {
    e?.preventDefault();
    setError(null);
    if (!displayName.trim()) {
      setError(t(lang, 'tokens.error.empty_name'));
      return;
    }
    setSubmitting(true);
    try {
      // dev_role token：caps 不发（grid 禁用，本身也是只读）；只送 display_name
      const patch = tok.is_dev_role
        ? { display_name: displayName.trim() }
        : { display_name: displayName.trim(), capabilities: caps };
      const resp = await window.__api.updateToken(tok.token_id, patch);
      onSaved(resp);
    } catch (e) {
      setSubmitting(false);
      const msg = e?.message || String(e);
      if (msg.includes('unknown capabilities')) {
        setError(t(lang, 'tokens.error.unknown_cap', { cap: msg.replace(/^.*unknown capabilities:?\s*/, '') }));
      } else {
        setError(`${t(lang, 'tokens.error.update')}: ${msg}`);
      }
    }
  };

  return <div className="tokens-dlg-overlay" onClick={(e) => {
    if (e.target === e.currentTarget && !submitting) onClose();
  }}>
    <div className="tokens-dlg" role="dialog" aria-modal="true">
      <form onSubmit={submit}>
        <div className="tokens-dlg-head">
          <div className="tokens-dlg-title">
            {t(lang, 'tokens.edit.title')}
            <span className="tokens-dlg-title-meta mono">· {tok.owner} · {tok.token_masked}</span>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} disabled={submitting} aria-label="close">×</button>
        </div>
        <div className="tokens-dlg-body">
          <div className="tokens-dlg-section">
            <label className="tokens-form-field">
              <span className="k">{t(lang, 'tokens.edit.field.display_name')} *</span>
              <input type="text" autoFocus value={displayName} maxLength={128}
                onChange={(e) => { setDisplayName(e.target.value); if (error) setError(null); }}
                disabled={submitting} />
            </label>
          </div>
          <div className="tokens-dlg-section">
            <div className="tokens-dlg-label">{t(lang, 'tokens.edit.field.capabilities')}</div>
            <CapabilityCheckboxGrid registry={registry} value={caps} onChange={setCaps}
              disabled={tok.is_dev_role}
              disabledReason={tok.is_dev_role ? t(lang, 'tokens.dev_role_banner') : null}
              lang={lang} />
          </div>
          {error && <div className="tokens-dlg-err">{error}</div>}
        </div>
        <div className="tokens-dlg-foot">
          <button type="button" className="btn ghost" onClick={onClose} disabled={submitting}>
            {t(lang, 'tokens.edit.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={submitting}>
            {submitting ? '…' : t(lang, 'tokens.edit.save')}
          </button>
        </div>
      </form>
    </div>
  </div>;
}


// ============================================================================
// TokenDeleteConfirm — 删除确认 dialog。
// backend 400 自删 / 末 dev token：UI 转译为对应中文 / 英文文案。
// ============================================================================
function TokenDeleteConfirm({ tok, onClose, onDeleted, lang }) {
  const [submitting, setSubmitting] = useStateT(false);
  const [error, setError] = useStateT(null);

  useEffectT(() => {
    const onKey = (e) => { if (e.key === 'Escape' && !submitting) onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [submitting, onClose]);

  const submit = async () => {
    setError(null); setSubmitting(true);
    try {
      await window.__api.deleteToken(tok.token_id);
      onDeleted(tok.token_id);
    } catch (e) {
      setSubmitting(false);
      const msg = e?.message || String(e);
      if (msg.includes('cannot delete self')) {
        setError(t(lang, 'tokens.error.self_delete'));
      } else if (msg.includes('last dev_role')) {
        setError(t(lang, 'tokens.error.last_dev'));
      } else {
        setError(`${t(lang, 'tokens.error.delete')}: ${msg}`);
      }
    }
  };

  return <div className="tokens-dlg-overlay" onClick={(e) => {
    if (e.target === e.currentTarget && !submitting) onClose();
  }}>
    <div className="tokens-dlg tokens-dlg-confirm" role="dialog" aria-modal="true">
      <div className="tokens-dlg-head">
        <div className="tokens-dlg-title">{t(lang, 'tokens.delete.title')}</div>
        <button type="button" className="icon-btn" onClick={onClose} disabled={submitting} aria-label="close">×</button>
      </div>
      <div className="tokens-dlg-body">
        <div className="tokens-confirm-msg">
          {t(lang, 'tokens.delete.confirm', { owner: tok.owner, display_name: tok.display_name })}
        </div>
        {error && <div className="tokens-dlg-err">{error}</div>}
      </div>
      <div className="tokens-dlg-foot">
        <button type="button" className="btn ghost" onClick={onClose} disabled={submitting}>
          {t(lang, 'tokens.delete.cancel')}
        </button>
        <button type="button" className="btn danger" onClick={submit} disabled={submitting}>
          {submitting ? '…' : t(lang, 'tokens.delete.submit')}
        </button>
      </div>
    </div>
  </div>;
}


// ============================================================================
// TokensTable — 中央数据表格。每行一条 token；caps 用 chip 列表展示，超过 5 个
// 折叠成 "+N more"（hover 看完整列表）。
// ============================================================================
function CapabilityChips({ caps, lang }) {
  if (!caps || caps.length === 0) {
    return <span className="tokens-cap-chip-none">{t(lang, 'tokens.cap_none')}</span>;
  }
  const MAX_VISIBLE = 5;
  const visible = caps.slice(0, MAX_VISIBLE);
  const overflow = caps.length - MAX_VISIBLE;
  return <span className="tokens-cap-chips">
    {visible.map(cap =>
      <span key={cap} className="tokens-cap-chip mono" title={cap}>{cap}</span>)}
    {overflow > 0 && <span className="tokens-cap-chip more"
      title={caps.slice(MAX_VISIBLE).join('\n')}>
      {t(lang, 'tokens.cap_more', { n: overflow })}
    </span>}
  </span>;
}


function TokensTable({ tokens, lang, onEdit, onDelete }) {
  return <table className="tokens-page-table">
    <thead>
      <tr>
        <th>{t(lang, 'tokens.col.display_name')}</th>
        <th>{t(lang, 'tokens.col.owner')}</th>
        <th>{t(lang, 'tokens.col.token_id')}</th>
        <th>{t(lang, 'tokens.col.role')}</th>
        <th>{t(lang, 'tokens.col.capabilities')}</th>
        <th className="tokens-page-table-actions-col">{t(lang, 'tokens.col.actions')}</th>
      </tr>
    </thead>
    <tbody>
      {tokens.map(tok => <tr key={tok.token_id}>
        <td>{tok.display_name}</td>
        <td className="mono">{tok.owner}</td>
        <td className="mono tokens-token-id">{tok.token_masked}</td>
        <td>
          {tok.is_dev_role
            ? <span className="tokens-dev-role-badge">{t(lang, 'tokens.dev_role_badge')}</span>
            : <span className="tokens-normal-badge">{t(lang, 'tokens.normal_badge')}</span>}
        </td>
        <td><CapabilityChips caps={tok.capabilities} lang={lang} /></td>
        <td className="tokens-page-table-actions">
          <button type="button" className="btn ghost xs" onClick={() => onEdit(tok)}>
            {t(lang, 'tokens.action.edit')}
          </button>
          <button type="button" className="btn ghost xs danger" onClick={() => onDelete(tok)}>
            {t(lang, 'tokens.action.delete')}
          </button>
        </td>
      </tr>)}
    </tbody>
  </table>;
}


// ============================================================================
// TokensPage — 顶层页面组件（被 app.jsx 路由）。
// 1. is_dev_role 守卫（即便 URL 直拼也兜底）
// 2. 一次性 fetch list + registry（mount 时并发，全部成功才渲染主体）
// 3. 维持 modal 状态：addOpen / editTarget / deleteTarget / plaintextResult
// ============================================================================
function TokensPage({ user, lang }) {
  // Defense-in-depth：即便 app.jsx 没把入口隐藏住（譬如有人手改 URL），这里也不渲染数据
  // 且不发请求（避免无谓 403 或时序泄露）。文案直接走 i18n forbidden 提示。
  if (!isDevRole(user)) {
    return <div className="tokens-page">
      <div className="tokens-empty">{t(lang, 'tokens.forbidden')}</div>
    </div>;
  }

  const [tokens, setTokens] = useStateT([]);
  const [registry, setRegistry] = useStateT(null);
  const [loading, setLoading] = useStateT(true);
  const [fetchErr, setFetchErr] = useStateT(null);

  // Modals + plaintext banner
  const [addOpen, setAddOpen] = useStateT(false);
  const [editTarget, setEditTarget] = useStateT(null);
  const [deleteTarget, setDeleteTarget] = useStateT(null);
  const [plaintextResult, setPlaintextResult] = useStateT(null);

  const reload = async () => {
    setFetchErr(null);
    try {
      const [list, reg] = await Promise.all([
        window.__api.listTokens(),
        window.__api.getTokensRegistry(),
      ]);
      setTokens(list || []);
      setRegistry(reg || null);
    } catch (e) {
      setFetchErr(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffectT(() => {
    let alive = true;
    (async () => {
      setLoading(true);
      try {
        const [list, reg] = await Promise.all([
          window.__api.listTokens(),
          window.__api.getTokensRegistry(),
        ]);
        if (!alive) return;
        setTokens(list || []);
        setRegistry(reg || null);
      } catch (e) {
        if (alive) setFetchErr(e?.message || String(e));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, []);

  // —— 回调（modal 关 / 提交成功）——
  const onCreateSuccess = (resp) => {
    setAddOpen(false);
    setPlaintextResult(resp);
    reload();
  };
  const onEditSaved = (updatedTok) => {
    setEditTarget(null);
    // 局部 patch（避免整列 refetch）；如果 token_id 命中 → 替换；否则 fallback 全 reload
    setTokens(prev => prev.map(t => t.token_id === updatedTok.token_id ? updatedTok : t));
  };
  const onDeleted = (tokenId) => {
    setDeleteTarget(null);
    setTokens(prev => prev.filter(t => t.token_id !== tokenId));
  };

  return <div className="tokens-page">
    <div className="tokens-head">
      <div className="tokens-head-text">
        <h2>{t(lang, 'tokens.title')}</h2>
        <div className="tokens-sub">{t(lang, 'tokens.subtitle')}</div>
      </div>
      <div className="tokens-head-actions">
        <button type="button" className="btn ghost" onClick={reload} disabled={loading}>
          {t(lang, 'tokens.refresh_btn')}
        </button>
        <button type="button" className="btn primary" onClick={() => setAddOpen(true)} disabled={loading || !registry}>
          + {t(lang, 'tokens.add_btn')}
        </button>
      </div>
    </div>

    {fetchErr && <div className="tokens-err">{t(lang, 'tokens.error.fetch')}: {fetchErr}</div>}

    {loading
      ? <div className="tokens-empty">{t(lang, 'tokens.loading')}</div>
      : tokens.length === 0
        ? <div className="tokens-empty">{t(lang, 'tokens.empty')}</div>
        : <div className="tokens-table-wrap">
            <TokensTable tokens={tokens} lang={lang}
              onEdit={(tok) => setEditTarget(tok)}
              onDelete={(tok) => setDeleteTarget(tok)} />
          </div>}

    {addOpen && registry && <TokenCreateModal
      registry={registry}
      onClose={() => setAddOpen(false)}
      onSuccess={onCreateSuccess}
      lang={lang} />}

    {editTarget && registry && <TokenEditModal
      tok={editTarget}
      registry={registry}
      onClose={() => setEditTarget(null)}
      onSaved={onEditSaved}
      lang={lang} />}

    {deleteTarget && <TokenDeleteConfirm
      tok={deleteTarget}
      onClose={() => setDeleteTarget(null)}
      onDeleted={onDeleted}
      lang={lang} />}

    {plaintextResult && <PlaintextTokenBanner
      token={plaintextResult.token}
      owner={plaintextResult.owner}
      displayName={plaintextResult.display_name}
      onDismiss={() => setPlaintextResult(null)}
      lang={lang} />}
  </div>;
}


// 暴露给 app.jsx 路由（与 admin.jsx 同 pattern：window.AdminPage / window.TokensPage）
window.TokensPage = TokensPage;
// 工具组件也暴露（便于 dev console 调试 / 后续复用）
Object.assign(window, { CapabilityCheckboxGrid });
