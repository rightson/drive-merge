// Explore: where is it? A query bar with filter chips, a facet sidebar, and results grouped by content with one
// presence column per cloud (docs/EXPLORE.md section 4).
import {
  html, useContext, useEffect, useMemo, useRef, useState,
} from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import * as api from './api.js';
import { Icon, iconFor } from './icons.js';
import { AppCtx, pending } from './ctx.js';
import { CloudDot, PresenceCells, PresenceHeads } from './clouds.js';
import { Modal, ViewHeader } from './ui.js';
import {
  compareHash, exploreHash, hasToken, parseQuery, plansHash, removeChip, setToken, toggleToken,
} from './query.js';
import {
  errorText, fmtBytes, fmtDay, fmtNum, parentOf,
} from './util.js';
import { PICK_LIMIT, recall, remember } from './selection.js';
import { GatherDialog } from './gather.js';

const PAGE = 50;
const DEBOUNCE_MS = 250;
const SORTS = ['relevance', 'size', 'mtime', 'name', 'copies'];
const HELP = [
  ['word  "two words"', 'word'], ['in:gdrive', 'in'], ['on:gdrive', 'on'], ['not:gdrive', 'not'], ['only:gdrive,dropbox', 'only'],
  ['copies:1  copies:>2  copies:2..4', 'copies'], ['type:pdf', 'type'], ['cat:photo', 'cat'],
  ['size:>10MB  size:<1k  size:1M..1G', 'size'], ['modified:2023  modified:>2024-01-01  modified:2021..2022', 'modified'],
  ['path:School/Thesis', 'path'],
];

function QueryHelp({ onClose }) {
  return html`<${Modal} label=${t('q.help.title')} onClose=${onClose} testid="query-help" wide>
    <header class="modal-head"><h2>${t('q.help.title')}</h2><span class="spacer"></span>
      <button class="icon-btn" aria-label=${t('common.close')} data-testid="query-help-close" onClick=${onClose}><${Icon} name="x" size=${16} /></button></header>
    <div class="modal-body">
      <table class="table help-table">
        <thead><tr><th>${t('q.help.token')}</th><th>${t('q.help.meaning')}</th></tr></thead>
        <tbody>${HELP.map(([token, key]) => html`<tr key=${key}><td><code>${token}</code></td><td>${t(`q.help.${key}`)}</td></tr>`)}</tbody>
      </table>
      <p class="note">${t('q.help.note')}</p>
    </div>
  <//>`;
}

function chipTip(chip) {
  const e = chip.error;
  if (!e) return undefined;
  return t(`q.err.${e.code}`, { key: chip.key, value: chip.value });
}

function Facet({ title, children }) {
  return html`<section class="facet"><h3>${title}</h3>${children}</section>`;
}

function FacetBox({ label, count, checked, onChange, testid, dot }) {
  return html`<label class="facet-row">
    <input type="checkbox" checked=${checked} data-testid=${testid} onChange=${onChange} />
    ${dot}<span class="facet-label">${label}</span><span class="facet-count">${fmtNum(count)}</span></label>`;
}

/** The folder that holds every path (their longest common parent). */
function commonFolder(paths) {
  const split = paths.map((p) => parentOf(p).split('/').filter(Boolean));
  const out = [];
  for (let i = 0; i < Math.min(...split.map((s) => s.length)); i += 1) {
    if (!split.every((s) => s[i] === split[0][i])) break;
    out.push(split[0][i]);
  }
  return out.join('/');
}

function Copies({ r, remotes, onReveal, onCompare, onDedupe, onRules }) {
  const folders = [];
  for (const c of r.copies) {
    const where = { remote: c.remote, path: parentOf(c.path) };
    if (!folders.some((f) => f.remote === where.remote && f.path === where.path)) folders.push(where);
  }
  // Several copies on one cloud: a dedupe plan scoped to that cloud and the folder that holds them.
  const crowded = remotes.map((x) => x.name).filter((n) => r.copies.filter((c) => c.remote === n).length >= 2);
  return html`<div class="ex-copies" role="rowgroup" data-testid="ex-copies">
    ${(folders.length >= 2 || crowded.length > 0) && html`<div class="ex-copies-head">
      ${folders.length >= 2 && html`<button class="btn btn-small" data-testid="ex-compare-folders"
        onClick=${() => onCompare(folders[0], folders[1])}><${Icon} name="swap" size=${14} /> ${t('ex.compareFolders')}</button>`}
      ${crowded.map((n) => html`<button key=${n} class="btn btn-small" data-testid=${'ex-dedupe-' + n}
        onClick=${() => onDedupe({ remote: n, path: commonFolder(r.copies.filter((c) => c.remote === n).map((c) => c.path)) })}>
        <${Icon} name="copy" size=${14} /> ${t('ex.dedupeHere', { cloud: n })}</button>`)}</div>`}
    ${r.copies.map((c) => html`<div class=${'ex-copy' + (c.matched ? '' : ' unmatched')} role="row" key=${c.entry_id} data-testid="ex-copy" data-matched=${c.matched ? 'true' : 'false'}>
      <span class="ex-copy-path" role="cell"><${CloudDot} remotes=${remotes} name=${c.remote} size=${8} />
        <span class="mono" title=${`${c.remote}:${c.path}`}>${c.remote}:${c.path}</span>
        ${!c.matched && html`<span class="badge badge-nomatch" title=${t('ex.noMatchTip')}>${t('ex.noMatch')}</span>`}
        ${c.staged && html`<span class="ex-staged" data-testid="ex-staged" title=${t('sel.stagedTip')}>
          <${Icon} name="arrow" size=${12} /> ${t('sel.stagedTo', { where: `${c.staged.remote}:${c.staged.path}` })}</span>`}</span>
      <span class="ex-copy-mtime muted" role="cell">${fmtDay(c.mtime)}</span>
      <span class="ex-copy-actions"><button class="btn btn-tiny" data-testid="ex-reveal" onClick=${() => onReveal({ remote: c.remote, path: c.path })}>
        <${Icon} name="jump" size=${12} /> ${t('ex.reveal')}</button>
      <button class="btn btn-tiny" data-testid="ex-rules" title=${t('ex.rulesTip')} onClick=${() => onRules({ remote: c.remote, path: c.path })}>
        <${Icon} name="rules" size=${12} /> ${t('ex.rules')}</button></span>
    </div>`)}
  </div>`;
}

function ResultRow({ r, remotes, open, selected, onSelect, onToggle, onReveal, onCompare, onDedupe, onRules }) {
  const counts = {};
  r.copies.forEach((c) => { counts[c.remote] = (counts[c.remote] || 0) + 1; });
  const cat = iconFor({ is_dir: false, category: r.category });
  return html`<div class=${'ex-result' + (open ? ' open' : '') + (selected ? ' picked' : '')} role="rowgroup" data-testid="ex-result" data-key=${r.key}
    data-selected=${selected ? 'true' : 'false'}>
    <div class="ex-row" role="row">
      <span class="ex-pick" role="cell"><input type="checkbox" checked=${selected} data-testid=${'ex-select-' + r.key}
        aria-label=${t('sel.pick', { name: r.name })} onChange=${(e) => onSelect(r.key, e.target.checked)} /></span>
      <span class="ex-name" role="cell">
        <button class="twisty-btn" aria-expanded=${open} aria-label=${t(open ? 'ex.collapse' : 'ex.expand', { name: r.name })}
          data-testid="ex-expand" onClick=${onToggle}><${Icon} name="chevron" size=${12} class=${open ? 'turned' : ''} /></button>
        <${Icon} name=${cat} size=${16} class=${'cat-' + (r.category || 'other')} />
        <span class="ex-title" title=${r.name}>${r.name}</span>
        ${r.copies_count > 1 && html`<span class="ex-copies-count" title=${t('ex.copiesTip')}>${t('ex.copiesN', { n: r.copies_count })}</span>`}
        ${r.staged > 0 && html`<span class="badge badge-staged" data-testid="ex-staged-badge" title=${t('sel.stagedBadgeTip', { n: r.staged })}>${t('sel.staged')}</span>`}
      </span>
      <${PresenceCells} remotes=${remotes} counts=${counts} verified=${r.verified !== false} />
      <span class="ex-size num" role="cell">${fmtBytes(r.size)}</span>
      <span class="ex-mtime muted" role="cell">${fmtDay(r.mtime)}</span>
    </div>
    ${open && html`<${Copies} r=${r} remotes=${remotes} onReveal=${onReveal} onCompare=${onCompare} onDedupe=${onDedupe} onRules=${onRules} />`}
  </div>`;
}

/** The selection bar (docs/BULK.md section 3): what the selection holds, from the server, and what to do with it. */
function SelectionBar({ bar, remotes, count, onGather, onDedupe, onClear, onUseCurrent }) {
  const d = bar.data;
  const redundant = d ? d.redundant.copies : 0;
  const err = bar.error;
  return html`<div class=${'sel-bar' + (bar.loading ? ' busy' : '')} role="region" aria-label=${t('sel.bar')} data-testid="ex-selection-bar">
    <div class="sel-stats">
      <strong data-testid="ex-sel-count">${t('sel.count', { n: fmtNum(d ? d.total : count) })}</strong>
      ${d && html`<span class="sel-dot" aria-hidden="true">·</span>
        <span data-testid="ex-sel-copies">${t('sel.copies', { n: fmtNum(d.copies) })}</span><span class="sel-dot" aria-hidden="true">·</span>
        <span data-testid="ex-sel-bytes" class="num">${fmtBytes(d.bytes)}</span>
        <span class="sel-remotes">${d.by_remote.filter((r) => r.contents > 0).map((r) => html`<span key=${r.remote} class="sel-remote"
          data-testid=${'ex-sel-remote-' + r.remote} title=${t('sel.remoteTip', { cloud: r.remote, n: fmtNum(r.contents), copies: fmtNum(r.copies), bytes: fmtBytes(r.bytes) })}>
          <${CloudDot} remotes=${remotes} name=${r.remote} size=${8} />${r.remote} ${fmtNum(r.contents)}</span>`)}</span>
        ${redundant > 0 && html`<span class="muted" data-testid="ex-sel-redundant" title=${t('sel.redundantTip', { bytes: fmtBytes(d.redundant.bytes) })}>
          ${t('sel.redundant', { n: fmtNum(redundant) })}</span>`}
        ${d.staged && d.staged.copies > 0 && html`<span class="badge badge-staged" data-testid="ex-sel-staged">${t('sel.stagedCopies', { n: fmtNum(d.staged.copies) })}</span>`}
        ${d.gone > 0 && html`<span class="muted" data-testid="ex-sel-gone">${t('sel.gone', { n: fmtNum(d.gone) })}</span>`}`}
      ${bar.loading && !d && html`<span class="spinner" aria-hidden="true"></span>`}
    </div>
    ${err && html`<p class="sel-error" role="alert" data-testid="ex-sel-error"><${Icon} name="alert" size=${14} /> ${errorText(err)}
      ${err.code === 'SELECTION_CHANGED' && err.params && html` <button class="link" data-testid="ex-sel-update"
        onClick=${() => onUseCurrent(err.params.total)}>${t('sel.useCurrent', { n: fmtNum(err.params.total) })}</button>`}</p>`}
    <div class="sel-actions">
      <button class="btn btn-primary btn-small" data-testid="ex-sel-gather" disabled=${!!err || (!d && bar.loading)} onClick=${onGather}>
        <${Icon} name="folder" size=${14} /> ${t('sel.gather')}</button>
      <button class="btn btn-small" data-testid="ex-sel-dedupe" disabled=${!!err || !d || redundant === 0}
        title=${d && redundant === 0 ? t('sel.noDuplicates') : undefined} onClick=${onDedupe}>
        <${Icon} name="copy" size=${14} /> ${t('sel.dedupe')}</button>
      <button class="btn btn-small" data-testid="ex-sel-clear" onClick=${onClear}>${t('sel.clear')}</button>
    </div>
  </div>`;
}

export function Explore({ params }) {
  const ctx = useContext(AppCtx);
  const remotes = ctx.config.remotes || [];
  const names = useMemo(() => remotes.map((r) => r.name), [remotes]);
  const urlQ = params.get('q') || '';
  const urlSort = params.get('sort') || '';
  const [text, setText] = useState(urlQ);
  const [sort, setSort] = useState(urlSort);
  const [state, setState] = useState({ loading: true, error: '', data: null, results: [] });
  const [more, setMore] = useState(false);
  const [open, setOpen] = useState({});
  const [help, setHelp] = useState(false);
  const input = useRef(null);
  const written = useRef({ q: urlQ, sort: urlSort });
  const reqId = useRef(0);
  const first = useRef(true);

  const parsed = useMemo(() => parseQuery(text, names), [text, names]);
  const sendKey = JSON.stringify([parsed.params, sort]);

  // The selection belongs to the query (not to the sort): kept per query for the visit, so Back finds it again.
  const queryKey = JSON.stringify(parsed.params);
  const selRef = useRef(null);
  const selKey = useRef(queryKey);
  if (selRef.current === null) selRef.current = recall(queryKey, text.trim(), parsed.params);
  const [, setRev] = useState(0);
  const [cleared, setCleared] = useState(false);
  const [limitNote, setLimitNote] = useState(false);
  const [bar, setBar] = useState({ loading: false, error: null, data: null });
  const [gathering, setGathering] = useState(false);
  const barSeq = useRef(0);
  const sel = selRef.current;
  const draftId = ctx.draft ? ctx.draft.id : undefined;
  const draftVersion = ctx.draft ? ctx.draft.version : undefined;

  // Back / Forward / a link from another view: the address bar is the source of truth for the text.
  useEffect(() => {
    if (urlQ !== written.current.q) { written.current.q = urlQ; setText(urlQ); }
    if (urlSort !== written.current.sort) { written.current.sort = urlSort; setSort(urlSort); }
  }, [urlQ, urlSort]);

  useEffect(() => {
    if (pending.exploreFocus && input.current) { pending.exploreFocus = false; input.current.focus(); input.current.select(); }
  }, []);

  /** Change the selection through `fn` (which returns '' or the refusal); the bar asks the server for the new numbers. */
  const mutate = (fn) => {
    const next = selRef.current.clone();
    if (!next.text) next.text = text.trim();
    const refusal = fn(next);
    setLimitNote(!!refusal);
    if (refusal) return;
    selRef.current = next;
    remember(selKey.current, next);
    if (!next.empty) setCleared(false);
    setRev((x) => x + 1);
  };

  // A new query: the selection of the old one is kept for when it comes back, the new query starts with its own.
  useEffect(() => {
    if (selKey.current === queryKey) return;
    const old = selRef.current;
    remember(selKey.current, old);
    const next = recall(queryKey, text.trim(), parsed.params);
    selKey.current = queryKey;
    selRef.current = next;
    setLimitNote(false);
    setCleared(!old.empty && next.empty);
    if (!next.empty) setCleared(false);
    setRev((x) => x + 1);
  }, [queryKey]);
  useEffect(() => () => remember(selKey.current, selRef.current), []);

  // The bar's numbers come from the server (debounced after each change); an answer to an older selection is dropped.
  const signature = sel.signature;
  useEffect(() => {
    barSeq.current += 1;
    const mine = barSeq.current;
    if (selRef.current.empty) { setBar({ loading: false, error: null, data: null }); return undefined; }
    setBar((b) => ({ ...b, loading: true, error: null }));
    const timer = setTimeout(async () => {
      try {
        const data = await api.selection({ selection: selRef.current.body(), draft: draftId });
        if (mine === barSeq.current) setBar({ loading: false, error: null, data });
      } catch (e) {
        if (mine === barSeq.current) setBar({ loading: false, error: e, data: null });
      }
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [signature, draftId, draftVersion]);

  /** Edit the query from a control (facet, chip, reset, Enter): a new history entry, so Back undoes it. */
  const commit = (next) => {
    written.current.q = next;
    setText(next);
    ctx.nav(exploreHash(next, sort));
  };

  const load = async (offset) => {
    const id = (reqId.current += 1);
    if (offset) setMore(true); else setState((s) => ({ ...s, loading: true, error: '' }));
    try {
      const data = await api.explore({ ...parsed.params, sort: sort || undefined, limit: PAGE, offset, draft: draftId });
      if (id !== reqId.current) return;  // a newer request was sent meanwhile: this answer is stale
      setState((s) => ({ loading: false, error: '', data, results: offset ? [...s.results, ...data.results] : data.results }));
    } catch (e) {
      if (id !== reqId.current) return;
      setState((s) => ({ ...s, loading: false, error: e, results: offset ? s.results : [], data: offset ? s.data : null }));
    } finally {
      if (id === reqId.current) setMore(false);
    }
  };

  // Typing is debounced; the first load is not worth waiting for.
  useEffect(() => {
    const delay = first.current ? 0 : DEBOUNCE_MS;
    first.current = false;
    const timer = setTimeout(() => load(0), delay);
    return () => clearTimeout(timer);
  }, [sendKey]);

  // The draft changed (a gather, an undo, a drag elsewhere): the "staged" marks are read again for the rows on show.
  const draftSeen = useRef(draftVersion);
  useEffect(() => {
    if (draftSeen.current === draftVersion) return;
    draftSeen.current = draftVersion;
    if (draftId === undefined || state.results.length === 0) return;
    const id = reqId.current;
    const limit = Math.min(200, Math.ceil(state.results.length / PAGE) * PAGE);
    api.explore({ ...parsed.params, sort: sort || undefined, limit, offset: 0, draft: draftId }).then((fresh) => {
      if (id !== reqId.current) return;
      setState((s) => ({ ...s, data: fresh, results: [...fresh.results, ...s.results.slice(fresh.results.length)] }));
    }, () => { /* the marks stay as they were */ });
  }, [draftVersion]);

  // The address bar follows the text (replacing the entry: Back should leave the view, not undo each letter).
  useEffect(() => {
    const timer = setTimeout(() => {
      const hash = exploreHash(text, sort);
      if (hash === window.location.hash || (hash === '#/explore' && window.location.hash === '')) return;
      written.current = { q: text.trim(), sort };
      ctx.nav(hash, { replace: true });
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [text, sort]);

  const data = state.data;
  const total = data ? data.total : 0;
  const shown = state.results.length;
  const facets = (data && data.facets) || {};
  const p = parsed.params;

  const onKeyDown = (e) => {
    if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); commit(text.trim()); }
    if (e.key === 'Escape' && text) { e.preventDefault(); setText(''); }
  };
  const copiesState = p.copies_min === 1 && p.copies_max === 1 ? '1' : p.copies_min === 2 && p.copies_max === 2 ? '2'
    : p.copies_min === 3 && p.copies_max === undefined ? '3+' : '';
  const toggleCopies = (value, token) => commit(setToken(text, names, 'copies', copiesState === value ? null : token));
  const toggleSingle = (key, current, value) => commit(setToken(text, names, key, current === value ? null : value));
  const reveal = (loc) => ctx.revealInWorkspace(loc);
  const compare = (a, b) => ctx.nav(compareHash(a, b));
  const dedupe = ({ remote, path }) => ctx.nav(plansHash('/new/dedupe', { remote, path }));
  /** "Where would the rules put this?": the editor's Explain, on a draft that copies the active rules (one that already does is reused). */
  const rulesFor = async ({ remote, path }) => {
    try {
      const state = await api.rules();
      const a = state.active || {};
      const drafts = Array.isArray(state.drafts) ? state.drafts : (state.drafts && state.drafts.versions) || [];
      let id = (drafts.find((d) => d.sha256 === a.sha256) || {}).id;
      if (!id) {
        const text = a.text || (await api.rulesDefault()).text;
        const made = await api.createRuleVersion({ text, ...(a.version_id ? { base_id: a.version_id } : {}) });
        id = (made.version || made).id;
      }
      ctx.nav(`#/rules/edit/${id}?explain=${encodeURIComponent(`${remote}:${path}`)}`);
    } catch (e) {
      ctx.toast('error', errorText(e));
    }
  };

  const keysOnPage = state.results.map((r) => r.key);
  const pickedOnPage = keysOnPage.filter((k) => sel.has(k)).length;
  const allPicked = keysOnPage.length > 0 && pickedOnPage === keysOnPage.length;
  const pick = (key, on) => mutate((s) => s.toggle(key, on));
  const pickPage = (on) => mutate((s) => { if (on) return s.selectPage(keysOnPage); s.deselectPage(keysOnPage); return ''; });
  const pickAll = () => mutate((s) => { s.selectAllMatching(parsed.params, total, text.trim()); return ''; });
  const clearSelection = () => { setCleared(false); mutate((s) => { s.clear(); return ''; }); };
  const cleanUp = () => ctx.nav(plansHash('/new/dedupe', sel.toDedupeParams()));

  const order = (list) => [...list].sort((a, b) => names.indexOf(a.name) - names.indexOf(b.name));

  return html`<main class="view view-explore" data-testid="view-explore" aria-label=${t('nav.explore')}>
    <${ViewHeader} title=${t('nav.explore')} hint=${t('ex.hint')} />
    <div class="ex-bar">
      <div class="ex-input">
        <${Icon} name="search" size=${16} class="ex-input-icon" />
        <input type="text" ref=${input} class="ex-input-box" data-testid="explore-input" value=${text} autocomplete="off" spellcheck="false"
          placeholder=${t('ex.placeholder')} aria-label=${t('ex.search')} onInput=${(e) => setText(e.target.value)} onKeyDown=${onKeyDown} />
        <button class="icon-btn" data-testid="explore-clear" aria-label=${t('ex.clear')} title=${t('ex.clear')} disabled=${!text}
          onClick=${() => { commit(''); input.current && input.current.focus(); }}><${Icon} name="refresh" size=${15} /></button>
        <button class="icon-btn" data-testid="explore-help" aria-label=${t('q.help.open')} title=${t('q.help.open')} onClick=${() => setHelp(true)}>
          <b aria-hidden="true">?</b></button>
      </div>
      ${parsed.chips.length > 0 && html`<ul class="qchips" aria-label=${t('ex.chips')} data-testid="query-chips">
        ${parsed.chips.map((c) => html`<li key=${c.start} class=${'qchip' + (c.error ? ' bad' : '')} data-testid="query-chip" data-key=${c.key}
          data-error=${c.error ? c.error.code : undefined} title=${chipTip(c)}>
          ${c.error && html`<${Icon} name="alert" size=${12} />`}<span>${c.key}:${c.value}</span>
          <button aria-label=${t('ex.removeChip', { chip: `${c.key}:${c.value}` })} data-testid="query-chip-remove"
            onClick=${() => commit(removeChip(text, c))}><${Icon} name="x" size=${11} /></button></li>`)}
      </ul>`}
      ${parsed.errors.length > 0 && html`<p class="qerrors" role="status" data-testid="query-errors">
        ${t('q.err.summary', { n: parsed.errors.length })}</p>`}
    </div>
    <div class="ex-layout">
      <aside class="facets" aria-label=${t('ex.facets')} data-testid="facets">
        <${Facet} title=${t('ex.facet.clouds')}>
          ${order(facets.remotes || []).map((f) => html`<${FacetBox} key=${f.name} testid=${'facet-on-' + f.name}
            label=${t('ex.facet.on', { cloud: f.name })} count=${f.contents} checked=${hasToken(parsed.chips, 'on', f.name)}
            dot=${html`<${CloudDot} remotes=${remotes} name=${f.name} size=${8} />`}
            onChange=${() => commit(toggleToken(text, names, 'on', f.name))} />`)}
        <//>
        <${Facet} title=${t('ex.facet.copies')}>
          ${[['1', 'copies:1', t('ex.facet.one')], ['2', 'copies:2', t('ex.facet.two')], ['3+', 'copies:>=3', t('ex.facet.many')]]
    .filter(([k]) => facets.copies && facets.copies[k] !== undefined)
    .map(([k, token, label]) => html`<${FacetBox} key=${k} testid=${'facet-copies-' + k} label=${label} count=${facets.copies[k]}
            checked=${copiesState === k} onChange=${() => toggleCopies(k, token.slice(7))} />`)}
        <//>
        <${Facet} title=${t('ex.facet.category')}>
          ${(facets.categories || []).map((f) => html`<${FacetBox} key=${f.category} testid=${'facet-cat-' + f.category}
            label=${t(`q.cat.${f.category}`, null, f.category)} count=${f.contents} checked=${p.category === f.category}
            onChange=${() => toggleSingle('cat', p.category, f.category)} />`)}
        <//>
        <${Facet} title=${t('ex.facet.type')}>
          ${(facets.exts || []).map((f) => html`<${FacetBox} key=${f.ext} testid=${'facet-type-' + f.ext} label=${'.' + f.ext} count=${f.contents}
            checked=${p.ext === f.ext} onChange=${() => toggleSingle('type', p.ext, f.ext)} />`)}
        <//>
      </aside>
      <section class="ex-results" aria-label=${t('ex.results')}>
        <div class="ex-toolbar">
          <span class="ex-count" data-testid="ex-count" aria-live="polite">${state.loading && !data ? t('common.loading')
    : data ? t('ex.count', { shown: fmtNum(shown), total: fmtNum(total) }) : ''}</span>
          <span class="spacer"></span>
          <label for="ex-sort" class="muted">${t('ex.sort')}</label>
          <select id="ex-sort" class="select" data-testid="explore-sort" value=${sort || 'relevance'}
            onChange=${(e) => { const v = e.target.value === 'relevance' ? '' : e.target.value; written.current.sort = v; setSort(v); ctx.nav(exploreHash(text, v)); }}>
            ${SORTS.map((s) => html`<option value=${s}>${t(`ex.sort.${s}`)}</option>`)}
          </select>
        </div>
        ${cleared && html`<p class="sel-note" role="status" data-testid="ex-sel-cleared"><${Icon} name="info" size=${14} /> ${t('sel.cleared')}
          <button class="icon-btn" aria-label=${t('common.close')} onClick=${() => setCleared(false)}><${Icon} name="x" size=${12} /></button></p>`}
        ${limitNote && html`<p class="sel-note warn" role="alert" data-testid="ex-sel-limit"><${Icon} name="alert" size=${14} /> ${t('sel.limit', { n: fmtNum(PICK_LIMIT) })}
          ${sel.mode === 'keys' && total > 0 && html`<button class="link" data-testid="ex-limit-all-matching" onClick=${pickAll}>${t('sel.allMatching', { n: fmtNum(total) })}</button>`}</p>`}
        ${shown > 0 && allPicked && sel.mode === 'keys' && total > 0 && html`<p class="sel-note" data-testid="ex-select-all-line">
          ${t('sel.pageAll', { n: fmtNum(shown) })}
          <button class="link" data-testid="ex-select-all-matching" onClick=${pickAll}>${t('sel.allMatching', { n: fmtNum(total) })}</button></p>`}
        ${sel.mode === 'query' && !sel.empty && html`<p class="sel-note" data-testid="ex-select-all-line">
          ${t('sel.allPicked', { n: fmtNum(sel.count) })}${sel.except.size > 0 && ` ${t('sel.excluded', { n: fmtNum(sel.except.size) })}`}</p>`}
        ${data && data.truncated && html`<p class="ex-truncated" role="status" data-testid="ex-truncated"><${Icon} name="alert" size=${14} /> ${t('ex.truncated')}</p>`}
        ${state.error && html`<p class="view-error" role="alert" data-testid="ex-error">${errorText(state.error)}</p>`}
        <div class="ex-table" role="table" aria-label=${t('ex.results')} style=${{ '--presence': remotes.length }}>
          <div class="ex-head" role="row">
            <span role="columnheader" class="ex-pick"><input type="checkbox" data-testid="ex-select-page" checked=${allPicked}
              ref=${(el) => { if (el) el.indeterminate = pickedOnPage > 0 && !allPicked; }} disabled=${keysOnPage.length === 0}
              aria-label=${t('sel.pickPage')} title=${t('sel.pickPage')} onChange=${(e) => pickPage(e.target.checked)} /></span>
            <span role="columnheader">${t('ex.col.name')}</span>
            <${PresenceHeads} remotes=${remotes} />
            <span role="columnheader" class="num">${t('ex.col.size')}</span>
            <span role="columnheader">${t('ex.col.modified')}</span>
          </div>
          ${state.results.map((r) => html`<${ResultRow} key=${r.key} r=${r} remotes=${remotes} open=${!!open[r.key]} selected=${sel.has(r.key)} onSelect=${pick}
            onToggle=${() => setOpen((o) => ({ ...o, [r.key]: !o[r.key] }))} onReveal=${reveal} onCompare=${compare} onDedupe=${dedupe} onRules=${rulesFor} />`)}
        </div>
        ${!state.loading && !state.error && data && shown === 0 && html`<p class="empty-hint" data-testid="ex-none">${t('ex.none')}</p>`}
        ${data && shown < total && html`<div class="ex-more"><button class="btn" data-testid="ex-load-more" disabled=${more}
          onClick=${() => load(shown)}>${more ? t('common.loading') : t('ex.loadMore')}</button></div>`}
        ${!sel.empty && html`<${SelectionBar} bar=${bar} remotes=${remotes} count=${sel.count} onGather=${() => setGathering(true)}
          onDedupe=${cleanUp} onClear=${clearSelection} onUseCurrent=${(n) => mutate((s) => { s.useCurrent(n); return ''; })} />`}
      </section>
    </div>
    ${help && html`<${QueryHelp} onClose=${() => setHelp(false)} />`}
    ${gathering && html`<${GatherDialog} getSelection=${() => selRef.current.body()} count=${bar.data ? bar.data.total : sel.count}
      onClose=${() => setGathering(false)} />`}
  </main>`;
}
