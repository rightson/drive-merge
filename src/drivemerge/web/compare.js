// Compare: how do these two folders differ? Two pickers, an aligned diff tree built on the page from the flat rows
// of GET /v1/compare, status filters, a detail panel and staging into the open draft (docs/EXPLORE.md section 5).
import {
  html, useContext, useEffect, useMemo, useRef, useState,
} from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import * as api from './api.js';
import { Icon, iconFor } from './icons.js';
import { AppCtx } from './ctx.js';
import { CloudDot, cloudColor } from './clouds.js';
import { ViewHeader } from './ui.js';
import { compareHash, parsePlace } from './query.js';
import {
  baseName, errorText, fmtBytes, fmtDateTime, fmtLoc, fmtNum, joinPath, parentOf,
} from './util.js';

const STATUSES = ['same', 'unverified', 'differs', 'moved', 'left_only', 'right_only'];
const FILTERS = ['differs', 'moved', 'left_only', 'right_only', 'unverified'];
const STATUS_GLYPH = { same: '=', unverified: '≈', differs: '≠', moved: '→', left_only: '◧', right_only: '◨' };
const ROW_CAP = 2000;
const EXPAND_BUDGET = 400;
const EMPTY = { remote: '', path: '' };

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
const byName = (a, b) => collator.compare(a.name, b.name);

// ---------------------------------------------------------------- model: the folders, built from the relative paths
function buildModel(rows, left, right) {
  const root = { id: 'd:', path: '', name: '', folders: new Map(), files: [], counts: {}, total: 0 };
  const items = new Map([[root.id, { kind: 'dir', node: root }]]);
  const dirsOn = { left: new Set(), right: new Set() };
  const within = (path, base) => (base && path.startsWith(base + '/') ? path.slice(base.length + 1) : path);
  const addDirs = (side, path) => {
    const parts = path.split('/');
    parts.pop();
    let acc = '';
    parts.forEach((seg) => { acc = acc ? `${acc}/${seg}` : seg; dirsOn[side].add(acc); });
  };
  rows.forEach((raw, i) => {
    const row = { ...raw, id: `f${i}` };
    items.set(row.id, { kind: 'file', row });
    if (row.left) addDirs('left', within(row.left.path, left.path));
    if (row.right) addDirs('right', within(row.right.path, right.path));
    const parts = row.rel.split('/');
    row.name = parts.pop();
    let node = root;
    const chain = [root];
    let acc = '';
    for (const seg of parts) {
      acc = acc ? `${acc}/${seg}` : seg;
      let f = node.folders.get(seg);
      if (!f) {
        f = { id: `d:${acc}`, path: acc, name: seg, folders: new Map(), files: [], counts: {}, total: 0, parent: node };
        node.folders.set(seg, f);
        items.set(f.id, { kind: 'dir', node: f });
      }
      node = f;
      chain.push(f);
    }
    node.files.push(row);
    row.parentId = node.id;
    chain.forEach((c) => { c.counts[row.status] = (c.counts[row.status] || 0) + 1; c.total += 1; });
  });
  return { root, items, dirsOn };
}

const visibleCount = (node, passes) => STATUSES.reduce((n, s) => n + (passes(s) ? node.counts[s] || 0 : 0), 0);

/** The rows to draw: expanded folders only, folders before files, at most `cap` of them. */
function flatten(root, expanded, passes, cap) {
  const out = [];
  let more = false;
  const walk = (node, depth) => {
    for (const f of [...node.folders.values()].sort(byName)) {
      if (!visibleCount(f, passes)) continue;
      if (out.length >= cap) { more = true; return; }
      out.push({ id: f.id, kind: 'dir', node: f, depth });
      if (expanded.has(f.path)) walk(f, depth + 1);
    }
    for (const row of [...node.files].sort(byName)) {
      if (!passes(row.status)) continue;
      if (out.length >= cap) { more = true; return; }
      out.push({ id: row.id, kind: 'file', row, depth });
    }
  };
  walk(root, 0);
  return { rows: out, more };
}

/** Breadth first, so the top of the tree opens before the depths do; a folder opens while the budget lasts. */
function autoExpand(root, passes) {
  const open = new Set();
  let budget = EXPAND_BUDGET;
  const queue = [root];
  while (queue.length) {
    const node = queue.shift();
    for (const f of [...node.folders.values()].sort(byName)) {
      const n = visibleCount(f, passes);
      if (!n || f.counts.same === f.total) continue;   // folders of only identical files stay closed, even when shown
      if (budget - n < 0 && open.size > 0) continue;
      budget -= n;
      open.add(f.path);
      queue.push(f);
    }
  }
  return open;
}

// ---------------------------------------------------------------- small parts
function StatusTag({ status }) {
  return html`<span class=${'cmp-tag tone-' + status} data-status=${status}><span class="cmp-glyph" aria-hidden="true">${STATUS_GLYPH[status]}</span>
    <span>${t(`cmp.status.${status}`)}</span></span>`;
}

function StackBar({ counts, total }) {
  const label = STATUSES.filter((s) => counts[s]).map((s) => `${t(`cmp.status.${s}`)} ${fmtNum(counts[s])}`).join(', ');
  return html`<span class="stackbar" role="img" aria-label=${label} title=${label}>
    ${STATUSES.filter((s) => counts[s]).map((s) => html`<span key=${s} class=${'seg tone-bg-' + s} style=${{ flexGrow: counts[s] }}></span>`)}</span>`;
}

function Elsewhere({ row, remotes, onReveal }) {
  if (row.status !== 'left_only' && row.status !== 'right_only') return null;
  const e = row.elsewhere;
  if (!e) return html`<p class="cmp-note" data-testid="cmp-elsewhere">${t('cmp.elsewhereUnknown')}</p>`;
  if (e.count === 0 || row.unique) {
    return html`<p class="cmp-note warn" data-testid="cmp-unique"><${Icon} name="alert" size=${14} /> ${t('cmp.unique')}</p>`;
  }
  return html`<div data-testid="cmp-elsewhere"><p class="cmp-note">${t('cmp.elsewhere', { n: fmtNum(e.count) })}</p>
    <ul class="cmp-examples">${(e.examples || []).map((x) => html`<li key=${fmtLoc(x)}><${CloudDot} remotes=${remotes} name=${x.remote} size=${8} />
      <span class="mono" title=${fmtLoc(x)}>${fmtLoc(x)}</span>
      <button class="btn btn-tiny" onClick=${() => onReveal(x)}>${t('ex.reveal')}</button></li>`)}</ul></div>`;
}

function Side({ title, file, remote, remotes }) {
  return html`<div class="cmp-side">
    <h4>${title}</h4>
    ${file ? html`<p class="cmp-path"><${CloudDot} remotes=${remotes} name=${file.remote || remote} size=${8} />
      <span class="mono" title=${`${file.remote || remote}:${file.path}`}>${file.remote || remote}:${file.path}</span></p>
      <p class="cmp-facts"><span>${fmtBytes(file.size)} <span class="muted">(${fmtNum(file.size)} B)</span></span>
        <span class="muted">${fmtDateTime(file.mtime)}</span></p>`
    : html`<p class="muted">${t('cmp.absent')}</p>`}
  </div>`;
}

function evidenceText(row) {
  const e = row.evidence || (row.status === 'moved' ? { kind: 'group' } : null);
  if (!e) return '';
  return t(`cmp.evidence.${e.kind}`, { hash: e.hash || '' });
}

function Detail({ item, data, remotes, moving, onReveal, onMove }) {
  if (!item) return html`<aside class="cmp-detail empty" data-testid="cmp-detail"><p class="empty-hint">${t('cmp.detailNone')}</p></aside>`;
  const { left, right } = data;
  if (item.kind === 'dir') {
    const n = item.node;
    const shown = STATUSES.filter((s) => n.counts[s]);
    const solo = shown.length === 1 && (shown[0] === 'left_only' || shown[0] === 'right_only') ? shown[0] : null;
    return html`<aside class="cmp-detail" data-testid="cmp-detail" data-kind="dir">
      <h3 class="cmp-detail-title"><${Icon} name="folder" size=${16} class="cat-folder" /> ${n.path || t('cmp.wholeFolder')}</h3>
      <p class="muted">${t('cmp.folderFiles', { n: fmtNum(n.total) })}</p>
      <ul class="cmp-counts">${shown.map((s) => html`<li key=${s}><${StatusTag} status=${s} /><strong>${fmtNum(n.counts[s])}</strong></li>`)}</ul>
      ${solo && html`<div class="cmp-actions"><button class="btn btn-small" data-testid="cmp-move" disabled=${moving} onClick=${() => onMove(item, solo)}>
        <${Icon} name="arrow" size=${14} /> ${t('cmp.moveOther')}</button></div>`}
    </aside>`;
  }
  const row = item.row;
  const solo = row.status === 'left_only' || row.status === 'right_only';
  return html`<aside class="cmp-detail" data-testid="cmp-detail" data-kind="file">
    <h3 class="cmp-detail-title"><${Icon} name=${iconFor({ is_dir: false, category: (row.left || row.right || {}).category })} size=${16}
      class=${'cat-' + ((row.left || row.right || {}).category || 'other')} /> <span>${row.name}</span></h3>
    <p><${StatusTag} status=${row.status} /></p>
    <${Side} title=${t('cmp.left')} file=${row.left} remote=${left.remote} remotes=${remotes} />
    <${Side} title=${t('cmp.right')} file=${row.right} remote=${right.remote} remotes=${remotes} />
    ${row.status === 'moved' && html`<p class="cmp-note" data-testid="cmp-moved-to">${t('cmp.movedTo', { from: row.rel, to: row.rel_right })}</p>`}
    ${evidenceText(row) && html`<p class="cmp-evidence" data-testid="cmp-evidence"><strong>${t('cmp.evidence')}</strong> ${evidenceText(row)}</p>`}
    <${Elsewhere} row=${row} remotes=${remotes} onReveal=${onReveal} />
    <div class="cmp-actions">
      ${row.left && html`<button class="btn btn-small" data-testid="cmp-reveal-left" onClick=${() => onReveal({ remote: row.left.remote || left.remote, path: row.left.path })}>
        <${Icon} name="jump" size=${13} /> ${t('cmp.revealLeft')}</button>`}
      ${row.right && html`<button class="btn btn-small" data-testid="cmp-reveal-right" onClick=${() => onReveal({ remote: row.right.remote || right.remote, path: row.right.path })}>
        <${Icon} name="jump" size=${13} /> ${t('cmp.revealRight')}</button>`}
      ${solo && html`<button class="btn btn-small" data-testid="cmp-move" disabled=${moving} onClick=${() => onMove(item, row.status)}>
        <${Icon} name="arrow" size=${14} /> ${t('cmp.moveOther')}</button>`}
    </div>
  </aside>`;
}

// ---------------------------------------------------------------- the picker: a cloud and a folder path with autocomplete
function PathPicker({ side, remotes, value, onChange, onSubmit, selectRef }) {
  const [focused, setFocused] = useState(false);
  const [options, setOptions] = useState([]);
  const [active, setActive] = useState(-1);
  const cache = useRef(new Map());
  const seq = useRef(0);
  const listId = `cmp-${side}-list`;

  useEffect(() => {
    if (!focused || !value.remote) { setOptions([]); return undefined; }
    const slash = value.path.lastIndexOf('/');
    const parent = slash < 0 ? '' : value.path.slice(0, slash);
    const prefix = value.path.slice(slash + 1).toLowerCase();
    const mine = (seq.current += 1);
    const key = `${value.remote}:${parent}`;
    const apply = (items) => {
      if (mine !== seq.current) return;
      setOptions(items.filter((i) => i.is_dir && !i.quarantine && i.name.toLowerCase().includes(prefix)).slice(0, 12));
      setActive(-1);
    };
    if (cache.current.has(key)) { apply(cache.current.get(key)); return undefined; }
    api.tree({ remote: value.remote, path: parent, limit: 500 }).then(
      (r) => { cache.current.set(key, r.items || []); apply(r.items || []); },
      () => apply([]),
    );
    return undefined;
  }, [focused, value.remote, value.path]);

  const choose = (o) => { onChange({ ...value, path: o.path }); setActive(-1); setOptions([]); };
  const onKeyDown = (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min(options.length - 1, a + 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(-1, a - 1)); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      if (active >= 0 && options[active]) choose(options[active]); else onSubmit();
    } else if (e.key === 'Escape') setOptions([]);
  };
  const label = side === 'left' ? t('cmp.left') : t('cmp.right');
  const shown = focused && options.length > 0;
  return html`<div class="picker" data-side=${side}>
    <label class="visually-hidden" for=${`cmp-${side}-remote`}>${t('cmp.cloudOf', { side: label })}</label>
    <select id=${`cmp-${side}-remote`} class="select" ref=${selectRef} data-testid=${`cmp-${side}-remote`} value=${value.remote}
      style=${value.remote ? { borderLeft: `4px solid ${cloudColor(remotes, value.remote)}` } : undefined}
      onChange=${(e) => onChange({ remote: e.target.value, path: '' })}>
      <option value="">${t('cmp.pickCloud')}</option>
      ${remotes.map((r) => html`<option value=${r.name}>${r.name}</option>`)}
    </select>
    <div class="picker-path">
      <label class="visually-hidden" for=${`cmp-${side}-path`}>${t('cmp.pathOf', { side: label })}</label>
      <input id=${`cmp-${side}-path`} type="text" role="combobox" aria-expanded=${shown} aria-controls=${listId} aria-autocomplete="list"
        aria-activedescendant=${active >= 0 ? `${listId}-${active}` : undefined} data-testid=${`cmp-${side}-path`}
        value=${value.path} disabled=${!value.remote} placeholder=${t('cmp.pathPlaceholder')} autocomplete="off" spellcheck="false"
        onInput=${(e) => onChange({ ...value, path: e.target.value.replace(/^\/+/, '') })} onKeyDown=${onKeyDown}
        onFocus=${() => setFocused(true)} onBlur=${() => setTimeout(() => setFocused(false), 120)} />
      ${shown && html`<ul class="search-results picker-list" role="listbox" id=${listId} data-testid=${`cmp-${side}-options`}>
        ${options.map((o, i) => html`<li role="option" id=${`${listId}-${i}`} key=${o.path} aria-selected=${i === active}>
          <button class=${'search-hit' + (i === active ? ' active' : '')} tabindex="-1" data-testid=${`cmp-${side}-option`}
            onMouseDown=${(e) => { e.preventDefault(); choose(o); }}>
            <${Icon} name="folder" size=${15} class="cat-folder" /><span class="hit-name">${o.name}</span>
            <span class="hit-path">${parentOf(o.path)}</span></button></li>`)}
      </ul>`}
    </div>
  </div>`;
}

// ---------------------------------------------------------------- the view
export function Compare({ params }) {
  const ctx = useContext(AppCtx);
  const remotes = ctx.config.remotes || [];
  const names = remotes.map((r) => r.name);
  const lText = params.get('l') || '';
  const rText = params.get('r') || '';
  const known = (p) => (p && names.includes(p.remote) ? p : null);
  const L = known(parsePlace(lText));
  const R = known(parsePlace(rText));
  const [dl, setDl] = useState(L || EMPTY);
  const [dr, setDr] = useState(R || EMPTY);
  const [nonce, setNonce] = useState(0);
  const [res, setRes] = useState({ loading: false, error: '', data: null });
  const [showSame, setShowSame] = useState(false);
  const [filters, setFilters] = useState([]);
  const [opened, setOpened] = useState({ model: null, map: {} });   // folders the person opened (true) or closed (false)
  const [sel, setSel] = useState('');
  const [focusId, setFocusId] = useState('');
  const [cap, setCap] = useState(ROW_CAP);
  const [moving, setMoving] = useState(false);
  const rightSelect = useRef(null);
  const tree = useRef(null);
  const wantFocus = useRef(null);
  const seq = useRef(0);

  useEffect(() => { setDl(L || EMPTY); setDr(R || EMPTY); }, [lText, rText]);
  useEffect(() => { if (L && !R && rightSelect.current) rightSelect.current.focus(); }, []);

  useEffect(() => {
    if (!L || !R) { setRes({ loading: false, error: '', data: null }); return; }
    const mine = (seq.current += 1);
    setRes({ loading: true, error: '', data: null });
    api.compare({ left: L, right: R }).then(
      (data) => { if (mine === seq.current) setRes({ loading: false, error: '', data }); },
      (e) => { if (mine === seq.current) setRes({ loading: false, error: e, data: null }); },
    );
  }, [lText, rText, nonce]);

  const data = res.data;
  const passes = useMemo(() => (s) => (s === 'same' ? showSame && filters.length === 0 : filters.length === 0 || filters.includes(s)),
    [showSame, filters.join(',')]);
  const model = useMemo(() => (data ? buildModel(data.rows, data.left, data.right) : null), [data]);

  // The tree opens by itself while a budget lasts; what the person opens or closes overrides that for this comparison.
  // Derived while rendering, so the first paint already has its folders open (a keyboard user must not meet them closed).
  const auto = useMemo(() => (model ? autoExpand(model.root, passes) : new Set()), [model, passes]);
  const expanded = useMemo(() => {
    const map = opened.model === model ? opened.map : {};
    const set = new Set([...auto].filter((path) => map[path] !== false));
    Object.keys(map).forEach((path) => { if (map[path]) set.add(path); });
    return set;
  }, [auto, opened, model]);
  useEffect(() => { setCap(ROW_CAP); setSel(''); }, [model]);

  const flat = useMemo(() => (model ? flatten(model.root, expanded, passes, cap) : { rows: [], more: false }), [model, expanded, passes, cap]);
  const selected = sel && model ? model.items.get(sel) || null : null;
  const swap = () => { const a = dl; setDl(dr); setDr(a); if (L && R) ctx.nav(compareHash(R, L)); };

  const run = () => {
    if (!dl.remote || !dr.remote) return;
    const hash = compareHash({ remote: dl.remote, path: dl.path.replace(/\/+$/, '') }, { remote: dr.remote, path: dr.path.replace(/\/+$/, '') });
    if (hash === window.location.hash) setNonce((n) => n + 1); else ctx.nav(hash);
  };

  // ------------------------------------------------------------ the tree
  const toggle = (path) => setOpened((cur) => ({
    model, map: { ...(cur.model === model ? cur.map : {}), [path]: !expanded.has(path) },
  }));
  const focusRow = (id) => { setFocusId(id); wantFocus.current = id; };
  useEffect(() => {
    const id = wantFocus.current;
    if (!id || !tree.current) return;
    const el = [...tree.current.querySelectorAll('[role="treeitem"]')].find((n) => n.dataset.id === id);
    if (el) { wantFocus.current = null; el.focus(); }   // not drawn yet (a folder opening): the next render tries again
  }, [focusId, flat]);

  const onKeyDown = (e) => {
    const rows = flat.rows;
    if (!rows.length) return;
    const i = Math.max(0, rows.findIndex((r) => r.id === (focusId || (rows[0] && rows[0].id))));
    const cur = rows[i];
    if (e.key === 'ArrowDown') { e.preventDefault(); focusRow(rows[Math.min(rows.length - 1, i + 1)].id); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); focusRow(rows[Math.max(0, i - 1)].id); }
    else if (e.key === 'Home') { e.preventDefault(); focusRow(rows[0].id); }
    else if (e.key === 'End') { e.preventDefault(); focusRow(rows[rows.length - 1].id); }
    else if (e.key === 'ArrowRight' && cur.kind === 'dir') {
      e.preventDefault();
      if (!expanded.has(cur.node.path)) toggle(cur.node.path); else if (rows[i + 1] && rows[i + 1].depth > cur.depth) focusRow(rows[i + 1].id);
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault();
      if (cur.kind === 'dir' && expanded.has(cur.node.path)) toggle(cur.node.path);
      else {
        const parent = cur.kind === 'dir' ? cur.node.parent : model.items.get(cur.row.parentId).node;
        if (parent && parent.path !== '' && rows.some((r) => r.id === parent.id)) focusRow(parent.id);
      }
    } else if (e.key === 'Enter') { e.preventDefault(); setSel(cur.id); }
    else if (e.key === ' ' && cur.kind === 'dir') { e.preventDefault(); toggle(cur.node.path); }
  };

  const clickRow = (r) => {
    setSel(r.id);
    setFocusId(r.id);
    if (r.kind === 'dir') toggle(r.node.path);
  };

  // ------------------------------------------------------------ staging
  const openWorkspace = () => ctx.nav('#/workspace');
  const staged = (what) => ctx.toast('success', what, { label: t('cmp.openWorkspace'), run: openWorkspace });

  async function merge(from, to) {
    if (!from.path || !to.path) { ctx.toast('warn', t('cmp.mergeRoot')); return; }
    const ok = await ctx.stageMove([{ remote: from.remote, path: from.path }], { remote: to.remote, path: parentOf(to.path) },
      { newName: baseName(to.path), keepBoth: false });
    if (ok) staged(t('cmp.staged', { from: fmtLoc(from), to: fmtLoc(to) }));
  }

  async function moveOther(item, status) {
    const side = status === 'left_only' ? 'left' : 'right';
    const from = data[side];
    const to = data[side === 'left' ? 'right' : 'left'];
    const rel = item.kind === 'dir' ? item.node.path : item.row.rel;
    const relParent = parentOf(rel);
    const dst = { remote: to.remote, path: relParent ? joinPath(to.path, relParent) : to.path };
    const src = { remote: from.remote, path: joinPath(from.path, rel) };
    setMoving(true);
    try {
      let exists = relParent === '' || model.dirsOn[side === 'left' ? 'right' : 'left'].has(relParent);
      if (!exists) {
        try {
          const listing = await api.tree({ remote: dst.remote, path: parentOf(dst.path), limit: 5000 });
          exists = (listing.items || []).some((i) => i.is_dir && i.path === dst.path);
        } catch (e) { exists = false; }
      }
      if (!exists) { ctx.toast('error', t('cmp.destMissing', { dst: fmtLoc(dst) })); return; }
      const ok = await ctx.stageMove([src], dst, { keepBoth: false });
      if (ok) staged(t('cmp.stagedMove', { from: fmtLoc(src), to: fmtLoc(dst) }));
    } finally {
      setMoving(false);
    }
  }

  // ------------------------------------------------------------ render
  const sum = (data && data.summary) || {};
  const files = (s) => (sum[s] ? sum[s].files : 0);
  const draftActive = ctx.draft && ctx.draft.actions.some((a) => a.state === 'active');
  const canMerge = !!data && !!data.left.path && !!data.right.path;
  const tabbable = flat.rows.some((r) => r.id === focusId) ? focusId : flat.rows[0] && flat.rows[0].id;
  const toggleFilter = (s) => setFilters((f) => (f.includes(s) ? f.filter((x) => x !== s) : [...f, s]));

  return html`<main class="view view-compare" data-testid="view-compare" aria-label=${t('nav.compare')}>
    <${ViewHeader} title=${t('nav.compare')} hint=${t('cmp.hint')} />
    <div class="cmp-pickers">
      <${PathPicker} side="left" remotes=${remotes} value=${dl} onChange=${setDl} onSubmit=${run} />
      <button class="icon-btn cmp-swap" data-testid="cmp-swap" aria-label=${t('cmp.swap')} title=${t('cmp.swap')} onClick=${swap}><${Icon} name="swap" size=${18} /></button>
      <${PathPicker} side="right" remotes=${remotes} value=${dr} onChange=${setDr} onSubmit=${run} selectRef=${rightSelect} />
      <button class="btn btn-primary" data-testid="cmp-run" disabled=${!dl.remote || !dr.remote} onClick=${run}>${t('cmp.run')}</button>
      ${data && html`<span class="cmp-similarity" data-testid="cmp-similarity" title=${t('cmp.similarityTip')}>
        ${t('cmp.similarity')} <strong>${Math.round((sum.similarity || 0) * 100)}%</strong></span>`}
    </div>
    ${draftActive && html`<p class="cmp-draftnote" role="note" data-testid="cmp-draft-note"><${Icon} name="info" size=${14} /> ${t('cmp.draftNote')}</p>`}
    ${!L && !R && !res.loading && html`<p class="empty-hint cmp-hint" data-testid="cmp-empty">${t('cmp.pickBoth')}</p>`}
    ${L && !R && html`<p class="empty-hint cmp-hint" data-testid="cmp-pick-right">${t('cmp.pickRight')}</p>`}
    ${res.loading && html`<p class="empty-hint cmp-hint"><span class="spinner"></span> ${t('cmp.comparing')}</p>`}
    ${res.error && html`<p class="view-error" role="alert" data-testid="cmp-error" data-code=${res.error.code || undefined}>${errorText(res.error)}</p>`}
    ${data && html`<div class="cmp-summary" data-testid="cmp-summary">
      <ul class="cmp-stats">${STATUSES.map((s) => html`<li key=${s} class=${'tone-' + s} data-testid=${'cmp-count-' + s} data-count=${files(s)}
        title=${t(`cmp.statusTip.${s}`)}><span class="cmp-glyph" aria-hidden="true">${STATUS_GLYPH[s]}</span>
        <span>${t(`cmp.status.${s}`)}</span> <strong>${fmtNum(files(s))}</strong></li>`)}
        ${sum.unique > 0 && html`<li class="tone-unique" data-testid="cmp-count-unique" data-count=${sum.unique} title=${t('cmp.uniqueTip')}>
          <${Icon} name="alert" size=${13} /> <span>${t('cmp.uniqueN', { n: fmtNum(sum.unique) })}</span></li>`}
      </ul>
      <div class="cmp-controls">
        <label class="check"><input type="checkbox" data-testid="cmp-show-same" checked=${showSame} onChange=${(e) => setShowSame(e.target.checked)} />
          <span>${t('cmp.showSame')}</span></label>
        <div class="filters" role="group" aria-label=${t('cmp.filter')}>
          ${FILTERS.map((s) => html`<button key=${s} class=${'filter-chip tone-' + s + (filters.includes(s) ? ' on' : '')} aria-pressed=${filters.includes(s)}
            data-testid=${'cmp-filter-' + s} onClick=${() => toggleFilter(s)}>${t(`cmp.status.${s}`)}</button>`)}
        </div>
        <span class="spacer"></span>
        <button class="btn btn-small" data-testid="cmp-merge-lr" disabled=${!canMerge} title=${canMerge ? t('cmp.mergeLRTip') : t('cmp.mergeRoot')}
          onClick=${() => merge(data.left, data.right)}><${Icon} name="merge" size=${14} /> ${t('cmp.mergeLR')}</button>
        <button class="btn btn-small" data-testid="cmp-merge-rl" disabled=${!canMerge} title=${canMerge ? t('cmp.mergeRLTip') : t('cmp.mergeRoot')}
          onClick=${() => merge(data.right, data.left)}><${Icon} name="merge" size=${14} /> ${t('cmp.mergeRL')}</button>
      </div>
    </div>
    <div class="cmp-body">
      <section class="cmp-tree-box" aria-label=${t('cmp.tree')}>
        <div class="cmp-tree-head" aria-hidden="true">
          <span>${fmtLoc(data.left)}</span><span class="swap-glyph">⇆</span><span>${fmtLoc(data.right)}</span></div>
        <div class="cmp-tree" role="tree" aria-label=${t('cmp.tree')} ref=${tree} data-testid="cmp-tree" onKeyDown=${onKeyDown}>
          ${flat.rows.length === 0 && html`<p class="empty-hint" data-testid="cmp-nothing">${data.rows.length === 0 ? t('cmp.bothEmpty') : t('cmp.nothingToShow')}</p>`}
          ${flat.rows.map((r) => {
    const indent = { paddingLeft: `${8 + r.depth * 18}px` };
    const isSel = sel === r.id;
    if (r.kind === 'dir') {
      const n = r.node;
      const open = expanded.has(n.path);
      const diff = n.total - (n.counts.same || 0);
      return html`<div key=${r.id} class=${'cmp-row dir' + (isSel ? ' selected' : '')} role="treeitem" aria-level=${r.depth + 1} aria-expanded=${open}
        aria-selected=${isSel} tabindex=${tabbable === r.id ? 0 : -1} data-id=${r.id} data-testid=${'cmp-dir-' + n.path} style=${indent}
        onClick=${() => clickRow(r)} onFocus=${() => { if (focusId !== r.id) setFocusId(r.id); }}>
        <span class=${'twisty' + (open ? ' open' : '')} aria-hidden="true"><${Icon} name="chevron" size=${12} /></span>
        <${Icon} name="folder" size=${16} class="cat-folder" />
        <span class="name" title=${n.path}>${n.name}/</span>
        <span class="cmp-folder-meta"><${StackBar} counts=${n.counts} total=${n.total} />
          <span class="cmp-diff-count" title=${t('cmp.diffCountTip')}>${fmtNum(diff)}</span></span></div>`;
    }
    const row = r.row;
    const file = row.left || row.right;
    return html`<div key=${r.id} class=${'cmp-row file st-' + row.status + (isSel ? ' selected' : '')} role="treeitem" aria-level=${r.depth + 1}
      aria-selected=${isSel} tabindex=${tabbable === r.id ? 0 : -1} data-id=${r.id} data-testid=${'cmp-file-' + row.rel} data-status=${row.status}
      style=${indent} onClick=${() => clickRow(r)} onFocus=${() => { if (focusId !== r.id) setFocusId(r.id); }}>
      <span class="twisty-space"></span>
      <${Icon} name=${iconFor({ is_dir: false, category: file && file.category })} size=${16} class=${'cat-' + ((file && file.category) || 'other')} />
      <span class="name" title=${row.rel}>${row.name}</span>
      <${StatusTag} status=${row.status} />
      ${row.status === 'moved' && html`<span class="cmp-moved-to" title=${row.rel_right}>→ ${row.rel_right}</span>`}
      ${(row.status === 'left_only' || row.status === 'right_only') && row.elsewhere && row.elsewhere.count > 0 && html`<span class="cmp-else"
        title=${t('cmp.elsewhere', { n: row.elsewhere.count })}>⧉ ${row.elsewhere.count}</span>`}
      ${row.unique && html`<span class="badge badge-dup" title=${t('cmp.unique')}>${t('cmp.uniqueBadge')}</span>`}
      <span class="meta"><span class="meta-size">${fmtBytes((row.left || row.right || {}).size)}</span></span></div>`;
  })}
          ${flat.more && html`<div class="row row-msg" role="presentation"><button class="link" data-testid="cmp-show-more"
            onClick=${() => setCap((c) => c + ROW_CAP)}>${t('cmp.showMore')}</button></div>`}
        </div>
      </section>
      <${Detail} item=${selected} data=${data} remotes=${remotes} moving=${moving}
        onReveal=${(loc) => ctx.revealInWorkspace(loc)} onMove=${moveOther} />
    </div>`}
  </main>`;
}
