// Sync (docs/ONBOARDING.md section 5): the job that lists every cloud into the catalog, its state for the whole page,
// and the header's Sync button with the progress pill and the per-cloud popover.
import {
  html, useContext, useEffect, useRef, useState,
} from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import * as api from './api.js';
import { Icon } from './icons.js';
import { AppCtx } from './ctx.js';
import { CloudDot } from './clouds.js';
import {
  errorText, fmtBytes, fmtNum, safeStorage, sleep,
} from './util.js';

const SYNC_POLL_MS = 600;
const SYNC_KEY = 'dm-sync-job';

/** The per-cloud view of a sync job: its progress while it runs, its result once it is over. */
function readSync(job, order, started) {
  const p = job.progress || {};
  const src = p.remotes && typeof p.remotes === 'object' && !Array.isArray(p.remotes) ? p.remotes : {};
  const now = Date.now();
  const out = {};
  for (const name of order) {
    const r = src[name] || (p.remote === name ? p : {});
    const state = r.state || 'queued';
    if (state === 'listing' && !started[name]) started[name] = { at: now, files: r.files || 0 };
    const s = started[name];
    const secs = s ? (now - s.at) / 1000 : 0;
    out[name] = {
      state, files: r.files || 0, bytes: r.bytes || 0, error: r.error || null,
      rate: s && secs >= 1 ? Math.round(((r.files || 0) - s.files) / secs) : null,
    };
  }
  const res = job.result && job.result.remotes;
  if (res) {
    for (const [name, r] of Object.entries(res)) {
      if (!out[name]) continue;
      const ok = r.status === 'complete' || r.status === 'done';
      out[name] = {
        ...out[name], state: ok ? 'done' : 'failed', files: r.files || 0, bytes: r.bytes || 0, error: ok ? null : (r.error || r.status || 'failed'),
      };
    }
  } else if (job.status === 'failed') {
    for (const name of order) if (out[name].state !== 'done') out[name] = { ...out[name], state: 'failed', error: job.error || null };
  }
  return out;
}

/**
 * The sync job of the whole page: started from the header, the Welcome screen, a cloud card or the free-space view,
 * shown by the header pill. `start(names)` resolves when the job is over: {ok, failed: [names], busy?}.
 */
export function useSyncJob({ names, onFinished }) {
  const [state, setState] = useState({
    phase: 'idle', order: [], remotes: {}, error: '',
  });
  const alive = useRef(true);
  const running = useRef(false);
  const namesRef = useRef(names);
  namesRef.current = names;
  const finishedRef = useRef(onFinished);
  finishedRef.current = onFinished;
  useEffect(() => () => { alive.current = false; }, []);

  async function track(jobId, order) {
    const started = {};
    let failures = 0;
    for (;;) {
      if (!alive.current) return null;
      try {
        const job = await api.job(jobId);
        if (!alive.current) return null;
        const remotes = readSync(job, order, started);
        setState((s) => ({ ...s, remotes }));
        failures = 0;
        if (job.status === 'done' || job.status === 'failed') return { job, remotes };
      } catch (e) {
        failures += 1;
        if (failures > 10) return { job: { status: 'failed', error: errorText(e) }, remotes: null };
      }
      await sleep(SYNC_POLL_MS);
    }
  }

  async function finish(out, order, all) {
    safeStorage('set', SYNC_KEY, '');
    running.current = false;
    const remotes = out.remotes || {};
    const failed = order.filter((n) => remotes[n] && remotes[n].state === 'failed');
    const phase = out.job.status === 'failed' && !out.remotes ? 'failed' : 'done';
    setState((s) => ({ ...s, phase, error: out.job.error || '' }));
    const result = { ok: out.job.status === 'done' && failed.length === 0, failed, names: order, all, error: out.job.error || '' };
    if (finishedRef.current) finishedRef.current(result);
    return result;
  }

  async function start(remotes) {
    if (running.current) return { ok: false, busy: true, failed: [], names: [] };
    const all = !remotes || !remotes.length;
    const order = all ? namesRef.current : remotes;
    running.current = true;
    setState({
      phase: 'running', order, error: '', remotes: Object.fromEntries(order.map((n) => [n, { state: 'queued', files: 0, bytes: 0 }])),
    });
    let jobId;
    try {
      jobId = (await api.sync(all ? undefined : remotes)).job_id;
    } catch (e) {
      running.current = false;
      setState((s) => ({ ...s, phase: 'failed', error: errorText(e) }));
      return { ok: false, failed: [], names: order, error: errorText(e) };
    }
    safeStorage('set', SYNC_KEY, String(jobId));
    const out = await track(jobId, order);
    return out ? finish(out, order, all) : { ok: false, failed: [], names: order };
  }

  /** A page reload while a sync runs: pick the job up again. */
  async function resume() {
    const id = safeStorage('get', SYNC_KEY);
    if (!id || running.current) return;
    try {
      const job = await api.job(id);
      if (job.status !== 'running' && job.status !== 'queued') { safeStorage('set', SYNC_KEY, ''); return; }
      const keys = job.progress && job.progress.remotes ? Object.keys(job.progress.remotes) : [];
      const order = keys.length ? keys : namesRef.current;
      running.current = true;
      setState({ phase: 'running', order, error: '', remotes: readSync(job, order, {}) });
      const out = await track(id, order);
      if (out) await finish(out, order, true);
    } catch (e) { safeStorage('set', SYNC_KEY, ''); }
  }

  return { ...state, running: state.phase === 'running', start, resume };
}

/** The Sync button, the progress pill (`Syncing 2/3 · 184,203 files`) and the per-cloud popover. */
export function SyncControl() {
  const ctx = useContext(AppCtx);
  const { sync } = ctx;
  const [open, setOpen] = useState(false);
  const box = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => { if (box.current && !box.current.contains(e.target)) setOpen(false); };
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onKey); };
  }, [open]);
  const remotes = ctx.config.remotes || [];
  const rows = sync.order.map((name) => ({ name, ...(sync.remotes[name] || { state: 'queued', files: 0, bytes: 0 }) }));
  const finished = rows.filter((r) => r.state === 'done' || r.state === 'failed').length;
  const failed = rows.filter((r) => r.state === 'failed').length;
  const files = rows.reduce((a, r) => a + (r.files || 0), 0);
  const label = sync.phase === 'running' ? t('sync.pill.running', { done: finished, total: rows.length, files: fmtNum(files) })
    : sync.phase === 'failed' ? t('sync.pill.failed')
      : failed ? t('sync.pill.doneFailed', { failed, total: rows.length })
        : t('sync.pill.done', { total: rows.length, files: fmtNum(files) });
  return html`<span class="sync-wrap" ref=${box}>
    <button class="btn btn-small" data-testid="sync-button" disabled=${sync.running || remotes.length === 0} onClick=${() => sync.start()}
      title=${remotes.length === 0 ? t('sync.noClouds') : t('sync.tip')}>
      ${sync.running ? html`<span class="spinner" aria-hidden="true"></span>` : html`<${Icon} name="refresh" size=${14} />`}
      <span class="btn-text">${t('sync.button')}</span></button>
    ${sync.phase !== 'idle' && html`<button class=${'sync-pill' + (sync.phase === 'failed' || failed ? ' warn' : sync.phase === 'done' ? ' ok' : '')}
      data-testid="sync-pill" data-phase=${sync.phase} aria-expanded=${open} aria-haspopup="true" onClick=${() => setOpen(!open)}>
      ${sync.phase === 'running' ? null : html`<${Icon} name=${sync.phase === 'done' && !failed ? 'check' : 'alert'} size=${12} />`}
      <span data-testid="sync-pill-text">${label}</span></button>`}
    ${open && sync.phase !== 'idle' && html`<div class="sync-popover" data-testid="sync-popover" role="dialog" aria-label=${t('sync.popover')}>
      <h3>${t('sync.popover')}</h3>
      <ul>${rows.map((r) => html`<li key=${r.name} data-testid=${'sync-row-' + r.name} data-state=${r.state}>
        <span class="sync-row-name"><${CloudDot} remotes=${remotes} name=${r.name} size=${8} />${r.name}</span>
        <span class=${'sync-state sync-state-' + r.state} data-testid=${'sync-state-' + r.name}>${t(`sync.state.${r.state}`, null, r.state)}</span>
        <span class="num muted" data-testid=${'sync-files-' + r.name}>${t('sync.files', { n: fmtNum(r.files) })} · ${fmtBytes(r.bytes)}</span>
        ${r.state === 'listing' && r.rate > 0 && html`<span class="muted" data-testid=${'sync-rate-' + r.name}>${t('sync.rate', { n: fmtNum(r.rate) })}</span>`}
        ${r.error && html`<p class="sync-error" data-testid=${'sync-error-' + r.name}><${Icon} name="alert" size=${12} /> ${r.error}</p>`}
      </li>`)}</ul>
      ${failed > 0 && html`<p class="muted sync-note">${t('sync.staleNote')}</p>`}
      ${sync.error && html`<p class="sync-error" data-testid="sync-error">${sync.error}</p>`}
    </div>`}
  </span>`;
}

