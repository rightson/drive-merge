// Free space (docs/ONBOARDING.md section 6): how much of one cloud is proven to exist on the others, what a
// verification would settle, what exists only here, and the one button that builds the plan which frees the proven part.
import {
  html, useContext, useEffect, useRef, useState,
} from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import * as api from './api.js';
import { Icon } from './icons.js';
import { AppCtx } from './ctx.js';
import { AgeChip, CloudDot } from './clouds.js';
import { ViewHeader } from './ui.js';
import { freeHash, plansHash } from './query.js';
import {
  ago, buildText, errorText, fmtBytes, fmtNum, sleep,
} from './util.js';

const PATH_MS = 400;
const cleanPath = (p) => String(p || '').trim().replace(/^\/+|\/+$/g, '');
const same = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);

/** Used / total with the part that freeing would give back striped at the end of the used part. */
function QuotaBar({ quota, freed, remote }) {
  if (!quota || quota.used === undefined || quota.used === null) {
    return html`<div class="free-quota" data-testid="free-quota" data-known="false">
      <span class="muted">${t('free.quotaUnknown')}</span>
      ${freed > 0 && html`<span data-testid="free-after">${t('free.wouldFree', { bytes: fmtBytes(freed) })}</span>`}</div>`;
  }
  const total = quota.total || 0;
  const after = Math.max(0, quota.used - freed);
  const pct = (n) => (total ? Math.min(100, (n / total) * 100) : 0);
  return html`<div class="free-quota" data-testid="free-quota" data-known="true">
    <div class="free-quota-text">
      <span data-testid="free-used">${total ? t('free.used', { used: fmtBytes(quota.used), total: fmtBytes(total) }) : t('free.usedOnly', { used: fmtBytes(quota.used) })}</span>
      <span data-testid="free-after"><strong>${t('free.after', { bytes: fmtBytes(after) })}</strong></span></div>
    ${total > 0 && html`<div class="free-quota-bar" role="img" data-testid="free-quota-bar"
      aria-label=${t('free.quotaLabel', { cloud: remote, used: fmtBytes(quota.used), after: fmtBytes(after) })}>
      <span class="free-quota-keep" style=${{ width: `${pct(after)}%` }}></span>
      <span class="free-quota-freed" data-testid="free-quota-freed" style=${{ width: `${pct(Math.min(freed, quota.used))}%` }}></span></div>`}
  </div>`;
}

function Bucket({ id, icon, tone, label, help, data, children }) {
  return html`<li class=${'free-bucket tone-' + tone} data-testid=${'bucket-' + id} data-files=${data.files} data-bytes=${data.bytes}>
    <span class="free-bucket-mark" aria-hidden="true"><${Icon} name=${icon} size=${16} /></span>
    <div class="free-bucket-main"><strong>${label}</strong><p class="muted">${help}</p>${children}</div>
    <div class="free-bucket-num"><strong data-testid=${'bucket-bytes-' + id}>${fmtBytes(data.bytes)}</strong>
      <span class="muted" data-testid=${'bucket-files-' + id}>${t('free.files', { n: fmtNum(data.files) })}</span></div>
  </li>`;
}

export function Free({ remote, params }) {
  const ctx = useContext(AppCtx);
  const remotes = ctx.config.remotes || [];
  const names = remotes.map((r) => r.name);
  const others = names.filter((n) => n !== remote);
  const path = cleanPath(params.get('path'));
  const keepParam = (params.get('keep') || '').split(',').map((s) => s.trim()).filter((s) => others.includes(s));
  const order = keepParam.length ? [...keepParam, ...others.filter((n) => !keepParam.includes(n))] : others;
  const enabled = keepParam.length ? keepParam : others;
  const keepKey = keepParam.join(',');
  const [pathText, setPathText] = useState(path);
  const [rep, setRep] = useState({ loading: true, data: null, error: null });
  const [reload, setReload] = useState(0);
  const [verify, setVerify] = useState(null);       // {phase: running|done|failed, bridged, failed, total}
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState(null);
  const seq = useRef(0);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const go = (patch, opts) => {
    const next = { path, keep: keepParam, ...patch };
    ctx.nav(freeHash(remote, { path: next.path, keep: next.keep }), opts);
  };
  useEffect(() => { setPathText(path); }, [path]);
  useEffect(() => {
    if (cleanPath(pathText) === path) return undefined;
    const timer = setTimeout(() => go({ path: cleanPath(pathText) }, { replace: true }), PATH_MS);
    return () => clearTimeout(timer);
  }, [pathText]);

  async function fetchReport() {
    return api.release(remote, { path, keep: keepParam });
  }
  useEffect(() => {
    const mine = (seq.current += 1);
    setRep((r) => ({ ...r, loading: true, error: null }));
    fetchReport().then(
      (data) => { if (mine === seq.current) setRep({ loading: false, data, error: null }); },
      (e) => { if (mine === seq.current) setRep({ loading: false, data: null, error: e }); },
    );
  }, [remote, path, keepKey, reload, ctx.syncEpoch]);

  const data = rep.data;
  const proven = (data && data.proven) || { files: 0, bytes: 0, by_remote: {} };
  const probable = (data && data.probable) || { files: 0, bytes: 0, entry_ids: [], entry_ids_total: 0 };
  const onlyHere = (data && data.only_here) || { files: 0, bytes: 0 };
  const unjudged = (data && data.unjudged) || { files: 0, bytes: 0 };
  const unusable = (data && data.elsewhere_unusable) || { files: 0, bytes: 0 };
  const freshness = (data && data.freshness) || {};
  const staleNames = Object.keys(freshness).filter((n) => freshness[n].stale);
  const stale = staleNames.length > 0;
  const writable = data ? data.writable !== false : true;

  const toggle = (name) => {
    const now = new Set(enabled);
    if (now.has(name)) now.delete(name); else now.add(name);
    if (now.size === 0) return;
    const next = order.filter((n) => now.has(n));
    go({ keep: same(next, others) ? [] : next });
  };
  const move = (name, delta) => {
    const i = order.indexOf(name);
    const j = i + delta;
    if (j < 0 || j >= order.length) return;
    const next = [...order];
    [next[i], next[j]] = [next[j], next[i]];
    const kept = next.filter((n) => enabled.includes(n));
    go({ keep: same(kept, others) ? [] : kept });
  };

  async function syncFirst() {
    const r = await ctx.sync.start(staleNames);
    if (!r.ok && !r.busy) ctx.toast('warn', r.failed && r.failed.length ? t('free.syncFailed', { clouds: r.failed.join(', ') }) : (r.error || t('free.syncFailedAll')));
    return r;
  }

  async function runVerify() {
    const ids = probable.entry_ids || [];
    if (!ids.length) return;
    setVerify({ phase: 'running', bridged: 0, failed: 0, total: ids.length });
    let id;
    try {
      id = (await api.startBridge({ entryIds: ids })).job_id;
    } catch (e) {
      setVerify({ phase: 'failed', error: errorText(e) });
      return;
    }
    let failures = 0;
    for (;;) {
      if (!alive.current) return;
      try {
        const j = await api.job(id);
        const p = j.progress || {};
        failures = 0;
        if (j.status === 'done') {
          const res = j.result || {};
          const failed = Array.isArray(res.failed) ? res.failed.length : (p.failed || 0);
          setVerify({ phase: 'done', bridged: res.bridged !== undefined ? res.bridged : p.bridged || 0, failed, total: p.total || ids.length });
          setReload((n) => n + 1);
          ctx.catalogRefreshed();
          return;
        }
        if (j.status === 'failed') { setVerify({ phase: 'failed', error: j.error || t('bridge.failed') }); return; }
        setVerify({ phase: 'running', bridged: p.bridged || 0, failed: p.failed || 0, total: p.total || ids.length });
      } catch (e) {
        failures += 1;
        if (failures > 10) { setVerify({ phase: 'failed', error: errorText(e) }); return; }
      }
      await sleep(700);
    }
  }

  async function create() {
    setCreating(true);
    setCreateError(null);
    try {
      if (stale) {
        const r = await syncFirst();
        if (!r.ok) return;
        const fresh = await fetchReport();
        if (alive.current) setRep({ loading: false, data: fresh, error: null });
        if (Object.values(fresh.freshness || {}).some((f) => f.stale)) return;
      }
      const body = { remote };
      if (path) body.path_prefix = path;
      if (keepParam.length) body.keep_on = keepParam;
      const r = await api.createPlan({ kind: 'release', params: body });
      if (!r.plan_id) { setCreateError({ code: r.code || 'NOTHING_TO_DO' }); return; }
      ctx.refreshPlans();
      ctx.nav(plansHash(`/${r.plan_id}`));
    } catch (e) {
      setCreateError(e);
    } finally {
      if (alive.current) setCreating(false);
    }
  }

  const copyTarget = others.map((n) => remotes.find((r) => r.name === n)).find((r) => r && r.writable !== false);
  const copyFirst = () => ctx.nav(plansHash('/new/consolidate', copyTarget ? { src: remote, target: copyTarget.name } : { src: remote }));
  const finished = ctx.plans.list.filter((p) => p.kind === 'release' && p.status === 'done' && p.params && p.params.remote === remote);
  const known = names.includes(remote);
  const progress = verify && verify.phase === 'running';

  return html`<main class="view view-free" data-testid="view-free" data-remote=${remote} aria-label=${t('free.title')}>
    <${ViewHeader} title=${t('free.title')} hint=${t('free.hint')}>
      <button class="btn btn-small" data-testid="free-back" onClick=${() => ctx.nav('#/overview')}>${t('free.back')}</button><//>
    <div class="view-body">
      <section class="ov-panel free-controls" data-testid="free-controls">
        <label class="field field-inline"><span>${t('free.on')}</span>
          <select class="select" data-testid="free-remote" value=${remote} onChange=${(e) => ctx.nav(freeHash(e.target.value))}>
            ${!known && html`<option value=${remote}>${remote}</option>`}
            ${names.map((n) => html`<option key=${n} value=${n}>${n}</option>`)}</select></label>
        <label class="field field-inline free-path"><span>${t('free.folder')}</span>
          <input type="text" data-testid="free-path" value=${pathText} placeholder=${t('free.folderPlaceholder')} autocomplete="off" spellcheck="false"
            onInput=${(e) => setPathText(e.target.value)} /></label>
        <span class="free-ages">${Object.keys(freshness).map((n) => html`<span key=${n} class="free-age"><${CloudDot} remotes=${remotes} name=${n} size=${8} />
          ${n}<${AgeChip} iso=${freshness[n].synced_at} testid=${'free-age-' + n} /></span>`)}</span>
      </section>
      ${rep.error && html`<p class="view-error" role="alert" data-testid="free-error">${errorText(rep.error)}</p>`}
      ${rep.loading && !data && !rep.error && html`<p class="empty-hint">${t('common.loading')}</p>`}
      ${data && html`
        <section class="ov-panel" data-testid="free-summary">
          <${QuotaBar} quota=${data.quota} freed=${proven.bytes} remote=${remote} />
        </section>
        ${stale && html`<div class="banner-warn free-stale" role="alert" data-testid="free-stale">
          <${Icon} name="alert" size=${16} />
          <span><strong>${t('free.staleTitle')}</strong> ${t('free.staleText')}
            ${staleNames.map((n) => html`<span key=${n} class="free-stale-item" data-testid=${'free-stale-' + n}>${n}: ${freshness[n].synced_at ? ago(freshness[n].synced_at) : t('age.never')}</span>`)}</span>
          <button class="btn btn-small btn-primary" data-testid="free-sync-first" disabled=${ctx.sync.running} onClick=${syncFirst}>
            ${ctx.sync.running ? html`<span class="spinner" aria-hidden="true"></span>` : html`<${Icon} name="refresh" size=${14} />`} ${t('free.syncFirst')}</button>
        </div>`}
        <section class="ov-panel" aria-labelledby="free-buckets-h">
          <h2 id="free-buckets-h">${t('free.buckets')}</h2>
          <ul class="free-buckets" data-testid="free-buckets">
            <${Bucket} id="proven" icon="check" tone="ok" label=${t('free.proven')} help=${t('free.provenHelp')} data=${proven}>
              ${Object.keys(proven.by_remote || {}).length > 0 && html`<p class="free-by" data-testid="proven-by">${t('free.copiesOn')}
                ${Object.entries(proven.by_remote).map(([n, b]) => html`<span key=${n} class="free-by-item" data-testid=${'proven-by-' + n} data-bytes=${b}>
                  <${CloudDot} remotes=${remotes} name=${n} size=${8} />${n} ${fmtBytes(b)}</span>`)}</p>`}
            <//>
            <${Bucket} id="probable" icon="info" tone="info" label=${t('free.probable')} help=${t('free.probableHelp')} data=${probable}>
              ${probable.files > 0 && html`<div class="free-act">
                <button class="btn btn-small" data-testid="free-verify" disabled=${progress || !(probable.entry_ids || []).length} onClick=${runVerify}>
                  <${Icon} name="search" size=${14} /> ${t('free.verify', { bytes: fmtBytes(probable.bytes) })}</button>
                ${probable.entry_ids_total > (probable.entry_ids || []).length && html`<span class="muted" data-testid="verify-capped">
                  ${t('free.verifyCapped', { n: fmtNum((probable.entry_ids || []).length), total: fmtNum(probable.entry_ids_total) })}</span>`}
              </div>`}
              ${verify && html`<p class=${'free-verify' + (verify.phase === 'failed' ? ' error' : '')} data-testid="verify-progress" data-phase=${verify.phase} aria-live="polite">
                ${verify.phase === 'running' ? html`<span class="spinner" aria-hidden="true"></span>` : null}
                ${verify.phase === 'running' ? t('bridge.running', { bridged: fmtNum(verify.bridged), failed: fmtNum(verify.failed), total: fmtNum(verify.total) })
    : verify.phase === 'done' ? t('free.verifyDone', { bridged: fmtNum(verify.bridged), failed: fmtNum(verify.failed) }) : verify.error}</p>`}
            <//>
            <${Bucket} id="only_here" icon="alert" tone="bad" label=${t('free.onlyHere', { cloud: remote })} help=${t('free.onlyHereHelp')} data=${onlyHere}>
              ${onlyHere.files > 0 && html`<div class="free-act"><button class="btn btn-small" data-testid="free-copy-first" onClick=${copyFirst}>
                <${Icon} name="copy" size=${14} /> ${t('free.copyFirst')}</button></div>`}
            <//>
            <${Bucket} id="unjudged" icon="more" tone="muted" label=${t('free.unjudged')} help=${t('free.unjudgedHelp')} data=${unjudged} />
            ${unusable.files > 0 && html`<${Bucket} id="elsewhere_unusable" icon="lock" tone="muted" label=${t('free.unusable')} help=${t('free.unusableHelp')} data=${unusable} />`}
          </ul>
        </section>
        <section class="ov-panel" aria-labelledby="free-keep-h">
          <h2 id="free-keep-h">${t('free.keepOn')}</h2>
          <p class="muted">${t('free.keepHelp')}</p>
          ${others.length === 0 ? html`<p class="empty-hint">${t('free.noOthers')}</p>` : html`
          <ol class="free-keep" data-testid="free-keep">${order.map((n, i) => html`<li key=${n} data-testid=${'keep-' + n} data-enabled=${enabled.includes(n) ? 'true' : 'false'}>
            <label class="check"><input type="checkbox" data-testid=${'keep-check-' + n} checked=${enabled.includes(n)}
              disabled=${enabled.includes(n) && enabled.length === 1} onChange=${() => toggle(n)} />
              <${CloudDot} remotes=${remotes} name=${n} size=${9} />${n}</label>
            <span class="spacer"></span>
            <button class="icon-btn" data-testid=${'keep-up-' + n} aria-label=${t('free.moveUp', { cloud: n })} disabled=${i === 0}
              onClick=${() => move(n, -1)}><${Icon} name="chevron" size=${14} class="rot-up" /></button>
            <button class="icon-btn" data-testid=${'keep-down-' + n} aria-label=${t('free.moveDown', { cloud: n })} disabled=${i === order.length - 1}
              onClick=${() => move(n, 1)}><${Icon} name="chevron" size=${14} class="rot-down" /></button>
          </li>`)}</ol>`}
        </section>
        ${finished.length > 0 && html`<section class="banner banner-info free-pending" data-testid="free-pending">
          <${Icon} name="info" size=${16} />
          <span>${t('free.pending', { cloud: remote })}
            ${finished.slice(0, 3).map((p) => html`<a key=${p.id} href=${plansHash(`/${p.id}`)} data-testid=${'free-pending-' + p.id}
              onClick=${(e) => { e.preventDefault(); ctx.nav(plansHash(`/${p.id}`)); }}>#${p.id}</a> `)}</span></section>`}
        <footer class="free-foot">
          ${!writable && html`<p class="pl-warn" data-testid="free-readonly" role="note"><${Icon} name="lock" size=${14} /> ${t('free.readOnly', { cloud: remote })}
            <a href="#/clouds" onClick=${(e) => { e.preventDefault(); ctx.nav('#/clouds'); }}>${t('free.readOnlyLink')}</a></p>`}
          ${createError && html`<p class="pl-error" role="alert" data-testid="free-create-error">${createError.code === 'NOTHING_TO_DO' ? t('plans.NOTHING_TO_DO') : buildText(createError)}</p>`}
          <p class="muted free-quarantine-note">${t('free.quarantineNote')}</p>
          <button class="btn btn-primary" data-testid="free-create" disabled=${creating || !writable || proven.files === 0} onClick=${create}>
            ${creating ? html`<span class="spinner" aria-hidden="true"></span>` : html`<${Icon} name=${stale ? 'refresh' : 'plus'} size=${15} />`}
            ${stale ? t('free.createStale', { bytes: fmtBytes(proven.bytes) }) : t('free.create', { bytes: fmtBytes(proven.bytes) })}</button>
        </footer>`}
    </div>
  </main>`;
}
