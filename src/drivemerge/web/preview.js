// Preview modal: simulation result in plain language, then (when allowed) approve, execute and rescan.
// The report pieces and the run logic live in report.js, shared with the plan review (plans.js).
import { html, useContext, useEffect, useState } from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import * as api from './api.js';
import { Icon } from './icons.js';
import { Modal } from './ui.js';
import { AppCtx } from './ctx.js';
import {
  ApprovalFields, Card, CliBox, CloudTable, EmptyDirsBox, ExecNote, KIND_ICON, ProgressBox, Problems, SimStatus, StaleBox, useRunner,
} from './report.js';
import {
  baseName, errorText, fmtBytes, fmtLoc, fmtNum, rootCauses,
} from './util.js';

export function PreviewModal({ draft, onClose }) {
  const ctx = useContext(AppCtx);
  const { config } = ctx;
  const remoteNames = (config.remotes || []).map((r) => r.name);
  const [data, setData] = useState(null);
  const planId = data && data.plan_id;
  const summary = (data && data.summary) || {};
  const run = useRunner({
    ctx, planId, touched: summary.remotes_touched, draftId: draft.id, remoteNames, initial: 'loading',
  });
  const {
    phase, setPhase, error, setError, typed, setTyped, job, prog, alive,
  } = run;

  async function load() {
    setPhase('loading');
    setError('');
    try {
      const r = await api.preview(draft.id);
      if (!alive.current) return;
      setData(r);
      setPhase(r.plan_id ? 'review' : 'empty');
    } catch (e) {
      if (!alive.current) return;
      setError(errorText(e));
      setPhase('error');
    }
  }
  useEffect(() => { load(); }, []);

  const sim = (data && data.simulation) || {};
  const conflicts = (data && data.conflicts) || [];
  const violations = rootCauses(sim.violations || [], conflicts, remoteNames);
  const warnings = sim.warnings || [];
  const staleActions = (data && data.stale_actions) || [];
  const skippedDirs = (data && data.empty_dirs_skipped) || [];
  const isOk = data ? (data.ok !== undefined ? !!data.ok : !!sim.ok && conflicts.length === 0) : false;
  const problems = violations.length + conflicts.length;
  const stale = phase === 'review' && data && data.draft_version !== undefined && draft.version !== data.draft_version;
  const code = (data && data.approval_code) || '';
  const matches = !!code && typed.trim().toLowerCase() === String(code).toLowerCase();
  const csrfOk = api.hasCsrf();
  const running = phase === 'approving' || phase === 'executing' || phase === 'rescanning';
  const afterRun = ['executing', 'done', 'failed', 'rescanning', 'rescanned'].includes(phase);

  const close = () => {
    if (running) return;
    if (afterRun) ctx.afterExecution();
    onClose();
  };
  const reveal = (loc) => {
    if (!loc) return;
    onClose();
    ctx.reveal('right', loc);
  };
  // A clash inside a merge is not fixed by renaming the merged row; the way out starts from the step that caused it.
  const fixClash = (c) => {
    onClose();
    ctx.resolveClash('right', [c.from], baseName(c.to.path));
  };

  const copy = (text) => { try { navigator.clipboard.writeText(text); ctx.toast('success', t('common.copied')); } catch (e) { /* no clipboard */ } };

  const footer = (() => {
    if (phase === 'done' || phase === 'failed') {
      return html`<button class="btn btn-primary" data-testid="rescan-button" onClick=${run.rescan}>
        <${Icon} name="refresh" size=${15} /> ${t('prog.rescan')}</button>
        <button class="btn" data-testid="preview-close" onClick=${close}>${t('common.close')}</button>`;
    }
    if (phase === 'rescanned') return html`<button class="btn btn-primary" data-testid="preview-close" onClick=${close}>${t('common.close')}</button>`;
    if (running) {
      return html`<span class="spinner" aria-hidden="true"></span><span class="muted">${phase === 'approving' ? t('prev.approving') : t('prog.wait')}</span>`;
    }
    const canApprove = phase === 'review' && config.approvals_enabled && config.execution_enabled !== false && isOk && !!code
      && !ctx.outdated;
    if (canApprove) {
      return html`<div class="approval" data-testid="approval-area">
        <${ApprovalFields} code=${code} typed=${typed} setTyped=${setTyped} matches=${matches} disabled=${stale || !csrfOk}
          onApprove=${run.approveAndExecute} label=${t('prev.approve')} />
        <button class="btn" data-testid="preview-close" onClick=${close}>${t('common.close')}</button>
      </div>
      ${!csrfOk && html`<p class="note error" data-testid="no-csrf">${t('prev.noCsrf')}</p>`}`;
    }
    return html`<button class="btn" data-testid="preview-close" onClick=${close}>${t('common.close')}</button>`;
  })();

  return html`<${Modal} label=${t('prev.title')} testid="preview-modal" wide onClose=${close} dismissable=${!running}
    initialFocus="self">
    <header class="modal-head">
      <h2>${t('prev.title')}</h2>
      ${planId && html`<span class="count-pill" data-testid="plan-id">${t('prev.plan', { id: planId })}</span>`}
      <span class="spacer"></span>
      <button class="icon-btn" aria-label=${t('common.close')} disabled=${running} onClick=${close}><${Icon} name="x" size=${16} /></button>
    </header>
    <div class="modal-body">
      ${phase === 'loading' && html`<div class="center-msg" data-testid="preview-loading"><span class="spinner"></span> ${t('prev.loading')}</div>`}
      ${phase === 'error' && html`<div class="note error" data-testid="preview-error">${error}
        <button class="btn btn-small" onClick=${load}>${t('common.retry')}</button></div>`}
      ${phase === 'empty' && html`<div class="center-msg" data-testid="preview-empty">
        ${staleActions.length ? t('prev.nothingStale') : skippedDirs.length ? t('prev.nothingEmptyDirs') : t('prev.nothing')}</div>
        <${StaleBox} actions=${staleActions} /><${EmptyDirsBox} dirs=${skippedDirs} />`}
      ${stale && html`<div class="banner banner-warn" data-testid="stale-banner"><${Icon} name="alert" size=${16} />
        <span>${t('prev.stale')}</span><button class="btn btn-small" data-testid="repreview" onClick=${load}>${t('prev.repreview')}</button></div>`}
      ${phase === 'review' && ctx.outdated && html`<div class="banner banner-warn" data-testid="outdated-note">
        <${Icon} name="alert" size=${16} /><span>${t('catalog.blocked')}</span>
        ${config.approvals_enabled && html`<button class="btn btn-small" data-testid="outdated-rescan" onClick=${run.rescan}>
          <${Icon} name="refresh" size=${14} /> ${t('catalog.rescan')}</button>`}</div>`}
      ${afterRun && html`<${ProgressBox} phase=${phase} job=${job} prog=${prog} planId=${planId} error=${error} onLeave=${onClose} />`}
      ${data && planId && html`
        <section class="cards" data-testid="preview-summary">
          <${Card} id="files" label=${t('prev.files')} value=${fmtNum(summary.moved_files)}
            sub=${summary.ops ? t('prev.ops', { move: summary.ops.move || 0, copy: summary.ops.copy || 0, del: summary.ops.delete || 0 }) : ''} />
          <${Card} id="bytes" label=${t('prev.bytes')} value=${fmtBytes(summary.moved_bytes)} />
          <${Card} id="cross" label=${t('prev.cross')} value=${fmtBytes(summary.cross_remote_bytes !== undefined ? summary.cross_remote_bytes : sim.bytes_cross_remote)}
            tone=${summary.cross_remote_bytes > 0 ? 'warn' : ''} sub=${summary.cross_remote_bytes > 0 ? t('prev.crossSub') : ''} />
          <${Card} id="dedupes" label=${t('prev.dedupes')} value=${fmtNum(summary.dedupes)} />
          <${Card} id="conflicts" label=${t('prev.conflicts')} value=${fmtNum(summary.conflicts !== undefined ? summary.conflicts : conflicts.length)}
            tone=${conflicts.length > 0 ? 'bad' : ''} />
          <${Card} id="empty" label=${t('prev.emptyLeft')} value=${fmtNum(data.empty_dirs_left)}
            sub=${data.empty_dirs_left > 0 ? t('prev.emptySub') : ''} />
        </section>
        <${SimStatus} isOk=${isOk} problems=${problems} />
        <${StaleBox} actions=${staleActions} /><${EmptyDirsBox} dirs=${skippedDirs} />
        <${Problems} violations=${violations} conflicts=${conflicts} warnings=${warnings} remoteNames=${remoteNames}
          onReveal=${reveal} onFixClash=${fixClash} />
        <${CloudTable} remotes=${sim.remotes} />
        ${(data.changes || []).length > 0 && html`<h3 class="section-title">${t('prev.changes', { n: data.changes.length })}</h3>
          <ul class="changes" data-testid="change-list">${data.changes.map((c, i) => html`<li key=${i} class="change" data-testid="change" data-kind=${c.kind}>
            <span class=${'kind kind-' + c.kind.replace('+', '-')}><${Icon} name=${KIND_ICON[c.kind] || 'arrow'} size=${12} />${t(`kind.${c.kind}`, null, c.kind)}</span>
            <span class="change-path"><span>${fmtLoc(c.from)}</span><${Icon} name="arrow" size=${12} /><button class="link" onClick=${() => reveal(c.to)}>${fmtLoc(c.to)}</button></span>
            <span class="change-meta">${t('pane.files', { n: fmtNum(c.files) })} · ${fmtBytes(c.bytes)}</span></li>`)}</ul>`}
        <${CliBox} config=${config} planId=${planId} onCopy=${copy} /><${ExecNote} config=${config} isOk=${isOk} />
        ${error && !afterRun && html`<p class="note error" data-testid="approval-error">${error}</p>`}
      `}
    </div>
    <footer class="modal-foot">${footer}</footer>
  <//>`;
}
