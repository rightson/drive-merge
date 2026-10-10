// Small shared widgets: modal shell, confirm/prompt dialogs, toast stack.
import { html, useEffect, useLayoutEffect, useRef, useState } from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import { Icon } from './icons.js';

const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

/** Dialog shell: backdrop, Escape to close, focus moved in and returned, Tab kept inside. */
export function Modal({ label, onClose, children, testid, wide = false, dismissable = true, initialFocus }) {
  const box = useRef(null);
  useEffect(() => {
    const before = document.activeElement;
    const target = initialFocus === 'self' ? box.current
      : (initialFocus && box.current.querySelector(initialFocus)) || box.current.querySelector(FOCUSABLE) || box.current;
    target.focus();
    return () => { if (before && before.focus && document.contains(before)) before.focus(); };
  }, []);
  const onKeyDown = (e) => {
    if (e.key === 'Escape' && dismissable && onClose) { e.stopPropagation(); onClose(); return; }
    if (e.key !== 'Tab') return;
    const nodes = [...box.current.querySelectorAll(FOCUSABLE)].filter((n) => n.offsetParent !== null);
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  return html`<div class="modal-backdrop" onMouseDown=${(e) => { if (e.target === e.currentTarget && dismissable && onClose) onClose(); }}>
    <div class=${'modal' + (wide ? ' modal-wide' : '')} role="dialog" aria-modal="true" aria-label=${label}
      data-testid=${testid} ref=${box} tabindex="-1" onKeyDown=${onKeyDown}>${children}</div>
  </div>`;
}

/** One dialog for both confirm() and prompt(); `request` is {type, title, message, label, value, okLabel, danger, resolve}. */
export function DialogHost({ request }) {
  const [value, setValue] = useState(request ? request.value || '' : '');
  useEffect(() => { setValue(request ? request.value || '' : ''); }, [request]);
  if (!request) return null;
  const done = (result) => request.resolve(result);
  const submit = (e) => {
    e.preventDefault();
    if (request.type === 'prompt') { if (value.trim()) done(value.trim()); } else done(true);
  };
  return html`<${Modal} label=${request.title} onClose=${() => done(request.type === 'prompt' ? null : false)}
    testid="dialog" initialFocus=${request.type === 'prompt' ? 'input' : '[data-ok]'}>
    <form class="dialog" onSubmit=${submit}>
      <h2 class="dialog-title">${request.title}</h2>
      ${request.message && html`<p class="dialog-message">${request.message}</p>`}
      ${request.type === 'prompt' && html`<label class="field">
        <span>${request.label}</span>
        <input type="text" data-testid="dialog-input" value=${value} autocomplete="off" spellcheck="false"
          onInput=${(e) => setValue(e.target.value)} onFocus=${(e) => e.target.select()} />
      </label>`}
      <div class="dialog-actions">
        <button type="button" class="btn" data-testid="dialog-cancel"
          onClick=${() => done(request.type === 'prompt' ? null : false)}>${t('common.cancel')}</button>
        <button type="submit" data-ok class=${'btn ' + (request.danger ? 'btn-danger' : 'btn-primary')} data-testid="dialog-ok"
          disabled=${request.type === 'prompt' && !value.trim()}>${request.okLabel || t('common.ok')}</button>
      </div>
    </form>
  <//>`;
}

const TOAST_ICON = { info: 'info', success: 'check', warn: 'alert', error: 'alert', claude: 'claude' };

/** One toast. Its timer (`ttl` ms, 0 = stays until closed) stands still while the pointer or keyboard focus is on it. */
function Toast({ x, dismiss }) {
  const [held, setHeld] = useState(false);
  useEffect(() => {
    if (!x.ttl || held) return undefined;
    const timer = setTimeout(() => dismiss(x.id), x.ttl);
    return () => clearTimeout(timer);
  }, [held, x.id, x.ttl]);
  return html`<div class=${'toast toast-' + x.kind} data-testid="toast" data-kind=${x.kind}
    role=${x.kind === 'error' ? 'alert' : 'status'} onMouseEnter=${() => setHeld(true)} onMouseLeave=${() => setHeld(false)}
    onFocusCapture=${() => setHeld(true)} onBlurCapture=${() => setHeld(false)}>
    <${Icon} name=${TOAST_ICON[x.kind] || 'info'} size=${16} />
    <span class="toast-text">${x.text}</span>
    ${x.action && html`<button class="btn btn-small" data-testid="toast-action"
      onClick=${() => { dismiss(x.id); x.action.run(); }}>${x.action.label}</button>`}
    <button class="icon-btn" aria-label=${t('common.close')} data-testid="toast-close" onClick=${() => dismiss(x.id)}>
      <${Icon} name="x" size=${14} /></button>
  </div>`;
}

/**
 * The toast stack, bottom right. Its height is published as --toast-reserve, which the modal backdrop
 * leaves free below the dialog: a toast must never sit on a dialog's footer buttons.
 */
export function Toasts({ toasts, dismiss }) {
  const box = useRef(null);
  useLayoutEffect(() => {
    const el = box.current;
    const root = document.documentElement.style;
    if (!el || !toasts.length) { root.removeProperty('--toast-reserve'); return undefined; }
    const publish = () => root.setProperty('--toast-reserve', `${el.offsetHeight + 16}px`);
    publish();
    if (typeof ResizeObserver === 'undefined') return () => root.removeProperty('--toast-reserve');
    const watcher = new ResizeObserver(publish);
    watcher.observe(el);
    return () => { watcher.disconnect(); root.removeProperty('--toast-reserve'); };
  }, [toasts.length > 0]);
  return html`<div class="toasts" ref=${box} data-testid="toasts" aria-live="polite" aria-atomic="false">
    ${toasts.map((x) => html`<${Toast} key=${x.id} x=${x} dismiss=${dismiss} />`)}
  </div>`;
}
