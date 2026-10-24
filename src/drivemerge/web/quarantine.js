// Quarantine (docs/QUARANTINE.md): what every executed plan parked in the quarantine folders, on which cloud, how much
// space it holds and since when; putting it back (a whole plan, a folder or single files); and deleting it for good.
// Nothing here is a new way to change a cloud: restore and purge are the closing steps DESIGN.md section 7.3 defines for
// an approved plan, behind the same gates as the terminal (the human channel, [safety] execution_enabled, a typed word).
// Every action first shows a read-only check against the catalog; the server checks everything live again.
import {
  html, useContext, useEffect, useMemo, useRef, useState,
} from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import * as api from './api.js';
import { Icon } from './icons.js';
import { AppCtx } from './ctx.js';
import { AgeChip, CloudName } from './clouds.js';
import { Modal, ViewHeader } from './ui.js';
import { planSummary } from './plans.js';
import {
  cloudHolds, quarantineHash, refreshQuarantine, useQuarantine,
} from './qstore.js';
import {
  baseName, errorText, fmtBytes, fmtDateTime, fmtDay, fmtNum, parentOf, sleep, wordParams,
} from './util.js';

const PAGE = 200;
const MAX_OPS = 5000;        // op_ids per request (docs/QUARANTINE.md section E)
const MAX_CLI_OPS = 20;      // --op lines in the terminal command
const AUTO_OPEN = 60;        // up to this many loaded items the whole tree starts open
const DAY_MS = 86400000;
const STATES = ['held', 'restored', 'purged', 'all'];

const natural = (a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
const sizeOf = (it) => it.size || 0;

// ------------------------------------------------------------------ small pieces

function KindPill({ kind }) {
  return html`<span class=${'kind kind-' + String(kind).replace(/[^a-z]/g, '-')}>${t(`kind.${kind}`, null, kind)}</span>`;
}

function ByClaude({ by }) {
  if (by !== 'mcp') return null;
  return html`<span class="badge badge-claude" data-testid="qr-claude-badge" title=${t('plans.byClaudeTip')}>
    <${Icon} name="claude" size=${11} /> ${t('plans.byClaude')}</span>`;
}

/** Whole days until a time (an ISO string), counted from now; null when there is none. */
function daysTo(iso) {
  if (!iso) return null;
  const then = Date.parse(String(iso).endsWith('Z') || /[+-]\d\d:?\d\d$/.test(String(iso)) ? iso : `${iso}Z`);
  return Number.isNaN(then) ? null : Math.ceil((then - Date.now()) / DAY_MS);
}

/** "due now" / "in 26 d" in the plan list. */
function dueText(iso) {
  const d = daysTo(iso);
  if (d === null) return '–';
  return d <= 0 ? t('qr.dueNow') : t('qr.dueIn', { n: d });
}

/** "due now" / "due in 26 d" on an item. */
function dueItemText(iso) {
  const d = daysTo(iso);
  if (d === null) return '–';
  return d <= 0 ? t('qr.due.now') : t('qr.due.in', { n: d });
}

/** The `[safety]` hint for a server that has approvals but not execution (as in the plan review). */
function ExecOff() {
  return html`<section class="cli-box" data-testid="qr-exec-disabled">
    <h3>${t('prev.execDisabledTitle')}</h3><p>${t('prev.execDisabled')}</p>
    <div class="cli-line"><code>[safety]  execution_enabled = true</code></div>
  </section>`;
}

const execOff = (config) => config.approvals_enabled !== false && config.execution_enabled === false;

/** Terminal commands for the read-only page: one line, with --op per chosen item up to MAX_CLI_OPS. */
function commandFor(spec) {
  const ops = spec.opIds && spec.opIds.length <= MAX_CLI_OPS ? spec.opIds.map((id) => ` --op ${id}`).join('') : '';
  if (spec.mode === 'restore') return `drivemerge plan restore ${spec.planId}${ops}`;
  if (spec.mode === 'purge') return `drivemerge plan purge ${spec.planId} --older-than-days ${spec.olderThan}${ops}`;
  return 'drivemerge quarantine purge-due' + (spec.remote ? ` --remote ${spec.remote}` : '');
}

function CliCommand({ spec, onCopy }) {
  const cmd = commandFor(spec);
  const many = spec.opIds && spec.opIds.length > MAX_CLI_OPS;
  return html`<section class="cli-box" data-testid="qr-cli">
    <h3>${t('qr.cli.title')}</h3>
    <p>${t('qr.cli.text')}</p>
    <div class="cli-line"><code data-testid="qr-cli-command">${cmd}</code>
      <button class="icon-btn" aria-label=${t('qr.cli.copy')} title=${t('qr.cli.copy')} onClick=${() => onCopy(cmd)}><${Icon} name="copy" size=${14} /></button></div>
    ${many && html`<p class="note" data-testid="qr-cli-many">${t('qr.cli.many', { n: MAX_CLI_OPS })}</p>`}
  </section>`;
}

// ------------------------------------------------------------------ the check, then the typed word, then the job

/** Add the checks of several plans into one (the overview's purge walks them in turn). `remote` narrows files and bytes to one cloud. */
function sumChecks(list, remote) {
  const pair = () => ({ files: 0, bytes: 0 });
  const out = {
    action: 'purge', files: 0, bytes: 0, too_recent: pair(), witness_gone: pair(), witness_unproven: { files: 0, bytes: 0, download_bytes: 0 },
    recent: pair(), by_remote: [], plans: list.length, remotes: [],
  };
  const byRemote = new Map();
  for (const c of list) {
    for (const r of c.by_remote || []) {
      const row = byRemote.get(r.remote) || { remote: r.remote, files: 0, bytes: 0 };
      row.files += r.files || 0;
      row.bytes += r.bytes || 0;
      byRemote.set(r.remote, row);
    }
    for (const k of ['too_recent', 'witness_gone', 'recent']) { out[k].files += c[k].files; out[k].bytes += c[k].bytes; }
    const u = c.witness_unproven;
    out.witness_unproven.files += u.files;
    out.witness_unproven.bytes += u.bytes;
    out.witness_unproven.download_bytes += u.download_bytes || 0;
    if (!remote) { out.files += c.files; out.bytes += c.bytes; }
  }
  out.by_remote = [...byRemote.values()];
  if (remote) {
    const row = byRemote.get(remote);
    out.files = row ? row.files : 0;
    out.bytes = row ? row.bytes : 0;
  }
  out.remotes = out.by_remote.map((r) => r.remote);
  return out;
}

function CheckLines({ mode, data, report }) {
  if (mode === 'restore') {
    return html`<ul class="qr-lines">
      ${data.files > 0
    ? html`<li data-testid="qr-check-will"><${Icon} name="check" size=${14} /> ${t('qr.restore.will', { files: fmtNum(data.files), bytes: fmtBytes(data.bytes) })}</li>`
    : html`<li data-testid="qr-check-none"><${Icon} name="info" size=${14} /> ${t('qr.restore.none')}</li>`}
      ${data.original_taken.files > 0 && html`<li class="warn" data-testid="qr-check-taken"><${Icon} name="alert" size=${14} />
        ${t('qr.restore.taken', { files: fmtNum(data.original_taken.files), bytes: fmtBytes(data.original_taken.bytes) })}</li>`}
      ${data.not_seen.files > 0 && html`<li class="warn" data-testid="qr-check-unseen"><${Icon} name="alert" size=${14} />
        ${t('qr.restore.unseen', { files: fmtNum(data.not_seen.files), bytes: fmtBytes(data.not_seen.bytes) })}</li>`}
      ${data.cross_remote_copies > 0 && html`<li data-testid="qr-check-twice"><${Icon} name="info" size=${14} />
        ${t('qr.restore.twice', { n: fmtNum(data.cross_remote_copies) })}</li>`}
    </ul>`;
  }
  return html`<ul class="qr-lines">
    ${data.files > 0
    ? html`<li data-testid="qr-check-will"><${Icon} name="trash" size=${14} /> ${t('qr.purge.will', { files: fmtNum(data.files), bytes: fmtBytes(data.bytes) })}</li>`
    : html`<li data-testid="qr-check-none"><${Icon} name="info" size=${14} /> ${t('qr.purge.none')}</li>`}
    ${data.recent.files > 0 && html`<li class="bad" data-testid="qr-check-recent"><${Icon} name="alert" size=${14} />
      ${t('qr.purge.recent', { files: fmtNum(data.recent.files), bytes: fmtBytes(data.recent.bytes) })}</li>`}
    ${data.too_recent.files > 0 && html`<li data-testid="qr-check-toorecent"><${Icon} name="info" size=${14} />
      ${t('qr.purge.tooRecent', { files: fmtNum(data.too_recent.files), bytes: fmtBytes(data.too_recent.bytes) })}</li>`}
    ${data.witness_gone.files > 0 && html`<li class="warn" data-testid="qr-check-gone"><${Icon} name="alert" size=${14} />
      ${t('qr.purge.gone', { files: fmtNum(data.witness_gone.files), bytes: fmtBytes(data.witness_gone.bytes) })}</li>`}
    ${data.witness_unproven.files > 0 && html`<li class="warn" data-testid="qr-check-unproven"><${Icon} name="alert" size=${14} />
      ${t('qr.purge.unproven', { files: fmtNum(data.witness_unproven.files), bytes: fmtBytes(data.witness_unproven.bytes), download: fmtBytes(data.witness_unproven.download_bytes || 0) })}</li>`}
    ${(data.by_remote || []).map((r) => {
    const q = (cloudHolds(report, r.remote) || {}).quota;
    if (!q || q.used === undefined || q.used === null) return null;
    const after = Math.max(0, q.used - (r.bytes || 0));
    return html`<li key=${r.remote} data-testid="qr-quota-after" data-cloud=${r.remote}><${Icon} name="cloud" size=${14} />
      ${q.total ? t('qr.purge.quota', { cloud: r.remote, after: fmtBytes(after), total: fmtBytes(q.total) }) : t('qr.purge.quotaUsed', { cloud: r.remote, after: fmtBytes(after) })}</li>`;
  })}
  </ul>`;
}

/** One failure of a restore or purge in the page's words; the executor's own text only where the page has no sentence. */
function Failure({ f }) {
  const words = t(`qr.fail.${f.code}`, wordParams('qr', f.params), '');
  return html`<li data-testid=${'qr-failure-' + f.op_id} data-code=${f.code}>
    <code class="code-pill">${f.code}</code> <span class="mono">${f.remote}:${f.path}</span>
    <span class="qr-fail-text">${words || f.error || ''}</span>
    ${(f.code === 'OP_FAILED' || !words) && f.error && words && html`<span class="muted"> ${f.error}</span>`}
  </li>`;
}

function Failures({ failures, failed }) {
  if (!failed) return null;
  return html`<div class="qr-failed">
    <p><strong>${t('qr.result.stayed', { n: fmtNum(failed) })}</strong></p>
    ${(failures || []).length > 0 && html`<ul class="qr-failures" data-testid="qr-failures">${failures.map((f) => html`<${Failure} key=${f.op_id} f=${f} />`)}</ul>`}
    ${failed > (failures || []).length && (failures || []).length > 0
      && html`<p class="muted">${t('qr.result.more', { n: fmtNum(failed - failures.length) })}</p>`}
  </div>`;
}

function Result({ mode, job }) {
  const r = job.result;
  if (!r) {
    return html`<div data-testid="qr-result" data-status="failed">
      <p class="note error">${t('qr.result.jobFailed')}</p>
      ${job.error && html`<p class="note">${t('prog.rawDetails')}</p><p class="mono">${job.error}</p>`}</div>`;
  }
  if (mode === 'purge-due') {
    const plans = r.plans || [];
    return html`<div data-testid="qr-result" data-status=${job.status}>
      <p><strong>${r.done ? t('qr.result.purged', { n: fmtNum(r.done) }) : t('qr.result.nothing')}</strong></p>
      ${plans.filter((p) => p.failed > 0 || p.error).map((p) => html`<div key=${p.plan_id} class="qr-plan-result" data-testid=${'qr-result-plan-' + p.plan_id}>
        <p class="muted">${t('qr.result.plan', { id: p.plan_id, done: fmtNum(p.done), failed: fmtNum(p.failed) })}</p>
        <${Failures} failures=${p.failures} failed=${p.failed} />
        ${p.error && !(p.failures || []).length && html`<p class="mono" data-testid=${'qr-plan-error-' + p.plan_id}>${p.error}</p>`}</div>`)}
      ${r.done > 0 && html`<p class="note">${t('qr.result.behind')}</p>`}
    </div>`;
  }
  const done = r.done || 0;
  const line = done ? t(mode === 'restore' ? 'qr.result.restored' : 'qr.result.purged', { n: fmtNum(done) }) : t('qr.result.nothing');
  return html`<div data-testid="qr-result" data-status=${job.status}>
    <p><strong>${line}</strong></p>
    <${Failures} failures=${r.failures} failed=${r.failed} />
    ${done > 0 && html`<p class="note">${t('qr.result.behind')}</p>`}
    ${job.status === 'failed' && !r.failed && job.error && html`<p class="note">${t('prog.rawDetails')}</p><p class="mono">${job.error}</p>`}
  </div>`;
}

function JobProgress({ mode, job }) {
  const p = (job && job.progress) || {};
  let done = 0;
  let total = 0;
  let text;
  if (mode === 'restore') {
    done = (p.restored || 0) + (p.failed || 0); total = p.total || 0;
    text = t('qr.progress.restore', { restored: fmtNum(p.restored || 0), failed: fmtNum(p.failed || 0), total: fmtNum(total) });
  } else if (mode === 'purge') {
    done = (p.purged || 0) + (p.failed || 0); total = p.total || 0;
    text = t('qr.progress.purge', { purged: fmtNum(p.purged || 0), failed: fmtNum(p.failed || 0), total: fmtNum(total) });
  } else {
    done = p.plans_done || 0; total = p.plans_total || 0;
    text = t('qr.progress.purgeDue', { done: fmtNum(done), total: fmtNum(total), purged: fmtNum(p.purged || 0), failed: fmtNum(p.failed || 0) });
  }
  const finished = job && job.status !== 'running';
  const pct = finished ? 100 : total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  return html`<div data-testid="qr-progress" data-phase=${finished ? 'finished' : 'running'} data-pct=${pct}>
    <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow=${pct} aria-label=${t('qr.progress.label')}><span style=${{ width: `${pct}%` }}></span></div>
    <p class="progress-nums" data-testid="qr-progress-nums">${text}</p>
  </div>`;
}

/**
 * The dialog for restoring or purging. `spec` is {mode: 'restore' | 'purge' | 'purge-due', planId, opIds (null: every held
 * item), olderThan (purge: days), scope ('all' | 'selected' | 'past'), remote and planIds (purge-due)}.
 * `onClose(changed)` tells the caller whether anything ran (it reloads its lists).
 */
function ActionDialog({ spec, onClose }) {
  const ctx = useContext(AppCtx);
  const { config } = ctx;
  const { mode } = spec;
  const word = mode === 'restore' ? 'restore' : 'purge';
  const report = useQuarantine().data;
  const [check, setCheck] = useState({ loading: true, error: null, data: null });
  const [typed, setTyped] = useState('');
  const [phase, setPhase] = useState('check');   // check | running | finished
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState('');
  const [job, setJob] = useState(null);
  const alive = useRef(true);
  const changed = useRef(false);
  useEffect(() => () => { alive.current = false; }, []);

  useEffect(() => {
    (async () => {
      try {
        let data;
        if (mode === 'purge-due') {
          const ids = spec.planIds || [];
          const checks = [];
          for (let i = 0; i < ids.length; i += 4) {
            const batch = await Promise.all(ids.slice(i, i + 4).map((id) => api.quarantineCheck(id, { action: 'purge', older_than_days: spec.olderThan })
              .catch((e) => { if (e.code === 'NOTHING_QUARANTINED') return null; throw e; })));
            checks.push(...batch.filter(Boolean));
          }
          data = sumChecks(checks, spec.remote);
        } else {
          const body = { action: mode };
          if (spec.opIds) body.op_ids = spec.opIds;
          if (mode === 'purge') body.older_than_days = spec.olderThan;
          data = await api.quarantineCheck(spec.planId, body);
        }
        if (alive.current) setCheck({ loading: false, error: null, data });
      } catch (e) {
        if (alive.current) setCheck({ loading: false, error: e, data: null });
      }
    })();
  }, []);

  const data = check.data;
  const readOnly = config.approvals_enabled === false;
  const off = execOff(config);
  const csrfOk = api.hasCsrf();
  const matches = typed.trim() === word;
  const ready = !!data && data.files > 0 && matches && !off && csrfOk && phase === 'check' && !starting;
  const copy = (text) => { try { navigator.clipboard.writeText(text); ctx.toast('success', t('common.copied')); } catch (e) { /* no clipboard */ } };

  async function poll(id) {
    let fails = 0;
    for (;;) {
      if (!alive.current) return null;
      try {
        const j = await api.job(id);
        if (!alive.current) return null;
        setJob(j);
        fails = 0;
        if (j.status !== 'running') return j;
      } catch (e) {
        fails += 1;
        if (fails > 10) return { status: 'failed', error: errorText(e), result: null, progress: {} };
      }
      await sleep(500);
    }
  }

  async function go() {
    if (!ready) return;
    setError('');
    setStarting(true);
    let id;
    try {
      let r;
      if (mode === 'restore') r = await api.restore(spec.planId, { confirm: 'restore', ...(spec.opIds ? { op_ids: spec.opIds } : {}) });
      else if (mode === 'purge') r = await api.purge(spec.planId, { confirm: 'purge', older_than_days: spec.olderThan, ...(spec.opIds ? { op_ids: spec.opIds } : {}) });
      else r = await api.purgeDue({ confirm: 'purge', ...(spec.remote ? { remote: spec.remote } : {}) });
      id = r.job_id;
    } catch (e) {
      if (alive.current) { setError(errorText(e)); setStarting(false); }
      return;
    }
    setPhase('running');
    const j = await poll(id);
    if (!j) return;
    setJob(j);
    setPhase('finished');
    setStarting(false);
    changed.current = true;
    const r = j.result || {};
    if (j.status === 'done' || (r.done || 0) > 0) {
      ctx.markOutdated(data.remotes && data.remotes.length ? data.remotes : (data.by_remote || []).map((x) => x.remote));
    }
    refreshQuarantine();
    if (j.status === 'done' && (r.done || 0) > 0) ctx.toast('success', t(mode === 'restore' ? 'qr.toast.restored' : 'qr.toast.purged', { n: fmtNum(r.done) }));
  }

  const title = t(mode === 'restore' ? 'qr.dialog.restore' : mode === 'purge' ? 'qr.dialog.purge' : 'qr.dialog.purgeDue');
  const scope = (() => {
    if (mode === 'purge-due') {
      const n = (spec.planIds || []).length;
      return spec.remote ? t('qr.scope.dueCloud', { cloud: spec.remote, n }) : t('qr.scope.due', { n });
    }
    if (spec.scope === 'selected') return t('qr.scope.selected', { n: fmtNum((spec.opIds || []).length), id: spec.planId });
    if (spec.scope === 'past') return t('qr.scope.past', { id: spec.planId });
    return t('qr.scope.all', { id: spec.planId });
  })();
  const running = phase === 'running';

  return html`<${Modal} label=${title} onClose=${() => onClose(changed.current)} testid="qr-dialog" dismissable=${!running} wide initialFocus="input">
    <form class="dialog qr-dialog" data-mode=${mode} data-phase=${phase} onSubmit=${(e) => { e.preventDefault(); go(); }}>
      <h2 class="dialog-title">${title}</h2>
      <p class="dialog-message" data-testid="qr-scope">${scope}</p>
      ${phase === 'check' && html`<section class="qr-check" data-testid="qr-dialog-check" aria-live="polite">
        ${check.loading && html`<p class="muted"><span class="spinner" aria-hidden="true"></span> ${t('qr.check.loading')}</p>`}
        ${check.error && html`<p class="note error" role="alert" data-testid="qr-check-error">${errorText(check.error)}</p>`}
        ${data && html`<${CheckLines} mode=${mode === 'restore' ? 'restore' : 'purge'} data=${data} report=${report} />
          <p class="note">${t('qr.check.note')}</p>`}
      </section>`}
      ${phase !== 'check' && html`<${JobProgress} mode=${mode} job=${job} />`}
      ${phase === 'finished' && job && html`<${Result} mode=${mode} job=${job} />`}
      ${phase === 'check' && data && data.files > 0 && !readOnly && !off && html`
        <label class="field"><span>${t(mode === 'restore' ? 'qr.restore.word' : 'qr.purge.word')}</span>
          <input type="text" data-testid="qr-confirm-input" value=${typed} autocomplete="off" spellcheck="false" placeholder=${word}
            onInput=${(e) => setTyped(e.target.value)} /></label>
        ${!csrfOk && html`<p class="note error" data-testid="qr-no-csrf">${t('prev.noCsrf')}</p>`}`}
      ${phase === 'check' && data && data.files > 0 && readOnly && html`<${CliCommand} spec=${spec} onCopy=${copy} />`}
      ${phase === 'check' && off && html`<${ExecOff} />`}
      ${error && html`<p class="note error" role="alert" data-testid="qr-dialog-error">${error}</p>`}
      <div class="dialog-actions">
        <button type="button" class="btn" data-testid="qr-cancel" disabled=${running} onClick=${() => onClose(changed.current)}>
          ${phase === 'finished' ? t('common.close') : t('common.cancel')}</button>
        ${phase !== 'finished' && !readOnly && html`<button type="submit" class=${'btn ' + (mode === 'restore' ? 'btn-primary' : 'btn-danger')} data-testid="qr-confirm"
          disabled=${!ready}>${(running || starting) && html`<span class="spinner" aria-hidden="true"></span> `}${t(mode === 'restore' ? 'qr.restore.go' : 'qr.purge.go')}</button>`}
      </div>
    </form>
  <//>`;
}

// ------------------------------------------------------------------ overview

function CloudCard({ r, remotes, active, filter, onFilter }) {
  const q = r.quota && r.quota.used !== undefined && r.quota.used !== null ? r.quota : null;
  const after = q ? Math.max(0, q.used - r.due_bytes) : null;
  return html`<article class=${'cloud-card qr-cloud' + (active ? ' on' : '')} data-testid=${'qr-cloud-' + r.remote} data-files=${r.files} data-due=${r.due_files}>
    <header><${CloudName} remotes=${remotes} name=${r.remote} /></header>
    ${r.files === 0
    ? html`<p class="muted" data-testid=${'qr-cloud-empty-' + r.remote}>${t('qr.cloud.empty')}</p>`
    : html`<p class="cloud-total"><strong>${t('qr.cloud.held', { files: fmtNum(r.files), bytes: fmtBytes(r.bytes) })}</strong></p>
      <p class=${r.due_files > 0 ? 'qr-due' : 'muted'}>${r.due_files > 0 ? t('qr.cloud.due', { files: fmtNum(r.due_files), bytes: fmtBytes(r.due_bytes) }) : t('qr.cloud.dueNone')}</p>`}
    ${q && html`<p class="qr-quota" data-testid=${'qr-quota-' + r.remote}>${q.total ? t('qr.cloud.quota', { used: fmtBytes(q.used), total: fmtBytes(q.total) }) : t('qr.cloud.quotaUsed', { used: fmtBytes(q.used) })}
      ${r.due_bytes > 0 && html`<br /><span class="muted">${t('qr.cloud.quotaAfter', { after: fmtBytes(after) })}</span>`}</p>`}
    ${r.files > 0 && html`<a class="link qr-filter-link" href=${quarantineHash(null, { remote: active ? '' : r.remote })} data-testid=${'qr-cloud-filter-' + r.remote}
      onClick=${(e) => { if (e.ctrlKey || e.metaKey || e.shiftKey || e.button) return; e.preventDefault(); onFilter(active ? '' : r.remote); }}>
      ${active ? t('qr.cloud.showAll') : t('qr.cloud.show')}</a>`}
  </article>`;
}

function PlanRow({ p, onOpen }) {
  const open = (e) => { if (e.ctrlKey || e.metaKey || e.shiftKey || e.button) return; e.preventDefault(); onOpen(p.plan_id); };
  const due = p.due.files > 0 ? (p.due.files < p.held.files ? t('qr.dueNowN', { n: fmtNum(p.due.files) }) : t('qr.dueNow')) : dueText(p.next_due_at);
  return html`<li class="qr-row" data-testid=${'qr-plan-row-' + p.plan_id} data-due=${p.due.files > 0 ? 'true' : 'false'}>
    <span class="pl-id">#${p.plan_id}</span>
    <${KindPill} kind=${p.kind} />
    <span class="pl-main"><span class="pl-sum" title=${p.title || ''}>${planSummary(p)} <${ByClaude} by=${p.created_by} /></span>
      <span class="pl-meta">${(p.remotes || []).join(t('common.listSep'))}</span></span>
    <span class="num qr-held" data-testid=${'qr-held-' + p.plan_id}>${t('qr.cloud.held', { files: fmtNum(p.held.files), bytes: fmtBytes(p.held.bytes) })}</span>
    <span class="muted" title=${fmtDateTime(p.oldest_at)}>${fmtDay(p.oldest_at)}</span>
    <span class=${'qr-dueat' + (p.due.files > 0 ? ' now' : '')} data-testid=${'qr-due-' + p.plan_id}>${due}</span>
    <a class="btn btn-small" href=${quarantineHash(p.plan_id)} data-testid=${'qr-plan-open-' + p.plan_id} onClick=${open}
      aria-label=${t('qr.openLink')}><${Icon} name="chevron" size=${14} /></a>
  </li>`;
}

function oldestScan(remotes) {
  const held = remotes.filter((r) => r.files > 0);
  const use = held.length ? held : remotes;
  if (!use.length) return undefined;
  let min = null;
  for (const r of use) {
    const at = r.last_scan && r.last_scan.finished_at;
    if (!at) return null;
    if (!min || at < min) min = at;
  }
  return min;
}

function QuarantineOverview({ params }) {
  const ctx = useContext(AppCtx);
  const { config } = ctx;
  const cloudList = config.remotes || [];
  const s = useQuarantine();
  const [dialog, setDialog] = useState(null);
  const filter = params.get('remote') || '';
  useEffect(() => { refreshQuarantine(); }, []);
  const data = s.data;
  const setFilter = (remote) => ctx.nav(quarantineHash(null, { remote }));
  const open = (id) => ctx.nav(quarantineHash(id, filter ? { remote: filter } : {}));

  const remotes = (data && data.remotes) || [];
  const plans = ((data && data.plans) || []).filter((p) => !filter || (p.remotes || []).includes(filter));
  const holding = plans.filter((p) => p.held.files > 0);
  const settled = plans.filter((p) => p.held.files === 0);
  const unknown = ((data && data.unknown) || []).filter((u) => !filter || u.remote === filter);
  const dueNow = filter ? (cloudHolds(data, filter) || {}).due_files || 0 : data ? data.totals.due_files : 0;
  const duePlansList = holding.filter((p) => p.due.files > 0).map((p) => p.plan_id);
  const retention = data ? data.retention_days : 30;
  const scanned = oldestScan(remotes);
  const nothing = data && holding.length === 0 && settled.length === 0 && unknown.length === 0;

  function purgeDue() {
    setDialog({ mode: 'purge-due', olderThan: retention, remote: filter || null, planIds: duePlansList, scope: 'due' });
  }

  return html`<main class="view view-quarantine" data-testid="view-quarantine" aria-label=${t('nav.quarantine')}>
    <${ViewHeader} title=${t('nav.quarantine')} hint=${t('qr.hint')}>
      ${data && html`<span class="qr-retention muted" data-testid="qr-retention" title=${t('qr.retentionTip')}>${t('qr.retention', { days: retention })}</span>`}
      ${scanned !== undefined && html`<${AgeChip} iso=${scanned} testid="qr-synced" />`}
    <//>
    <div class="view-body">
      ${s.error && !data && html`<p class="view-error" role="alert" data-testid="qr-error">${t('qr.loadError')} ${errorText(s.error)}</p>`}
      ${!s.loaded && html`<p class="empty-hint">${t('common.loading')}</p>`}
      ${execOff(config) && html`<${ExecOff} />`}
      ${data && html`<div class="cloud-cards qr-cloud-cards" data-testid="qr-clouds">
        ${remotes.map((r) => html`<${CloudCard} key=${r.remote} r=${r} remotes=${cloudList} active=${filter === r.remote} onFilter=${setFilter} />`)}
      </div>`}
      ${data && filter && html`<p class="muted qr-filter-note" data-testid="qr-filter-note">${t('qr.cloud.only', { cloud: filter })}
        <button class="link" data-testid="qr-filter-clear" onClick=${() => setFilter('')}>${t('qr.cloud.showAll')}</button></p>`}
      ${data && (holding.length > 0 || dueNow > 0) && html`<div class="qr-actions">
        <button class="btn btn-danger-ghost" data-testid="qr-purge-due" disabled=${dueNow === 0 || duePlansList.length === 0 || execOff(config)}
          title=${dueNow === 0 ? t('qr.purgeDueNone') : ''} onClick=${purgeDue}>
          <${Icon} name="trash" size=${14} /> ${filter ? t('qr.purgeDueCloud', { cloud: filter }) : t('qr.purgeDue')}</button>
      </div>`}
      ${data && holding.length > 0 && html`<section class="ov-panel" aria-labelledby="qr-plans-h">
        <div class="ov-panel-head"><h2 id="qr-plans-h">${t('qr.plans')}</h2></div>
        <div class="qr-head" aria-hidden="true"><span>${t('qr.col.plan')}</span><span></span><span></span><span class="num">${t('qr.col.held')}</span>
          <span>${t('qr.col.ran')}</span><span>${t('qr.col.due')}</span><span></span></div>
        <ul class="qr-list" data-testid="qr-plans">${holding.map((p) => html`<${PlanRow} key=${p.plan_id} p=${p} onOpen=${open} />`)}</ul>
      </section>`}
      ${nothing && html`<p class="empty-hint" data-testid="qr-empty">${t('qr.empty')}</p>`}
      ${data && holding.length === 0 && !nothing && settled.length > 0 && html`<p class="empty-hint" data-testid="qr-none-held">${t('qr.noPlans')}</p>`}
      ${settled.length > 0 && html`<details class="ov-panel qr-settled" data-testid="qr-settled">
        <summary>${t('qr.settled', { n: fmtNum(settled.length) })}</summary>
        <ul class="qr-list">${settled.map((p) => html`<li key=${p.plan_id} class="qr-row qr-settled-row" data-testid=${'qr-settled-row-' + p.plan_id}>
          <span class="pl-id">#${p.plan_id}</span><${KindPill} kind=${p.kind} />
          <span class="pl-main"><span class="pl-sum">${planSummary(p)}</span></span>
          <span class="muted">${t('qr.settledRow', { restored: fmtNum(p.restored), purged: fmtNum(p.purged) })}</span>
          <a class="btn btn-small" href=${quarantineHash(p.plan_id, { state: 'all' })} data-testid=${'qr-settled-open-' + p.plan_id}
            onClick=${(e) => { e.preventDefault(); ctx.nav(quarantineHash(p.plan_id, { state: 'all' })); }}>${t('qr.openLink')}</a></li>`)}</ul>
      </details>`}
      ${unknown.length > 0 && html`<details class="ov-panel qr-unknown" data-testid="qr-unknown">
        <summary>${t('qr.unknown', { files: fmtNum(unknown.reduce((a, u) => a + u.files, 0)), bytes: fmtBytes(unknown.reduce((a, u) => a + u.bytes, 0)) })}</summary>
        <p class="muted">${t('qr.unknownHelp')}</p>
        <ul class="qr-unknown-list">${unknown.map((u) => html`<li key=${u.remote} data-testid=${'qr-unknown-' + u.remote}>
          <strong>${t('qr.unknownRow', { cloud: u.remote, files: fmtNum(u.files), bytes: fmtBytes(u.bytes) })}</strong>
          <span class="muted"> ${t('qr.unknownEg')}: </span>${(u.examples || []).map((x, i) => html`<span key=${i} class="mono qr-example">${x}</span>`)}</li>`)}</ul>
      </details>`}
    </div>
    ${dialog && html`<${ActionDialog} spec=${dialog} onClose=${(changed) => { setDialog(null); if (changed) refreshQuarantine(); }} />`}
  </main>`;
}

// ------------------------------------------------------------------ a plan's quarantine

/** The loaded items as a tree of original folders. With several clouds the clouds are the first level. */
function buildTree(items, multi) {
  const root = { key: '', kids: new Map(), files: [], ids: [], count: 0, bytes: 0 };
  const keyOf = (remote, path) => (multi ? `${remote}:${path}` : path);
  for (const it of items) {
    const segs = it.path.split('/');
    segs.pop();
    const chain = [];
    if (multi) chain.push({ name: it.remote, path: '', remote: it.remote });
    segs.forEach((name, i) => chain.push({ name, path: segs.slice(0, i + 1).join('/'), remote: it.remote }));
    let node = root;
    node.count += 1; node.bytes += sizeOf(it);
    for (const c of chain) {
      const key = keyOf(c.remote, c.path);
      if (!node.kids.has(c.name)) node.kids.set(c.name, { key, name: c.name, remote: c.remote, path: c.path, kids: new Map(), files: [], ids: [], count: 0, bytes: 0 });
      node = node.kids.get(c.name);
      node.count += 1; node.bytes += sizeOf(it);
      if (it.state === 'held') node.ids.push(it.op_id);
    }
    node.files.push(it);
  }
  return root;
}

function defaultOpen(root, total) {
  const open = new Set();
  const walk = (node, depth) => {
    for (const kid of node.kids.values()) {
      if (total <= AUTO_OPEN || depth === 0) open.add(kid.key);
      walk(kid, depth + 1);
    }
  };
  walk(root, 0);
  if (total > AUTO_OPEN) { for (const kid of root.kids.values()) open.add(kid.key); }
  return open;
}

function flatten(node, open, depth, rows) {
  const folders = [...node.kids.values()].sort((a, b) => natural(a.name, b.name));
  for (const f of folders) {
    rows.push({ type: 'folder', node: f, depth });
    if (open.has(f.key)) flatten(f, open, depth + 1, rows);
  }
  [...node.files].sort((a, b) => natural(baseName(a.path), baseName(b.path))).forEach((it) => rows.push({ type: 'file', item: it, depth }));
  return rows;
}

function flagsOf(it) {
  if (it.state !== 'held' || !it.catalog) return [];
  const c = it.catalog;
  const out = [];
  if (c.original_taken) out.push('taken');
  if (c.seen === false) out.push('unseen');
  if (c.witness_live === false) out.push('witnessGone');
  else if (c.witness_live === true && c.witness_same === false) out.push('unproven');
  return out;
}

function Flags({ it }) {
  const fl = flagsOf(it);
  if (!fl.length) return null;
  return html`<span class="qr-flags">${fl.map((f) => html`<span key=${f} class=${'qr-flag qr-flag-' + f} data-flag=${f} title=${t(`qr.flag.${f}Tip`)}>
    <${Icon} name="alert" size=${11} />${t(`qr.flag.${f}`)}</span>`)}</span>`;
}

function ItemRow({ row, sel, focusKey, detail, onToggle, onDetail, onFocus }) {
  const it = row.item;
  const held = it.state === 'held';
  const name = baseName(it.path);
  const key = `i:${it.op_id}`;
  return html`<div class=${'qr-item' + (held ? '' : ' settled') + (detail === it.op_id ? ' selected' : '')} role="treeitem" aria-level=${row.depth + 1}
    aria-selected=${detail === it.op_id} tabindex=${focusKey === key ? 0 : -1} data-rowkey=${key} data-testid=${'qr-item-' + it.op_id} data-state=${it.state}
    style=${{ '--depth': row.depth }} onClick=${() => onDetail(it.op_id)} onFocus=${() => onFocus(key)}>
    ${held ? html`<input type="checkbox" class="qr-cb" data-testid=${'qr-check-' + it.op_id} checked=${sel.has(it.op_id)} aria-label=${t('qr.selectItem', { name })}
      tabindex="-1" onClick=${(e) => e.stopPropagation()} onChange=${() => onToggle([it.op_id])} />` : html`<span class="qr-check-space"></span>`}
    <span class="twisty-space"></span>
    <${Icon} name="file" size=${15} />
    <span class="name" title=${it.path}>${name}</span>
    <${Flags} it=${it} />
    <span class="meta">
      <span class="meta-size">${it.size ? fmtBytes(it.size) : ''}</span>
      <span class="qr-when">${held ? dueItemText(it.due_at) : it.state === 'restored' ? t('qr.at.restored', { when: fmtDay(it.restored_at) }) : t('qr.at.purged', { when: fmtDay(it.purged_at) })}</span>
    </span>
  </div>`;
}

function FolderRow({ row, sel, focusKey, open, onToggle, onOpen, onFocus }) {
  const n = row.node;
  const chosen = n.ids.filter((id) => sel.has(id)).length;
  const all = n.ids.length > 0 && chosen === n.ids.length;
  const key = `f:${n.key}`;
  return html`<div class="qr-folder" role="treeitem" aria-level=${row.depth + 1} aria-expanded=${open} tabindex=${focusKey === key ? 0 : -1}
    data-rowkey=${key} data-testid=${'qr-folder-' + n.key} style=${{ '--depth': row.depth }} onClick=${() => onOpen(n.key)} onFocus=${() => onFocus(key)}>
    ${n.ids.length > 0 ? html`<input type="checkbox" class="qr-cb" data-testid=${'qr-folder-check-' + n.key} checked=${all} indeterminate=${chosen > 0 && !all}
      aria-label=${t('qr.selectFolder', { name: n.name })} tabindex="-1" onClick=${(e) => e.stopPropagation()} onChange=${() => onToggle(n.ids, all)} />`
    : html`<span class="qr-check-space"></span>`}
    <button class=${'twisty' + (open ? ' open' : '')} tabindex="-1" aria-hidden="true" data-testid=${'qr-toggle-' + n.key}
      onClick=${(e) => { e.stopPropagation(); onOpen(n.key); }}><${Icon} name="chevron" size=${12} /></button>
    <${Icon} name="folder" size=${15} />
    <span class="name">${n.name}/</span>
    <span class="meta"><span class="meta-files">${fmtNum(n.count)}</span><span class="meta-size">${fmtBytes(n.bytes)}</span></span>
  </div>`;
}

function Witness({ it }) {
  const c = it.catalog || {};
  if (!it.witness) return html`<span class="muted">${t('qr.detail.noWitness')}</span>`;
  const status = c.witness_live === false ? 'gone' : c.witness_live === true && c.witness_same === true ? 'same' : c.witness_live === true ? 'live' : null;
  return html`<span class="mono">${it.witness.remote}:${it.witness.path}</span>
    ${status && html`<span class=${'qr-wit qr-wit-' + status} data-testid="qr-witness-status" data-status=${status}><${Icon} name=${status === 'same' ? 'check' : 'alert'} size=${12} /> ${t(`qr.witness.${status}`)}</span>`}`;
}

function ItemDetail({ it, onReveal }) {
  if (!it) return html`<aside class="qr-detail empty" data-testid="qr-detail"><p class="muted">${t('qr.detail.empty')}</p></aside>`;
  return html`<aside class="qr-detail" data-testid="qr-detail" data-op=${it.op_id}>
    <h3>${baseName(it.path)}</h3>
    <dl>
      <dt>${t('qr.detail.was')}</dt><dd class="mono" data-testid="qr-detail-was">${it.remote}:${it.path}</dd>
      <dt>${t('qr.detail.now')}</dt><dd class="mono" data-testid="qr-detail-now">${it.state === 'purged' ? '–' : `${it.remote}:${it.quarantine_path}`}</dd>
      <dt>${t('qr.detail.why')}</dt><dd data-testid="qr-detail-why">${it.reason || '–'} <span class="muted">(${t('qr.detail.step', { seq: it.seq, op: t(`plans.op.${it.op}`, null, it.op) })})</span></dd>
      <dt>${t('qr.detail.witness')}</dt><dd data-testid="qr-detail-witness"><${Witness} it=${it} /></dd>
      <dt>${t('qr.detail.size')}</dt><dd>${it.size ? fmtBytes(it.size) : '–'}</dd>
      <dt>${t('qr.detail.quarantined')}</dt><dd>${fmtDateTime(it.quarantined_at)}</dd>
      ${it.state === 'held' && html`<dt>${t('qr.detail.dueAt')}</dt><dd>${fmtDateTime(it.due_at)} · ${dueItemText(it.due_at)}</dd>`}
      ${it.restored_at && html`<dt>${t('qr.detail.restoredAt')}</dt><dd>${fmtDateTime(it.restored_at)}</dd>`}
      ${it.purged_at && html`<dt>${t('qr.detail.purgedAt')}</dt><dd>${fmtDateTime(it.purged_at)}</dd>`}
    </dl>
    <${Flags} it=${it} />
    <div class="qr-detail-links">
      <button class="link" data-testid="qr-reveal-original" onClick=${() => onReveal({ remote: it.remote, path: parentOf(it.path) })}>
        <${Icon} name="jump" size=${12} /> ${t('qr.reveal.original')}</button>
      ${it.witness && html`<button class="link" data-testid="qr-reveal-witness" onClick=${() => onReveal({ remote: it.witness.remote, path: it.witness.path })}>
        <${Icon} name="jump" size=${12} /> ${t('qr.reveal.witness')}</button>`}
    </div>
  </aside>`;
}

function usePlanItems(id, state, remote, path) {
  const [s, setS] = useState({ loading: true, error: null, plan: null, items: [], total: 0, bytes: 0 });
  const seq = useRef(0);
  const [token, setToken] = useState(0);
  const query = (offset) => ({
    state: state === 'held' ? undefined : state, remote: remote || undefined, path: path || undefined, offset: offset || undefined, limit: PAGE,
  });
  const key = `${id}|${state}|${remote}|${path}`;
  useEffect(() => {
    const mine = (seq.current += 1);
    setS((x) => ({ ...x, loading: true, error: null }));
    api.quarantineItems(id, query(0)).then(
      (r) => { if (mine === seq.current) setS({ loading: false, error: null, plan: r.plan, items: r.items, total: r.total, bytes: r.bytes }); },
      (e) => { if (mine === seq.current) setS((x) => ({ ...x, loading: false, error: e, items: [], total: 0, bytes: 0 })); },
    );
  }, [key, token]);
  const more = () => {
    const mine = (seq.current += 1);
    setS((x) => ({ ...x, loading: true }));
    return api.quarantineItems(id, query(s.items.length)).then(
      (r) => { if (mine === seq.current) setS((x) => ({ loading: false, error: null, plan: r.plan, items: [...x.items, ...r.items], total: r.total, bytes: r.bytes })); },
      (e) => { if (mine === seq.current) setS((x) => ({ ...x, loading: false, error: e })); },
    );
  };
  return [s, more, () => setToken((n) => n + 1)];
}

function PurgeMenu({ disabled, selected, onPick }) {
  const [open, setOpen] = useState(false);
  const box = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const away = (e) => { if (box.current && !box.current.contains(e.target)) setOpen(false); };
    const esc = (e) => { if (e.key === 'Escape') { e.stopPropagation(); setOpen(false); } };
    document.addEventListener('mousedown', away);
    document.addEventListener('keydown', esc, true);
    return () => { document.removeEventListener('mousedown', away); document.removeEventListener('keydown', esc, true); };
  }, [open]);
  const pick = (which) => { setOpen(false); onPick(which); };
  return html`<span class="qr-menu-box" ref=${box}>
    <button class="btn btn-danger-ghost" data-testid="qr-purge-menu" aria-haspopup="menu" aria-expanded=${open} disabled=${disabled} onClick=${() => setOpen(!open)}>
      <${Icon} name="trash" size=${14} /> ${t('qr.purge')} ▾</button>
    ${open && html`<div class="qr-menu" role="menu">
      <button role="menuitem" data-testid="qr-purge-selected" disabled=${selected === 0} onClick=${() => pick('selected')}>${t('qr.purgeSelected')}</button>
      <button role="menuitem" data-testid="qr-purge-due-plan" onClick=${() => pick('past')}>${t('qr.purgePast')}</button>
      <button role="menuitem" data-testid="qr-purge-now" onClick=${() => pick('all')}>${t('qr.purgeNow')}</button>
    </div>`}
  </span>`;
}

function PlanQuarantine({ id, params }) {
  const ctx = useContext(AppCtx);
  const { config } = ctx;
  const report = useQuarantine().data;
  const state = STATES.includes(params.get('state')) ? params.get('state') : 'held';
  const remote = params.get('remote') || '';
  const path = (params.get('path') || '').replace(/^\/+|\/+$/g, '');
  const [pathText, setPathText] = useState(path);
  const [s, loadMore, reload] = usePlanItems(id, state, remote, path);
  const [sel, setSel] = useState(() => new Set());
  const [detail, setDetail] = useState(null);
  const [auto, setAuto] = useState(true);
  const [openSet, setOpenSet] = useState(() => new Set());
  const [focusKey, setFocusKey] = useState(null);
  const [dialog, setDialog] = useState(null);
  const tree = useRef(null);
  const filterKey = `${id}|${state}|${remote}|${path}`;

  useEffect(() => { refreshQuarantine(); }, [id]);
  useEffect(() => { setSel(new Set()); setDetail(null); setAuto(true); setFocusKey(null); }, [filterKey]);
  useEffect(() => { setPathText(path); }, [path]);
  // The folder box writes the address a moment after typing stops; Back and links rewrite the box.
  useEffect(() => {
    if (pathText.replace(/^\/+|\/+$/g, '') === path) return undefined;
    const timer = setTimeout(() => ctx.nav(quarantineHash(id, { state: state === 'held' ? '' : state, remote, path: pathText.replace(/^\/+|\/+$/g, '') }), { replace: true }), 300);
    return () => clearTimeout(timer);
  }, [pathText]);

  const plan = s.plan;
  const items = s.items;
  const remotesHere = useMemo(() => {
    const fromReport = ((report && report.plans) || []).find((p) => p.plan_id === id);
    if (fromReport && fromReport.remotes && fromReport.remotes.length) return fromReport.remotes;
    return [...new Set(items.map((i) => i.remote))];
  }, [report, id, items.length]);
  const multi = new Set(items.map((i) => i.remote)).size > 1;
  const root = useMemo(() => buildTree(items, multi), [items, multi]);
  const effectiveOpen = auto ? defaultOpen(root, items.length) : openSet;
  const rows = useMemo(() => flatten(root, effectiveOpen, 0, []), [root, auto, openSet, items.length]);
  const byId = useMemo(() => new Map(items.map((i) => [i.op_id, i])), [items]);
  const chosen = [...sel].filter((x) => byId.has(x));
  const chosenBytes = chosen.reduce((a, x) => a + sizeOf(byId.get(x)), 0);
  const retention = report ? report.retention_days : 30;
  const reportPlan = ((report && report.plans) || []).find((p) => p.plan_id === id);
  const planHeld = reportPlan ? reportPlan.held.files : (state === 'held' || state === 'all' ? s.total : 1);
  const off = execOff(config);
  const tooMany = chosen.length > MAX_OPS;

  const filterNav = (patch, replace = false) => {
    const next = { state, remote, path, ...patch };
    ctx.nav(quarantineHash(id, { state: next.state === 'held' ? '' : next.state, remote: next.remote, path: next.path }), { replace });
  };
  const toggleOpen = (key) => {
    const base = new Set(effectiveOpen);
    if (base.has(key)) base.delete(key); else base.add(key);
    setOpenSet(base);
    setAuto(false);
  };
  const toggleIds = (ids, allChosen) => setSel((cur) => {
    const next = new Set(cur);
    const everything = typeof allChosen === 'boolean' ? allChosen : ids.every((x) => next.has(x));
    ids.forEach((x) => { if (everything) next.delete(x); else next.add(x); });
    return next;
  });

  function startRestore(all) {
    setDialog({ mode: 'restore', planId: id, opIds: all ? null : chosen, scope: all ? 'all' : 'selected' });
  }
  function startPurge(which) {
    if (which === 'selected') setDialog({ mode: 'purge', planId: id, opIds: chosen, olderThan: 0, scope: 'selected' });
    else if (which === 'past') setDialog({ mode: 'purge', planId: id, opIds: null, olderThan: retention, scope: 'past' });
    else setDialog({ mode: 'purge', planId: id, opIds: null, olderThan: 0, scope: 'all' });
  }
  const closeDialog = (changed) => {
    setDialog(null);
    if (changed) { setSel(new Set()); reload(); refreshQuarantine(); }
  };

  const onKeyDown = (e) => {
    const rowEl = e.target.closest && e.target.closest('[data-rowkey]');
    if (!rowEl || e.target.tagName === 'INPUT' && e.key !== ' ') return;
    const keys = [...tree.current.querySelectorAll('[data-rowkey]')];
    const at = keys.indexOf(rowEl);
    const key = rowEl.dataset.rowkey;
    const move = (to) => { const el = keys[Math.max(0, Math.min(keys.length - 1, to))]; if (el) { setFocusKey(el.dataset.rowkey); el.focus(); } };
    if (e.key === 'ArrowDown') { e.preventDefault(); move(at + 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); move(at - 1); }
    else if (e.key === 'ArrowRight' && key.startsWith('f:') && rowEl.getAttribute('aria-expanded') === 'false') { e.preventDefault(); toggleOpen(key.slice(2)); }
    else if (e.key === 'ArrowLeft' && key.startsWith('f:') && rowEl.getAttribute('aria-expanded') === 'true') { e.preventDefault(); toggleOpen(key.slice(2)); }
    else if (e.key === ' ') {
      e.preventDefault();
      const box = rowEl.querySelector('input[type=checkbox]');
      if (box) box.click();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (key.startsWith('f:')) toggleOpen(key.slice(2)); else setDetail(Number(key.slice(2)));
    }
  };

  const shownFiles = items.length;
  const loadedAll = shownFiles >= s.total;
  const detailItem = detail !== null ? byId.get(detail) : null;
  const ranAt = plan && plan.finished_at ? fmtDay(plan.finished_at) : '';

  return html`<main class="view view-quarantine view-quarantine-plan" data-testid="view-quarantine-plan" data-id=${id} aria-label=${t('qr.planTitle', { id })}>
    <${ViewHeader} title=${t('qr.planTitle', { id })} hint=${plan ? `${t(`kind.${plan.kind}`, null, plan.kind)}${plan.title ? ` · ${plan.title}` : ''}${ranAt ? ` · ${t('qr.ran', { when: ranAt })}` : ''}` : ''}>
      <a class="btn btn-small" href="#/quarantine" data-testid="qr-back" onClick=${(e) => { e.preventDefault(); ctx.nav(quarantineHash(null, remote ? { remote } : {})); }}>${t('qr.back')}</a>
      <a class="btn btn-small" href=${`#/plans/${id}`} data-testid="qr-open-review" onClick=${(e) => { e.preventDefault(); ctx.nav(`#/plans/${id}`); }}>${t('qr.openReview')}</a>
    <//>
    <div class="qr-filters" data-testid="qr-filters">
      <label class="field field-inline"><span>${t('qr.filter.state')}</span>
        <select class="select" data-testid="qr-filter-state" value=${state} onChange=${(e) => filterNav({ state: e.target.value })}>
          ${STATES.map((x) => html`<option key=${x} value=${x} selected=${x === state}>${t(`qr.state.${x}`)}</option>`)}</select></label>
      <label class="field field-inline"><span>${t('qr.filter.cloud')}</span>
        <select class="select" data-testid="qr-filter-remote" value=${remote} onChange=${(e) => filterNav({ remote: e.target.value })}>
          <option value="" selected=${remote === ''}>${t('qr.filter.allClouds')}</option>
          ${remotesHere.map((x) => html`<option key=${x} value=${x} selected=${x === remote}>${x}</option>`)}</select></label>
      <label class="field field-inline qr-path"><span>${t('qr.filter.folder')}</span>
        <input type="text" class="qr-path-input" data-testid="qr-filter-path" value=${pathText} placeholder=${t('qr.filter.folderPlaceholder')}
          autocomplete="off" spellcheck="false" onInput=${(e) => setPathText(e.target.value)} /></label>
      <span class="spacer"></span>
      <span class="muted qr-total" data-testid="qr-total">${t('qr.total', { files: fmtNum(s.total), bytes: fmtBytes(s.bytes) })}</span>
    </div>
    ${execOff(config) && html`<div class="qr-pad"><${ExecOff} /></div>`}
    <div class="qr-split">
      <section class="qr-tree-box">
        ${s.error && html`<p class="view-error" role="alert" data-testid="qr-error">${errorText(s.error)}</p>`}
        ${s.loading && items.length === 0 && !s.error && html`<p class="empty-hint">${t('common.loading')}</p>`}
        ${!s.loading && !s.error && items.length === 0 && html`<p class="empty-hint" data-testid="qr-no-items">${t('qr.noItems')}</p>`}
        <div class="qr-tree" role="tree" aria-label=${t('qr.tree.label')} ref=${tree} data-testid="qr-tree" onKeyDown=${onKeyDown}>
          ${rows.map((row, i) => {
    const fk = focusKey || (rows[0] && (rows[0].type === 'folder' ? `f:${rows[0].node.key}` : `i:${rows[0].item.op_id}`));
    return row.type === 'folder'
      ? html`<${FolderRow} key=${'f:' + row.node.key} row=${row} sel=${sel} focusKey=${fk} open=${effectiveOpen.has(row.node.key)}
          onToggle=${toggleIds} onOpen=${toggleOpen} onFocus=${setFocusKey} />`
      : html`<${ItemRow} key=${'i:' + row.item.op_id} row=${row} sel=${sel} focusKey=${fk} detail=${detail}
          onToggle=${toggleIds} onDetail=${setDetail} onFocus=${setFocusKey} />`;
  })}
        </div>
        ${items.length > 0 && !loadedAll && html`<div class="ex-more"><span class="muted" data-testid="qr-loaded">${t('qr.loaded', { shown: fmtNum(shownFiles), files: fmtNum(s.total) })}</span>
          <button class="btn" data-testid="qr-more" disabled=${s.loading} onClick=${loadMore}>${t('qr.loadMore', { n: Math.min(PAGE, s.total - shownFiles) })}</button></div>`}
      </section>
      <${ItemDetail} it=${detailItem} onReveal=${(loc) => ctx.revealInWorkspace(loc)} />
    </div>
    <footer class="plan-bar qr-bar" data-testid="qr-bar">
      <span class="qr-selected" data-testid="qr-selected" data-count=${chosen.length}>${chosen.length ? t('qr.selected', { n: fmtNum(chosen.length), bytes: fmtBytes(chosenBytes) }) : t('qr.selectedNone')}</span>
      ${tooMany && html`<span class="note error" data-testid="qr-too-many">${t('qr.tooMany', { n: fmtNum(MAX_OPS) })}</span>`}
      <span class="spacer"></span>
      <button class="btn" data-testid="qr-restore-selected" disabled=${chosen.length === 0 || tooMany || off} onClick=${() => startRestore(false)}>
        <${Icon} name="undo" size=${14} /> ${t('qr.restoreSelected')}</button>
      <button class="btn btn-primary" data-testid="qr-restore-all" disabled=${off || planHeld === 0} onClick=${() => startRestore(true)}>
        <${Icon} name="undo" size=${14} /> ${t('qr.restoreAll')}</button>
      <${PurgeMenu} disabled=${off} selected=${chosen.length && !tooMany ? chosen.length : 0} onPick=${startPurge} />
    </footer>
    ${dialog && html`<${ActionDialog} spec=${dialog} onClose=${closeDialog} />`}
  </main>`;
}

// ------------------------------------------------------------------ the box on a plan's review

/**
 * "N files (X) of this plan are in quarantine", with Restore… and a link to the plan's quarantine. Nothing is drawn while
 * the plan holds nothing (or the answer is not there), so a plan that kept no files shows no box.
 */
export function PlanQuarantineBox({ planId }) {
  const ctx = useContext(AppCtx);
  const [info, setInfo] = useState(null);
  const [dialog, setDialog] = useState(null);
  const [token, setToken] = useState(0);
  useEffect(() => {
    let live = true;
    api.quarantineItems(planId, { limit: 1 }).then(
      (r) => { if (live) setInfo({ total: r.total, bytes: r.bytes }); },
      () => { if (live) setInfo(null); },
    );
    return () => { live = false; };
  }, [planId, token]);
  if (!info || !info.total) return null;
  return html`<section class="purge-box qr-box" data-testid="plan-quarantine-box" data-files=${info.total}>
    <${Icon} name="lock" size=${18} />
    <div class="purge-main"><strong data-testid="plan-quarantine-text">${t('qr.box.text', { files: fmtNum(info.total), bytes: fmtBytes(info.bytes) })}</strong>
      <p class="muted">${t('qr.box.help')}</p></div>
    <div class="purge-actions">
      <button class="btn" data-testid="plan-quarantine-restore" disabled=${execOff(ctx.config)}
        onClick=${() => setDialog({ mode: 'restore', planId, opIds: null, scope: 'all' })}>${t('qr.box.restore')}</button>
      <a class="btn" href=${quarantineHash(planId)} data-testid="plan-quarantine-open"
        onClick=${(e) => { if (e.ctrlKey || e.metaKey || e.shiftKey || e.button) return; e.preventDefault(); ctx.nav(quarantineHash(planId)); }}>${t('qr.openLink')}</a>
    </div>
    ${dialog && html`<${ActionDialog} spec=${dialog} onClose=${(changed) => { setDialog(null); if (changed) setToken((n) => n + 1); }} />`}
  </section>`;
}

// ------------------------------------------------------------------ the route

export function Quarantine({ route }) {
  if (route.id) return html`<${PlanQuarantine} key=${route.id} id=${route.id} params=${route.params} />`;
  return html`<${QuarantineOverview} params=${route.params} />`;
}
