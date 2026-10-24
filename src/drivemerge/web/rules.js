// Rules (docs/RULES.md): the layout rules that decide where consolidate and reorganize plans put files. The overview of
// the active rules, drafts, Claude's proposals and the history (#/rules); the editor with live validation and a live
// before/after preview (#/rules/edit/<id>); the review of one version (#/rules/v/<id>); the activation dialog; and the two
// pieces other views embed: RulesField (the plan forms) and PlanRulesBox (the plan review).
//
// Nothing here changes a cloud. Rules only shape the plans built later; making a version active is a human act (approvals
// on the page, or `drivemerge rules activate <id>` in a terminal) and writes one file after showing the diff.
import {
  html, useContext, useEffect, useRef, useState,
} from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import * as api from './api.js';
import { Icon } from './icons.js';
import { AppCtx } from './ctx.js';
import { CloudName } from './clouds.js';
import { Modal, ViewHeader } from './ui.js';
import { parseSize } from './query.js';
import {
  Outline, RuleTextEditor, TEMPLATE_VARS, countText, exampleFile, itemAtLine, renderTarget,
} from './ruletext.js';
import {
  ago, errorText, fmtBytes, fmtDateTime, fmtNum,
} from './util.js';

const CHECK_MS = 200;       // validation after a pause in typing
const PREVIEW_MS = 600;     // the preview, counted from the last keystroke
const SAVE_MS = 2000;       // draft autosave
const PAGE = 100;           // files per page of the preview
const CATEGORIES = ['photo', 'video', 'audio', 'document', 'pdf', 'spreadsheet', 'presentation', 'archive', 'code', 'design', 'other'];
const FILTERS = ['changed', 'fallback', 'all'];

/** `#/rules`, `#/rules/edit/14?back=...`, `#/rules/v/15`. */
export function rulesHash(sub = '', obj = {}) {
  const q = Object.entries(obj).filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join('&');
  return '#/rules' + sub + (q ? `?${q}` : '');
}

/** A hash with one query parameter set (or removed with null), for the link back to a plan form. */
function withParam(hash, key, value) {
  const [path, query = ''] = hash.split('?');
  const p = new URLSearchParams(query);
  if (value === null || value === undefined || value === '') p.delete(key); else p.set(key, String(value));
  const s = p.toString();
  return path + (s ? `?${s}` : '');
}

const linkTo = (ctx, hash) => (e) => {
  if (e.ctrlKey || e.metaKey || e.shiftKey || e.button) return;
  e.preventDefault();
  ctx.nav(hash);
};

function session(op, key, value) {
  try {
    if (op === 'get') return window.sessionStorage.getItem(key);
    if (op === 'set') window.sessionStorage.setItem(key, value);
    if (op === 'del') window.sessionStorage.removeItem(key);
  } catch (e) { /* storage blocked: the unsaved text then lives only in the page */ }
  return null;
}

const unwrap = (r) => (r && r.version) || r;
const listOf = (r, key) => (Array.isArray(r) ? r : (r && r[key]) || []);
const ruleCount = (v) => (v ? (Array.isArray(v.rules) ? v.rules.length : v.rules) : null);

// ------------------------------------------------------------------ words for what the server says

const blank = (o) => Object.fromEntries(Object.entries(o).map(([k, v]) => [k, v === null || v === undefined ? '' : v]));

function wordRuleParams(p) {
  const out = blank(p);
  if (typeof out.reason === 'string') out.reason = t(`rulecheck.RULES_CANNOT_EDIT.reason.${out.reason}`, null, out.reason);
  if (out.where === 'target' || out.where === 'fallback') out.where = t(`rules.tpl.${out.where}`);
  if (typeof out.limit === 'number') out.limit = fmtNum(out.limit);
  if (typeof out.min === 'number') out.min = fmtBytes(out.min);
  if (typeof out.max === 'number') out.max = fmtBytes(out.max);
  return out;
}

/** The line a problem of the checker points at, or null. */
export const problemLine = (item) => {
  const v = item.params && item.params.line !== undefined ? item.params.line : item.line;
  return typeof v === 'number' ? v : null;
};

/**
 * One error or warning of the checker in the page's language: "Rule 3 (screenshots): unknown category “foto” — did you
 * mean “photo”?". The server sends a code and params (line, rule_index, rule, key and the code's own); the English
 * `message` shows only for a code the page has no sentence for.
 */
export function problemText(item, ns = 'rulecheck') {
  const p = { ...(item.params || {}) };
  ['line', 'rule_index', 'rule', 'key'].forEach((k) => { if (p[k] === undefined && item[k] !== undefined) p[k] = item[k]; });
  const vars = wordRuleParams(p);
  const base = `${ns}.${item.code}`;
  const key = item.code === 'TOML_SYNTAX' && !vars.column ? `${base}.nocol` : base;
  let body = t(key, vars, '');
  if (!body) return item.message || item.code;
  if (p.suggestion) body += ` ${t(`${base}.hint`, vars, '')}`;
  if (!p.rule_index) return body;
  const where = p.rule ? t('rules.where.rule', vars) : t('rules.where.ruleNoName', vars);
  return t('rules.problemLine', { where, text: body });
}

/** A refusal of a rules route: a rule error (a code of the checker) or a coded refusal (err.<CODE>). */
export function rulesErrorText(e) {
  if (e && e.code && t(`rulecheck.${e.code}`, null, '')) return problemText({ code: e.code, params: e.params, message: e.message });
  return errorText(e);
}

/** "~/rules.toml" / "packaged defaults": where the active rules come from. */
function sourceName(active) {
  if (!active || active.source === 'default' || !active.path) return t('rsrc.default');
  return active.path;
}

/** "ui:alice" and "ui" are both the person at the browser: the part before the colon names the channel. */
function whoName(by) {
  if (!by) return '';
  const kind = String(by).split(':')[0];
  return t(`rby.${kind}`, null, by);
}

/**
 * What the active rules are and since when, from `GET /v1/rules`: the server's own fields (`rules`, `since`, `by`,
 * `changed_outside`, `write_path`) with the history row as the fallback.
 */
function activeInfo(state) {
  const a = (state && state.active) || {};
  const history = (state && state.history) || [];
  const id = a.version_id !== undefined && a.version_id !== null ? a.version_id : null;
  const row = history.find((h) => h.status === 'active') || (id !== null ? history.find((h) => h.id === id) : null) || null;
  const count = a.rules !== undefined && a.rules !== null ? a.rules : ruleCount(row);
  return {
    a,
    row,
    id,
    count,
    since: a.since || (row ? (row.activated_at || row.created_at) : null),
    by: a.by || (row ? (row.activated_by || row.created_by) : null),
    outside: a.changed_outside !== undefined ? !!a.changed_outside : !!row && row.created_by === 'file',
    file: a.write_path || a.path || null,
  };
}

function copyText(ctx, text) {
  try { navigator.clipboard.writeText(text); ctx.toast('success', t('common.copied')); } catch (e) { /* no clipboard */ }
}

// ------------------------------------------------------------------ small pieces

function ChangeChips({ changes }) {
  const counts = {};
  (changes || []).forEach((c) => { counts[c.change] = (counts[c.change] || 0) + 1; });
  const kinds = ['added', 'removed', 'changed', 'moved'].filter((k) => counts[k]);
  if (!kinds.length) return html`<span class="muted" data-testid="rules-changes-none">${t('rules.noChanges')}</span>`;
  return html`<span class="rl-changes" data-testid="rules-changes">${kinds.map((k) => html`<span key=${k} class=${'rl-chg rl-chg-' + k} data-change=${k}>
    ${t(`rchange.${k}`, { n: counts[k] })}</span>`)}</span>`;
}

function Who({ by }) {
  if (by === 'mcp') {
    return html`<span class="badge badge-claude" data-testid="rules-claude-badge" title=${t('plans.byClaudeTip')}>
      <${Icon} name="claude" size=${11} /> ${t('plans.byClaude')}</span>`;
  }
  return html`<span class="muted">${whoName(by)}</span>`;
}

function StatusChip({ status }) {
  return html`<span class=${'rl-status rl-status-' + status} data-status=${status}>${t(`rstatus.${status}`, null, status)}</span>`;
}

/** A text diff (the server's hunks): context lines grey, additions green, removals red. */
export function DiffView({ diff, testid = 'rules-diff' }) {
  const hunks = (diff && diff.hunks) || [];
  if (!hunks.length) return html`<p class="empty-hint" data-testid=${testid + '-none'}>${t('rules.diff.none')}</p>`;
  return html`<div class="rd" data-testid=${testid}>
    <p class="rd-sum"><span class="rd-add">+${fmtNum(diff.added)}</span> <span class="rd-del">−${fmtNum(diff.removed)}</span></p>
    ${hunks.map((h, i) => html`<div class="rd-hunk" key=${i}>
      <div class="rd-head">@@ −${h.old_start} +${h.new_start} @@</div>
      ${h.lines.map((l, j) => html`<div key=${j} class=${'rd-line rd-' + (l.op === '+' ? 'add' : l.op === '-' ? 'del' : 'ctx')} data-op=${l.op}>
        <span class="rd-op" aria-hidden="true">${l.op === ' ' ? '' : l.op}</span><span class="rd-text">${l.text}</span></div>`)}
    </div>`)}</div>`;
}

/** The command to run in a terminal instead of activating on the page (a page without approvals). */
function CliCommand({ id, flush, note }) {
  const ctx = useContext(AppCtx);
  const cmd = `drivemerge rules activate ${id}`;
  return html`<section class="cli-box" data-testid="rules-cli">
    <h3>${t('rules.cli.title')}</h3>
    <p>${t('rules.cli.text')}</p>
    <div class="cli-line"><code data-testid="rules-cli-command">${cmd}</code>
      <button class="icon-btn" aria-label=${t('common.copy')} title=${t('common.copy')} data-testid="rules-cli-copy"
        onClick=${async () => { if (flush) await flush(); copyText(ctx, cmd); }}><${Icon} name="copy" size=${14} /></button></div>
    ${note && html`<p class="note">${note}</p>`}
  </section>`;
}

const approvalsOn = (ctx) => ctx.config.approvals_enabled !== false;

/** "Use in plan ▾": a small menu with the two plan forms. */
function UsePlanMenu({ onPick, disabled }) {
  const [open, setOpen] = useState(false);
  const box = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const away = (e) => { if (box.current && !box.current.contains(e.target)) setOpen(false); };
    const esc = (e) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', away);
    document.addEventListener('keydown', esc);
    return () => { document.removeEventListener('mousedown', away); document.removeEventListener('keydown', esc); };
  }, [open]);
  return html`<span class="rl-menu" ref=${box}>
    <button class="btn btn-small" data-testid="rules-use-plan" aria-haspopup="menu" aria-expanded=${open} disabled=${disabled}
      onClick=${() => setOpen(!open)}>${t('rules.usePlan')} ▾</button>
    ${open && html`<div class="rl-menu-list" role="menu">
      <button role="menuitem" class="rl-menu-item" data-testid="rules-use-consolidate" onClick=${() => { setOpen(false); onPick('consolidate'); }}>
        <${Icon} name="merge" size=${14} /> ${t('rules.useConsolidate')}</button>
      <button role="menuitem" class="rl-menu-item" data-testid="rules-use-reorganize" onClick=${() => { setOpen(false); onPick('reorganize'); }}>
        <${Icon} name="folder" size=${14} /> ${t('rules.useReorganize')}</button></div>`}
  </span>`;
}

// ------------------------------------------------------------------ the preview

/**
 * The live preview of a rules text (`source` is {text} or {version_id}) over the real catalog. It runs 600 ms after the
 * last change, only while `ready` (the text passed the check); an answer for an older request is dropped, a request
 * the server superseded is ignored, and an expired preview is computed again.
 */
function useRulePreview({ source, scope, ready }) {
  const [st, setSt] = useState({ data: null, files: [], total: 0, loading: false, error: null, busy: false });
  const [filter, setFilterState] = useState('changed');
  const [q, setQState] = useState('');
  const seq = useRef(0);
  const pageSeq = useRef(0);
  const stRef = useRef(st);
  stRef.current = st;
  const filterRef = useRef(filter);
  filterRef.current = filter;
  const qRef = useRef(q);
  qRef.current = q;
  const changed = useRef({ key: '', at: 0 });
  const ran = useRef(false);
  const key = JSON.stringify([source, scope.remote, scope.path, scope.root]);

  const body = (mode) => ({
    ...source,
    ...(scope.remote ? { remotes: [scope.remote] } : {}),
    target_root: scope.root || '',
    ...(scope.path ? { path_prefix: scope.path } : {}),
    mode, filter: filterRef.current, limit: PAGE,
  });

  async function run(mode = 'auto') {
    seq.current += 1;
    const mine = seq.current;
    ran.current = true;
    setSt((s) => ({ ...s, loading: true, error: null }));
    try {
      const r = await api.previewRules(body(mode));
      if (mine !== seq.current) return;
      const page = r.page || { files: [], total: 0 };
      setSt({ data: r, files: page.files || [], total: page.total || 0, loading: false, error: null, busy: false });
      if (qRef.current) loadPage(filterRef.current, qRef.current, r);
    } catch (e) {
      if (mine !== seq.current) return;
      if (e.code === 'PREVIEW_SUPERSEDED') { setSt((s) => ({ ...s, loading: false })); return; }
      setSt((s) => ({ ...s, loading: false, error: e }));
    }
  }

  async function loadPage(f, text, data = stRef.current.data, append = false) {
    if (!data) return;
    pageSeq.current += 1;
    const mine = pageSeq.current;
    const offset = append ? stRef.current.files.length : 0;
    setSt((s) => ({ ...s, busy: true }));
    try {
      const page = await api.previewFiles(data.preview_id, { filter: f, q: text || undefined, offset, limit: PAGE });
      if (mine !== pageSeq.current) return;
      setSt((s) => ({ ...s, busy: false, error: null, files: append ? [...s.files, ...(page.files || [])] : (page.files || []), total: page.total || 0 }));
    } catch (e) {
      if (mine !== pageSeq.current) return;
      setSt((s) => ({ ...s, busy: false }));
      if (e.code === 'PREVIEW_EXPIRED') run('auto'); else setSt((s) => ({ ...s, error: e }));
    }
  }

  useEffect(() => {
    if (key !== changed.current.key) changed.current = { key, at: Date.now() };
    if (!ready) return undefined;
    const wait = ran.current ? Math.max(0, PREVIEW_MS - (Date.now() - changed.current.at)) : 0;
    const timer = setTimeout(() => run('auto'), wait);
    return () => clearTimeout(timer);
  }, [key, ready]);

  return {
    ...st,
    filter,
    q,
    blocked: !ready,
    exact: () => run('exact'),
    rerun: () => run('auto'),
    setFilter: (f) => { setFilterState(f); filterRef.current = f; loadPage(f, qRef.current); },
    setQ: (text) => { setQState(text); qRef.current = text; loadPage(filterRef.current, text); },
    more: () => loadPage(filterRef.current, qRef.current, stRef.current.data, true),
  };
}

/** The rows of a preview by rule name, with the count each shows: "≈ 611,532" when sampled. */
function previewRows(data) {
  if (!data) return {};
  const out = {};
  (data.rules || []).forEach((r) => {
    out[r.name] = { ...r, text: countText(data.sampled ? r.estimate_files : r.files, data.sampled) };
  });
  if (data.fallback) {
    out.__fallback = { ...data.fallback, text: countText(data.sampled ? data.fallback.estimate_files : data.fallback.files, data.sampled) };
  }
  return out;
}

function ExplainSteps({ title, ex, testid }) {
  if (!ex) return null;
  return html`<section class="rx-side" data-testid=${testid}>
    <h4>${title}</h4>
    <p class="rx-dest">${ex.in_place
    ? html`<${Icon} name="check" size=${13} /> ${t('rules.explain.inPlace')}`
    : html`<span class="mono" data-testid=${testid + '-target'}>${ex.target}</span>`}
      ${ex.rule && html`<span class="badge badge-new">${ruleLabel(ex.rule)}</span>`}</p>
    ${ex.unit && html`<p class="muted">${t('rules.explain.unit', { unit: ex.unit, rule: ex.unit_rule || '' })}</p>`}
    <ol class="rx-steps">${(ex.steps || []).map((s, i) => html`<li key=${i} class=${'rx-step rx-' + s.result} data-result=${s.result}>
      <b>${s.rule}</b> <span class="rx-kind">${s.unit ? t('rules.explain.unitRule') : ''}</span>
      <span>${s.result === 'match' ? t('rules.explain.match')
    : s.result === 'skip' ? (s.failed_key
      ? t('rules.explain.skip', { key: s.failed_key, wanted: listText(s.wanted), got: listText(s.got) })
      : t('rules.explain.skipKind'))
      : s.unit && !ex.unit ? t('rules.explain.notTriedUnit') : t('rules.explain.notTried')}</span></li>`)}</ol>
  </section>`;
}

/** The server names the fallback "fallback" in every `rule` field (a rule cannot take that name): word it in the page's language. */
const ruleLabel = (name) => (!name || name === 'fallback' ? t('rules.outline.fallback') : name);

const listText = (v) => (v === null || v === undefined ? '–' : Array.isArray(v) ? v.join(', ') || '–' : String(v));

/** "Where would the rules put this file?": each rule in order with the first key that failed, under the edited and the active rules. */
function Explain({ source, scope, initial, onInitialDone, explainRef }) {
  const [text, setText] = useState(initial || '');
  const [st, setSt] = useState({ loading: false, error: null, data: null, asked: '' });
  const seq = useRef(0);
  async function ask(place) {
    const at = String(place).indexOf(':');
    if (at <= 0) { setSt({ loading: false, error: { code: 'BAD_PLACE' }, data: null, asked: place }); return; }
    seq.current += 1;
    const mine = seq.current;
    setSt((s) => ({ ...s, loading: true, error: null, asked: place }));
    try {
      const data = await api.explainRules({ ...source, remote: place.slice(0, at), path: place.slice(at + 1), target_root: scope.root || '' });
      if (mine === seq.current) setSt({ loading: false, error: null, data, asked: place });
    } catch (e) {
      if (mine === seq.current) setSt({ loading: false, error: e, data: null, asked: place });
    }
  }
  useEffect(() => { if (initial) { setText(initial); ask(initial); if (onInitialDone) onInitialDone(); } }, [initial]);
  if (explainRef) explainRef.current = ask;
  return html`<section class="rx" data-testid="rules-explain" data-loading=${String(st.loading)}>
    <h3 class="section-title">${t('rules.explain.title')}</h3>
    <form class="rx-form" onSubmit=${(e) => { e.preventDefault(); if (text.trim()) ask(text.trim()); }}>
      <label class="visually-hidden" for="rx-input">${t('rules.explain.label')}</label>
      <input id="rx-input" type="text" data-testid="rules-explain-input" value=${text} placeholder=${t('rules.explain.placeholder')}
        autocomplete="off" spellcheck="false" onInput=${(e) => setText(e.target.value)} />
      <button class="btn btn-small" type="submit" data-testid="rules-explain-go" disabled=${!text.trim() || st.loading}>${t('rules.explain.go')}</button>
    </form>
    ${st.loading && html`<p class="empty-hint"><span class="spinner" aria-hidden="true"></span> ${t('rules.working')}</p>`}
    ${st.error && html`<p class="pl-error" role="alert" data-testid="rules-explain-error">${st.error.code === 'BAD_PLACE' ? t('rules.explain.badPlace') : rulesErrorText(st.error)}</p>`}
    ${st.data && html`<div class="rx-grid">
      <${ExplainSteps} title=${t('rules.explain.edited')} ex=${st.data.edited} testid="rules-explain-edited" />
      <${ExplainSteps} title=${t('rules.explain.active')} ex=${st.data.active} testid="rules-explain-active" />
    </div>`}
  </section>`;
}

function PreviewPane({ pv, scope, setScope, source, remotes, readOnlyScope, explainInitial, onExplainDone, explainRef }) {
  const d = pv.data;
  const sampled = !!(d && d.sampled);
  const scale = d && d.sample_files ? d.files_in_scope / d.sample_files : 1;
  const est = (n) => (n === null || n === undefined ? null : sampled ? Math.round(n * scale) : n);
  const inPlace = !d ? 0 : sampled && d.estimate_in_place !== undefined ? d.estimate_in_place : est(d.in_place || 0);
  const rows = d ? d.rules || [] : [];
  const showBytes = rows.some((r) => r.bytes > 0);
  const filters = [...FILTERS, ...(pv.filter.startsWith('rule:') ? [pv.filter] : [])];
  const explain = (f) => { if (explainRef.current) explainRef.current(`${f.remote}:${f.path}`); };

  return html`<div class="rp" data-testid="rules-preview" aria-busy=${pv.loading}>
    <div class="rp-scope" data-testid="rules-preview-scope">
      <label class="field"><span>${t('rules.scope.clouds')}</span>
        <select class="select" data-testid="rules-scope-remote" value=${scope.remote} disabled=${readOnlyScope}
          onChange=${(e) => setScope({ ...scope, remote: e.target.value })}>
          <option value="">${t('rules.scope.all')}</option>
          ${remotes.map((r) => html`<option key=${r.name} value=${r.name}>${r.name}</option>`)}</select></label>
      <label class="field"><span>${t('rules.scope.folder')}</span>
        <input type="text" data-testid="rules-scope-path" value=${scope.path} placeholder=${t('dedupe.pathPlaceholder')} autocomplete="off"
          spellcheck="false" onInput=${(e) => setScope({ ...scope, path: e.target.value })} /></label>
      <label class="field"><span>${t('rules.scope.root')}</span>
        <input type="text" data-testid="rules-scope-root" value=${scope.root} placeholder=${t('cons.rootPlaceholder')} autocomplete="off"
          spellcheck="false" onInput=${(e) => setScope({ ...scope, root: e.target.value })} /></label>
    </div>
    ${pv.loading && html`<p class="rp-working" data-testid="rules-preview-working" role="status"><span class="spinner" aria-hidden="true"></span> ${t('rules.working')}</p>`}
    ${pv.blocked && html`<p class="pl-warn" data-testid="rules-preview-blocked" role="status"><${Icon} name="alert" size=${14} /> ${t('rules.preview.blocked')}</p>`}
    ${pv.error && html`<p class="view-error" role="alert" data-testid="rules-preview-error">${rulesErrorText(pv.error)}
      <button class="link" onClick=${pv.rerun}>${t('common.retry')}</button></p>`}
    ${!d && !pv.loading && !pv.error && !pv.blocked && html`<p class="empty-hint">${t('rules.preview.none')}</p>`}
    ${d && html`<div class=${pv.loading || pv.blocked ? 'rp-stale' : ''}>
      ${sampled
    ? html`<p class="rp-mode" data-testid="rules-preview-sampled" data-rate=${d.sample_rate}><b aria-hidden="true">≈</b>
          ${t('rules.preview.sampled', { sample: fmtNum(d.sample_files), files: fmtNum(d.files_in_scope), pct: (d.sample_rate * 100).toFixed(1) })}
          <button class="link" data-testid="rules-preview-exact-go" disabled=${pv.loading} onClick=${pv.exact}>${t('rules.preview.countAll')}</button></p>`
    : html`<p class="rp-mode" data-testid="rules-preview-exact"><${Icon} name="check" size=${13} /> ${t('rules.preview.exact', { files: fmtNum(d.files_in_scope) })}</p>`}
      <div class="rp-cards">
        <div class="card" data-testid="rules-preview-scope-files"><div class="card-value">${fmtNum(d.files_in_scope)}</div>
          <div class="card-label">${t('rules.preview.inScope')}</div></div>
        <div class=${'card' + (d.changed > 0 ? ' card-warn' : '')} data-testid="rules-preview-changed" data-value=${sampled ? d.estimate_changed : d.changed}>
          <div class="card-value">${countText(sampled ? d.estimate_changed : d.changed, sampled)}</div>
          <div class="card-label">${t('rules.preview.changed')}</div></div>
        <div class="card" data-testid="rules-preview-fallback" data-value=${sampled ? d.fallback.estimate_files : d.fallback.files}>
          <div class="card-value">${countText(sampled ? d.fallback.estimate_files : d.fallback.files, sampled)}</div>
          <div class="card-label">${t('rules.preview.fallback')}</div></div>
        <div class="card" data-testid="rules-preview-inplace" data-value=${inPlace}>
          <div class="card-value">${countText(inPlace, sampled)}</div>
          <div class="card-label">${t('rules.preview.inPlace')}</div></div>
      </div>
      ${d.shared_targets > 0 && html`<p class="pl-help" data-testid="rules-preview-shared">${t('rules.preview.shared', { n: fmtNum(d.shared_targets) })}</p>`}
      <h3 class="section-title">${t('rules.preview.perRule')}</h3>
      <div class="rp-table-wrap"><table class="table rp-table" data-testid="rules-preview-rules">
        <thead><tr><th>${t('rules.col.rule')}</th><th class="num">${t('rules.col.active')}</th><th class="num">${t('rules.col.edited')}</th>
          ${showBytes && html`<th class="num">${t('rules.col.bytes')}</th>`}</tr></thead>
        <tbody>${rows.map((r) => {
    const edited = sampled ? r.estimate_files : r.files;
    const active = sampled ? r.estimate_active_files : r.active_files;
    return html`<tr key=${r.name} data-testid=${'rules-rule-row-' + r.name} data-edited=${edited} data-active=${active === null ? '' : active}
          class=${pv.filter === 'rule:' + r.name ? 'on' : ''}>
          <td><button class="link rp-rule" onClick=${() => pv.setFilter('rule:' + r.name)} title=${t('rules.preview.showFiles')}>${r.name}</button>
            ${r.unit && html`<span class="badge badge-readonly" title=${t('rules.preview.unitTip')}>${t('rules.preview.unit', { n: r.units === null || r.units === undefined ? '–' : fmtNum(r.units) })}</span>`}
            ${r.removed && html`<span class="badge badge-conflict" data-testid=${'rules-rule-removed-' + r.name}>${t('rules.preview.removed')}</span>`}
            ${edited === 0 && !r.removed && html`<span class="badge badge-nomatch" data-testid=${'rules-rule-empty-' + r.name}>${t('rules.preview.noMatch')}</span>`}
            ${(active === null || active === undefined) && !r.removed && html`<span class="badge badge-new">${t('rules.preview.newRule')}</span>`}</td>
          <td class="num">${active === null || active === undefined ? '–' : countText(active, sampled)}</td>
          <td class="num">${countText(edited, sampled)}${active != null && edited !== active ? html`<span class=${'delta ' + (edited > active ? 'up' : 'down')}>${edited > active ? '+' : '−'}${fmtNum(Math.abs(edited - active))}</span>` : ''}</td>
          ${showBytes && html`<td class="num">${fmtBytes(sampled && r.estimate_bytes !== undefined ? r.estimate_bytes : r.bytes)}</td>`}</tr>`;
  })}
          <tr data-testid="rules-rule-row-fallback" class=${pv.filter === 'fallback' ? 'on' : ''}>
            <td><button class="link rp-rule" onClick=${() => pv.setFilter('fallback')}>${t('rules.outline.fallback')}</button></td>
            <td class="num">${countText(est(d.fallback.active_files), sampled)}</td>
            <td class="num">${countText(sampled ? d.fallback.estimate_files : d.fallback.files, sampled)}</td>${showBytes && html`<td class="num">${fmtBytes(d.fallback.bytes)}</td>`}</tr>
        </tbody></table></div>
      ${(d.transitions || []).length > 0 && html`<h3 class="section-title">${t('rules.preview.moves')}</h3>
        <ul class="rp-moves" data-testid="rules-transitions">${d.transitions.slice(0, 8).map((m, i) => html`<li key=${i} data-testid="rules-transition"
          data-from=${m.from === null ? '' : m.from} data-to=${m.to}>
          <span>${m.from === null ? t('rules.preview.noRule') : ruleLabel(m.from)}</span> <${Icon} name="arrow" size=${12} /> <span>${ruleLabel(m.to)}</span>
          <b class="num">${countText(sampled ? m.estimate_files : m.files, sampled)}</b></li>`)}</ul>`}
    </div>`}
    <${Explain} source=${source} scope=${scope} initial=${explainInitial} onInitialDone=${onExplainDone} explainRef=${explainRef} />
    ${d && html`<section class="rp-files" data-testid="rules-files">
      <h3 class="section-title">${t('rules.files.title')}</h3>
      <div class="pl-tabs" role="tablist" aria-label=${t('rules.files.title')}>
        ${filters.map((f) => html`<button key=${f} role="tab" class=${pv.filter === f ? 'on' : ''} aria-selected=${pv.filter === f}
          data-testid=${'rules-files-tab-' + f} onClick=${() => pv.setFilter(f)}>${f.startsWith('rule:') ? f.slice(5) : t(`rules.files.${f}`)}</button>`)}
        <span class="spacer"></span>
        <input type="search" class="rp-q" data-testid="rules-files-q" value=${pv.q} placeholder=${t('rules.files.search')} aria-label=${t('rules.files.search')}
          onInput=${(e) => pv.setQ(e.target.value)} />
      </div>
      <p class="muted rp-total" data-testid="rules-files-total">${t('rules.files.total', { shown: fmtNum(pv.files.length), total: fmtNum(pv.total) })}</p>
      <ul class="rp-files-list">${pv.files.map((f) => html`<li key=${f.entry_id + ':' + f.path} class="rp-file" data-testid="rules-file-row" data-entry=${f.entry_id}
        data-in-place=${String(!!f.in_place)}>
        <div class="rp-file-from mono" title=${`${f.remote}:${f.path}`}><${CloudName} remotes=${remotes} name=${f.remote} /> <span>${f.path}</span></div>
        <div class="rp-file-to">
          ${f.in_place ? html`<span class="muted">${t('rules.files.inPlace')}</span>`
    : html`<span class="mono rp-old" title=${t('rules.files.activeTo')}>${f.active_target || '–'}</span> <${Icon} name="arrow" size=${11} />
            <span class="mono rp-new" data-testid="rules-file-target">${f.target}</span>`}
          <span class="badge badge-new">${ruleLabel(f.rule)}</span>
          ${f.unit && html`<span class="badge badge-readonly" title=${t('rules.files.unitTip')}>${t('rules.files.unit', { unit: f.unit })}</span>`}
          <button class="btn btn-tiny" data-testid="rules-file-explain" onClick=${() => explain(f)}>${t('rules.explain.button')}</button></div></li>`)}
      </ul>
      ${pv.files.length === 0 && !pv.busy && html`<p class="empty-hint" data-testid="rules-files-none">${t('rules.files.none')}</p>`}
      ${pv.files.length < pv.total && html`<div class="ex-more"><button class="btn" data-testid="rules-files-more" disabled=${pv.busy} onClick=${pv.more}>
        ${pv.busy ? t('common.loading') : t('rules.files.more', { n: fmtNum(pv.total - pv.files.length) })}</button></div>`}
    </section>`}
  </div>`;
}

// ------------------------------------------------------------------ the activation dialog

/**
 * Activating a version writes its exact text to the active file. The dialog names the file, shows the diff against the
 * rules in force and how many files would change destination, and sends the SHA-256 of the active file it showed; if
 * that file changed meanwhile (another tab, an editor, the CLI) the server refuses and the dialog shows the new diff.
 */
function ActivateDialog({ id, onClose, onDone }) {
  const ctx = useContext(AppCtx);
  const [st, setSt] = useState({ loading: true, version: null, state: null, error: null });
  const [count, setCount] = useState({ status: 'working', value: null, approx: false });
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState(null);
  const seq = useRef(0);

  // Focus can be on the page body (the clicked button is gone, or the person clicked the page); Escape must still close
  // the dialog, as in every other Modal. Inside the dialog Modal handles it and stops the event, so this never runs twice.
  // An Escape that cancels an IME composition belongs to the composition. Busy blocks it like `dismissable` does.
  const keyRef = useRef(null);
  keyRef.current = { busy, onClose };
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'Escape' || e.isComposing || e.keyCode === 229 || e.defaultPrevented) return;
      const k = keyRef.current;
      if (!k.busy) k.onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  async function load() {
    seq.current += 1;
    const mine = seq.current;
    setSt((s) => ({ ...s, loading: true, error: null }));
    setCount({ status: 'working', value: null, approx: false });
    try {
      const [version, state] = await Promise.all([api.ruleVersion(id), api.rules()]);
      if (mine !== seq.current) return;
      setSt({ loading: false, version: unwrap(version), state, error: null });
      countChanged(mine);
    } catch (e) {
      if (mine === seq.current) setSt({ loading: false, version: null, state: null, error: e });
    }
  }
  async function countChanged(mine) {
    try {
      const r = await api.previewRules({ version_id: id, mode: 'exact', limit: 1 });
      if (mine === seq.current) setCount({ status: 'done', value: r.changed, approx: false });
    } catch (e) {
      if (e.code !== 'PREVIEW_TOO_LARGE') { if (mine === seq.current) setCount({ status: 'failed', value: null, approx: false }); return; }
      try {
        const r = await api.previewRules({ version_id: id, limit: 1 });
        if (mine === seq.current) setCount({ status: 'done', value: r.sampled ? r.estimate_changed : r.changed, approx: !!r.sampled });
      } catch (e2) { if (mine === seq.current) setCount({ status: 'failed', value: null, approx: false }); }
    }
  }
  useEffect(() => { load(); return () => { seq.current += 1; }; }, [id]);

  const v = st.version;
  const active = st.state ? st.state.active : null;
  const info = activeInfo(st.state);
  const invalid = !!v && v.ok === false;

  async function confirm() {
    setBusy(true);
    setFailure(null);
    try {
      await api.activateRules(id, { base_sha256: active.sha256 });
      ctx.toast('success', t('rules.activated', { id }));
      onDone();
    } catch (e) {
      setFailure(e);
      if (e.code === 'RULES_ACTIVE_CHANGED') load();
    } finally {
      setBusy(false);
    }
  }
  const retired = !!info.id && v && v.id === info.id;
  return html`<${Modal} label=${t('rules.activate.title', { id })} onClose=${onClose} testid="rules-activate-dialog" wide dismissable=${!busy}>
    <header class="modal-head"><h2>${t('rules.activate.title', { id })}</h2><span class="spacer"></span>
      <button class="icon-btn" aria-label=${t('common.close')} onClick=${onClose}><${Icon} name="x" size=${16} /></button></header>
    <div class="modal-body ra-body">
      ${st.loading && !v && html`<p class="empty-hint"><span class="spinner"></span> ${t('common.loading')}</p>`}
      ${st.error && html`<p class="view-error" role="alert">${rulesErrorText(st.error)}</p>`}
      ${v && active && html`
        <p data-testid="rules-activate-file">${info.file
    ? t('rules.activate.writes', { path: info.file })
    : t('rules.activate.noFile')}</p>
        <p class="ra-meta"><${ChangeChips} changes=${v.changes} />
          <span data-testid="rules-activate-count" data-status=${count.status}>${count.status === 'working'
    ? html`<span class="spinner" aria-hidden="true"></span> ${t('rules.activate.counting')}`
    : count.status === 'done' ? t('rules.activate.changed', { n: `${count.approx ? '≈ ' : ''}${fmtNum(count.value)}` })
      : t('rules.activate.countFailed')}</span></p>
        ${retired && html`<p class="note">${t('rules.activate.alreadyActive')}</p>`}
        ${invalid && html`<p class="pl-error" role="alert" data-testid="rules-activate-invalid">${t('err.RULES_INVALID')}</p>`}
        <${DiffView} diff=${v.diff} testid="rules-activate-diff" />
        <p class="pl-help">${t('rules.activate.unchanged')}</p>`}
      ${failure && html`<div class="ra-error" role="alert" data-testid="rules-activate-error" data-code=${failure.code}>
        <p>${rulesErrorText(failure)}</p>
        ${failure.code === 'RULES_INVALID' && ((failure.params && failure.params.errors) || []).length > 0 && html`<ul>${failure.params.errors.map((x, i) =>
    html`<li key=${i}>${problemText(x)}</li>`)}</ul>`}
        ${failure.code === 'RULES_ACTIVE_CHANGED' && html`<p class="note">${t('rules.activate.reloaded')}</p>`}
        ${failure.code === 'RULES_FILE_UNWRITABLE' && v && html`<p class="note">${t('rules.activate.unwritable')}</p>
          <div class="cli-line"><code>drivemerge rules activate ${id}</code></div>
          <button class="btn btn-small" data-testid="rules-activate-copy-text" onClick=${() => copyText(ctx, v.text)}><${Icon} name="copy" size=${13} /> ${t('rules.activate.copyText')}</button>`}
      </div>`}
      ${!api.hasCsrf() && html`<p class="note error" data-testid="no-csrf">${t('prev.noCsrf')}</p>`}
    </div>
    <footer class="modal-foot">
      <button class="btn" data-testid="rules-activate-cancel" disabled=${busy} onClick=${onClose}>${t('common.cancel')}</button>
      <button class="btn btn-primary" data-testid="rules-activate-confirm" disabled=${busy || st.loading || !v || invalid || retired || !api.hasCsrf()} onClick=${confirm}>
        ${busy && html`<span class="spinner" aria-hidden="true"></span>`} ${t('rules.activate.go')}</button>
    </footer>
  <//>`;
}

// ------------------------------------------------------------------ the overview

async function newDraft(ctx, text, extra = {}) {
  const v = unwrap(await api.createRuleVersion({ text, ...extra }));
  ctx.refreshRules();
  ctx.nav(rulesHash(`/edit/${v.id}`));
  return v;
}

function RulesOverview() {
  const ctx = useContext(AppCtx);
  const [s, setS] = useState({ loaded: false, data: null, error: null });
  const [activating, setActivating] = useState(null);
  const [busy, setBusy] = useState(false);
  const seq = useRef(0);
  const readOnly = !approvalsOn(ctx);

  async function load() {
    seq.current += 1;
    const mine = seq.current;
    try {
      const data = await api.rules();
      if (mine === seq.current) setS({ loaded: true, data, error: null });
    } catch (e) {
      if (mine === seq.current) setS((x) => ({ ...x, loaded: true, error: e }));
    }
  }
  useEffect(() => { load(); return () => { seq.current += 1; }; }, []);
  const proposalsKey = ctx.rules.proposals.map((p) => p.id).join(',');
  const first = useRef(true);
  useEffect(() => { if (first.current) { first.current = false; return; } load(); }, [proposalsKey]);

  const data = s.data;
  const info = activeInfo(data);
  const a = info.a;
  const proposals = data ? listOf(data.proposals, 'versions') : [];
  const drafts = data ? listOf(data.drafts, 'versions') : [];
  const history = data ? listOf(data.history, 'versions') : [];
  const invalid = !!a.error;

  async function guarded(fn) {
    if (busy) return;
    setBusy(true);
    try { await fn(); } catch (e) { ctx.toast('error', rulesErrorText(e)); } finally { setBusy(false); }
  }
  const fromActive = () => guarded(async () => {
    let text = a.text;
    if (!text) text = (await api.rulesDefault()).text;
    await newDraft(ctx, text, a.version_id ? { base_id: a.version_id } : {});
  });
  const fromDefaults = () => guarded(async () => { await newDraft(ctx, (await api.rulesDefault()).text); });
  const fromVersion = (v) => guarded(async () => {
    const full = unwrap(await api.ruleVersion(v.id));
    await newDraft(ctx, full.text, { base_id: v.id });
  });
  const discard = (v) => guarded(async () => {
    const ok = await ctx.confirm({ title: t('rules.draft.deleteTitle', { id: v.id }), message: t('rules.draft.deleteText'), okLabel: t('rules.draft.delete'), danger: true });
    if (!ok) return;
    await api.discardRuleVersion(v.id);
    await load();
  });
  const usePlan = (kind, version) => ctx.nav(`#/plans/new/${kind}` + (version ? `?rules=${version}` : ''));
  const done = () => { setActivating(null); load(); ctx.refreshRules(); };

  return html`<main class="view view-rules" data-testid="view-rules" aria-label=${t('nav.rules')}>
    <${ViewHeader} title=${t('nav.rules')} hint=${t('rules.hint')}>
      <button class="btn btn-small" data-testid="rules-new-active" disabled=${busy || !data} onClick=${fromActive}>${t('rules.newActive')}</button>
      <button class="btn btn-small" data-testid="rules-new-default" disabled=${busy} onClick=${fromDefaults}>${t('rules.newDefault')}</button>
    <//>
    <div class="view-body">
      ${s.error && !data && html`<p class="view-error" role="alert" data-testid="rules-error">${rulesErrorText(s.error)}</p>`}
      ${!s.loaded && html`<p class="empty-hint">${t('common.loading')}</p>`}
      ${data && html`
      <section class="ov-panel rl-active" data-testid="rules-active" data-source=${a.source || ''} data-owned=${String(!!a.owned)} aria-labelledby="rl-active-h">
        <div class="ov-panel-head"><h2 id="rl-active-h">${t('rules.active')}</h2></div>
        <p class="rl-source" data-testid="rules-active-source">${a.source === 'default' || !a.path
    ? html`<span>${t('rules.src.default')}</span>`
    : html`<code class="mono">${a.path}</code>${info.id !== null ? html` · <span>v${info.id}</span>` : ''}${info.count !== null && info.count !== undefined
      ? html` · <span>${t('rules.count', { n: info.count })}</span>` : ''}${info.since ? html` · <span title=${fmtDateTime(info.since)}>${info.by ? t('rules.since', {
        when: ago(info.since), by: whoName(info.by),
      }) : t('rules.sinceOnly', { when: ago(info.since) })}</span>` : ''}`}</p>
        ${a.owned && a.source === 'file' && html`<p class="pl-help">${t('rules.ownedFile')}</p>`}
        ${info.outside && !invalid && html`<p class="pl-warn" data-testid="rules-outside-change"><${Icon} name="alert" size=${14} />
          ${t('rules.outside', { when: ago(info.since), id: info.id })}</p>`}
        ${invalid && html`<p class="view-error rl-invalid" role="alert" data-testid="rules-active-invalid" data-code=${a.error.code}>
          <${Icon} name="alert" size=${14} /> ${t('rules.invalid')} ${(t(`rulecheck.${a.error.code}`, null, '') ? problemText(a.error) : errorText({ code: a.error.code, params: a.error.params }))}</p>`}
        <div class="rl-actions">
          <button class="btn btn-small" data-testid="rules-open-editor" disabled=${busy} onClick=${fromActive}>${t('rules.openEditor')}</button>
          <button class="btn btn-small" data-testid="rules-use-consolidate" disabled=${invalid} onClick=${() => usePlan('consolidate')}>
            <${Icon} name="merge" size=${14} /> ${t('rules.useConsolidate')}</button>
          <button class="btn btn-small" data-testid="rules-use-reorganize" disabled=${invalid} onClick=${() => usePlan('reorganize')}>
            <${Icon} name="folder" size=${14} /> ${t('rules.useReorganize')}</button>
        </div>
      </section>

      <section class="ov-panel" data-testid="rules-proposals" aria-labelledby="rl-prop-h">
        <div class="ov-panel-head"><h2 id="rl-prop-h">${t('rules.proposals')} <span class="count-pill" data-testid="rules-proposals-count">${proposals.length}</span></h2></div>
        <p class="pl-help">${t('rules.proposalsHelp')}</p>
        ${proposals.length === 0 && html`<p class="empty-hint" data-testid="rules-proposals-none">${t('rules.proposalsNone')}</p>`}
        <ul class="rl-list">${proposals.map((v) => html`<li key=${v.id} class="rl-row" data-testid=${'rules-proposal-row-' + v.id} data-id=${v.id}>
          <a class="rl-row-main" href=${rulesHash(`/v/${v.id}`)} onClick=${linkTo(ctx, rulesHash(`/v/${v.id}`))}>
            <span class="rl-id">v${v.id}</span><${Who} by=${v.created_by} />
            <span class="rl-title">${v.title || t('rules.untitled')}</span>
            <${ChangeChips} changes=${v.changes} />
            <span class="muted rl-when" title=${fmtDateTime(v.created_at)}>${ago(v.created_at)}</span>
            <${Icon} name="chevron" size=${14} /></a></li>`)}</ul>
      </section>

      <section class="ov-panel" data-testid="rules-drafts" aria-labelledby="rl-draft-h">
        <div class="ov-panel-head"><h2 id="rl-draft-h">${t('rules.drafts')} <span class="count-pill">${drafts.length}</span></h2></div>
        ${drafts.length === 0 && html`<p class="empty-hint" data-testid="rules-drafts-none">${t('rules.draftsNone')}</p>`}
        <ul class="rl-list">${drafts.map((v) => html`<li key=${v.id} class="rl-row" data-testid=${'rules-draft-row-' + v.id} data-id=${v.id}>
          <a class="rl-row-main" href=${rulesHash(`/edit/${v.id}`)} onClick=${linkTo(ctx, rulesHash(`/edit/${v.id}`))}>
            <span class="rl-id">v${v.id}</span><span class="rl-title">${v.title || t('rules.untitled')}</span>
            ${v.ok === false && html`<span class="badge badge-conflict" title=${t('rules.draft.hasErrors')}>${t('rules.draft.errors')}</span>`}
            <span class="muted rl-when" title=${fmtDateTime(v.updated_at)}>${t('rules.edited', { when: ago(v.updated_at) })}</span>
            <${Icon} name="chevron" size=${14} /></a>
          <button class="icon-btn" aria-label=${t('rules.draft.delete')} title=${t('rules.draft.delete')} data-testid=${'rules-draft-delete-' + v.id}
            disabled=${busy} onClick=${() => discard(v)}><${Icon} name="trash" size=${14} /></button></li>`)}</ul>
      </section>

      <section class="ov-panel" data-testid="rules-history" aria-labelledby="rl-hist-h">
        <div class="ov-panel-head"><h2 id="rl-hist-h">${t('rules.history')}</h2></div>
        ${history.length === 0 && html`<p class="empty-hint" data-testid="rules-history-none">${t('rules.historyNone')}</p>`}
        <ul class="rl-list">${history.map((v) => html`<li key=${v.id} class="rl-row" data-testid=${'rules-history-row-' + v.id} data-id=${v.id} data-status=${v.status}>
          <a class="rl-row-main" href=${rulesHash(`/v/${v.id}`)} onClick=${linkTo(ctx, rulesHash(`/v/${v.id}`))}>
            <span class="rl-id">v${v.id}</span><${StatusChip} status=${v.status} />
            <span class="rl-title">${v.title || ''}</span>
            <span class="muted">${whoName(v.activated_by || v.created_by)}</span>
            <span class="muted rl-when" title=${fmtDateTime(v.activated_at || v.created_at)}>${ago(v.activated_at || v.created_at)}</span></a>
          <span class="rl-row-actions">
            ${v.status === 'retired' && (readOnly
    ? html`<span class="rl-cmd" data-testid=${'rules-restore-' + v.id}><code>drivemerge rules activate ${v.id}</code>
                <button class="icon-btn" aria-label=${t('common.copy')} title=${t('common.copy')} onClick=${() => copyText(ctx, `drivemerge rules activate ${v.id}`)}><${Icon} name="copy" size=${13} /></button></span>`
    : html`<button class="btn btn-tiny" data-testid=${'rules-restore-' + v.id} onClick=${() => setActivating(v.id)}>${t('rules.restore')}</button>`)}
            <button class="btn btn-tiny" data-testid=${'rules-history-edit-' + v.id} disabled=${busy} onClick=${() => fromVersion(v)}>${t('rules.openEditor')}</button>
          </span></li>`)}</ul>
      </section>`}
    </div>
    ${activating !== null && html`<${ActivateDialog} id=${activating} onClose=${() => setActivating(null)} onDone=${done} />`}
  </main>`;
}

// ------------------------------------------------------------------ the insert form

function InsertRuleForm({ outline, after: start, remotes, onClose, onSubmit }) {
  const [f, setF] = useState({
    name: '', unit: false, categories: [], ext: '', path_glob: '', name_regex: '', min_size: '', max_size: '', clouds: [], target: '',
  });
  const [after, setAfter] = useState(start);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const target = useRef(null);
  const set = (patch) => setF((x) => ({ ...x, ...patch }));
  const toggle = (key, value) => set({ [key]: f[key].includes(value) ? f[key].filter((x) => x !== value) : [...f[key], value] });
  const sizeBad = (v) => v.trim() !== '' && parseSize(v) === null;
  const example = renderTarget(f.target, exampleFile((remotes[0] && remotes[0].name) || 'gdrive'));
  const chip = (name) => {
    const el = target.current;
    const at = el ? el.selectionStart : f.target.length;
    const end = el ? el.selectionEnd : at;
    set({ target: `${f.target.slice(0, at)}{${name}}${f.target.slice(end)}` });
    if (el) el.focus();
  };

  function rule() {
    const r = { name: f.name.trim(), target: f.target.trim() };
    if (f.unit) r.unit = true;
    if (f.categories.length) r.category = f.categories;
    const ext = f.ext.split(/[\s,]+/).map((x) => x.replace(/^\./, '').toLowerCase()).filter(Boolean);
    if (ext.length) r.ext = ext;
    if (f.path_glob.trim()) r.path_glob = f.path_glob.trim();
    if (f.name_regex.trim()) r.name_regex = f.name_regex.trim();
    if (f.min_size.trim()) r.min_size = parseSize(f.min_size);
    if (f.max_size.trim()) r.max_size = parseSize(f.max_size);
    if (f.clouds.length) r.remote = f.clouds;
    return r;
  }
  const valid = f.name.trim() && f.target.trim() && !sizeBad(f.min_size) && !sizeBad(f.max_size);
  async function submit(e) {
    e.preventDefault();
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    try { await onSubmit(after, rule()); } catch (err) { setError(err); setBusy(false); }
  }

  return html`<${Modal} label=${t('rules.insert.title')} onClose=${onClose} testid="rules-insert-form" initialFocus="input">
    <form class="dialog ri" onSubmit=${submit}>
      <h2 class="dialog-title">${t('rules.insert.title')}</h2>
      <p class="pl-help">${t('rules.insert.help')}</p>
      <label class="field"><span>${t('rules.insert.after')}</span>
        <select class="select" data-testid="rules-insert-after" value=${after} onChange=${(e) => setAfter(Number(e.target.value))}>
          <option value="0">${t('rules.insert.top')}</option>
          ${outline.map((it) => html`<option key=${it.index} value=${it.index}>${t('rules.insert.afterRule', { n: it.index, name: it.name })}</option>`)}</select></label>
      <label class="field"><span>${t('rules.insert.name')}</span>
        <input type="text" data-testid="rules-insert-name" value=${f.name} autocomplete="off" spellcheck="false" onInput=${(e) => set({ name: e.target.value })} /></label>
      <fieldset class="pl-radios"><legend>${t('rules.insert.kind')}</legend>
        <label class="check"><input type="radio" name="ri-kind" data-testid="rules-insert-loose" checked=${!f.unit} onChange=${() => set({ unit: false })} /> ${t('rules.insert.loose')}</label>
        <label class="check"><input type="radio" name="ri-kind" data-testid="rules-insert-unit" checked=${f.unit} onChange=${() => set({ unit: true })} /> ${t('rules.insert.unit')}</label>
      </fieldset>
      <fieldset class="pl-radios"><legend>${t('rules.insert.categories')}</legend>
        <div class="ri-checks">${CATEGORIES.map((c) => html`<label key=${c} class="check"><input type="checkbox" data-testid=${'rules-insert-cat-' + c}
          checked=${f.categories.includes(c)} onChange=${() => toggle('categories', c)} /> ${t(`q.cat.${c}`, null, c)}</label>`)}</div>
      </fieldset>
      <label class="field"><span>${t('rules.insert.ext')}</span>
        <input type="text" data-testid="rules-insert-ext" value=${f.ext} placeholder="pdf, docx" autocomplete="off" spellcheck="false" onInput=${(e) => set({ ext: e.target.value })} /></label>
      <label class="field"><span>${t('rules.insert.glob')}</span>
        <input type="text" data-testid="rules-insert-glob" value=${f.path_glob} placeholder="*/Scans/*" autocomplete="off" spellcheck="false" onInput=${(e) => set({ path_glob: e.target.value })} /></label>
      <label class="field"><span>${t('rules.insert.regex')}</span>
        <input type="text" data-testid="rules-insert-regex" value=${f.name_regex} placeholder="(?i)^receipt" autocomplete="off" spellcheck="false" onInput=${(e) => set({ name_regex: e.target.value })} /></label>
      <div class="ri-sizes">
        <label class="field"><span>${t('rules.insert.minSize')}</span>
          <input type="text" data-testid="rules-insert-min" value=${f.min_size} placeholder="10M" aria-invalid=${sizeBad(f.min_size)} autocomplete="off" onInput=${(e) => set({ min_size: e.target.value })} /></label>
        <label class="field"><span>${t('rules.insert.maxSize')}</span>
          <input type="text" data-testid="rules-insert-max" value=${f.max_size} placeholder="1G" aria-invalid=${sizeBad(f.max_size)} autocomplete="off" onInput=${(e) => set({ max_size: e.target.value })} /></label>
      </div>
      ${remotes.length > 0 && html`<fieldset class="pl-radios"><legend>${t('rules.insert.clouds')}</legend>
        <div class="ri-checks">${remotes.map((r) => html`<label key=${r.name} class="check"><input type="checkbox" data-testid=${'rules-insert-cloud-' + r.name}
          checked=${f.clouds.includes(r.name)} onChange=${() => toggle('clouds', r.name)} /> ${r.name}</label>`)}</div></fieldset>`}
      <label class="field"><span>${t('rules.insert.target')}</span>
        <input type="text" ref=${target} data-testid="rules-insert-target" value=${f.target} placeholder="Receipts/{year}/{name}" autocomplete="off" spellcheck="false"
          onInput=${(e) => set({ target: e.target.value })} /></label>
      <div class="ri-chips" aria-label=${t('rules.insert.variables')}>${TEMPLATE_VARS.map((v) => html`<button key=${v} type="button" class="btn btn-tiny"
        data-testid=${'rules-insert-var-' + v} onClick=${() => chip(v)}>{${v}}</button>`)}</div>
      <p class="ri-example" data-testid="rules-insert-example">${f.target.trim()
    ? html`<span class="muted">${t('rules.insert.example')}</span> <span class="mono">Photos/Camera/IMG_20190304_1021.jpg</span> <${Icon} name="arrow" size=${11} />
        <span class="mono" data-testid="rules-insert-rendered">${example.text}</span>${example.unknown.length > 0 && html` <span class="pl-error">${t('rules.insert.unknownVar', { names: example.unknown.join(', ') })}</span>`}`
    : ''}</p>
      ${error && html`<p class="note error" role="alert" data-testid="rules-insert-error">${rulesErrorText(error)}</p>`}
      <div class="dialog-actions">
        <button type="button" class="btn" data-testid="rules-insert-cancel" onClick=${onClose}>${t('common.cancel')}</button>
        <button type="submit" class="btn btn-primary" data-testid="rules-insert-submit" disabled=${!valid || busy}>
          ${busy && html`<span class="spinner" aria-hidden="true"></span>`} ${t('rules.insert.go')}</button>
      </div>
    </form>
  <//>`;
}

// ------------------------------------------------------------------ the editor

const emptyScope = { remote: '', path: '', root: '' };

function RuleEditor({ id, route }) {
  const ctx = useContext(AppCtx);
  const remotes = ctx.config.remotes || [];
  const readOnly = !approvalsOn(ctx);
  const [load, setLoad] = useState({ status: 'loading', version: null, error: null });
  const [text, setText] = useState('');
  const [title, setTitle] = useState('');
  const [saveState, setSaveState] = useState('saved');     // saved | dirty | saving | conflict | error
  const [check, setCheck] = useState(null);                 // the checker's answer, with the `text` it was made for
  const [checkFail, setCheckFail] = useState(null);
  const [scope, setScope] = useState(emptyScope);
  const [pane, setPane] = useState('text');
  const [selected, setSelected] = useState(null);
  const [request, setRequest] = useState(null);
  const [showDiff, setShowDiff] = useState(false);
  const [insertAt, setInsertAt] = useState(null);
  const [activating, setActivating] = useState(false);
  const [explainAsk, setExplainAsk] = useState(route.params.get('explain') || '');

  const textRef = useRef('');
  const titleRef = useRef('');
  const savedRef = useRef({ text: '', title: '' });
  const revisionRef = useRef(0);
  const baseRef = useRef(null);
  const versionRef = useRef(null);
  const saving = useRef(null);
  const saveTimer = useRef(null);
  const conflict = useRef(false);
  const checkSeq = useRef(0);
  const firstCheck = useRef(true);
  const editor = useRef(null);
  const explainRef = useRef(null);
  const requestId = useRef(0);
  textRef.current = text;
  titleRef.current = title;

  const isDirty = () => textRef.current !== savedRef.current.text || titleRef.current !== savedRef.current.title;
  const keep = () => {
    if (isDirty()) session('set', `dm-rules-${id}`, JSON.stringify({ text: textRef.current, title: titleRef.current, revision: revisionRef.current }));
    else session('del', `dm-rules-${id}`);
  };

  // ---- load
  async function loadDraft(forget = false) {
    try {
      const v = unwrap(await api.ruleVersion(id));
      if (v.status !== 'draft') { ctx.nav(rulesHash(`/v/${id}`), { replace: true }); return; }
      let start = v.text;
      let startTitle = v.title || '';
      let dirty = false;
      if (forget) session('del', `dm-rules-${id}`);
      else {
        let kept = null;
        try { kept = JSON.parse(session('get', `dm-rules-${id}`) || 'null'); } catch (e) { kept = null; }
        if (kept && kept.revision === v.revision && (kept.text !== v.text || kept.title !== startTitle)) { start = kept.text; startTitle = kept.title; dirty = true; }
      }
      versionRef.current = v;
      baseRef.current = v.base_id || null;
      revisionRef.current = v.revision || 0;
      savedRef.current = { text: v.text, title: v.title || '' };
      conflict.current = false;
      firstCheck.current = true;
      setText(start);
      setTitle(startTitle);
      setSaveState(dirty ? 'dirty' : 'saved');
      setLoad({ status: 'ready', version: v, error: null });
    } catch (e) {
      setLoad({ status: 'error', version: null, error: e });
    }
  }
  useEffect(() => { loadDraft(); }, [id]);
  const ready = load.status === 'ready';

  // ---- check (200 ms after the last keystroke; an answer for an older text is dropped)
  useEffect(() => {
    if (!ready) return undefined;
    checkSeq.current += 1;
    const mine = checkSeq.current;
    const delay = firstCheck.current ? 0 : CHECK_MS;
    firstCheck.current = false;
    const timer = setTimeout(async () => {
      try {
        const r = await api.checkRules(text);
        if (mine !== checkSeq.current) return;
        setCheck({ ...r, text });
        setCheckFail(null);
      } catch (e) {
        if (mine !== checkSeq.current) return;
        if (e.code && e.status === 400) {
          setCheck((c) => ({
            ok: false, errors: [{ code: e.code, params: e.params || {}, message: e.message }], warnings: [], outline: c ? c.outline : [], layout: c ? c.layout : null, diff: c ? c.diff : null, changes: [], text,
          }));
          setCheckFail(null);
        } else {
          setCheckFail(e);
        }
      }
    }, delay);
    return () => clearTimeout(timer);
  }, [text, ready]);

  // ---- autosave (2 s after typing stops), and the unsaved text kept for a reload
  async function doSave() {
    if (conflict.current) return;
    if (saving.current) { await saving.current; if (!isDirty() || conflict.current) return; }
    const body = { text: textRef.current, revision: revisionRef.current, title: titleRef.current };
    const p = (async () => {
      setSaveState('saving');
      try {
        const v = unwrap(await api.updateRuleDraft(id, body));
        revisionRef.current = v.revision !== undefined ? v.revision : revisionRef.current + 1;
        savedRef.current = { text: body.text, title: body.title };
        setSaveState(isDirty() ? 'dirty' : 'saved');
        keep();
      } catch (e) {
        if (e.code === 'RULES_DRAFT_CHANGED') { conflict.current = true; setSaveState('conflict'); } else setSaveState('error');
      }
    })();
    saving.current = p;
    try { await p; } finally { saving.current = null; }
    if (isDirty() && !conflict.current && saveTimer.current === null) saveTimer.current = setTimeout(() => { saveTimer.current = null; doSave(); }, SAVE_MS);
  }
  useEffect(() => {
    if (!ready) return undefined;
    keep();
    clearTimeout(saveTimer.current);
    saveTimer.current = null;
    if (!isDirty()) { if (saveState === 'dirty') setSaveState('saved'); return undefined; }
    if (conflict.current) return undefined;
    setSaveState((s) => (s === 'saving' ? s : 'dirty'));
    saveTimer.current = setTimeout(() => { saveTimer.current = null; doSave(); }, SAVE_MS);
    return () => { clearTimeout(saveTimer.current); saveTimer.current = null; };
  }, [text, title, ready]);
  useEffect(() => () => {
    // Leaving with text that is not saved yet: send it on the way out.
    if (!conflict.current && isDirty() && versionRef.current) {
      api.updateRuleDraft(id, { text: textRef.current, revision: revisionRef.current, title: titleRef.current }).then(() => session('del', `dm-rules-${id}`), () => {});
    }
  }, []);

  const flush = async () => {
    clearTimeout(saveTimer.current);
    saveTimer.current = null;
    if (saving.current) await saving.current;
    if (isDirty() && !conflict.current) await doSave();
    return !isDirty() && !conflict.current;
  };

  async function reloadFromServer() { setLoad({ status: 'loading', version: null, error: null }); await loadDraft(true); }
  async function saveAsNew() {
    try {
      const v = unwrap(await api.createRuleVersion({ text: textRef.current, ...(baseRef.current ? { base_id: baseRef.current } : {}), ...(titleRef.current ? { title: titleRef.current } : {}) }));
      session('del', `dm-rules-${id}`);
      conflict.current = false;
      savedRef.current = { text: textRef.current, title: titleRef.current };
      ctx.refreshRules();
      ctx.nav(rulesHash(`/edit/${v.id}`));
    } catch (e) { ctx.toast('error', rulesErrorText(e)); }
  }

  // ---- derived from the check
  const current = check && check.text === text ? check : null;
  const shown = check || { ok: true, errors: [], warnings: [], outline: [], layout: null, diff: null, changes: [] };
  const errors = (current || { errors: [] }).errors;
  const warnings = (current || { warnings: [] }).warnings;
  const wordedErrors = errors.map((e) => ({ ...e, line: problemLine(e), text: problemText(e) }));
  const wordedWarnings = warnings.map((w) => ({ ...w, line: problemLine(w), text: problemText(w, 'rulewarn') }));
  const errorRules = new Set(errors.map((e) => (e.params && e.params.rule_index) || 0).filter(Boolean));
  const checkReady = !!current && current.ok;

  const pv = useRulePreview({ source: { text }, scope, ready: ready && checkReady });
  const rows = previewRows(pv.data);
  const outlineRows = {};
  Object.entries(rows).forEach(([k, r]) => { outlineRows[k] = { text: r.text }; });

  const goto = (line) => { requestId.current += 1; setRequest({ id: requestId.current, line, scrollTop: null }); setPane('text'); };
  const onCursor = (line) => {
    const it = itemAtLine(shown.outline, line);
    if (it) setSelected(it.index);
  };

  function applyText(next, line, scrollTop) {
    setText(next);
    if (line) { requestId.current += 1; setRequest({ id: requestId.current, line, scrollTop }); }
  }
  function transformed(next, newCheck, op, from, label, lineHint) {
    const scrollTop = editor.current ? editor.current.scrollTopNow() : null;
    const outline = (newCheck && newCheck.outline) || [];
    const at = op.op === 'move' ? op.to : op.op === 'insert' ? op.after + 1 : null;
    const target = at ? outline.find((x) => x.index === at) : null;
    applyText(next, target ? target.line : lineHint, scrollTop);
    if (newCheck) setCheck({ ...newCheck, text: next });
    if (target) setSelected(target.index); else if (op.op === 'remove') setSelected(null);
    ctx.toast('info', label, { label: t('rules.undo'), run: () => applyText(from, null, null) });
  }
  const onOutline = (next, meta) => {
    const op = meta.op;
    const it = (shown.outline || []).find((x) => x.index === op.index);
    const label = op.op === 'move' ? t('rules.moved', { name: it ? it.name : '', n: op.to }) : t('rules.removed', { name: it ? it.name : '' });
    transformed(next, meta.check, op, meta.from, label, it ? it.line : 1);
  };
  async function submitInsert(after, rule) {
    const r = await api.transformRules(textRef.current, { op: 'insert', after, rule });
    setInsertAt(null);
    transformed(r.text, r.check, { op: 'insert', after }, textRef.current, t('rules.inserted', { name: rule.name }), 1);
  }

  // ---- header actions
  const base = baseRef.current;
  const back = (() => { const b = route.params.get('back'); return b && /^#\/plans(\/|\?|$)/.test(b) ? withParam(b, 'rules', id) : ''; })();
  async function usePlan(kind) {
    const ok = await flush();
    if (!ok) { ctx.toast('error', t('rules.saveFirst')); return; }
    const q = new URLSearchParams({ rules: String(id) });
    if (scope.root) q.set('root', scope.root);
    if (scope.path) q.set('path', scope.path);
    if (kind === 'reorganize' && scope.remote) q.set('remote', scope.remote);
    ctx.nav(`#/plans/new/${kind}?${q}`);
  }
  async function activate() {
    const ok = await flush();
    if (!ok) { ctx.toast('error', t('rules.saveFirst')); return; }
    setActivating(true);
  }

  if (load.status === 'loading') return html`<main class="view view-rules" data-testid="view-rules-edit" data-state="loading"><div class="center-msg"><span class="spinner"></span> ${t('common.loading')}</div></main>`;
  if (load.status === 'error') {
    return html`<main class="view view-rules" data-testid="view-rules-edit" data-state="error">
      <${ViewHeader} title=${t('rules.editor.title', { id })}><button class="btn btn-small" onClick=${() => ctx.nav(rulesHash())}>${t('rules.back')}</button><//>
      <div class="view-body"><p class="view-error" role="alert" data-testid="rules-edit-error">${load.error.status === 404 ? t('rules.notFound', { id }) : rulesErrorText(load.error)}
        <button class="link" onClick=${() => loadDraft()}>${t('common.retry')}</button></p></div></main>`;
  }

  const stateText = { saved: t('rules.save.saved'), dirty: t('rules.save.dirty'), saving: t('rules.save.saving'), conflict: t('rules.save.conflict'), error: t('rules.save.error') }[saveState];
  const errorCount = errors.length;
  return html`<main class="view view-rules view-rules-edit" data-testid="view-rules-edit" data-id=${id} aria-label=${t('rules.editor.title', { id })}>
    <${ViewHeader} title=${t('rules.editor.title', { id })} hint=${base ? t('rules.editor.from', { id: base }) : ''}>
      ${back && html`<a class="btn btn-small" href=${back} data-testid="rules-back" onClick=${linkTo(ctx, back)}>${t('rules.backToForm')}</a>`}
      <button class="btn btn-small" data-testid="rules-back-top" onClick=${() => ctx.nav(rulesHash())}>${t('rules.back')}</button>
    <//>
    <div class="re-bar">
      <label class="visually-hidden" for="re-title">${t('rules.titleLabel')}</label>
      <input id="re-title" type="text" class="re-title" data-testid="rules-title" value=${title} placeholder=${t('rules.titlePlaceholder')} maxlength="200"
        autocomplete="off" onInput=${(e) => setTitle(e.target.value)} />
      <span class=${'re-state re-state-' + saveState} data-testid="rules-save-state" data-state=${saveState} role="status">${stateText}</span>
      ${checkFail && html`<span class="pl-error" data-testid="rules-check-failed">${t('rules.checkFailed')}</span>`}
      <span class="spacer"></span>
      <button class=${'btn btn-small' + (showDiff ? ' on' : '')} data-testid="rules-diff-toggle" aria-pressed=${showDiff} onClick=${() => setShowDiff(!showDiff)}>${t('rules.diff')}</button>
      <${UsePlanMenu} onPick=${usePlan} />
      ${!readOnly && html`<button class="btn btn-primary btn-small" data-testid="rules-activate" disabled=${!checkReady || saveState === 'conflict'} onClick=${activate}
        title=${checkReady ? '' : t('rules.activate.fixFirst')}>${t('rules.activate.button')}</button>`}
    </div>
    ${readOnly && html`<div class="re-cli"><${CliCommand} id=${id} flush=${flush} note=${isDirty() ? t('rules.cli.saveFirst') : ''} /></div>`}
    ${saveState === 'conflict' && html`<div class="banner banner-warn re-conflict" role="alert" data-testid="rules-draft-conflict"><${Icon} name="alert" size=${16} />
      <span>${t('err.RULES_DRAFT_CHANGED', { version: id, revision: revisionRef.current })}</span>
      <button class="btn btn-small" data-testid="rules-conflict-reload" onClick=${reloadFromServer}>${t('rules.conflict.reload')}</button>
      <button class="btn btn-small" data-testid="rules-conflict-new" onClick=${saveAsNew}>${t('rules.conflict.saveNew')}</button></div>`}
    ${showDiff && html`<section class="re-diff ov-panel"><h3 class="section-title">${t('rules.diff.title')}</h3>
      <${DiffView} diff=${shown.diff} /></section>`}
    <div class="re-tabs" role="tablist" aria-label=${t('rules.editor.panes')}>
      ${['outline', 'text', 'preview'].map((p) => html`<button key=${p} role="tab" class=${pane === p ? 'on' : ''} aria-selected=${pane === p}
        data-testid=${'rules-pane-tab-' + p} onClick=${() => setPane(p)}>${t(`rules.pane.${p}`)}${p === 'text' && errorCount > 0 ? html` <span class="count-pill count-pill-warn">${errorCount}</span>` : ''}</button>`)}
    </div>
    <div class="re-panes" data-pane=${pane}>
      <section class="re-pane re-pane-outline" aria-label=${t('rules.pane.outline')}>
        <h3 class="re-pane-h">${t('rules.pane.outline')}</h3>
        <${Outline} text=${text} outline=${shown.outline} fallback=${shown.layout ? shown.layout.fallback : null} rows=${outlineRows} changes=${shown.changes}
          selected=${selected} errorsByRule=${errorRules} onSelect=${setSelected} onGoto=${goto}
          onText=${(next, meta) => onOutline(next, meta)} onError=${(e) => ctx.toast('error', rulesErrorText(e))}
          onInsert=${(index) => setInsertAt(index)} disabled=${!current || !current.ok} />
      </section>
      <section class="re-pane re-pane-text" aria-label=${t('rules.pane.text')}>
        <h3 class="re-pane-h">rules.toml</h3>
        <${RuleTextEditor} text=${text} onChange=${setText} errors=${wordedErrors} warnings=${wordedWarnings} request=${request} onCursor=${onCursor} handle=${editor} />
        <div class="re-problems">
          ${wordedErrors.length > 0 && html`<ul class="re-list re-errors" data-testid="rules-errors">${wordedErrors.map((e, i) => html`<li key=${i} class="re-problem re-error"
            data-testid=${'rules-error-' + (i + 1)} data-code=${e.code}><${Icon} name="alert" size=${14} />
            <span class="re-problem-text">${e.text}</span><code class="code-pill">${e.code}</code>
            ${e.line && html`<button class="link" data-testid=${'rules-goto-' + e.line} onClick=${() => goto(e.line)}>${t('rules.gotoLine', { line: e.line })}</button>`}</li>`)}</ul>`}
          ${wordedWarnings.length > 0 && html`<ul class="re-list re-warnings" data-testid="rules-warnings">${wordedWarnings.map((w, i) => html`<li key=${i} class="re-problem re-warning"
            data-testid=${'rules-warning-' + (i + 1)} data-code=${w.code}><${Icon} name="info" size=${14} />
            <span class="re-problem-text">${w.text}</span><code class="code-pill">${w.code}</code>
            ${w.line && html`<button class="link" data-testid=${'rules-goto-w-' + w.line} onClick=${() => goto(w.line)}>${t('rules.gotoLine', { line: w.line })}</button>`}</li>`)}</ul>`}
          ${current && current.ok && wordedWarnings.length === 0 && html`<p class="re-ok" data-testid="rules-check-ok"><${Icon} name="check" size=${14} /> ${t('rules.check.ok', { n: (current.outline || []).length })}</p>`}
        </div>
      </section>
      <section class="re-pane re-pane-preview" aria-label=${t('rules.pane.preview')}>
        <h3 class="re-pane-h">${t('rules.pane.preview')}</h3>
        <${PreviewPane} pv=${pv} scope=${scope} setScope=${setScope} source=${{ text }} remotes=${remotes} explainInitial=${explainAsk}
          onExplainDone=${() => setExplainAsk('')} explainRef=${explainRef} />
      </section>
    </div>
    ${insertAt !== null && html`<${InsertRuleForm} outline=${shown.outline || []} after=${insertAt} remotes=${remotes} onClose=${() => setInsertAt(null)} onSubmit=${submitInsert} />`}
    ${activating && html`<${ActivateDialog} id=${id} onClose=${() => setActivating(false)} onDone=${() => { setActivating(false); ctx.refreshRules(); ctx.nav(rulesHash()); }} />`}
  </main>`;
}

// ------------------------------------------------------------------ the review of one version

function VersionReview({ id, route }) {
  const ctx = useContext(AppCtx);
  const remotes = ctx.config.remotes || [];
  const readOnly = !approvalsOn(ctx);
  const [st, setSt] = useState({ status: 'loading', v: null, error: null });
  const [scope, setScope] = useState(emptyScope);
  const [activating, setActivating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [explainAsk, setExplainAsk] = useState(route.params.get('explain') || '');
  const explainRef = useRef(null);

  async function load() {
    try {
      const v = unwrap(await api.ruleVersion(id));
      setSt({ status: 'ready', v, error: null });
    } catch (e) {
      setSt({ status: 'error', v: null, error: e });
    }
  }
  useEffect(() => { load(); }, [id]);
  const v = st.v;
  const pv = useRulePreview({ source: { version_id: id }, scope, ready: !!v && v.ok !== false });

  async function guarded(fn) {
    if (busy) return;
    setBusy(true);
    try { await fn(); } catch (e) { ctx.toast('error', rulesErrorText(e)); } finally { setBusy(false); }
  }
  const editAsDraft = () => guarded(async () => { await newDraft(ctx, v.text, { base_id: v.id, ...(v.title ? { title: v.title } : {}) }); });
  const dismiss = () => guarded(async () => {
    const ok = await ctx.confirm({ title: t('rules.dismiss.title', { id }), message: t('rules.dismiss.text'), okLabel: t('rules.dismiss'), danger: true });
    if (!ok) return;
    await api.discardRuleVersion(id);
    ctx.refreshRules();
    ctx.toast('success', t('rules.dismissed', { id }));
    ctx.nav(rulesHash());
  });
  const usePlan = (kind) => {
    const q = new URLSearchParams({ rules: String(id) });
    if (scope.root) q.set('root', scope.root);
    if (scope.path) q.set('path', scope.path);
    if (kind === 'reorganize' && scope.remote) q.set('remote', scope.remote);
    ctx.nav(`#/plans/new/${kind}?${q}`);
  };

  if (st.status === 'loading') return html`<main class="view view-rules" data-testid="view-rules-version" data-state="loading"><div class="center-msg"><span class="spinner"></span> ${t('common.loading')}</div></main>`;
  if (st.status === 'error') {
    return html`<main class="view view-rules" data-testid="view-rules-version" data-state="error">
      <${ViewHeader} title=${t('rules.version.title', { id })}><button class="btn btn-small" onClick=${() => ctx.nav(rulesHash())}>${t('rules.back')}</button><//>
      <div class="view-body"><p class="view-error" role="alert" data-testid="rules-version-error">${st.error.status === 404 ? t('rules.notFound', { id }) : rulesErrorText(st.error)}</p></div></main>`;
  }
  const canActivate = ['proposed', 'retired', 'draft'].includes(v.status) && v.ok !== false;
  const check = v.check || {};
  const errors = (check.errors || []);
  const warnings = (check.warnings || []);
  return html`<main class="view view-rules view-rules-version" data-testid="view-rules-version" data-id=${id} data-status=${v.status} aria-label=${t('rules.version.title', { id })}>
    <${ViewHeader} title=${t('rules.version.title', { id })} hint=${v.title || ''}>
      <button class="btn btn-small" data-testid="rules-back-top" onClick=${() => ctx.nav(rulesHash())}>${t('rules.back')}</button>
    <//>
    <div class="view-body">
      <div class="pl-facts" data-testid="rules-version-facts"><${StatusChip} status=${v.status} /><${Who} by=${v.created_by} />
        <span class="muted" title=${fmtDateTime(v.created_at)}>${ago(v.created_at)}</span><${ChangeChips} changes=${v.changes} /></div>
      ${v.created_by === 'mcp' && v.status === 'proposed' && html`<p class="pl-warn" data-testid="rules-not-active"><${Icon} name="info" size=${14} /> ${t('rules.proposalNote')}</p>`}
      ${(v.title || v.note) && html`<section class="ov-panel rv-claim" data-testid="rules-version-note">
        <h3 class="section-title">${v.created_by === 'mcp' ? t('rules.fromClaude') : t('rules.note')}</h3>
        ${v.title && html`<p class="rv-claim-title" data-testid="rules-version-title">${v.title}</p>`}
        ${v.note && html`<p class="rv-claim-note" data-testid="rules-version-notetext">${v.note}</p>`}
        ${v.created_by === 'mcp' && html`<p class="pl-help">${t('rules.untrusted')}</p>`}
      </section>`}
      <div class="rl-actions">
        ${canActivate && (readOnly ? null : html`<button class="btn btn-primary btn-small" data-testid="rules-activate" onClick=${() => setActivating(true)}>${t('rules.activate.button')}</button>`)}
        <button class="btn btn-small" data-testid="rules-edit-as-draft" disabled=${busy} onClick=${editAsDraft}>${t('rules.editAsDraft')}</button>
        <${UsePlanMenu} onPick=${usePlan} />
        ${v.status === 'proposed' && html`<button class="btn btn-small btn-danger-ghost" data-testid="rules-dismiss" disabled=${busy} onClick=${dismiss}>${t('rules.dismiss')}</button>`}
      </div>
      ${canActivate && readOnly && html`<${CliCommand} id=${id} />`}
      ${errors.length > 0 && html`<ul class="re-list re-errors" data-testid="rules-errors">${errors.map((e, i) => html`<li key=${i} class="re-problem re-error"
        data-testid=${'rules-error-' + (i + 1)}><${Icon} name="alert" size=${14} /><span class="re-problem-text">${problemText(e)}</span><code class="code-pill">${e.code}</code></li>`)}</ul>`}
      ${warnings.length > 0 && html`<ul class="re-list re-warnings" data-testid="rules-warnings">${warnings.map((w, i) => html`<li key=${i} class="re-problem re-warning"
        data-testid=${'rules-warning-' + (i + 1)}><${Icon} name="info" size=${14} /><span class="re-problem-text">${problemText(w, 'rulewarn')}</span><code class="code-pill">${w.code}</code></li>`)}</ul>`}
      <div class="rv-cols">
        <section class="ov-panel"><h3 class="section-title">${t('rules.diff.title')}</h3><${DiffView} diff=${v.diff} /></section>
        <section class="ov-panel"><h3 class="section-title">${t('rules.pane.preview')}</h3>
          <${PreviewPane} pv=${pv} scope=${scope} setScope=${setScope} source=${{ version_id: id }} remotes=${remotes} explainInitial=${explainAsk}
            onExplainDone=${() => setExplainAsk('')} explainRef=${explainRef} /></section>
      </div>
      <details class="ov-panel" data-testid="rules-version-text"><summary>${t('rules.fullText')}</summary><pre class="rv-text mono">${v.text}</pre></details>
    </div>
    ${activating && html`<${ActivateDialog} id=${id} onClose=${() => setActivating(false)} onDone=${() => { setActivating(false); ctx.refreshRules(); ctx.nav(rulesHash()); }} />`}
  </main>`;
}

// ------------------------------------------------------------------ embedded in the plan views

/**
 * The "Layout rules" field of the consolidate and reorganize forms: the active rules (and where they come from) or one
 * version of the Rules view, which may be a draft and is then not in force. `value` is a version id or ''.
 */
export function RulesField({ value, onChange, backHash }) {
  const ctx = useContext(AppCtx);
  const [active, setActive] = useState(null);
  const [version, setVersion] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.rules().then(setActive, () => setActive(null)); }, []);
  useEffect(() => {
    setVersion(null);
    if (value) api.ruleVersion(value).then((v) => setVersion(unwrap(v)), () => setVersion(null));
  }, [value]);
  const info = activeInfo(active);
  const here = () => backHash || window.location.hash;
  async function edit() {
    if (busy) return;
    setBusy(true);
    try {
      if (value && version && version.status === 'draft') {
        ctx.nav(rulesHash(`/edit/${value}`, { back: here() }));
        return;
      }
      let text = version ? version.text : info.a.text;
      if (!text) text = (await api.rulesDefault()).text;
      const base = version ? version.id : info.a.version_id;
      const v = unwrap(await api.createRuleVersion({ text, ...(base ? { base_id: base } : {}) }));
      ctx.refreshRules();
      ctx.nav(rulesHash(`/edit/${v.id}`, { back: here() }));
    } catch (e) {
      ctx.toast('error', rulesErrorText(e));
    } finally {
      setBusy(false);
    }
  }
  const isActive = !value || (version && version.status === 'active');
  return html`<div class="rf" data-testid="rules-field" data-version=${value || ''} data-active=${String(!!isActive)}>
    <span class="rf-label">${t('rules.field.title')}</span>
    <p class="rf-text" data-testid="rules-field-text">${!value
    ? t('rules.field.active', { source: active ? sourceName(active.active) : '…' })
    : version ? t('rules.field.version', { id: value, status: t(`rstatus.${version.status}`, null, version.status) }) + (version.status === 'active' ? '' : ` — ${t('rules.field.notActive')}`)
      : t('rules.field.versionLoading', { id: value })}</p>
    ${version && version.title && html`<p class="pl-help">${version.title}</p>`}
    <div class="rf-actions">
      <button type="button" class="btn btn-small" data-testid="rules-field-edit" disabled=${busy} onClick=${edit}>${t('rules.field.edit')}</button>
      ${value && html`<button type="button" class="btn btn-small" data-testid="rules-field-active" onClick=${() => onChange('')}>${t('rules.field.useActive')}</button>`}
    </div>
  </div>`;
}

/**
 * On the review of a consolidate or reorganize plan: which rules built it, and a note when they are not the active
 * ones (a draft, a proposal, rules passed inline, or rules that have been replaced since). Plans built before rules were
 * recorded have no `rules_ref` and show nothing.
 */
export function PlanRulesBox({ plan }) {
  const ref = plan && plan.params && plan.params.rules_ref;
  if (!ref) return null;
  return html`<${PlanRulesBoxBody} plan=${plan} rulesRef=${ref} />`;
}

function PlanRulesBoxBody({ plan, rulesRef: ref }) {
  const ctx = useContext(AppCtx);
  const [active, setActive] = useState(null);
  const [version, setVersion] = useState(null);
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.rules().then(setActive, () => setActive(null)); }, [plan.id]);
  useEffect(() => {
    setVersion(null);
    if (ref.version !== null && ref.version !== undefined) api.ruleVersion(ref.version).then((v) => setVersion(unwrap(v)), () => setVersion(null));
  }, [ref.version]);
  const same = active ? active.active.sha256 === ref.sha256 : null;
  const proposed = ref.source === 'version' && version && version.status === 'proposed';
  const why = proposed ? t('plan.rules.proposed', { id: ref.version })
    : ref.source === 'inline' ? t('plan.rules.inline')
      : ref.source === 'version' ? t('plan.rules.version', { id: ref.version, status: version ? t(`rstatus.${version.status}`, null, version.status) : '' })
        : t('plan.rules.replaced');
  async function edit() {
    if (busy) return;
    setBusy(true);
    try {
      const v = unwrap(await api.createRuleVersion({ text: plan.params.rules, ...(ref.version ? { base_id: ref.version } : {}), title: t('plan.rules.draftTitle', { id: plan.id }) }));
      ctx.refreshRules();
      ctx.nav(rulesHash(`/edit/${v.id}`));
    } catch (e) {
      ctx.toast('error', rulesErrorText(e));
    } finally {
      setBusy(false);
    }
  }
  return html`<section class=${'ov-panel plan-rules' + (same === false ? ' not-active' : '')} data-testid="plan-rules-box" data-same=${same === null ? '' : String(same)}
    data-source=${ref.source}>
    <div class="ov-panel-head"><h2>${t('plan.rules.title')}</h2></div>
    <p class="plan-rules-line" data-testid="plan-rules-line">${t(`rsrc.${ref.source}`, null, ref.source)}${ref.path ? html` · <code class="mono">${ref.path}</code>` : ''}${ref.version !== null && ref.version !== undefined ? html` · v${ref.version}` : ''}
      · <code class="mono" title=${ref.sha256}>${String(ref.sha256).slice(0, 8)}</code></p>
    ${same === false && html`<p class="pl-warn" data-testid="plan-rules-not-active"><${Icon} name="alert" size=${14} /> ${why}</p>`}
    ${same === true && html`<p class="pl-help" data-testid="plan-rules-active">${t('plan.rules.isActive')}</p>`}
    <div class="rl-actions">
      <button class="btn btn-small" data-testid="plan-rules-view" aria-expanded=${show} onClick=${() => setShow(!show)}>${show ? t('plan.rules.hide') : t('plan.rules.view')}</button>
      <button class="btn btn-small" data-testid="plan-rules-edit" disabled=${busy || !plan.params.rules} onClick=${edit}>${t('rules.openEditor')}</button>
    </div>
    ${show && html`<pre class="rv-text mono" data-testid="plan-rules-text">${plan.params.rules}</pre>`}
  </section>`;
}

// ------------------------------------------------------------------ the route

export function Rules({ route }) {
  if (route.page === 'edit') return html`<${RuleEditor} key=${route.id} id=${route.id} route=${route} />`;
  if (route.page === 'version') return html`<${VersionReview} key=${route.id} id=${route.id} route=${route} />`;
  return html`<${RulesOverview} />`;
}
