// Overview: how do my clouds overlap? Cloud cards, an intersection chart (UpSet style, inline SVG) and the
// similar-folder list (docs/EXPLORE.md section 3).
import {
  html, useContext, useEffect, useRef, useState,
} from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import * as api from './api.js';
import { Icon } from './icons.js';
import { AppCtx } from './ctx.js';
import { CloudDot, CloudName, cloudColor } from './clouds.js';
import { ViewHeader } from './ui.js';
import { compareHash, exploreHash } from './query.js';
import { errorText, fmtBytes, fmtDateTime, fmtLoc, fmtNum, suggestionText } from './util.js';

const CATEGORIES = ['photo', 'video', 'audio', 'document', 'pdf', 'spreadsheet', 'presentation', 'archive', 'code', 'design', 'other'];
const MAX_BARS = 24;

function CloudCard({ remote, remotes, category, onOpen }) {
  const only = remote.only_here || { contents: 0, bytes: 0 };
  const unverified = remote.unverified_files || 0;
  const scan = remote.last_scan;
  return html`<article class="cloud-card" data-testid=${'cloud-card-' + remote.name} style=${{ '--c': cloudColor(remotes, remote.name) }}>
    <header>
      <${CloudName} remotes=${remotes} name=${remote.name} />
      <span class="cloud-provider">${remote.provider}</span>
      ${remote.writable === false && html`<span class="badge badge-readonly" title=${t('ov.readOnlyTip')}>${t('ov.readOnly')}</span>`}
    </header>
    <p class="cloud-total"><strong data-testid="cloud-files">${fmtNum(remote.files)}</strong> ${t('ov.files')} · ${fmtBytes(remote.bytes)}</p>
    <p class="cloud-scan">${scan && scan.finished_at ? t('ov.scanned', { when: fmtDateTime(scan.finished_at) }) : t('ov.neverScanned')}</p>
    <button class=${'cloud-only' + (only.contents > 0 ? ' risk' : '')} data-testid=${'cloud-only-' + remote.name}
      disabled=${only.contents === 0} title=${t('ov.onlyHereTip')}
      onClick=${() => onOpen(`only:${remote.name}` + (category ? ` cat:${category}` : ''))}>
      <span class="cloud-only-label">${t('ov.onlyHere')}</span>
      <span><strong>${fmtNum(only.contents)}</strong> · ${fmtBytes(only.bytes)}</span>
    </button>
    <p class=${'cloud-unverified' + (unverified > 0 ? ' warn' : '')} data-testid=${'cloud-unverified-' + remote.name}>
      ${t('ov.noHash')}: <strong>${fmtNum(unverified)}</strong></p>
    ${unverified > 0 && html`<p class="cloud-undercount" data-testid=${'cloud-undercount-' + remote.name}>
      <${Icon} name="alert" size=${13} /> <span>${t('ov.underCounted')} <code>drivemerge bridge</code></span></p>`}
  </article>`;
}

/** Width of an element, kept up to date. */
function useWidth(ref) {
  const [w, setW] = useState(900);
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const read = () => setW(Math.max(320, Math.floor(el.clientWidth)));
    read();
    if (typeof ResizeObserver === 'undefined') { window.addEventListener('resize', read); return () => window.removeEventListener('resize', read); }
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return w;
}

const compact = (n) => {
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`;
  if (n >= 1e4) return `${(n / 1e3).toFixed(n >= 1e5 ? 0 : 1)}k`;
  return fmtNum(n);
};

/** The intersection chart: a bar per exact combination of clouds, a dot matrix underneath. */
function Intersections({ data, remotes, measure, category, onOpen }) {
  const box = useRef(null);
  const width = useWidth(box);
  const [hot, setHot] = useState(null);
  const value = (b) => (measure === 'bytes' ? b.bytes : b.contents);
  const bars = (data.intersections || []).map((b) => ({ ...b, key: b.remotes.join(',') })).sort((a, b) => value(b) - value(a));
  if (data.other) bars.push({ ...data.other, key: '', other: true, remotes: [] });
  const gutter = 124;
  const rowH = 24;
  const barH = 150;
  const top = 26;
  const colW = Math.max(30, Math.min(104, Math.floor((width - gutter - 12) / Math.max(1, bars.length))));
  const svgW = gutter + colW * bars.length + 12;
  const matrixTop = top + barH + 12;
  const svgH = matrixTop + remotes.length * rowH + 8;
  const max = Math.max(1, ...bars.map(value));
  const barW = Math.round(colW * 0.6);
  const text = (b) => (b.other
    ? t('ov.bar.other', { n: fmtNum(b.combinations), contents: fmtNum(b.contents), bytes: fmtBytes(b.bytes) })
    : t('ov.bar.label', { clouds: b.remotes.join(' + '), contents: fmtNum(b.contents), bytes: fmtBytes(b.bytes), copies: fmtNum(b.copies) }));
  const open = (b) => { if (!b.other) onOpen(`only:${b.remotes.join(',')}` + (category ? ` cat:${category}` : '')); };
  const hovered = hot !== null ? bars[hot] : null;

  return html`<div class="chart-box" ref=${box}>
    <p class="chart-readout" aria-live="polite" data-testid="chart-readout">${hovered ? text(hovered) : t('ov.chart.hint')}</p>
    <div class="chart-scroll">
      <svg class="chart" width=${svgW} height=${svgH} viewBox=${`0 0 ${svgW} ${svgH}`} role="group" aria-label=${t('ov.chart.label')}
        data-testid="chart">
        ${remotes.map((r, i) => html`<g key=${r.name}>
          ${i % 2 === 0 && html`<rect class="chart-stripe" x="0" y=${matrixTop + i * rowH} width=${svgW} height=${rowH} />`}
          <circle cx="12" cy=${matrixTop + i * rowH + rowH / 2} r="5" fill=${cloudColor(remotes, r.name)} />
          <text class="chart-cloud" x="24" y=${matrixTop + i * rowH + rowH / 2 + 4}>${r.name}</text></g>`)}
        <line class="chart-axis" x1=${gutter} x2=${svgW - 8} y1=${top + barH} y2=${top + barH} />
        ${bars.map((b, j) => {
    const x = gutter + j * colW;
    const h = Math.max(value(b) > 0 ? 2 : 0, Math.round((value(b) / max) * barH));
    const idx = b.remotes.map((n) => remotes.findIndex((r) => r.name === n)).filter((i) => i >= 0);
    const fill = b.other ? 'var(--muted)' : b.remotes.length === 1 ? cloudColor(remotes, b.remotes[0]) : 'var(--accent)';
    return html`<g key=${b.key || 'other'} class=${'chart-col' + (hot === j ? ' hot' : '') + (b.other ? ' other' : '')}
      role=${b.other ? 'img' : 'button'} tabindex=${b.other ? undefined : 0} aria-label=${text(b)}
      data-testid=${b.other ? 'chart-other' : 'chart-bar'} data-remotes=${b.remotes.join(',')} data-contents=${b.contents} data-bytes=${b.bytes}
      onMouseEnter=${() => setHot(j)} onMouseLeave=${() => setHot(null)} onFocus=${() => setHot(j)} onBlur=${() => setHot(null)}
      onClick=${() => open(b)} onKeyDown=${(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(b); } }}>
      <title>${text(b)}</title>
      <rect class="chart-hit" x=${x} y=${top - 14} width=${colW} height=${svgH - top + 14} />
      <rect class="chart-bar" x=${x + (colW - barW) / 2} y=${top + barH - h} width=${barW} height=${h} rx="3" fill=${fill} />
      <text class="chart-value" x=${x + colW / 2} y=${top + barH - h - 5} text-anchor="middle">${measure === 'bytes' ? fmtBytes(b.bytes) : compact(b.contents)}</text>
      ${b.other
    ? html`<text class="chart-dots" x=${x + colW / 2} y=${matrixTop + rowH / 2 + 4} text-anchor="middle">…</text>`
    : html`<g>
          ${idx.length > 1 && html`<line class="chart-link" x1=${x + colW / 2} x2=${x + colW / 2} y1=${matrixTop + Math.min(...idx) * rowH + rowH / 2}
            y2=${matrixTop + Math.max(...idx) * rowH + rowH / 2} />`}
          ${remotes.map((r, i) => html`<circle key=${r.name} cx=${x + colW / 2} cy=${matrixTop + i * rowH + rowH / 2} r="5"
            class=${idx.includes(i) ? 'chart-on' : 'chart-off'} fill=${idx.includes(i) ? cloudColor(remotes, r.name) : undefined} />`)}</g>`}
    </g>`;
  })}
      </svg>
    </div>
  </div>`;
}

function CompareMenu({ folders, onPick }) {
  const [open, setOpen] = useState(false);
  const rest = folders.slice(1);
  if (folders.length < 2) return null;
  if (folders.length === 2) {
    return html`<button class="btn btn-small" data-testid="ov-compare" onClick=${() => onPick(folders[0], folders[1])}>
      <${Icon} name="swap" size=${14} /> ${t('ov.compare')}</button>`;
  }
  return html`<span class="menu-wrap">
    <button class="btn btn-small" data-testid="ov-compare" aria-haspopup="menu" aria-expanded=${open} onClick=${() => setOpen(!open)}
      onKeyDown=${(e) => { if (e.key === 'Escape') setOpen(false); }}>
      <${Icon} name="swap" size=${14} /> ${t('ov.compare')} ▾</button>
    ${open && html`<ul class="menu" role="menu" onKeyDown=${(e) => { if (e.key === 'Escape') setOpen(false); }}>
      ${rest.map((f) => html`<li key=${fmtLoc(f)} role="none"><button role="menuitem" data-testid="ov-compare-pair"
        onClick=${() => { setOpen(false); onPick(folders[0], f); }}>${fmtLoc(folders[0])} ⇆ ${fmtLoc(f)}</button></li>`)}
    </ul>`}
  </span>`;
}

function Similar({ items, state, remotes, onCompare }) {
  return html`<section class="ov-panel" aria-labelledby="ov-similar-h" data-testid="ov-similar-section">
    <h2 id="ov-similar-h">${t('ov.similar')}</h2>
    ${state.loading && html`<p class="empty-hint">${t('common.loading')}</p>`}
    ${state.error && html`<p class="empty-hint error">${errorText(state.error)}</p>`}
    ${!state.loading && !state.error && items.length === 0 && html`<p class="empty-hint">${t('ov.similarNone')}</p>`}
    <ul class="sim-list">
      ${items.map((s) => {
    const folders = s.folders || [];
    const text = suggestionText(s);
    const pct = s.kind === 'duplicates' && s.params ? Math.round((s.params.similarity || 0) * 100) : null;
    return html`<li key=${s.id} class="sim-row" data-testid="ov-similar" data-kind=${s.kind}>
      <span class=${'badge ' + (s.kind === 'duplicates' ? 'badge-dup' : 'badge-new')}>${t(s.kind === 'duplicates' ? 'sug.duplicates' : 'sug.related')}</span>
      <div class="sim-main">
        <div class="sim-title">${text.title}</div>
        <div class="sim-folders">${folders.slice(0, 3).map((f) => html`<span key=${fmtLoc(f)} class="sim-folder" title=${fmtLoc(f)}>
          <${CloudDot} remotes=${remotes} name=${f.remote} size=${8} /><span>${fmtLoc(f)}</span></span>`)}
          ${folders.length > 3 && html`<span class="muted">+${folders.length - 3}</span>`}</div>
      </div>
      ${pct !== null && html`<span class="sim-score" title=${t('ov.similarity')}>${pct}%</span>`}
      <${CompareMenu} folders=${folders} onPick=${onCompare} />
    </li>`;
  })}
    </ul>
  </section>`;
}

export function Overview() {
  const ctx = useContext(AppCtx);
  const remotes = ctx.config.remotes || [];
  const [category, setCategory] = useState('');
  const [measure, setMeasure] = useState('contents');
  const [cov, setCov] = useState({ loading: true, data: null, error: '' });
  const [sims, setSims] = useState({ loading: true, items: [], error: '' });
  const seq = useRef(0);

  useEffect(() => {
    const mine = (seq.current += 1);
    setCov((c) => ({ ...c, loading: true, error: '' }));
    api.coverage({ category: category || undefined, maxBars: MAX_BARS }).then(
      (data) => { if (mine === seq.current) setCov({ loading: false, data, error: '' }); },
      (e) => { if (mine === seq.current) setCov({ loading: false, data: null, error: e }); },
    );
  }, [category]);

  useEffect(() => {
    let alive = true;
    Promise.all([api.suggestions('related', { limit: 10 }), api.suggestions('duplicates', { limit: 10 })]).then(
      ([a, b]) => { if (alive) setSims({ loading: false, items: [...a, ...b].sort((x, y) => (y.score || 0) - (x.score || 0)).slice(0, 10), error: '' }); },
      (e) => { if (alive) setSims({ loading: false, items: [], error: e }); },
    );
    return () => { alive = false; };
  }, []);

  const data = cov.data;
  const totals = (data && data.totals) || {};
  const openExplore = (text) => ctx.nav(exploreHash(text));
  const hasBars = data && ((data.intersections || []).length > 0 || data.other);

  return html`<main class="view view-overview" data-testid="view-overview" aria-label=${t('nav.overview')}>
    <${ViewHeader} title=${t('nav.overview')} hint=${t('ov.hint')} />
    <div class="view-body">
      ${cov.error && html`<p class="view-error" role="alert" data-testid="ov-error">${errorText(cov.error)}</p>`}
      ${!data && cov.loading && html`<p class="empty-hint">${t('common.loading')}</p>`}
      ${data && html`<div class="cloud-cards" data-testid="cloud-cards">
        ${(data.remotes || []).map((r) => html`<${CloudCard} key=${r.name} remote=${r} remotes=${remotes} category=${category} onOpen=${openExplore} />`)}
      </div>`}
      ${data && html`<section class="ov-panel" aria-labelledby="ov-chart-h">
        <div class="ov-panel-head">
          <h2 id="ov-chart-h">${t('ov.chart.title')}</h2>
          <p class="ov-totals" data-testid="ov-totals">
            <span>${t('ov.unique')} <strong>${fmtBytes(totals.bytes)}</strong></span>
            <span>${t('ov.stored')} <strong>${fmtBytes(totals.stored_bytes)}</strong></span>
            <span>${t('ov.redundant')} <strong>${fmtBytes(totals.redundant_bytes)}</strong></span>
            <span class="muted">${t('ov.contents', { n: fmtNum(totals.contents) })}</span>
          </p>
          <span class="spacer"></span>
          <div class="segmented" role="group" aria-label=${t('ov.measure')}>
            <button class=${measure === 'contents' ? 'on' : ''} aria-pressed=${measure === 'contents'} data-testid="measure-contents"
              onClick=${() => setMeasure('contents')}>${t('ov.byFiles')}</button>
            <button class=${measure === 'bytes' ? 'on' : ''} aria-pressed=${measure === 'bytes'} data-testid="measure-bytes"
              onClick=${() => setMeasure('bytes')}>${t('ov.byBytes')}</button>
          </div>
          <label class="visually-hidden" for="ov-category">${t('ov.category')}</label>
          <select id="ov-category" class="select" data-testid="ov-category" value=${category} onChange=${(e) => setCategory(e.target.value)}>
            <option value="">${t('ov.allCategories')}</option>
            ${CATEGORIES.map((c) => html`<option value=${c}>${t(`q.cat.${c}`, null, c)}</option>`)}
          </select>
        </div>
        ${hasBars
    ? html`<${Intersections} data=${data} remotes=${remotes} measure=${measure} category=${category} onOpen=${openExplore} />`
    : html`<p class="empty-hint" data-testid="ov-empty">${t('ov.noData')}</p>`}
      </section>`}
      <${Similar} items=${sims.items} state=${sims} remotes=${remotes}
        onCompare=${(a, b) => ctx.nav(compareHash({ remote: a.remote, path: a.path }, { remote: b.remote, path: b.path }))} />
    </div>
  </main>`;
}
