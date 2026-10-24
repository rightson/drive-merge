// The text side of the Rules editor (docs/RULES.md section 4, decision R2): a plain <textarea> with a line-number gutter
// whose marks show the lines that have an error or a warning, the outline whose actions are text transformations done by
// the server (move, remove, insert), and the pure helpers the editor needs (line <-> offset, a rendering of a target
// template). No third-party editor, no syntax highlighting: the text stays the source of truth.
import {
  html, useLayoutEffect, useRef, useState,
} from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import * as api from './api.js';
import { Icon } from './icons.js';
import { fmtNum } from './util.js';

/** Height of one text line in the editor and its gutter, in pixels (styles.css uses the same number). */
export const LINE_PX = 20;

// ------------------------------------------------------------------ pure helpers

/** Number of lines of a text (an empty text has one). */
export const lineCount = (text) => String(text).split('\n').length;

/** 1-based line of a character offset. */
export function lineOfOffset(text, offset) {
  let line = 1;
  const end = Math.min(offset, text.length);
  for (let i = 0; i < end; i += 1) if (text.charCodeAt(i) === 10) line += 1;
  return line;
}

/** Offset of the first character of a 1-based line (clamped to the text). */
export function offsetOfLine(text, line) {
  let at = 0;
  for (let n = 1; n < line; n += 1) {
    const next = text.indexOf('\n', at);
    if (next < 0) return text.length;
    at = next + 1;
  }
  return at;
}

/** The outline item that holds a line (its header to its last line), or null. */
export function itemAtLine(outline, line) {
  return (outline || []).find((it) => line >= it.line && line <= (it.end_line || it.line)) || null;
}

/** A file to show what a target template makes of it (the variables of classify.VARIABLES). */
export function exampleFile(remote = 'gdrive') {
  return {
    name: 'IMG_20190304_1021.jpg', stem: 'IMG_20190304_1021', ext: 'jpg', category: 'photo', year: '2019', month: '03', day: '04',
    remote, parent: 'Camera', dir: 'Photos/Camera', path: 'Photos/Camera/IMG_20190304_1021.jpg', unit: 'MyProject', relpath: 'src/app.js',
  };
}

export const TEMPLATE_VARS = ['name', 'stem', 'ext', 'category', 'year', 'month', 'day', 'remote', 'parent', 'dir', 'path', 'unit', 'relpath'];

/** The example file's destination under a template; unknown variables are kept as typed and listed in `unknown`. */
export function renderTarget(template, file) {
  const unknown = [];
  const out = String(template).replace(/\{(\w+)\}/g, (m, name) => {
    if (file[name] === undefined) { unknown.push(name); return m; }
    return file[name] === '' ? '_' : file[name];
  });
  return { text: out, unknown };
}

// ------------------------------------------------------------------ the text editor

/**
 * `errors` and `warnings`: [{line, text}] already worded. `request`: {id, line, scrollTop} asks for the cursor to be put
 * on a line after the next render (the outline does this); the editor scrolls only when the line is out of view, and
 * restores `scrollTop` first when given, so that a text that was just replaced does not jump.
 */
export function RuleTextEditor({
  text, onChange, errors = [], warnings = [], request = null, onCursor, readOnly = false, label, handle,
}) {
  const area = useRef(null);
  const [top, setTop] = useState(0);
  const lines = lineCount(text);
  const nums = useRef('');
  if (nums.current.split('\n').length !== lines) nums.current = Array.from({ length: lines }, (_, i) => i + 1).join('\n');
  const digits = String(lines).length;
  const gutterW = Math.max(3, digits) * 8 + 26;

  const marks = new Map();
  warnings.forEach((w) => { if (w.line) marks.set(w.line, { kind: 'warning', items: [...((marks.get(w.line) || {}).items || []), w.text] }); });
  errors.forEach((e) => { if (e.line) marks.set(e.line, { kind: 'error', items: [...(((marks.get(e.line) || {}).kind === 'error' && marks.get(e.line).items) || []), e.text] }); });

  function goTo(line, keepScroll = null) {
    const el = area.current;
    if (!el) return;
    const before = keepScroll !== null && keepScroll !== undefined ? keepScroll : el.scrollTop;
    const off = offsetOfLine(el.value, line);
    try { el.focus({ preventScroll: true }); } catch (e) { el.focus(); }
    el.setSelectionRange(off, off);
    el.scrollTop = before;
    const y = (line - 1) * LINE_PX;
    if (y < el.scrollTop || y > el.scrollTop + el.clientHeight - LINE_PX * 2) el.scrollTop = Math.max(0, y - el.clientHeight / 3);
    setTop(el.scrollTop);
    if (onCursor) onCursor(line);
  }
  if (handle) handle.current = { goTo, focus: () => area.current && area.current.focus(), scrollTopNow: () => (area.current ? area.current.scrollTop : 0) };

  useLayoutEffect(() => { if (request) goTo(request.line, request.scrollTop); }, [request && request.id]);

  const sync = (e) => setTop(e.target.scrollTop);
  const cursor = () => { if (onCursor && area.current) onCursor(lineOfOffset(area.current.value, area.current.selectionStart)); };

  return html`<div class="rt" data-testid="rules-editor">
    <div class="rt-gutter" data-testid="rules-gutter" aria-hidden="true" style=${{ width: `${gutterW}px` }}>
      <div class="rt-inner" style=${{ transform: `translateY(${-top}px)` }}>
        <pre class="rt-nums">${nums.current}</pre>
        ${[...marks.entries()].map(([line, m]) => html`<span key=${line} class=${'rt-mark rt-mark-' + m.kind} data-line=${line} data-mark=${m.kind}
          title=${m.items.join('\n')} style=${{ top: `${(line - 1) * LINE_PX}px` }}>${line}</span>`)}
      </div>
    </div>
    <div class="rt-body">
      <div class="rt-bands" aria-hidden="true"><div class="rt-inner" style=${{ transform: `translateY(${-top}px)` }}>
        ${[...marks.entries()].map(([line, m]) => html`<div key=${line} class=${'rt-band rt-band-' + m.kind} style=${{ top: `${(line - 1) * LINE_PX}px` }}></div>`)}
      </div></div>
      <textarea ref=${area} class="rt-area" data-testid="rules-text" value=${text} wrap="off" spellcheck="false" autocomplete="off"
        autocapitalize="off" readOnly=${readOnly} aria-label=${label || t('rules.editor.label')}
        onInput=${(e) => onChange(e.target.value)} onScroll=${sync} onKeyUp=${cursor} onClick=${cursor}></textarea>
    </div>
  </div>`;
}

// ------------------------------------------------------------------ the outline

const kindOf = (changes, name) => (changes || []).find((c) => c.name === name);

/**
 * The rules in order with their match summary and preview count. Move, Remove and Insert send the text to the server,
 * which edits the whole [[rule]] block (comments included) and answers the new text only when it parses into exactly
 * the expected rules; `onText(newText, {line})` receives it. `rows` maps a rule name to its preview row.
 */
export function Outline({
  text, outline, fallback, rows, changes, selected, errorsByRule, onSelect, onGoto, onText, onError, onInsert, disabled,
}) {
  const [busy, setBusy] = useState(false);
  const items = outline || [];
  const pick = items.find((it) => it.index === selected) || null;

  async function act(op) {
    if (busy) return;
    setBusy(true);
    const sent = text;
    try {
      const r = await api.transformRules(sent, op);
      onText(r.text, { op, check: r.check, from: sent });
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }
  const move = (d) => pick && act({ op: 'move', index: pick.index, to: pick.index + d });
  const remove = () => pick && act({ op: 'remove', index: pick.index });

  const summary = (m) => {
    const parts = [];
    if (m.category && m.category.length) parts.push(m.category.join(', '));
    if (m.ext && m.ext.length) parts.push(m.ext.map((e) => `.${e}`).join(' '));
    if (m.path_glob) parts.push(m.path_glob);
    if (m.name_regex) parts.push(`~ ${m.name_regex}`);
    if (m.min_size) parts.push(`≥ ${m.min_size}`);
    if (m.max_size) parts.push(`≤ ${m.max_size}`);
    if (m.remote && m.remote.length) parts.push(m.remote.join(', '));
    return parts.join(' · ') || t('rules.outline.matchAll');
  };

  return html`<div class="ro" data-testid="rules-outline">
    <ol class="ro-list" aria-label=${t('rules.outline.title')}>
      ${items.map((it) => {
    const row = rows && rows[it.name];
    const change = kindOf(changes, it.name);
    const bad = errorsByRule && errorsByRule.has(it.index);
    return html`<li key=${it.index}><button type="button" class=${'ro-item' + (selected === it.index ? ' on' : '') + (bad ? ' bad' : '')}
        data-testid=${'rules-outline-item-' + it.index} data-name=${it.name} data-unit=${String(!!it.unit)} data-error=${String(!!bad)}
        data-change=${change ? change.change : undefined} aria-pressed=${selected === it.index}
        onClick=${() => { onSelect(it.index); onGoto(it.line); }}>
        <span class="ro-mark" aria-hidden="true">${it.unit ? '▣' : '●'}</span>
        <span class="ro-name" title=${it.target}>${it.name}</span>
        ${change && html`<span class=${'ro-change ro-change-' + change.change} title=${t(`rchange.${change.change}`, { n: 1 })}>${change.change === 'added' ? '✚' : change.change === 'moved' ? '↕' : change.change === 'removed' ? '−' : '✎'}</span>`}
        ${bad && html`<${Icon} name="alert" size=${12} class="ro-bad" />`}
        <span class="ro-count num" data-testid=${'rules-outline-count-' + it.index}>${row ? row.text : ''}</span>
        <span class="ro-sub">${summary(it.match || {})} → ${it.target}</span></button></li>`;
  })}
      ${fallback !== undefined && fallback !== null && html`<li class="ro-fallback"><span class="ro-item static" data-testid="rules-outline-fallback">
        <span class="ro-mark" aria-hidden="true">◌</span><span class="ro-name">${t('rules.outline.fallback')}</span>
        <span class="ro-count num">${rows && rows.__fallback ? rows.__fallback.text : ''}</span>
        <span class="ro-sub">${fallback}</span></span></li>`}
      ${items.length === 0 && html`<li class="empty-hint">${t('rules.outline.none')}</li>`}
    </ol>
    <div class="ro-actions">
      <button type="button" class="btn btn-small" data-testid="rules-insert" disabled=${disabled || busy} onClick=${() => onInsert(pick ? pick.index : 0)}>
        <${Icon} name="plus" size=${14} /> ${t('rules.insert')}</button>
      <button type="button" class="btn btn-small" data-testid="rules-move-up" aria-label=${t('rules.moveUp')} title=${t('rules.moveUp')}
        disabled=${disabled || busy || !pick || pick.index <= 1} onClick=${() => move(-1)}>↑</button>
      <button type="button" class="btn btn-small" data-testid="rules-move-down" aria-label=${t('rules.moveDown')} title=${t('rules.moveDown')}
        disabled=${disabled || busy || !pick || pick.index >= items.length} onClick=${() => move(1)}>↓</button>
      <button type="button" class="btn btn-small btn-danger-ghost" data-testid="rules-remove" disabled=${disabled || busy || !pick} onClick=${remove}>
        <${Icon} name="trash" size=${14} /> ${t('rules.remove')}</button>
    </div>
    ${pick && html`<p class="pl-help" data-testid="rules-outline-selected">${t('rules.outline.selected', { name: pick.name })}</p>`}
  </div>`;
}

/** The count a preview row shows next to a rule: "≈ 611,532" when sampled, "11,680" when exact. */
export function countText(n, sampled) {
  if (n === null || n === undefined) return '–';
  return `${sampled ? '≈ ' : ''}${fmtNum(n)}`;
}
