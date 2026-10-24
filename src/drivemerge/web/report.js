// The simulation report and the run panel, shared by the workspace preview (preview.js) and the plan review
// (plans.js, docs/PLANS.md section E): problem lists in plain language, the per-cloud table, the progress panel, the
// approval fields and the hook that approves, executes and rescans.
import { html, useContext, useEffect, useRef, useState } from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import * as api from './api.js';
import { Icon } from './icons.js';
import { AppCtx } from './ctx.js';
import { quarantineHash, refreshQuarantine } from './qstore.js';
import {
  describeAction, errorText, fmtBytes, fmtLoc, fmtNum, problemDetail, rescanText, sleep, staleText, violationLoc,
} from './util.js';

export const KIND_ICON = { move: 'arrow', 'copy+delete': 'cloud', merge: 'merge', dedupe: 'copy' };

export function anyLoc(x) {
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

export function Card({ id, label, value, sub, tone }) {
  return html`<div class=${'card' + (tone ? ' card-' + tone : '')} data-testid=${'summary-' + id}>
    <div class="card-value">${value}</div><div class="card-label">${label}</div>${sub && html`<div class="card-sub">${sub}</div>`}
  </div>`;
}

/**
 * One violation, warning or conflict. The head, the explanation and the detail line are in the person's language;
 * the code stays beside the detail as secondary monospace text, for reports and bug trackers.
 */
export function Problem({ ns, code, detail, loc, onReveal, tone, testid, children }) {
  const title = t(`${ns}.${code}.title`, null, code);
  const text = t(`${ns}.${code}.text`, null, '');
  return html`<li class=${'problem problem-' + tone} data-testid=${testid} data-code=${code}>
    <div class="problem-head"><strong>${title}</strong>
      ${loc && html`<button class="link" data-testid="reveal-link" onClick=${() => onReveal(loc)}>
        <${Icon} name="jump" size=${12} /> ${t('prev.reveal')}</button>`}</div>
    ${text && html`<p class="problem-text">${text}</p>`}
    <p class="problem-detail" data-testid="problem-raw"><code class="code-pill">${code}</code>${detail ? ` ${detail}` : ''}</p>
    ${children}
  </li>`;
}

/** Steps that a rescan left with nothing to act on: named, struck through, and explained. */
export function StaleBox({ actions }) {
  if (!actions.length) return null;
  return html`<section class="stale-box" data-testid="stale-actions">
    <h3><${Icon} name="alert" size=${16} /> ${t('prev.staleTitle', { n: actions.length })}</h3>
    <p>${t('prev.staleText')}</p>
    <ul class="stale-list">${actions.map((a) => html`<li key=${a.id} data-testid="stale-action" data-action-id=${a.id}
      data-code=${a.stale_reason && a.stale_reason.code}>
      <span class="seq">#${a.seq}</span>
      <div><div class="stale-title">${describeAction(a).title}</div>
        <div class="stale-why">${staleText(a.stale_reason)}</div></div></li>`)}</ul>
  </section>`;
}

/** Folders the draft moves that hold no files: a cloud changes through files only, so these stay where they are. */
export function EmptyDirsBox({ dirs }) {
  if (!dirs.length) return null;
  return html`<section class="stale-box" data-testid="empty-dirs-skipped">
    <h3><${Icon} name="alert" size=${16} /> ${t('prev.skippedTitle', { n: dirs.length })}</h3>
    <p>${t('prev.skippedText')}</p>
    <ul class="stale-list">${dirs.map((d, i) => html`<li key=${i} data-testid="empty-dir-skipped">
      <span class="change-path"><span>${fmtLoc(d.from)}</span><${Icon} name="arrow" size=${12} /><span>${fmtLoc(d.to)}</span></span></li>`)}</ul>
  </section>`;
}

export function delta(n) {
  if (!n) return null;
  return html`<span class=${'delta ' + (n > 0 ? 'up' : 'down')}>${n > 0 ? '+' : '−'}${fmtNum(Math.abs(n))}</span>`;
}

/** The green or red verdict of a simulation. */
export function SimStatus({ isOk, problems }) {
  return html`<section class=${'sim ' + (isOk ? 'sim-ok' : 'sim-bad')} data-testid="sim-status" data-ok=${String(isOk)}>
    <${Icon} name=${isOk ? 'check' : 'alert'} size=${18} />
    <div><strong>${isOk ? t('prev.simOk') : t('prev.simBad', { n: problems || 1 })}</strong>
      <span class="muted">${isOk ? t('prev.simOkHint') : t('prev.simBadHint')}</span></div>
  </section>`;
}

/**
 * The violations, the conflicts and the warnings of a report, in that order. `violations` is already reduced to its
 * root causes (util.rootCauses). `onFixClash` is optional: only the workspace can take a step back.
 */
export function Problems({ violations, conflicts, warnings, remoteNames, onReveal, onFixClash }) {
  return html`
    ${violations.length > 0 && html`<ul class="problems" data-testid="violation-list">
      ${violations.map((v, i) => html`<${Problem} key=${i} ns="viol" tone="bad" testid="violation" code=${v.code} detail=${problemDetail('viol', v)}
        loc=${violationLoc(v, remoteNames)} onReveal=${onReveal} />`)}</ul>`}
    ${conflicts.length > 0 && html`<h3 class="section-title">${t('prev.conflictList')}</h3><ul class="problems" data-testid="conflict-list">
      ${conflicts.map((c, i) => html`<${Problem} key=${i} ns="conflict" tone="bad" testid="conflict" code=${c.code} detail=${problemDetail('conflict', c)}
        loc=${c.to} onReveal=${onReveal}>
        <p class="problem-path" data-testid="conflict-path">${anyLoc(c.from)} → ${anyLoc(c.to)}${c.with ? ` (${t('prev.with')} ${anyLoc(c.with)})` : ''}</p>
        ${onFixClash && c.code === 'DIFFERENT_CONTENT' && c.from && c.to && html`<button class="link" data-testid="conflict-fix"
          onClick=${() => onFixClash(c)}>${t('clash.fix')}</button>`}
      <//>`)}</ul>`}
    ${warnings.length > 0 && html`<details class="warnings" data-testid="warning-list"><summary>${t('prev.warnings', { n: warnings.length })}</summary>
      <ul class="problems">${warnings.map((w, i) => html`<${Problem} key=${i} ns="viol" tone="warn" testid="warning" code=${w.code} detail=${problemDetail('viol', w)} />`)}</ul></details>`}`;
}

/** Before and after for every cloud the report touches. */
export function CloudTable({ remotes }) {
  if (!remotes || !remotes.length) return null;
  return html`<h3 class="section-title">${t('prev.perCloud')}</h3>
    <table class="table" data-testid="cloud-table"><thead><tr>
      <th>${t('prev.cloud')}</th><th class="num">${t('prev.filesBefore')}</th><th class="num">${t('prev.filesAfter')}</th>
      <th class="num">${t('prev.bytesBefore')}</th><th class="num">${t('prev.bytesAfter')}</th>
      <th class="num">${t('prev.bytesIn')}</th><th class="num">${t('prev.quarantined')}</th></tr></thead>
      <tbody>${remotes.map((r) => html`<tr key=${r.remote} data-testid=${'cloud-row-' + r.remote}>
        <td>${r.remote}</td><td class="num">${fmtNum(r.files_before)}</td>
        <td class="num">${fmtNum(r.files_after)}${delta(r.files_after - r.files_before)}</td>
        <td class="num">${fmtBytes(r.bytes_before)}</td><td class="num">${fmtBytes(r.bytes_after)}</td>
        <td class="num">${r.bytes_in ? fmtBytes(r.bytes_in) : '–'}</td>
        <td class="num">${r.files_quarantined ? `${fmtNum(r.files_quarantined)} · ${fmtBytes(r.bytes_quarantined)}` : '–'}</td></tr>`)}</tbody></table>`;
}

/**
 * What to do after a run that stopped: with approvals the quarantine can be restored on the page, so the sentence points
 * to the Quarantine view; without, the terminal command stays. `onLeave` closes a dialog that the link would leave behind.
 */
export function FailedHelp({ planId, onLeave }) {
  const ctx = useContext(AppCtx);
  if (!ctx || !ctx.config || ctx.config.approvals_enabled === false) return t('prog.failedHelp', { id: planId });
  const go = (e) => {
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.button) return;
    e.preventDefault();
    if (onLeave) onLeave();
    ctx.nav(quarantineHash(planId));
  };
  return html`${t('prog.failedHelpPage', { id: planId })} <a href=${quarantineHash(planId)} data-testid="failure-quarantine-link" onClick=${go}>${t('qr.openLink')}</a>`;
}

/** The progress panel of a run (and of the rescan after it). */
export function ProgressBox({ phase, job, prog, planId, error, onLeave }) {
  const finished = prog ? (prog.done || 0) + (prog.failed || 0) + (prog.skipped || 0) : 0;
  const total = prog ? prog.ops_total || 0 : 0;
  const pct = phase === 'done' || phase === 'rescanning' || phase === 'rescanned' ? 100 : total ? Math.min(100, Math.round((finished / total) * 100)) : 0;
  const failureLines = ((prog && prog.recent_journal) || []).map(journalError).filter(Boolean).slice(-6);
  if (job && job.error && !failureLines.some((x) => job.error.includes(x) || x.includes(job.error))) failureLines.push(job.error);

  return html`<section class=${'progress-box phase-' + phase} data-testid="progress" data-phase=${phase}>
    <h3>${phase === 'executing' ? t('prog.running') : phase === 'failed' ? t('prog.failed') : phase === 'rescanning' ? t('prog.rescanning')
    : phase === 'rescanned' ? t('prog.rescanned') : t('prog.done')}</h3>
    <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow=${pct} aria-label=${t('prog.label')}
      data-testid="progress-bar" data-pct=${pct}><span style=${{ width: `${pct}%` }}></span></div>
    ${phase === 'rescanning' ? html`<div class="progress-nums" data-testid="progress-nums">${rescanText(job)}</div>`
    : html`<div class="progress-nums" data-testid="progress-nums">
      ${t('prog.nums', { done: fmtNum(finished), total: fmtNum(total), failed: fmtNum(prog ? prog.failed || 0 : 0), skipped: fmtNum(prog ? prog.skipped || 0 : 0) })}</div>`}
    ${phase === 'executing' && prog && prog.current && html`<div class="progress-current" data-testid="progress-current">
      <${Icon} name="arrow" size=${13} /> ${describeCurrent(prog.current)}</div>`}
    ${failureLines.length > 0 && html`<p class="note" data-testid="failure-raw-note">${t('prog.rawDetails')}</p>
      <ul class="progress-failures" data-testid="progress-failures">
      ${failureLines.map((x, i) => html`<li key=${i}><${Icon} name="alert" size=${13} /> ${x}</li>`)}</ul>`}
    ${phase === 'failed' && html`<p class="note" data-testid="failure-help"><${FailedHelp} planId=${planId} onLeave=${onLeave} /></p>`}
    ${phase === 'done' && html`<p class="note" data-testid="rescan-help">${t('prog.rescanHelp')}</p>`}
    ${error && html`<p class="note error">${error}</p>`}
  </section>`;
}

/** The terminal commands to run instead, for a server that cannot approve. */
export function CliBox({ config, planId, onCopy }) {
  if (config.approvals_enabled !== false || !planId) return null;
  return html`<section class="cli-box" data-testid="cli-commands">
    <h3>${t('prev.cliTitle')}</h3>
    <p>${t('prev.cliText')}</p>
    ${['approve', 'execute'].map((verb) => html`<div class="cli-line" key=${verb}>
      <code data-testid=${'cli-' + verb}>drivemerge plan ${verb} ${planId}</code>
      <button class="icon-btn" aria-label=${t('common.copy')} title=${t('common.copy')} onClick=${() => onCopy(`drivemerge plan ${verb} ${planId}`)}>
        <${Icon} name="copy" size=${14} /></button></div>`)}
    ${config.execution_enabled === false && html`<p class="note">${t('prev.execDisabled')}</p>`}
  </section>`;
}

/** Approved by a human but the configuration does not let anything run. */
export function ExecNote({ config, isOk }) {
  if (!(config.approvals_enabled && config.execution_enabled === false && isOk)) return null;
  return html`<section class="cli-box" data-testid="execution-disabled">
    <h3>${t('prev.execDisabledTitle')}</h3><p>${t('prev.execDisabled')}</p>
    <div class="cli-line"><code>[safety]  execution_enabled = true</code></div>
  </section>`;
}

/** The code, the box to type it into and the approve button; the caller wraps them in its own footer or panel. */
export function ApprovalFields({ code, typed, setTyped, matches, disabled, onApprove, label, icon = 'check' }) {
  return html`
    <div class="approval-code"><span>${t('prev.code')}</span><code data-testid="approval-code">${code}</code></div>
    <label class="approval-field"><span>${t('prev.typeCode')}</span>
      <input type="text" data-testid="approval-input" value=${typed} autocomplete="off" spellcheck="false" maxlength="32"
        disabled=${disabled} placeholder=${t('prev.codePlaceholder')} onInput=${(e) => setTyped(e.target.value)}
        onKeyDown=${(e) => { if (e.key === 'Enter' && matches && !disabled) onApprove(); }} /></label>
    <button class="btn btn-primary" data-testid="approval-button" disabled=${!matches || disabled} onClick=${onApprove}>
      <${Icon} name=${icon} size=${15} /> ${label}</button>`;
}

/**
 * Approve, execute and rescan with their state. `phase` also carries the caller's own phases (the preview's
 * loading / review / empty / error); the runner adds approving, approved, executing, done, failed, rescanning,
 * rescanned. `touched` are the clouds the plan changes (named in the "catalog is behind" banner) and `draftId` the
 * draft that the run closes, if any.
 */
export function useRunner({
  ctx, planId, touched, draftId, remoteNames, initial = 'loading', reviewPhase = 'review',
}) {
  const [phase, setPhase] = useState(initial);
  const [error, setError] = useState('');
  const [typed, setTyped] = useState('');
  const [approved, setApproved] = useState(false);
  const [job, setJob] = useState(null);
  const [prog, setProg] = useState(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

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

  /** Type the code. Returns true when the server took it. */
  async function approve({ stay = false } = {}) {
    setError('');
    setPhase('approving');
    try {
      if (!approved) { await api.approve(planId, typed.trim()); setApproved(true); }
    } catch (e) {
      setError(e.code ? errorText(e) : t('prev.refused', { reason: e.message }));
      setPhase(reviewPhase);
      return false;
    }
    if (stay) setPhase('approved');
    return true;
  }

  async function execute(back = reviewPhase) {
    setError('');
    let jobId;
    try {
      jobId = (await api.execute(planId)).job_id;
    } catch (e) {
      setError(errorText(e));
      setPhase(back);
      return;
    }
    setPhase('executing');
    const out = await pollJob(jobId, true);
    if (!out) return;
    const failed = out.job.status === 'failed' || (out.prog && (out.prog.failed > 0 || out.prog.status === 'failed'))
      || (out.job.result && (out.job.result.error || out.job.result.status === 'failed'));
    setPhase(failed ? 'failed' : 'done');
    ctx.markOutdated(touched, draftId);  // the executor does not touch the catalog; only a scan does
    refreshQuarantine();                 // a run may have parked files: the rail badge and the Quarantine view hear of it
  }

  async function approveAndExecute() {
    if (await approve()) await execute();
  }

  async function rescan() {
    const back = phase;  // where to return to if the scan fails: the finished run, or the review
    setError('');
    setPhase('rescanning');
    setJob(null);
    try {
      const behind = ctx.outdated && ctx.outdated.remotes;
      const remotes = behind && behind.length ? behind : touched && touched.length ? touched : remoteNames;
      const out = await pollJob((await api.rescan(remotes)).job_id, false);
      if (!out) return;
      if (out.job.status === 'failed') { setError(out.job.error || t('prog.rescanFailed')); setPhase(back); return; }
      setPhase('rescanned');
      ctx.catalogRefreshed();
      ctx.afterExecution();
    } catch (e) {
      setError(errorText(e));
      setPhase(back);
    }
  }

  return {
    phase, setPhase, error, setError, typed, setTyped, approved, job, prog, alive, approve, execute, approveAndExecute, rescan,
  };
}
