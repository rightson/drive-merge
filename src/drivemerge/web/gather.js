// "Gather into a folder" (docs/BULK.md section 4): the dialog that moves one copy of every selected content into one
// folder, as ONE step of the open draft. It first asks the server for a dry run (nothing is staged) and shows what
// would move, what is already there and what is skipped, each skip worded here from its code and params.
import { html, useContext, useEffect, useRef, useState } from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import * as api from './api.js';
import { Icon } from './icons.js';
import { AppCtx } from './ctx.js';
import { Modal } from './ui.js';
import { PathPicker } from './compare.js';
import {
  errorText, fmtBytes, fmtLoc, fmtNum, joinPath, wordParams,
} from './util.js';

const DEBOUNCE_MS = 300;
const SAMPLE = 20;
const SKIP_LIST = 8;
/** Show the skip codes in this order: the harmless one first, the ones the person can fix after it. */
const SKIP_ORDER = ['ALREADY_THERE', 'NOT_ON_TARGET', 'NAME_TAKEN', 'NAME_CLASH', 'READ_ONLY', 'NATIVE_DOC', 'AMBIGUOUS_PATH', 'GONE'];

/** The sentence of one skipped content, from its code and params (a code this page does not know shows the code). */
function skipText(item) {
  return t(`gather.skip.${item.code}`, wordParams('gather.skip', item.params), item.code);
}

function Skipped({ skipped }) {
  const by = skipped.by_code || {};
  const codes = [...SKIP_ORDER.filter((c) => by[c]), ...Object.keys(by).filter((c) => !SKIP_ORDER.includes(c) && by[c])];
  const total = codes.reduce((n, c) => n + by[c], 0);
  const [open, setOpen] = useState(false);
  return html`<details class="gather-skipped" data-testid="gather-skipped" open=${open} onToggle=${(e) => setOpen(e.target.open)}>
    <summary>${t('gather.skipped', { n: fmtNum(total) })}</summary>
    ${codes.map((code) => {
    const items = (skipped.items || []).filter((i) => i.code === code);
    return html`<section key=${code} class=${'gather-skip' + (code === 'ALREADY_THERE' ? ' benign' : '')} data-testid=${'gather-skip-' + code}
        data-count=${by[code]}>
      <h4>${t(`gather.skipHead.${code}`, { n: fmtNum(by[code]) }, code)}</h4>
      <ul>${items.slice(0, SKIP_LIST).map((i) => html`<li key=${i.key}><span class="mono gather-name" title=${i.name || i.key}>${i.name || i.key}</span>
        <span class="muted">${skipText(i)}</span></li>`)}
        ${by[code] > Math.min(items.length, SKIP_LIST) && html`<li class="muted">${t('gather.andMore', { n: fmtNum(by[code] - Math.min(items.length, SKIP_LIST)) })}</li>`}
      </ul></section>`;
  })}
  </details>`;
}

/** What the dry run found, in words: the moves (with a sample), what crosses clouds, and the skipped contents. */
function Check({ check, dst }) {
  const files = check.files || 0;
  const cross = check.cross_cloud || { files: 0, bytes: 0 };
  const picked = check.picked || [];
  const where = fmtLoc(dst);
  return html`<div class="gather-check" data-testid="gather-check" data-files=${files}>
    ${files > 0 ? html`<p class="gather-headline"><strong>${t('gather.moves', { n: fmtNum(files), bytes: fmtBytes(check.bytes), dst: where })}</strong></p>
      ${cross.files > 0 && html`<p class="gather-cross-note" data-testid="gather-cross-count"><${Icon} name="alert" size=${14} />
        ${t('gather.crossCount', { n: fmtNum(cross.files), bytes: fmtBytes(cross.bytes) })}</p>`}
      <ul class="gather-sample" data-testid="gather-sample">
        ${picked.slice(0, SAMPLE).map((p) => html`<li key=${p.key} data-cross=${p.cross_cloud ? 'true' : 'false'}>
          <span class="mono" title=${fmtLoc(p.from)}>${fmtLoc(p.from)}</span><span class="gather-arrow" aria-hidden="true">→</span>
          <span class="mono" title=${fmtLoc(p.to)}>${fmtLoc(p.to)}</span>
          ${p.cross_cloud && html`<span class="badge badge-cross">${t('gather.crossBadge')}</span>`}</li>`)}
        ${files > Math.min(picked.length, SAMPLE) && html`<li class="muted">${t('gather.andMore', { n: fmtNum(files - Math.min(picked.length, SAMPLE)) })}</li>`}
      </ul>`
    : html`<p class="gather-none" data-testid="gather-none">${t('gather.nothing')}</p>`}
    ${check.skipped && Object.keys(check.skipped.by_code || {}).length > 0 && html`<${Skipped} skipped=${check.skipped} />`}
    ${files > 0 && html`<p class="note">${t('gather.others')}</p>`}
  </div>`;
}

export function GatherDialog({ getSelection, count, onClose }) {
  const ctx = useContext(AppCtx);
  const remotes = ctx.config.remotes || [];
  const writable = remotes.filter((r) => r.writable !== false);
  const [dst, setDst] = useState({ remote: writable.length === 1 ? writable[0].name : '', path: '' });
  const [cross, setCross] = useState(false);
  const [run, setRun] = useState({ loading: false, error: null, check: null, key: '' });
  const [attempt, setAttempt] = useState(0);
  const [staging, setStaging] = useState(false);
  const [stageError, setStageError] = useState(null);
  const seq = useRef(0);
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);

  const path = dst.path.replace(/^\/+|\/+$/g, '');
  const request = () => ({ selection: getSelection(), dst: { remote: dst.remote, path }, cross_cloud: cross });
  const inputKey = `${dst.remote}|${path}|${cross}`;

  // Every change of the destination or the option asks for a dry run (debounced); an answer to an older ask is dropped.
  // "Try again" (after an error) bumps `attempt`, which asks once more with the same inputs.
  useEffect(() => {
    seq.current += 1;
    const mine = seq.current;
    setStageError(null);
    if (!dst.remote) { setRun({ loading: false, error: null, check: null, key: '' }); return undefined; }
    setRun((r) => ({ ...r, loading: true, error: null }));
    const timer = setTimeout(async () => {
      try {
        const check = await api.gather(ctx.draft.id, { ...request(), dry_run: true });
        if (mine === seq.current && mounted.current) setRun({ loading: false, error: null, check, key: inputKey });
      } catch (e) {
        if (mine === seq.current && mounted.current) setRun({ loading: false, error: e, check: null, key: inputKey });
      }
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [inputKey, ctx.draft.id, attempt]);

  const fresh = run.check && run.key === inputKey && !run.loading;
  const ready = fresh && run.check.files > 0 && !staging;

  async function stage() {
    setStaging(true);
    setStageError(null);
    const out = await ctx.stageGather(request());
    if (!mounted.current) return;
    setStaging(false);
    if (out && out.result) onClose(); else setStageError((out && out.error) || null);
  }

  async function newFolder() {
    if (!dst.remote) return;
    const name = await ctx.prompt({ title: t('mkdir.title'), message: t('mkdir.where', { dst: fmtLoc({ remote: dst.remote, path }) }),
      label: t('mkdir.label'), value: t('mkdir.default'), okLabel: t('mkdir.ok') });
    if (!name) return;
    const ok = await ctx.stageMkdir({ remote: dst.remote, path }, name);
    if (ok && mounted.current) setDst({ remote: dst.remote, path: joinPath(path, name) });
  }

  const error = stageError || run.error;
  return html`<${Modal} label=${t('gather.title', { n: fmtNum(count) })} onClose=${onClose} testid="gather-dialog" wide>
    <header class="modal-head"><h2>${t('gather.title', { n: fmtNum(count) })}</h2><span class="spacer"></span>
      <button class="icon-btn" aria-label=${t('common.close')} data-testid="gather-close" onClick=${onClose}><${Icon} name="x" size=${16} /></button></header>
    <div class="modal-body gather-body">
      <p class="gather-hint">${t('gather.hint')}</p>
      <div class="gather-into"><span class="gather-label">${t('gather.into')}</span>
        <${PathPicker} side="right" prefix="gather-dst" label=${t('gather.destination')} remotes=${writable} value=${dst}
          onChange=${setDst} onSubmit=${() => {}} draft=${ctx.draft.id} version=${ctx.draft.version} />
        <button class="btn btn-small" data-testid="gather-new-folder" disabled=${!dst.remote} onClick=${newFolder}>
          <${Icon} name="plus" size=${14} /> ${t('gather.newFolder')}</button></div>
      <label class="check gather-cross" data-testid="gather-cross-label">
        <input type="checkbox" data-testid="gather-cross" checked=${cross} onChange=${(e) => setCross(e.target.checked)} />
        ${dst.remote ? t('gather.cross', { cloud: dst.remote }) : t('gather.crossAny')}</label>
      ${cross && html`<p class="pl-warn" role="note" data-testid="gather-cross-warning"><${Icon} name="alert" size=${14} /> ${t('gather.crossWarning')}</p>`}
      <div class=${'gather-result' + (run.loading ? ' pl-stale' : '')} aria-live="polite">
        ${!dst.remote && html`<p class="empty-hint">${t('gather.pickFirst')}</p>`}
        ${run.loading && html`<p class="muted"><span class="spinner" aria-hidden="true"></span> ${t('gather.checking')}</p>`}
        ${error && html`<div class="gather-error"><p class="view-error" role="alert" data-testid="gather-error" data-code=${error.code || ''}>${errorText(error)}</p>
          <button class="btn btn-small" data-testid="gather-retry" disabled=${run.loading || staging} onClick=${() => setAttempt((n) => n + 1)}>${t('gather.tryAgain')}</button></div>`}
        ${run.check && !run.error && html`<${Check} check=${run.check} dst=${{ remote: dst.remote, path }} />`}
      </div>
    </div>
    <footer class="modal-foot">
      <button class="btn" data-testid="gather-cancel" onClick=${onClose}>${t('common.cancel')}</button>
      <button class="btn btn-primary" data-testid="gather-stage" disabled=${!ready} onClick=${stage}>
        ${staging ? t('common.loading') : t('gather.stage')}</button>
    </footer>
  <//>`;
}
