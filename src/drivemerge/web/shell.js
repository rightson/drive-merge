// The rail and the router (docs/EXPLORE.md section F). The route lives in location.hash:
//   #/overview   #/explore?q=...   #/compare?l=remote:path&r=remote:path   #/workspace
// No route (or the token exchange's #token=) means the workspace, so a page opened as before shows the workspace.
import { html, useEffect, useState } from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import { Icon } from './icons.js';
import { pending } from './ctx.js';
import { Overview } from './overview.js';
import { Explore } from './explore.js';
import { Compare } from './compare.js';

export const VIEWS = ['overview', 'explore', 'compare', 'workspace'];
const VIEW_ICON = { overview: 'chart', explore: 'search', compare: 'swap', workspace: 'columns' };
const ROUTE = /^\/(overview|explore|compare|workspace)(?:\?(.*))?$/;

export function parseHash(hash) {
  const h = String(hash || '').replace(/^#/, '');
  const m = ROUTE.exec(h);
  if (!m) return { view: 'workspace', params: new URLSearchParams(), hash: '', explicit: false };
  return { view: m[1], params: new URLSearchParams(m[2] || ''), hash: `#${h}`, explicit: true };
}

/** The view the address bar shows right now (read by code that must stay quiet while another view is on top). */
export const currentView = () => parseHash(window.location.hash).view;

// What each view showed last, so that coming back to it from the rail finds it as it was.
const remembered = {};
const listeners = new Set();
const announce = () => listeners.forEach((fn) => fn(window.location.hash));

/** Go to a route: `replace` rewrites the current history entry (typing in the Explore box), otherwise Back returns here. */
export function navigate(to, { replace = false } = {}) {
  const target = to.startsWith('#') ? to : `#${to}`;
  if (target === window.location.hash) return;
  if (replace) {
    window.history.replaceState(null, '', window.location.pathname + window.location.search + target);
    announce();
  } else {
    window.location.hash = target;
  }
}

export function useRoute() {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const on = () => setHash(window.location.hash);
    listeners.add(on);
    window.addEventListener('hashchange', on);
    return () => { listeners.delete(on); window.removeEventListener('hashchange', on); };
  }, []);
  const route = parseHash(hash);
  if (route.explicit) remembered[route.view] = route.hash;
  return route;
}

const isTyping = (el) => !!el && (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) || el.isContentEditable);

/** Alt+1 ... Alt+4 switch views; "/" jumps to the Explore box unless a text field has the keyboard. */
export function useShortcuts(route) {
  useEffect(() => {
    const onKey = (e) => {
      if (document.querySelector('.modal-backdrop')) return;
      if (e.altKey && !e.ctrlKey && !e.metaKey && /^Digit[1-4]$/.test(e.code)) {
        e.preventDefault();
        const view = VIEWS[Number(e.code.slice(5)) - 1];
        navigate(remembered[view] || `#/${view}`);
        return;
      }
      if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey && !isTyping(e.target)) {
        e.preventDefault();
        const box = document.querySelector('[data-testid="explore-input"]');
        if (route.view === 'explore' && box) { box.focus(); box.select(); return; }
        pending.exploreFocus = true;  // the box takes the focus when it mounts
        navigate(remembered.explore || '#/explore');
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [route.view]);
}

export function Rail({ route }) {
  return html`<nav class="rail" aria-label=${t('nav.label')} data-testid="rail">
    ${VIEWS.map((view, i) => html`<a key=${view} class=${'rail-item' + (route.view === view ? ' on' : '')} href=${`#/${view}`}
      data-testid=${'rail-' + view} aria-current=${route.view === view ? 'page' : undefined}
      title=${`${t(`nav.${view}Tip`)} (Alt+${i + 1})`}
      onClick=${(e) => { if (e.ctrlKey || e.metaKey || e.shiftKey || e.button) return; e.preventDefault(); navigate(remembered[view] || `#/${view}`); }}>
      <${Icon} name=${VIEW_ICON[view]} size=${20} /><span class="rail-label">${t(`nav.${view}`)}</span></a>`)}
  </nav>`;
}

/** The read-only views; the workspace is not here: it stays mounted by the App and is only hidden. */
export function ViewHost({ route }) {
  if (route.view === 'overview') return html`<${Overview} />`;
  if (route.view === 'explore') return html`<${Explore} params=${route.params} />`;
  if (route.view === 'compare') return html`<${Compare} params=${route.params} />`;
  return null;
}
