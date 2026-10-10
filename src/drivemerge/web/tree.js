// One tree pane: root selector, breadcrumb, search, lazy tree with selection and drag and drop.
import {
  html, useContext, useEffect, useMemo, useReducer, useRef, useState,
} from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import * as api from './api.js';
import { Icon, iconFor } from './icons.js';
import { AppCtx } from './ctx.js';
import {
  ancestorsOf, baseName, errorText, fmtBytes, fmtLoc, fmtNum, isUnder, joinPath, keyOf, parentOf, splitKey,
} from './util.js';

const PAGE = 500;
const SPRING_MS = 700;

// The item being dragged, shared by both panes (dataTransfer is unreadable until drop).
let currentDrag = null;
// What Ctrl+X took: a keyboard route to the same move as dragging.
let cutBuffer = null;

const initialState = (root) => ({
  root, view: 'after', ghosts: false,
  roots: null, expanded: {}, cache: {},
  sel: [], anchor: null, focus: null, renaming: null,
  dropKey: null, scrollTo: null, flash: null,
});

function reducer(s, a) {
  switch (a.type) {
    case 'set': return { ...s, ...a.patch };
    case 'cache': return { ...s, cache: { ...s.cache, [a.key]: a.data } };
    case 'clearCache': return { ...s, cache: {} };
    case 'expand': return { ...s, expanded: { ...s.expanded, ...Object.fromEntries(a.keys.map((k) => [k, true])) } };
    case 'collapse': {
      const expanded = { ...s.expanded };
      delete expanded[a.key];
      return { ...s, expanded };
    }
    default: return s;
  }
}

function buildRows(s) {
  const rows = [];
  if (!s.roots) return rows;
  const roots = s.root === 'all' ? s.roots : s.roots.filter((r) => r.remote === s.root);
  const walk = (node, level) => {
    const key = keyOf(node.remote, node.path);
    rows.push({ kind: 'node', key, node, level });
    if (!node.is_dir || !s.expanded[key]) return;
    const c = s.cache[key];
    if (!c) { rows.push({ kind: 'loading', key: `${key}#loading`, level: level + 1 }); return; }
    if (c.error) { rows.push({ kind: 'error', key: `${key}#error`, level: level + 1, text: c.error }); return; }
    for (const child of c.items) walk(child, level + 1);
    const ghosts = s.ghosts && s.view === 'after' ? c.ghosts || [] : [];
    for (const g of ghosts) rows.push({ kind: 'ghost', key: `${key}#ghost:${g.name}`, level: level + 1, ghost: { ...g, remote: node.remote } });
    if (c.items.length < c.total) rows.push({ kind: 'more', key: `${key}#more`, level: level + 1, dirKey: key });
    else if (!c.items.length && !ghosts.length) rows.push({ kind: 'empty', key: `${key}#empty`, level: level + 1 });
  };
  roots.forEach((r) => walk(r, 0));
  return rows;
}

function dragImage(e, count) {
  if (count < 2 || !e.dataTransfer.setDragImage) return null;
  const el = document.createElement('div');
  el.className = 'drag-image';
  el.textContent = t('pane.dragCount', { n: count });
  document.body.appendChild(el);
  e.dataTransfer.setDragImage(el, 12, 12);
  return el;
}

export function Pane({ side, draft, tick, handle }) {
  const ctx = useContext(AppCtx);
  const remotes = ctx.config.remotes || [];
  const [s, dispatch] = useReducer(reducer, remotes.length === 1 ? remotes[0].name : 'all', initialState);
  const st = useRef(s);
  st.current = s;
  const draftId = draft ? draft.id : null;
  const seqs = useRef({});
  const epoch = useRef(0);
  const sig = useRef('');
  const box = useRef(null);
  const spring = useRef({ key: null, timer: null });
  const [search, setSearch] = useState({ q: '', results: null, busy: false });
  const searchSeq = useRef(0);

  const rows = useMemo(() => buildRows(s), [s.roots, s.expanded, s.cache, s.ghosts, s.view, s.root]);
  const nodeRows = useMemo(() => rows.filter((r) => r.kind === 'node'), [rows]);
  const byKey = useMemo(() => new Map(nodeRows.map((r) => [r.key, r])), [nodeRows]);
  const editable = s.view === 'after' && !!draft && draft.status === 'open';

  // ---------------------------------------------------------------- loading
  const queryParams = () => ({
    draft: draftId, view: st.current.view,
    ghosts: st.current.ghosts && st.current.view === 'after' ? 1 : 0,
  });

  async function loadDir(remote, path, { append = false } = {}) {
    const key = keyOf(remote, path);
    const mine = (seqs.current[key] = (seqs.current[key] || 0) + 1);
    const ep = epoch.current;
    const prev = st.current.cache[key];
    try {
      const r = await api.tree({
        remote, path, ...queryParams(), limit: PAGE, offset: append && prev ? prev.items.length : 0,
      });
      if (ep !== epoch.current || seqs.current[key] !== mine) return null;
      const items = append && prev ? prev.items.concat(r.items) : r.items;
      const data = { items, ghosts: r.ghosts || [], total: r.total === undefined ? items.length : r.total };
      dispatch({ type: 'cache', key, data });
      return data;
    } catch (e) {
      if (ep === epoch.current && seqs.current[key] === mine) {
        dispatch({ type: 'cache', key, data: { items: [], ghosts: [], total: 0, error: errorText(e) } });
      }
      return null;
    }
  }

  /** Fetch a listing now and put it in the cache, whatever else is in flight (used when a revealed path is missing). */
  async function reload(remote, path) {
    const ep = epoch.current;
    try {
      const r = await api.tree({ remote, path, ...queryParams(), limit: PAGE, offset: 0 });
      if (ep !== epoch.current) return null;
      const data = { items: r.items, ghosts: r.ghosts || [], total: r.total === undefined ? r.items.length : r.total };
      dispatch({ type: 'cache', key: keyOf(remote, path), data });
      return data;
    } catch (e) {
      return null;
    }
  }

  async function loadRoots() {
    const ep = epoch.current;
    try {
      const roots = await api.roots(st.current.view === 'before' ? null : draftId);  // "before" is the catalog as it is
      if (ep === epoch.current) dispatch({ type: 'set', patch: { roots } });
      return roots;
    } catch (e) {
      ctx.toast('error', errorText(e));
      return st.current.roots || [];
    }
  }

  // Structural changes (another draft, view or ghosts) drop the cache; a plain tick refetches in place so
  // the tree does not flicker and expansion survives.
  useEffect(() => {
    const next = `${draftId}|${s.view}|${s.ghosts ? 1 : 0}`;
    if (sig.current !== next) {
      sig.current = next;
      epoch.current += 1;
      seqs.current = {};
      dispatch({ type: 'clearCache' });
    }
    let alive = true;
    (async () => {
      const roots = await loadRoots();
      if (!alive) return;
      const names = new Set(roots.map((r) => r.remote));
      const keys = Object.keys(st.current.expanded).filter((k) => names.has(splitKey(k).remote));
      await Promise.all(keys.map((k) => { const { remote, path } = splitKey(k); return loadDir(remote, path); }));
    })();
    return () => { alive = false; };
  }, [draftId, s.view, s.ghosts, tick]);

  // A single-cloud root is shown opened.
  useEffect(() => {
    if (s.root === 'all') return;
    const key = keyOf(s.root, '');
    if (!st.current.expanded[key]) {
      dispatch({ type: 'expand', keys: [key] });
      loadDir(s.root, '');
    }
  }, [s.root]);

  // Drop selection and focus that no longer exist after a refresh.
  useEffect(() => {
    if (!s.roots) return;
    const alive = s.sel.filter((k) => byKey.has(k));
    if (alive.length !== s.sel.length) dispatch({ type: 'set', patch: { sel: alive, focus: byKey.has(s.focus) ? s.focus : alive[0] || null } });
  }, [byKey]);

  useEffect(() => {
    if (!s.scrollTo || !box.current) return;
    const el = [...box.current.querySelectorAll('[role="treeitem"]')].find((n) => n.dataset.key === s.scrollTo);
    if (el) el.scrollIntoView({ block: 'center' });
    dispatch({ type: 'set', patch: { scrollTo: null } });
    if (s.flash) setTimeout(() => dispatch({ type: 'set', patch: { flash: null } }), 1600);
  }, [s.scrollTo, rows]);

  useEffect(() => {
    const clear = () => {
      clearTimeout(spring.current.timer);
      spring.current.key = null;
      currentDrag = null;
      dispatch({ type: 'set', patch: { dropKey: null } });
    };
    window.addEventListener('dragend', clear);
    window.addEventListener('drop', clear);
    return () => { window.removeEventListener('dragend', clear); window.removeEventListener('drop', clear); };
  }, []);

  // ---------------------------------------------------------------- expand / reveal
  function toggle(key) {
    if (st.current.expanded[key]) { dispatch({ type: 'collapse', key }); return; }
    dispatch({ type: 'expand', keys: [key] });
    if (!st.current.cache[key]) { const { remote, path } = splitKey(key); loadDir(remote, path); }
  }

  async function listing(remote, path) {
    const cached = st.current.cache[keyOf(remote, path)];
    if (cached && !cached.error) return cached;
    return loadDir(remote, path);
  }

  /** Walk down to `loc` loading each level; follow a moved folder's ghost when the path no longer exists. */
  async function resolve(loc) {
    let cur = { remote: loc.remote, path: loc.path };
    for (let hop = 0; hop < 8; hop += 1) {
      const segs = cur.path ? cur.path.split('/') : [];
      let parent = '';
      let miss = null;
      for (let i = 0; i < segs.length; i += 1) {
        let l = await listing(cur.remote, parent);
        let hit = l && l.items.find((x) => x.name === segs[i]);
        if (!hit) {  // the cached listing may predate a change that was just staged
          l = await reload(cur.remote, parent);
          hit = l && l.items.find((x) => x.name === segs[i]);
        }
        if (!hit) { miss = { parent, rest: segs.slice(i + 1), name: segs[i] }; break; }
        parent = hit.path;
      }
      if (!miss) return { loc: cur, exact: true };
      if (st.current.view === 'after' && draftId) {
        let ghost = null;
        try {
          const r = await api.tree({ remote: cur.remote, path: miss.parent, draft: draftId, view: 'after', ghosts: 1, limit: PAGE });
          ghost = (r.ghosts || []).find((g) => g.name === miss.name);
        } catch (e) { /* fall through to nearest parent */ }
        if (ghost) {
          cur = { remote: ghost.moved_to.remote, path: [ghost.moved_to.path, ...miss.rest].filter(Boolean).join('/') };
          continue;
        }
      }
      return { loc: { remote: cur.remote, path: miss.parent }, exact: false };
    }
    return { loc: cur, exact: false };
  }

  async function reveal(loc, { expandTarget = false } = {}) {
    if (st.current.root !== 'all' && st.current.root !== loc.remote) dispatch({ type: 'set', patch: { root: loc.remote } });
    if (!st.current.roots) await loadRoots();
    const { loc: found, exact } = await resolve(loc);
    const keys = [keyOf(found.remote, '')];
    const chain = ancestorsOf(found.path);
    chain.slice(0, -1).forEach((p) => keys.push(keyOf(found.remote, p)));
    const targetKey = keyOf(found.remote, found.path);
    if (expandTarget || !found.path) keys.push(targetKey);
    dispatch({ type: 'expand', keys });
    if ((expandTarget || !found.path) && !st.current.cache[targetKey]) await loadDir(found.remote, found.path);
    dispatch({ type: 'set', patch: { sel: [targetKey], anchor: targetKey, focus: targetKey, scrollTo: targetKey, flash: targetKey } });
    if (!exact) ctx.toast('info', t('pane.revealNearest', { loc: fmtLoc(found) }));
  }

  // The App calls these through its registry (reveal from search results, suggestions and the preview).
  handle.current = { reveal, side };

  // ---------------------------------------------------------------- selection
  function selectKeys(sel, anchor, focus) {
    dispatch({ type: 'set', patch: { sel, anchor: anchor === undefined ? st.current.anchor : anchor, focus } });
  }

  function onRowClick(e, row) {
    if (row.kind !== 'node') return;
    const cur = st.current;
    if (e.ctrlKey || e.metaKey) {
      const sel = cur.sel.includes(row.key) ? cur.sel.filter((k) => k !== row.key) : [...cur.sel, row.key];
      selectKeys(sel, row.key, row.key);
    } else if (e.shiftKey && cur.anchor && byKey.has(cur.anchor)) {
      const keys = nodeRows.map((r) => r.key);
      const a = keys.indexOf(cur.anchor);
      const b = keys.indexOf(row.key);
      selectKeys(keys.slice(Math.min(a, b), Math.max(a, b) + 1), cur.anchor, row.key);
    } else {
      selectKeys([row.key], row.key, row.key);
    }
  }

  function moveFocus(delta, extend) {
    const keys = nodeRows.map((r) => r.key);
    if (!keys.length) return;
    const i = Math.max(0, keys.indexOf(st.current.focus));
    const next = keys[Math.min(keys.length - 1, Math.max(0, i + delta))];
    focusRow(next);
    if (extend && st.current.anchor) {
      const a = keys.indexOf(st.current.anchor);
      const b = keys.indexOf(next);
      selectKeys(keys.slice(Math.min(a, b), Math.max(a, b) + 1), st.current.anchor, next);
    } else {
      selectKeys([next], next, next);
    }
  }

  function focusRow(key) {
    dispatch({ type: 'set', patch: { focus: key } });
    requestAnimationFrame(() => {
      const el = box.current && [...box.current.querySelectorAll('[role="treeitem"]')].find((n) => n.dataset.key === key);
      if (el) el.focus();
    });
  }

  function cutSelection() {
    const items = st.current.sel.map((k) => byKey.get(k)).filter((r) => r && r.node.path)
      .map((r) => ({ remote: r.node.remote, path: r.node.path, is_dir: r.node.is_dir }));
    if (!items.length || !editable) return;
    cutBuffer = { items };
    ctx.toast('info', t('pane.cut', { n: items.length }));
  }

  async function pasteHere() {
    if (!cutBuffer) { ctx.toast('info', t('pane.nothingCut')); return; }
    const dst = destinationFolder();
    const target = dst && { ...dst, is_dir: true };
    if (!target || !canDrop(cutBuffer, target)) { ctx.toast('error', t('pane.cannotPaste')); return; }
    const { items } = cutBuffer;
    cutBuffer = null;
    const ok = await ctx.stageMove(items.map(({ remote, path }) => ({ remote, path })), dst);
    if (ok) showLanded(target, items.map((i) => baseName(i.path)));
  }

  function onKeyDown(e) {
    if (e.target.tagName === 'INPUT') return;
    const cur = st.current;
    const row = byKey.get(cur.focus);
    if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && (e.key === 'x' || e.key === 'v')) {
      e.preventDefault();
      if (e.key === 'x') cutSelection(); else pasteHere();
      return;
    }
    switch (e.key) {
      case 'ArrowDown': e.preventDefault(); moveFocus(1, e.shiftKey); break;
      case 'ArrowUp': e.preventDefault(); moveFocus(-1, e.shiftKey); break;
      case 'Home': e.preventDefault(); if (nodeRows.length) { focusRow(nodeRows[0].key); selectKeys([nodeRows[0].key], nodeRows[0].key, nodeRows[0].key); } break;
      case 'End': {
        e.preventDefault();
        const last = nodeRows[nodeRows.length - 1];
        if (last) { focusRow(last.key); selectKeys([last.key], last.key, last.key); }
        break;
      }
      case 'ArrowRight':
        if (!row || !row.node.is_dir) break;
        e.preventDefault();
        if (!cur.expanded[row.key]) toggle(row.key); else moveFocus(1, false);
        break;
      case 'ArrowLeft':
        if (!row) break;
        e.preventDefault();
        if (row.node.is_dir && cur.expanded[row.key]) toggle(row.key);
        else if (row.node.path) {
          const parent = keyOf(row.node.remote, parentOf(row.node.path));
          if (byKey.has(parent)) { focusRow(parent); selectKeys([parent], parent, parent); }
        }
        break;
      case 'Enter':
        if (row && row.node.is_dir) { e.preventDefault(); toggle(row.key); }
        break;
      case ' ':
        if (row) { e.preventDefault(); onRowClick({ ctrlKey: true }, row); }
        break;
      case 'F2':
        if (row && row.node.path && editable) { e.preventDefault(); dispatch({ type: 'set', patch: { renaming: row.key } }); }
        break;
      default:
    }
  }

  // ---------------------------------------------------------------- drag and drop
  function canDrop(drag, node) {
    if (!drag || !editable || !node.is_dir) return false;
    for (const it of drag.items) {
      if (it.is_dir && it.remote === node.remote && isUnder(node.path, it.path)) return false;
    }
    return !drag.items.every((it) => it.remote === node.remote && parentOf(it.path) === node.path);
  }

  function onDragStart(e, row) {
    if (!editable || !row.node.path) { e.preventDefault(); return; }
    let keys = st.current.sel;
    if (!keys.includes(row.key)) { keys = [row.key]; selectKeys(keys, row.key, row.key); }
    const items = keys.map((k) => byKey.get(k)).filter((r) => r && r.node.path)
      .map((r) => ({ remote: r.node.remote, path: r.node.path, is_dir: r.node.is_dir }));
    currentDrag = { items, side };
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', items.map(fmtLoc).join('\n'));
    const img = dragImage(e, items.length);
    if (img) setTimeout(() => img.remove(), 0);
  }

  function setDrop(key) {
    if (st.current.dropKey !== key) dispatch({ type: 'set', patch: { dropKey: key } });
    const sp = spring.current;
    if (sp.key !== key) {
      clearTimeout(sp.timer);
      sp.key = key;
      const row = key && byKey.get(key);
      if (row && !st.current.expanded[key]) sp.timer = setTimeout(() => toggle(key), SPRING_MS);
    }
  }

  function onRowDragOver(e, row) {
    if (!currentDrag) return;
    if (row.kind === 'node' && canDrop(currentDrag, row.node)) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      setDrop(row.key);
    } else {
      setDrop(null);
    }
  }

  async function onDrop(e, node) {
    const drag = currentDrag;
    if (!drag || !canDrop(drag, node)) return;
    e.preventDefault();
    clearTimeout(spring.current.timer);
    spring.current.key = null;
    currentDrag = null;
    dispatch({ type: 'set', patch: { dropKey: null } });
    const ok = await ctx.stageMove(drag.items.map(({ remote, path }) => ({ remote, path })), { remote: node.remote, path: node.path });
    if (ok) showLanded(node, drag.items.map((i) => baseName(i.path)));
  }

  /** After a drop: open the folder it landed in and flash the first arrival, so the move is seen to happen. */
  async function showLanded(node, names) {
    const key = keyOf(node.remote, node.path);
    dispatch({ type: 'expand', keys: [key] });
    const data = await reload(node.remote, node.path);
    const first = data && names.find((n) => data.items.some((i) => i.name === n));
    if (first) {
      const landed = keyOf(node.remote, joinPath(node.path, first));
      dispatch({ type: 'set', patch: { flash: landed, scrollTo: landed } });
    }
  }

  function onTreeDragOver(e) {
    const el = box.current;
    if (!currentDrag || !el) return;
    const r = el.getBoundingClientRect();
    if (e.clientY < r.top + 36) el.scrollTop -= 14;
    else if (e.clientY > r.bottom - 36) el.scrollTop += 14;
  }

  function onTreeDragLeave(e) {
    if (!box.current.contains(e.relatedTarget)) setDrop(null);
  }

  // ---------------------------------------------------------------- toolbar actions
  function destinationFolder() {
    const cur = st.current;
    const row = byKey.get(cur.focus) || byKey.get(cur.sel[0]);
    if (row) {
      return row.node.is_dir ? { remote: row.node.remote, path: row.node.path }
        : { remote: row.node.remote, path: parentOf(row.node.path) };
    }
    return cur.root !== 'all' ? { remote: cur.root, path: '' } : null;
  }

  async function newFolder() {
    const dst = destinationFolder();
    if (!dst) { ctx.toast('info', t('pane.pickFolderFirst')); return; }
    const name = await ctx.prompt({
      title: t('mkdir.title'), message: t('mkdir.where', { dst: fmtLoc(dst) }),
      label: t('mkdir.label'), value: t('mkdir.default'), okLabel: t('mkdir.ok'),
    });
    if (!name) return;
    const ok = await ctx.stageMkdir(dst, name);
    if (ok) {
      dispatch({ type: 'expand', keys: [keyOf(dst.remote, dst.path)] });
      await reveal({ remote: dst.remote, path: joinPath(dst.path, name) }, { expandTarget: true });
    }
  }

  function commitRename(row, value) {
    dispatch({ type: 'set', patch: { renaming: null } });
    const name = value.trim();
    if (!name || name === row.node.name) return;
    ctx.rename({ remote: row.node.remote, path: row.node.path, is_dir: row.node.is_dir }, name);
  }

  async function runSearch(q) {
    setSearch({ q, results: q.trim() ? search.results : null, busy: !!q.trim() });
    if (!q.trim()) return;
    const mine = (searchSeq.current += 1);
    try {
      const results = await api.search(q.trim(), { remote: st.current.root === 'all' ? undefined : st.current.root });
      if (mine === searchSeq.current) setSearch({ q, results, busy: false });
    } catch (e) {
      if (mine === searchSeq.current) setSearch({ q, results: [], busy: false });
      ctx.toast('error', errorText(e));
    }
  }

  const searchTimer = useRef(null);
  const searchBox = useRef(null);
  useEffect(() => {
    if (!search.results) return undefined;
    const close = (e) => { if (searchBox.current && !searchBox.current.contains(e.target)) setSearch((x) => ({ ...x, results: null })); };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [search.results]);
  function onSearchInput(e) {
    const q = e.target.value;
    setSearch((x) => ({ ...x, q }));
    clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(() => runSearch(q), 220);
  }

  function pickResult(r) {
    setSearch({ q: '', results: null, busy: false });
    reveal({ remote: r.remote, path: r.path });
  }

  // ---------------------------------------------------------------- render
  const focusNode = byKey.get(s.focus);
  const crumbs = [];
  if (focusNode) {
    const n = focusNode.node;
    crumbs.push({ label: n.remote, remote: n.remote, path: '' });
    ancestorsOf(n.path).forEach((p) => crumbs.push({ label: baseName(p), remote: n.remote, path: p }));
  }

  const sideLabel = side === 'left' ? t('pane.left') : t('pane.right');
  return html`<section class=${'pane' + (s.view === 'before' ? ' pane-before' : '')} data-testid=${'pane-' + side}
    aria-label=${sideLabel}>
    <header class="pane-head">
      <div class="pane-row">
        <label class="visually-hidden" for=${'root-' + side}>${t('pane.root')}</label>
        <select id=${'root-' + side} class="select" data-testid=${'root-select-' + side} value=${s.root}
          onChange=${(e) => dispatch({ type: 'set', patch: { root: e.target.value } })}>
          <option value="all">${t('pane.allClouds')}</option>
          ${remotes.map((r) => html`<option value=${r.name}>${r.name}${r.provider ? ` · ${r.provider}` : ''}</option>`)}
        </select>
        <div class="segmented" role="group" aria-label=${t('pane.view')}>
          <button class=${s.view === 'after' ? 'on' : ''} aria-pressed=${s.view === 'after'} data-testid=${'view-after-' + side}
            onClick=${() => dispatch({ type: 'set', patch: { view: 'after' } })}>${t('pane.after')}</button>
          <button class=${s.view === 'before' ? 'on' : ''} aria-pressed=${s.view === 'before'} data-testid=${'view-before-' + side}
            onClick=${() => dispatch({ type: 'set', patch: { view: 'before' } })}>${t('pane.before')}</button>
        </div>
        <label class=${'check' + (s.view === 'before' ? ' disabled' : '')} title=${t('pane.ghostsHint')}>
          <input type="checkbox" data-testid=${'ghost-toggle-' + side} checked=${s.ghosts} disabled=${s.view === 'before'}
            onChange=${(e) => dispatch({ type: 'set', patch: { ghosts: e.target.checked } })} />
          <span>${t('pane.ghosts')}</span>
        </label>
      </div>
      <div class="pane-row">
        <div class="search" ref=${searchBox}>
          <${Icon} name="search" size=${15} class="search-icon" />
          <input type="search" class="search-input" data-testid=${'search-' + side} value=${search.q}
            placeholder=${t('pane.searchPlaceholder')} aria-label=${t('pane.search')} autocomplete="off" spellcheck="false"
            onInput=${onSearchInput}
            onKeyDown=${(e) => { if (e.key === 'Escape') setSearch({ q: '', results: null, busy: false }); }} />
          ${search.results && html`<ul class="search-results" role="listbox" data-testid=${'search-results-' + side}>
            ${search.results.length === 0 && html`<li class="search-empty">${t('pane.noResults')}</li>`}
            ${search.results.map((r) => html`<li role="option" key=${r.id || fmtLoc(r)}>
              <button class="search-hit" data-testid=${'search-result-' + fmtLoc(r)} onClick=${() => pickResult(r)}>
                <${Icon} name=${iconFor(r)} size=${15} class=${'cat-' + (r.is_dir ? 'folder' : r.category || 'other')} />
                <span class="hit-name">${r.name}</span>
                <span class="hit-path">${r.remote}:${parentOf(r.path)}</span>
              </button></li>`)}
          </ul>`}
        </div>
        <button class="btn btn-small" data-testid=${'mkdir-' + side} disabled=${!editable} onClick=${newFolder}
          title=${t('pane.newFolderHint')}><${Icon} name="folderPlus" size=${15} /> ${t('pane.newFolder')}</button>
      </div>
      <nav class="crumbs" aria-label=${t('pane.breadcrumb')} data-testid=${'crumbs-' + side}>
        ${crumbs.length === 0 && html`<span class="crumb-muted">${s.root === 'all' ? t('pane.allClouds') : s.root}</span>`}
        ${crumbs.map((c, i) => {
          const key = keyOf(c.remote, c.path);
          return html`<span class="crumb-wrap" key=${key}>
            ${i > 0 && html`<${Icon} name="chevron" size=${11} class="crumb-sep" />`}
            <button class=${'crumb' + (s.dropKey === 'crumb:' + key ? ' drop' : '')} data-testid=${'crumb-' + key}
              onClick=${() => reveal({ remote: c.remote, path: c.path })}
              onDragOver=${(e) => {
                if (currentDrag && canDrop(currentDrag, { remote: c.remote, path: c.path, is_dir: true })) {
                  e.preventDefault(); e.dataTransfer.dropEffect = 'move'; dispatch({ type: 'set', patch: { dropKey: 'crumb:' + key } });
                }
              }}
              onDragLeave=${() => dispatch({ type: 'set', patch: { dropKey: null } })}
              onDrop=${(e) => onDrop(e, { remote: c.remote, path: c.path, is_dir: true })}>${c.label}</button>
          </span>`;
        })}
      </nav>
    </header>
    ${s.view === 'before' && html`<div class="pane-note" data-testid=${'before-note-' + side}>${t('pane.beforeNote')}</div>`}
    <div class="tree" role="tree" aria-label=${sideLabel} aria-multiselectable="true" ref=${box}
      data-testid=${'tree-' + side} onKeyDown=${onKeyDown} onDragOver=${onTreeDragOver} onDragLeave=${onTreeDragLeave}>
      ${!s.roots && html`<div class="tree-msg">${t('common.loading')}</div>`}
      ${s.roots && rows.length === 0 && html`<div class="tree-msg">${t('pane.noClouds')}</div>`}
      ${rows.map((row) => html`<${Row} key=${row.key} row=${row} s=${s} tabbable=${row.key === (s.focus || (nodeRows[0] && nodeRows[0].key))}
        editable=${editable} onClick=${onRowClick} onFocus=${(key) => { if (st.current.focus !== key) dispatch({ type: 'set', patch: { focus: key } }); }}
        onToggle=${toggle} dispatch=${dispatch}
        onDragStart=${onDragStart} onDragOver=${onRowDragOver} onDrop=${onDrop}
        onRename=${commitRename} onMore=${(dirKey) => { const { remote, path } = splitKey(dirKey); loadDir(remote, path, { append: true }); }}
        onGhost=${(g) => ctx.reveal(side === 'left' ? 'right' : 'left', g.moved_to)} />`)}
    </div>
  </section>`;
}

function Badges({ node }) {
  const origin = (node.origins || []).map(fmtLoc).join(', ');
  return html`<span class="badges">
    ${node.virtual && html`<span class="badge badge-new" data-badge="virtual">${t('badge.new')}</span>`}
    ${node.moved_here && html`<span class="badge badge-moved" data-badge="moved" title=${t('tip.movedFrom', { from: origin })}>
      <${Icon} name="arrow" size=${10} />${t('badge.moved')}</span>`}
    ${node.merged && html`<span class="badge badge-merged" data-badge="merged" title=${t('tip.merged')}>${t('badge.merged')}</span>`}
    ${node.conflict && html`<span class="badge badge-conflict" data-badge="conflict" title=${t('tip.conflict')}>${t('badge.conflict')}</span>`}
    ${node.dedupe && html`<span class="badge badge-dedupe" data-badge="dedupe" title=${t('tip.dedupe')}>${t('badge.dedupe')}</span>`}
    ${!node.dedupe && node.dup_files > 0 && html`<span class="badge badge-dup" data-badge="dup"
      title=${t('tip.dup', { n: node.dup_files })}>${node.is_dir ? t('badge.dupN', { n: fmtNum(node.dup_files) }) : t('badge.dup')}</span>`}
  </span>`;
}

function Row({ row, s, tabbable, editable, onClick, onFocus, onToggle, dispatch, onDragStart, onDragOver, onDrop, onRename, onMore, onGhost }) {
  const indent = { paddingLeft: `${8 + row.level * 16}px` };
  if (row.kind === 'loading') return html`<div class="row row-msg" role="presentation" style=${indent}>${t('common.loading')}</div>`;
  if (row.kind === 'empty') return html`<div class="row row-msg" role="presentation" style=${indent}>${t('pane.empty')}</div>`;
  if (row.kind === 'error') return html`<div class="row row-msg row-error" role="presentation" style=${indent}>${row.text}</div>`;
  if (row.kind === 'more') {
    return html`<div class="row row-msg" role="presentation" style=${indent}><button class="link" data-testid="load-more"
      onClick=${() => onMore(row.dirKey)}>${t('pane.loadMore')}</button></div>`;
  }
  if (row.kind === 'ghost') {
    const g = row.ghost;
    return html`<div class="row row-ghost" role="presentation" style=${indent} data-testid=${'ghost-' + fmtLoc({ remote: g.remote, path: g.path })}>
      <span class="twisty-space"></span>
      <${Icon} name=${g.is_dir ? 'folder' : 'file'} size=${16} class="cat-ghost" />
      <span class="name">${g.name}</span>
      <button class="ghost-link" title=${t('pane.ghostReveal')} onClick=${() => onGhost(g)}>→ ${fmtLoc(g.moved_to)}</button>
    </div>`;
  }

  const n = row.node;
  const key = row.key;
  const isDir = n.is_dir;
  const open = isDir && !!s.expanded[key];
  const selected = s.sel.includes(key);
  const cls = ['row'];
  if (selected) cls.push('selected');
  if (s.focus === key) cls.push('focused');
  if (n.moved_here) cls.push('is-moved');
  if (n.merged) cls.push('is-merged');
  if (n.conflict) cls.push('is-conflict');
  if (n.virtual) cls.push('is-virtual');
  if (n.dedupe) cls.push('is-dedupe');
  if (isDir && s.dropKey === key) cls.push('drop');
  if (s.flash === key) cls.push('flash');
  if (!n.path) cls.push('is-root');
  const category = isDir ? (n.path ? 'folder' : 'cloud') : n.category || 'other';
  const renaming = s.renaming === key;

  return html`<div class=${cls.join(' ')} style=${indent} role="treeitem" aria-level=${row.level + 1}
    aria-selected=${selected} aria-expanded=${isDir ? open : undefined} tabindex=${tabbable ? 0 : -1}
    data-testid=${'node-' + key} data-key=${key} data-remote=${n.remote} data-path=${n.path} data-kind=${isDir ? 'dir' : 'file'}
    data-droptarget=${isDir ? key : undefined}
    draggable=${editable && !!n.path && !renaming}
    onClick=${(e) => onClick(e, row)} onFocus=${() => onFocus(key)} onDragStart=${(e) => onDragStart(e, row)}
    onDragOver=${(e) => onDragOver(e, row)} onDrop=${(e) => onDrop(e, n)}>
    ${isDir
    ? html`<button class=${'twisty' + (open ? ' open' : '')} tabindex="-1" aria-hidden="true" data-testid=${'toggle-' + key}
        onClick=${(e) => { e.stopPropagation(); onToggle(key); }}><${Icon} name="chevron" size=${12} /></button>`
    : html`<span class="twisty-space"></span>`}
    <${Icon} name=${iconFor(n)} size=${16} class=${'cat-' + category} />
    ${renaming
    ? html`<input class="rename" data-testid="rename-input" defaultValue=${n.name} ref=${(el) => {
      if (el && !el.dataset.ready) {
        el.dataset.ready = '1';
        el.focus();
        const dot = n.name.lastIndexOf('.');
        el.setSelectionRange(0, !isDir && dot > 0 ? dot : n.name.length);
      }
    }}
        onClick=${(e) => e.stopPropagation()} onDblClick=${(e) => e.stopPropagation()}
        onKeyDown=${(e) => {
          e.stopPropagation();
          if (e.key === 'Enter') onRename(row, e.target.value);
          else if (e.key === 'Escape') dispatch({ type: 'set', patch: { renaming: null } });
        }}
        onBlur=${() => dispatch({ type: 'set', patch: { renaming: null } })} />`
    : html`<span class="name" title=${n.name} onDblClick=${(e) => {
      if (editable && n.path) { e.stopPropagation(); dispatch({ type: 'set', patch: { renaming: key } }); }
    }}>${n.name}</span>`}
    <${Badges} node=${n} />
    <span class="meta">
      ${isDir
    ? (n.files > 0 ? html`<span class="meta-files">${t('pane.files', { n: fmtNum(n.files) })}</span>` : html`<span class="meta-files">${t('pane.emptyShort')}</span>`)
    : null}
      <span class="meta-size">${isDir ? (n.bytes > 0 ? fmtBytes(n.bytes) : '') : fmtBytes(n.size)}</span>
    </span>
  </div>`;
}
