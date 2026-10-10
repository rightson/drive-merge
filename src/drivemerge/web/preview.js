// Preview modal: simulation result in plain language, then (when allowed) approve, execute and rescan.
import { html, useContext, useEffect, useRef, useState } from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import * as api from './api.js';
import { Icon } from './icons.js';
import { Modal } from './ui.js';
import { AppCtx } from './ctx.js';
import { errorText, fmtBytes, fmtLoc, fmtNum, locationInMessage, sleep } from './util.js';

const KIND_ICON = { move: 'arrow', 'copy+delete': 'cloud', merge: 'merge', dedupe: 'copy' };

function anyLoc(x) {
  if (!x) return '';
  if (typeof x === 'string') return x;
  if (x.remote !== undefined) return fmtLoc({ remote: x.remote, path: x.path || '' });
  return '';
}

function describeCurrent(cur) {
  if (!cur) return '';
  if (typeof cur === 'string') return cur;
  if (cur.description || cur.message) return cur.description || cur.message;
  const src = anyLoc(cur.src) || anyLoc(cur.source) || cur.path || '';
  const dst = anyLoc(cur.dst) || anyLoc(cur.dest);
  return [cur.op || cur.action, src, dst && `→ ${dst}`].filter(Boolean).join(' ');
}

function journalError(entry) {
  if (!entry) return '';
  let d = entry.detail;
  if (typeof d === 'string') { try { d = JSON.parse(d); } catch (e) { /* plain text */ } }
  if (entry.action !== 'error' && !(d && typeof d === 'object' && d.error)) return '';
  if (d && typeof d === 'object') return d.error || d.message || JSON.stringify(d);
  return d || entry.message || t('prog.unknownFailure');
}

function Card({ id, label, value, sub, tone }) {
  return html`<div class=${'card' + (tone ? ' card-' + tone : '')} data-testid=${'summary-' + id}>
    <div class="card-value">${value}</div><div class="card-label">${label}</div>${sub && html`<div class="card-sub">${sub}</div>`}
  </div>`;
}

function Problem({ code, message, loc, onReveal, tone }) {
  const title = t(`viol.${code}.title`, null, code);
  const text = t(`viol.${code}.text`, null, '');
  return html`<li class=${'problem problem-' + tone} data-testid=${tone === 'warn' ? 'warning' : 'violation'} data-code=${code}>
    <div class="problem-head"><code class="code-pill">${code}</code><strong>${title}</strong>
      ${loc && html`<button class="link" data-testid="reveal-link" onClick=${() => onReveal(loc)}>
        <${Icon} name="jump" size=${12} /> ${t('prev.reveal')}</button>`}</div>
    ${text && html`<p class="problem-text">${text}</p>`}
    ${message && html`<p class="problem-detail">${message}</p>`}
  </li>`;
}

export function PreviewModal({ draft, onClose }) {
  const ctx = useContext(AppCtx);
  const { config } = ctx;
  const remoteNames = (config.remotes || []).map((r) => r.name);
  const [phase, setPhase] = useState('loading');
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [typed, setTyped] = useState('');
  const [approved, setApproved] = useState(false);
  const [job, setJob] = useState(null);
  const [prog, setProg] = useState(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  async function run() {
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
  useEffect(() => { run(); }, []);

  const sim = (data && data.simulation) || {};
  const violations = sim.violations || [];
  const warnings = sim.warnings || [];
  const conflicts = (data && data.conflicts) || [];
  const summary = (data && data.summary) || {};
  const isOk = data ? (data.ok !== undefined ? !!data.ok : !!sim.ok && conflicts.length === 0) : false;
  const problems = violations.length + conflicts.length;
  const planId = data && data.plan_id;
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

  async function pollJob(id, withPlan) {
    let failures = 0;
    for (;;) {
      if (!alive.current) return null;
      try {
        const [j, p] = await Promise.all([api.job(id), withPlan ? api.progress(planId) : null]);
        if (!alive.current) return null;
        setJob(j);
        if (p) setProg(p);
        failures = 0;
        if (j.status === 'done' || j.status === 'failed') return { job: j, prog: p };
      } catch (e) {
        failures += 1;
        if (failures > 10) return { job: { status: 'failed', error: errorText(e) }, prog: null };
      }
      await sleep(700);
    }
  }

  async function approveAndExecute() {
    setError('');
    setPhase('approving');
    try {
      if (!approved) { await api.approve(planId, typed.trim()); setApproved(true); }
    } catch (e) {
      setError(e.code ? errorText(e) : t('prev.refused', { reason: e.message }));
      setPhase('review');
      return;
    }
    let jobId;
    try {
      jobId = (await api.execute(planId)).job_id;
    } catch (e) {
      setError(errorText(e));
      setPhase('review');
      return;
    }
    setPhase('executing');
    const out = await pollJob(jobId, true);
    if (!out) return;
    const failed = out.job.status === 'failed' || (out.prog && (out.prog.failed > 0 || out.prog.status === 'failed'))
      || (out.job.result && (out.job.result.error || out.job.result.status === 'failed'));
    setPhase(failed ? 'failed' : 'done');
  }

  async function rescan() {
    setError('');
    setPhase('rescanning');
    setJob(null);
    try {
      const remotes = summary.remotes_touched && summary.remotes_touched.length ? summary.remotes_touched : remoteNames;
      const out = await pollJob((await api.rescan(remotes)).job_id, false);
      if (!out) return;
      if (out.job.status === 'failed') { setError(out.job.error || t('prog.rescanFailed')); setPhase('done'); return; }
      setPhase('rescanned');
      ctx.afterExecution();
    } catch (e) {
      setError(errorText(e));
      setPhase('done');
    }
  }

  const copy = (text) => { try { navigator.clipboard.writeText(text); ctx.toast('success', t('common.copied')); } catch (e) { /* no clipboard */ } };

  // ------------------------------------------------------------------ pieces
  const finished = prog ? (prog.done || 0) + (prog.failed || 0) + (prog.skipped || 0) : 0;
  const total = prog ? prog.ops_total || 0 : 0;
  const pct = phase === 'done' || phase === 'rescanning' || phase === 'rescanned' ? 100 : total ? Math.min(100, Math.round((finished / total) * 100)) : 0;
  const failureLines = ((prog && prog.recent_journal) || []).map(journalError).filter(Boolean).slice(-6);
  if (job && job.error && !failureLines.some((x) => job.error.includes(x) || x.includes(job.error))) failureLines.push(job.error);

  const progressBox = afterRun && html`<section class=${'progress-box phase-' + phase} data-testid="progress" data-phase=${phase}>
    <h3>${phase === 'executing' ? t('prog.running') : phase === 'failed' ? t('prog.failed') : phase === 'rescanning' ? t('prog.rescanning')
    : phase === 'rescanned' ? t('prog.rescanned') : t('prog.done')}</h3>
    <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow=${pct} aria-label=${t('prog.label')}
      data-testid="progress-bar" data-pct=${pct}><span style=${{ width: `${pct}%` }}></span></div>
    ${phase === 'rescanning' ? html`<div class="progress-nums" data-testid="progress-nums">${rescanText(job)}</div>`
    : html`<div class="progress-nums" data-testid="progress-nums">
      ${t('prog.nums', { done: fmtNum(finished), total: fmtNum(total), failed: fmtNum(prog ? prog.failed || 0 : 0), skipped: fmtNum(prog ? prog.skipped || 0 : 0) })}</div>`}
    ${phase === 'executing' && prog && prog.current && html`<div class="progress-current" data-testid="progress-current">
      <${Icon} name="arrow" size=${13} /> ${describeCurrent(prog.current)}</div>`}
    ${failureLines.length > 0 && html`<ul class="progress-failures" data-testid="progress-failures">
      ${failureLines.map((x, i) => html`<li key=${i}><${Icon} name="alert" size=${13} /> ${x}</li>`)}</ul>`}
    ${phase === 'failed' && html`<p class="note" data-testid="failure-help">${t('prog.failedHelp', { id: planId })}</p>`}
    ${phase === 'done' && html`<p class="note" data-testid="rescan-help">${t('prog.rescanHelp')}</p>`}
    ${error && html`<p class="note error">${error}</p>`}
  </section>`;

  const cliBox = config.approvals_enabled === false && planId && html`<section class="cli-box" data-testid="cli-commands">
    <h3>${t('prev.cliTitle')}</h3>
    <p>${t('prev.cliText')}</p>
    ${['approve', 'execute'].map((verb) => html`<div class="cli-line" key=${verb}>
      <code data-testid=${'cli-' + verb}>drivemerge plan ${verb} ${planId}</code>
      <button class="icon-btn" aria-label=${t('common.copy')} title=${t('common.copy')} onClick=${() => copy(`drivemerge plan ${verb} ${planId}`)}>
        <${Icon} name="copy" size=${14} /></button></div>`)}
    ${config.execution_enabled === false && html`<p class="note">${t('prev.execDisabled')}</p>`}
  </section>`;

  const execNote = config.approvals_enabled && config.execution_enabled === false && isOk && html`<section class="cli-box"
    data-testid="execution-disabled">
    <h3>${t('prev.execDisabledTitle')}</h3><p>${t('prev.execDisabled')}</p>
    <div class="cli-line"><code>[safety]  execution_enabled = true</code></div>
  </section>`;

  const footer = (() => {
    if (phase === 'done' || phase === 'failed') {
      return html`<button class="btn btn-primary" data-testid="rescan-button" onClick=${rescan}>
        <${Icon} name="refresh" size=${15} /> ${t('prog.rescan')}</button>
        <button class="btn" data-testid="preview-close" onClick=${close}>${t('common.close')}</button>`;
    }
    if (phase === 'rescanned') return html`<button class="btn btn-primary" data-testid="preview-close" onClick=${close}>${t('common.close')}</button>`;
    if (running) {
      return html`<span class="spinner" aria-hidden="true"></span><span class="muted">${phase === 'approving' ? t('prev.approving') : t('prog.wait')}</span>`;
    }
    const canApprove = phase === 'review' && config.approvals_enabled && config.execution_enabled !== false && isOk && !!code;
    if (canApprove) {
      return html`<div class="approval" data-testid="approval-area">
        <div class="approval-code"><span>${t('prev.code')}</span><code data-testid="approval-code">${code}</code></div>
        <label class="approval-field"><span>${t('prev.typeCode')}</span>
          <input type="text" data-testid="approval-input" value=${typed} autocomplete="off" spellcheck="false" maxlength="32"
            disabled=${stale || !csrfOk} placeholder=${t('prev.codePlaceholder')} onInput=${(e) => setTyped(e.target.value)}
            onKeyDown=${(e) => { if (e.key === 'Enter' && matches && !stale) approveAndExecute(); }} /></label>
        <button class="btn btn-primary" data-testid="approval-button" disabled=${!matches || stale || !csrfOk} onClick=${approveAndExecute}>
          <${Icon} name="check" size=${15} /> ${t('prev.approve')}</button>
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
        <button class="btn btn-small" onClick=${run}>${t('common.retry')}</button></div>`}
      ${phase === 'empty' && html`<div class="center-msg" data-testid="preview-empty">${t('prev.nothing')}</div>`}
      ${stale && html`<div class="banner banner-warn" data-testid="stale-banner"><${Icon} name="alert" size=${16} />
        <span>${t('prev.stale')}</span><button class="btn btn-small" data-testid="repreview" onClick=${run}>${t('prev.repreview')}</button></div>`}
      ${progressBox}
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
        <section class=${'sim ' + (isOk ? 'sim-ok' : 'sim-bad')} data-testid="sim-status" data-ok=${String(isOk)}>
          <${Icon} name=${isOk ? 'check' : 'alert'} size=${18} />
          <div><strong>${isOk ? t('prev.simOk') : t('prev.simBad', { n: problems || 1 })}</strong>
            <span class="muted">${isOk ? t('prev.simOkHint') : t('prev.simBadHint')}</span></div>
        </section>
        ${violations.length > 0 && html`<ul class="problems" data-testid="violation-list">
          ${violations.map((v, i) => html`<${Problem} key=${i} tone="bad" code=${v.code} message=${v.message}
            loc=${locationInMessage(v.message, remoteNames)} onReveal=${reveal} />`)}</ul>`}
        ${conflicts.length > 0 && html`<h3 class="section-title">${t('prev.conflictList')}</h3><ul class="problems" data-testid="conflict-list">
          ${conflicts.map((c, i) => html`<li key=${i} class="problem problem-bad" data-testid="conflict" data-code=${c.code}>
            <div class="problem-head"><code class="code-pill">${c.code}</code><strong>${t(`viol.${c.code}.title`, null, t('prev.conflictOne'))}</strong>
              ${c.to && html`<button class="link" data-testid="reveal-link" onClick=${() => reveal(c.to)}><${Icon} name="jump" size=${12} /> ${t('prev.reveal')}</button>`}</div>
            <p class="problem-detail">${c.reason}</p>
            <p class="problem-path">${anyLoc(c.from)} → ${anyLoc(c.to)}${c.with ? ` (${t('prev.with')} ${anyLoc(c.with)})` : ''}</p></li>`)}</ul>`}
        ${warnings.length > 0 && html`<details class="warnings" data-testid="warning-list"><summary>${t('prev.warnings', { n: warnings.length })}</summary>
          <ul class="problems">${warnings.map((w, i) => html`<${Problem} key=${i} tone="warn" code=${w.code} message=${w.message} />`)}</ul></details>`}
        ${(sim.remotes || []).length > 0 && html`<h3 class="section-title">${t('prev.perCloud')}</h3>
          <table class="table" data-testid="cloud-table"><thead><tr>
            <th>${t('prev.cloud')}</th><th class="num">${t('prev.filesBefore')}</th><th class="num">${t('prev.filesAfter')}</th>
            <th class="num">${t('prev.bytesBefore')}</th><th class="num">${t('prev.bytesAfter')}</th>
            <th class="num">${t('prev.bytesIn')}</th><th class="num">${t('prev.quarantined')}</th></tr></thead>
            <tbody>${sim.remotes.map((r) => html`<tr key=${r.remote} data-testid=${'cloud-row-' + r.remote}>
              <td>${r.remote}</td><td class="num">${fmtNum(r.files_before)}</td>
              <td class="num">${fmtNum(r.files_after)}${delta(r.files_after - r.files_before)}</td>
              <td class="num">${fmtBytes(r.bytes_before)}</td><td class="num">${fmtBytes(r.bytes_after)}</td>
              <td class="num">${r.bytes_in ? fmtBytes(r.bytes_in) : '–'}</td>
              <td class="num">${r.files_quarantined ? `${fmtNum(r.files_quarantined)} · ${fmtBytes(r.bytes_quarantined)}` : '–'}</td></tr>`)}</tbody></table>`}
        ${(data.changes || []).length > 0 && html`<h3 class="section-title">${t('prev.changes', { n: data.changes.length })}</h3>
          <ul class="changes" data-testid="change-list">${data.changes.map((c, i) => html`<li key=${i} class="change" data-testid="change" data-kind=${c.kind}>
            <span class=${'kind kind-' + c.kind.replace('+', '-')}><${Icon} name=${KIND_ICON[c.kind] || 'arrow'} size=${12} />${t(`kind.${c.kind}`, null, c.kind)}</span>
            <span class="change-path"><span>${fmtLoc(c.from)}</span><${Icon} name="arrow" size=${12} /><button class="link" onClick=${() => reveal(c.to)}>${fmtLoc(c.to)}</button></span>
            <span class="change-meta">${t('pane.files', { n: fmtNum(c.files) })} · ${fmtBytes(c.bytes)}</span></li>`)}</ul>`}
        ${cliBox}${execNote}
        ${error && !afterRun && html`<p class="note error" data-testid="approval-error">${error}</p>`}
      `}
    </div>
    <footer class="modal-foot">${footer}</footer>
  <//>`;
}

function rescanText(job) {
  const p = (job && job.progress) || {};
  if (p.stage === 'identity' || p.stage === 'dir_stats') return t(`prog.stage.${p.stage}`);
  if (!p.remote) return t('prog.wait');
  return t('prog.scanning', { remote: p.remote, i: p.remote_index || 1, n: p.remotes_total || 1, files: fmtNum(p.files || 0) });
}

function delta(n) {
  if (!n) return null;
  return html`<span class=${'delta ' + (n > 0 ? 'up' : 'down')}>${n > 0 ? '+' : '−'}${fmtNum(Math.abs(n))}</span>`;
}
