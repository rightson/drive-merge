// Side drawers: the staged-changes list and the folder suggestions.
import { html, useContext, useEffect, useRef, useState } from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import * as api from './api.js';
import { Icon } from './icons.js';
import { AppCtx } from './ctx.js';
import {
  GATHER_NOTE, describeAction, errorText, fmtBytes, fmtLoc, fmtNum, rescanText, sleep, staleText, suggestionText,
} from './util.js';

const SUGGESTION_NOTE = /^(related|duplicates):[0-9a-f]+$/i;

function Drawer({ title, count, testid, onClose, children, footer }) {
  return html`<aside class="drawer" data-testid=${testid} aria-label=${title}>
    <header class="drawer-head">
      <h2>${title}</h2>
      ${count !== undefined && html`<span class="count-pill">${count}</span>`}
      <button class="icon-btn drawer-close" aria-label=${t('common.close')} data-testid="drawer-close" onClick=${onClose}>
        <${Icon} name="x" size=${16} /></button>
    </header>
    <div class="drawer-body">${children}</div>
    ${footer && html`<footer class="drawer-foot">${footer}</footer>`}
  </aside>`;
}

function StagedItem({ a, undone }) {
  const ctx = useContext(AppCtx);
  const d = describeAction(a);
  const note = a.note === GATHER_NOTE ? '' : a.note && !SUGGESTION_NOTE.test(a.note) ? a.note : a.note ? t('staged.fromSuggestion') : '';
  const stale = !undone && !!a.stale;
  return html`<li class=${'staged-item' + (undone ? ' undone' : '') + (stale ? ' stale' : '')}
    data-testid=${undone ? 'staged-item-undone' : 'staged-item'}
    data-action-id=${a.id} data-seq=${a.seq} data-created-by=${a.created_by} data-stale=${stale ? 'true' : 'false'}>
    <span class="seq">#${a.seq}</span>
    <div class="staged-main">
      <button class="staged-title" title=${t('staged.reveal')} onClick=${() => ctx.reveal('right', a.dst)}>${d.title}</button>
      ${d.detail && html`<div class="staged-detail">${d.detail}</div>`}
      ${stale && html`<div class="staged-why" data-testid="staged-stale" data-code=${a.stale_reason && a.stale_reason.code}>
        <${Icon} name="alert" size=${13} /><span><strong class="badge badge-stale">${t('stale.badge')}</strong>${staleText(a.stale_reason)} ${t('stale.remove')}</span></div>`}
      ${note && html`<div class="staged-note">${note}</div>`}
    </div>
    ${a.created_by === 'mcp' && html`<span class="badge badge-claude" data-testid="claude-badge"
      title=${t('staged.byClaude')}><${Icon} name="claude" size=${11} />Claude</span>`}
    ${!undone && html`<button class="icon-btn" data-testid="staged-remove" aria-label=${t('staged.remove')}
      title=${t('staged.remove')} onClick=${() => ctx.removeAction(a.id)}><${Icon} name="x" size=${14} /></button>`}
  </li>`;
}

export function StagedDrawer({ draft, onClose }) {
  const ctx = useContext(AppCtx);
  const active = draft.actions.filter((a) => a.state === 'active');
  const undone = draft.actions.filter((a) => a.state === 'undone');
  const sum = draft.summary || {};
  const footer = html`
    <button class="btn" data-testid="drawer-undo" disabled=${!draft.undo_available} onClick=${ctx.undo}>
      <${Icon} name="undo" size=${15} /> ${t('draft.undo')}</button>
    <button class="btn" data-testid="drawer-redo" disabled=${!draft.redo_available} onClick=${ctx.redo}>
      <${Icon} name="redo" size=${15} /> ${t('draft.redo')}</button>
    <span class="spacer"></span>
    <button class="btn btn-danger-ghost" data-testid="staged-clear" disabled=${draft.actions.length === 0} onClick=${ctx.clearDraft}>
      <${Icon} name="trash" size=${15} /> ${t('staged.clear')}</button>`;
  return html`<${Drawer} title=${t('staged.title')} count=${active.length} testid="staged-drawer" onClose=${onClose} footer=${footer}>
    ${sum.moved_files > 0 && html`<p class="drawer-summary" data-testid="staged-summary">
      ${t('staged.summary', { files: fmtNum(sum.moved_files), bytes: fmtBytes(sum.moved_bytes) })}
      ${sum.cross_remote_bytes > 0 && ` ${t('staged.summaryCross', { bytes: fmtBytes(sum.cross_remote_bytes) })}`}</p>`}
    ${sum.stale_actions > 0 && html`<p class="drawer-stale" data-testid="staged-stale-summary">
      <${Icon} name="alert" size=${14} /> ${t('staged.staleSummary', { n: sum.stale_actions })}</p>`}
    ${active.length === 0 && html`<p class="empty-hint">${t('staged.empty')}</p>`}
    <ol class="staged-list" data-testid="staged-list">
      ${active.map((a) => html`<${StagedItem} key=${a.id} a=${a} />`)}
    </ol>
    ${undone.length > 0 && html`<h3 class="drawer-sub">${t('staged.redoable', { n: undone.length })}</h3>
      <ol class="staged-list">${undone.map((a) => html`<${StagedItem} key=${a.id} a=${a} undone />`)}</ol>`}
  <//>`;
}

/** A suggestion is staged while the draft holds all its steps (each carries the suggestion id as its note). */
const isStaged = (draft, s) => {
  const steps = draft.actions.filter((a) => a.state === 'active' && a.note === s.id).length;
  return steps > 0 && steps >= (s.actions || []).length;
};

function SuggestionCard({ s, added, onAdd, onReveal, onCompare }) {
  const folders = s.folders || [];
  const text = suggestionText(s);
  return html`<article class=${'sug-card' + (added ? ' added' : '')} data-testid="suggestion-card" data-sid=${s.id}>
    <h3 class="sug-title">${text.title}</h3>
    <p class="sug-reason">${text.reason}</p>
    <ul class="sug-folders">
      ${folders.map((f, i) => html`<li key=${fmtLoc(f)}>
        <span class="sug-folder-name" title=${fmtLoc(f)}><${Icon} name="folder" size=${14} class="cat-folder" />
          <span>${fmtLoc(f)}</span>${i === 0 && html`<span class="badge badge-anchor">${t('sug.anchor')}</span>`}</span>
        <span class="sug-folder-meta">${t('pane.files', { n: fmtNum(f.files) })} · ${fmtBytes(f.bytes)}</span>
        <span class="sug-folder-btns">
          <button class="btn btn-tiny" data-testid=${'sug-folder-left-' + i} onClick=${() => onReveal('left', [f])}>${t('sug.left')}</button>
          <button class="btn btn-tiny" data-testid=${'sug-folder-right-' + i} onClick=${() => onReveal('right', [f])}>${t('sug.right')}</button>
        </span></li>`)}
    </ul>
    ${(s.terms || []).length > 0 && html`<div class="chips">${s.terms.map((term) => html`<span class="chip" key=${term}>${term}</span>`)}</div>`}
    <div class="sug-actions">
      <button class="btn btn-small" data-testid="suggestion-show-left" onClick=${() => onReveal('left', folders)}>${t('sug.showLeft')}</button>
      <button class="btn btn-small" data-testid="suggestion-show-right" onClick=${() => onReveal('right', folders)}>${t('sug.showRight')}</button>
      ${folders.length > 1 && html`<button class="btn btn-small" data-testid="suggestion-compare" onClick=${() => onCompare(folders[0], folders[1])}>
        ${t('sug.compare')}</button>`}
      <button class="btn btn-small btn-primary" data-testid="suggestion-add" disabled=${added || !(s.actions || []).length}
        onClick=${onAdd}>${added ? t('sug.added') : t('sug.add')}</button>
    </div>
  </article>`;
}

export function SuggestionsDrawer({ draft, onClose }) {
  const ctx = useContext(AppCtx);
  const [kind, setKind] = useState('related');
  const [state, setState] = useState({ loading: true, items: [], error: '' });

  async function load(k) {
    setState({ loading: true, items: [], error: '' });
    try {
      setState({ loading: false, items: await api.suggestions(k), error: '' });
    } catch (e) {
      setState({ loading: false, items: [], error: errorText(e) });
    }
  }
  useEffect(() => { load(kind); }, [kind]);

  const add = (s) => ctx.stageMany(s.actions);

  // Anchor on the chosen side and the first other folder on the opposite side, so the pair can be compared at once.
  function show(side, folders) {
    if (!folders.length) return;
    const other = side === 'left' ? 'right' : 'left';
    ctx.reveal(side, folders[0], { expandTarget: true });
    if (folders.length > 1) ctx.reveal(other, folders[1], { expandTarget: true });
  }

  const tabs = [['related', t('sug.related')], ['duplicates', t('sug.duplicates')]];
  return html`<${Drawer} title=${t('sug.title')} testid="suggestions-drawer" onClose=${onClose}>
    <div class="tabs" role="tablist">
      ${tabs.map(([k, label]) => html`<button role="tab" aria-selected=${kind === k} class=${kind === k ? 'on' : ''}
        data-testid=${'sug-tab-' + k} onClick=${() => setKind(k)}>${label}</button>`)}
      <span class="spacer"></span>
      <button class="icon-btn" aria-label=${t('common.refresh')} title=${t('common.refresh')} onClick=${() => load(kind)}>
        <${Icon} name="refresh" size=${15} /></button>
    </div>
    <p class="drawer-hint">${kind === 'related' ? t('sug.relatedHint') : t('sug.duplicatesHint')}</p>
    ${state.loading && html`<p class="empty-hint">${t('common.loading')}</p>`}
    ${state.error && html`<p class="empty-hint error">${state.error}</p>`}
    ${!state.loading && !state.error && state.items.length === 0 && html`<p class="empty-hint">${t('sug.none')}</p>`}
    <div class="sug-list" data-testid="suggestion-list">
      ${state.items.map((s) => html`<${SuggestionCard} key=${s.id} s=${s} added=${isStaged(draft, s)}
        onAdd=${() => add(s)} onReveal=${show} onCompare=${ctx.compare} />`)}
    </div>
  <//>`;
}

/**
 * After a run the catalog lags behind the clouds until a scan finishes. Said once the run ends and kept on screen
 * (closing the dialog must not hide it), with the one click that fixes it; a terminal-only page shows the command.
 */
export function CatalogBanner({ remotes, onFresh, onDismiss }) {
  const ctx = useContext(AppCtx);
  const [job, setJob] = useState(null);
  const [error, setError] = useState('');
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  const canRescan = ctx.config.approvals_enabled !== false;

  async function rescan() {
    setError('');
    setJob({ progress: {} });
    try {
      const { job_id: id } = await api.rescan(remotes);
      for (let misses = 0; alive.current;) {
        await sleep(700);
        let j;
        try { j = await api.job(id); } catch (e) { misses += 1; if (misses > 10) throw e; continue; }
        misses = 0;
        if (!alive.current) return;
        setJob(j);
        if (j.status === 'failed') throw new Error(j.error || t('prog.rescanFailed'));
        if (j.status === 'done') { onFresh(); return; }
      }
    } catch (e) {
      if (!alive.current) return;
      setError(errorText(e));
      setJob(null);
    }
  }

  return html`<div class="banner banner-outdated" role="status" data-testid="catalog-banner">
    <${Icon} name="alert" size=${17} />
    <span class="banner-text"><strong>${t('catalog.title')}</strong> ${t('catalog.text', { remotes: remotes.join(', ') })}
      ${!canRescan && html` ${t('catalog.cli')} ${remotes.map((r) => html`<code key=${r}>drivemerge scan ${r}</code>${' '}`)}`}
      ${error && html` <span class="banner-error" data-testid="catalog-error">${error}</span>`}</span>
    ${job ? html`<span class="spinner" aria-hidden="true"></span>
        <span class="banner-progress" data-testid="catalog-progress">${rescanText(job)}</span>`
    : canRescan && html`<button class="btn btn-small" data-testid="catalog-rescan" onClick=${rescan}>
        <${Icon} name="refresh" size=${14} /> ${t('catalog.rescan')}</button>`}
    <button class="icon-btn" data-testid="catalog-dismiss" aria-label=${t('catalog.dismiss')} title=${t('catalog.dismiss')}
      disabled=${!!job} onClick=${onDismiss}><${Icon} name="x" size=${15} /></button>
  </div>`;
}
