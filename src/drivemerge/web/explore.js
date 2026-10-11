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

function Copies({ r, remotes, onReveal, onCompare, onDedupe }) {
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
        ${!c.matched && html`<span class="badge badge-nomatch" title=${t('ex.noMatchTip')}>${t('ex.noMatch')}</span>`}</span>
      <span class="ex-copy-mtime muted" role="cell">${fmtDay(c.mtime)}</span>
      <button class="btn btn-tiny" data-testid="ex-reveal" onClick=${() => onReveal({ remote: c.remote, path: c.path })}>
        <${Icon} name="jump" size=${12} /> ${t('ex.reveal')}</button>
    </div>`)}
  </div>`;
}

function ResultRow({ r, remotes, open, onToggle, onReveal, onCompare, onDedupe }) {
  const counts = {};
  r.copies.forEach((c) => { counts[c.remote] = (counts[c.remote] || 0) + 1; });
  const cat = iconFor({ is_dir: false, category: r.category });
  return html`<div class=${'ex-result' + (open ? ' open' : '')} role="rowgroup" data-testid="ex-result" data-key=${r.key}>
    <div class="ex-row" role="row">
      <span class="ex-name" role="cell">
        <button class="twisty-btn" aria-expanded=${open} aria-label=${t(open ? 'ex.collapse' : 'ex.expand', { name: r.name })}
          data-testid="ex-expand" onClick=${onToggle}><${Icon} name="chevron" size=${12} class=${open ? 'turned' : ''} /></button>
        <${Icon} name=${cat} size=${16} class=${'cat-' + (r.category || 'other')} />
        <span class="ex-title" title=${r.name}>${r.name}</span>
        ${r.copies_count > 1 && html`<span class="ex-copies-count" title=${t('ex.copiesTip')}>${t('ex.copiesN', { n: r.copies_count })}</span>`}
      </span>
      <${PresenceCells} remotes=${remotes} counts=${counts} verified=${r.verified !== false} />
      <span class="ex-size num" role="cell">${fmtBytes(r.size)}</span>
      <span class="ex-mtime muted" role="cell">${fmtDay(r.mtime)}</span>
    </div>
    ${open && html`<${Copies} r=${r} remotes=${remotes} onReveal=${onReveal} onCompare=${onCompare} onDedupe=${onDedupe} />`}
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

  // Back / Forward / a link from another view: the address bar is the source of truth for the text.
  useEffect(() => {
    if (urlQ !== written.current.q) { written.current.q = urlQ; setText(urlQ); }
    if (urlSort !== written.current.sort) { written.current.sort = urlSort; setSort(urlSort); }
  }, [urlQ, urlSort]);

  useEffect(() => {
    if (pending.exploreFocus && input.current) { pending.exploreFocus = false; input.current.focus(); input.current.select(); }
  }, []);

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
      const data = await api.explore({ ...parsed.params, sort: sort || undefined, limit: PAGE, offset });
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
        ${data && data.truncated && html`<p class="ex-truncated" role="status" data-testid="ex-truncated"><${Icon} name="alert" size=${14} /> ${t('ex.truncated')}</p>`}
        ${state.error && html`<p class="view-error" role="alert" data-testid="ex-error">${errorText(state.error)}</p>`}
        <div class="ex-table" role="table" aria-label=${t('ex.results')} style=${{ '--presence': remotes.length }}>
          <div class="ex-head" role="row">
            <span role="columnheader">${t('ex.col.name')}</span>
            <${PresenceHeads} remotes=${remotes} />
            <span role="columnheader" class="num">${t('ex.col.size')}</span>
            <span role="columnheader">${t('ex.col.modified')}</span>
          </div>
          ${state.results.map((r) => html`<${ResultRow} key=${r.key} r=${r} remotes=${remotes} open=${!!open[r.key]}
            onToggle=${() => setOpen((o) => ({ ...o, [r.key]: !o[r.key] }))} onReveal=${reveal} onCompare=${compare} onDedupe=${dedupe} />`)}
        </div>
        ${!state.loading && !state.error && data && shown === 0 && html`<p class="empty-hint" data-testid="ex-none">${t('ex.none')}</p>`}
        ${data && shown < total && html`<div class="ex-more"><button class="btn" data-testid="ex-load-more" disabled=${more}
          onClick=${() => load(shown)}>${more ? t('common.loading') : t('ex.loadMore')}</button></div>`}
      </section>
    </div>
    ${help && html`<${QueryHelp} onClose=${() => setHelp(false)} />`}
  </main>`;
}
