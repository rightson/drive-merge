// The rail and the router (docs/EXPLORE.md section F). The route lives in location.hash:
//   #/overview   #/explore?q=...   #/compare?l=remote:path&r=remote:path   #/workspace
//   #/plans   #/plans/<id>   #/plans/new/dedupe?...   #/plans/new/consolidate?...   (docs/PLANS.md)
//   #/clouds   #/free/<remote>?path=...&keep=a,b   (docs/ONBOARDING.md)
//   #/quarantine?remote=...   #/quarantine/<id>?state=&remote=&path=   (docs/QUARANTINE.md)
// No route (or the token exchange's #token=) means the workspace, so a page opened as before shows the workspace.
import { html, useEffect, useState } from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import { Icon } from './icons.js';
import { pending } from './ctx.js';
import { Overview } from './overview.js';
import { Explore } from './explore.js';
import { Compare } from './compare.js';
import { Plans } from './plans.js';
import { Clouds } from './onboarding.js';
import { Free } from './free.js';
import { Quarantine } from './quarantine.js';
import { duePlans, useQuarantine } from './qstore.js';

// The keys Alt+1 ... Alt+6 select, in order; the rail shows Plans and Quarantine between Compare and the workspace.
export const VIEWS = ['overview', 'explore', 'compare', 'workspace', 'plans', 'quarantine'];
export const RAIL = ['clouds', 'overview', 'explore', 'compare', 'plans', 'quarantine', 'workspace'];
const VIEW_ICON = {
  clouds: 'cloud', overview: 'chart', explore: 'search', compare: 'swap', workspace: 'columns', plans: 'plans', quarantine: 'lock',
};
const ROUTE = /^\/(clouds|overview|explore|compare|workspace)(?:\?(.*))?$/;
const FREE_ROUTE = /^\/free\/([^/?]+)(?:\?(.*))?$/;
const PLANS_ROUTE = /^\/plans((?:\/[^?]*)?)(?:\?(.*))?$/;
const QUARANTINE_ROUTE = /^\/quarantine(?:\/(\d+))?(?:\?(.*))?$/;

/** Where a plans route points: the list, one plan's review, or a form that creates a plan. */
function plansPage(sub) {
  const path = sub.replace(/\/+$/, '');
  const id = /^\/(\d+)$/.exec(path);
  if (id) return { page: 'review', id: Number(id[1]) };
  const form = /^\/new\/(dedupe|consolidate)$/.exec(path);
  if (form) return { page: 'new', kind: form[1] };
  return { page: 'list' };
}

export function parseHash(hash) {
  // The Setup screen is gone (docs/ONBOARDING.md section G): an old #/setup bookmark means the Clouds view.
  const h = String(hash || '').replace(/^#/, '').replace(/^\/setup(?=$|\?)/, '/clouds');
  const m = ROUTE.exec(h);
  if (m) return { view: m[1], params: new URLSearchParams(m[2] || ''), hash: `#${h}`, explicit: true };
  const f = FREE_ROUTE.exec(h);
  if (f) {
    let remote = f[1];
    try { remote = decodeURIComponent(remote); } catch (e) { /* keep the raw text */ }
    return { view: 'free', remote, params: new URLSearchParams(f[2] || ''), hash: `#${h}`, explicit: true };
  }
  const q = QUARANTINE_ROUTE.exec(h);
  if (q) return { view: 'quarantine', id: q[1] ? Number(q[1]) : null, params: new URLSearchParams(q[2] || ''), hash: `#${h}`, explicit: true };
  const p = PLANS_ROUTE.exec(h);
  if (p) return { view: 'plans', params: new URLSearchParams(p[2] || ''), hash: `#${h}`, explicit: true, ...plansPage(p[1] || '') };
  return { view: 'workspace', params: new URLSearchParams(), hash: '', explicit: false };
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
    // The Setup screen is gone: an old #/setup address is rewritten to #/clouds, also when it is typed while the page is open.
    const old = () => /^#\/setup(?:\?|$)/.test(window.location.hash);
    const on = () => { if (old()) navigate('#/clouds', { replace: true }); else setHash(window.location.hash); };
    if (old()) navigate('#/clouds', { replace: true });
    listeners.add(on);
    window.addEventListener('hashchange', on);
    return () => { listeners.delete(on); window.removeEventListener('hashchange', on); };
  }, []);
  const route = parseHash(hash);
  if (route.explicit) remembered[route.view] = route.hash;
  return route;
}

const isTyping = (el) => !!el && (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) || el.isContentEditable);

/** Alt+1 ... Alt+6 switch views; "/" jumps to the Explore box unless a text field has the keyboard. */
export function useShortcuts(route) {
  useEffect(() => {
    const onKey = (e) => {
      if (document.querySelector('.modal-backdrop')) return;
      if (e.altKey && !e.ctrlKey && !e.metaKey && /^Digit[1-6]$/.test(e.code)) {
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

/** `waiting`: how many plans wait for a review (a badge on the Plans item, read by assistive technology as well). */
export function Rail({ route, waiting = 0 }) {
  const quarantined = useQuarantine();
  const due = duePlans(quarantined.data);
  return html`<nav class="rail" aria-label=${t('nav.label')} data-testid="rail">
    ${RAIL.map((view) => html`<a key=${view} class=${'rail-item' + (route.view === view ? ' on' : '')} href=${`#/${view}`}
      data-testid=${'rail-' + view} aria-current=${route.view === view ? 'page' : undefined}
      title=${VIEWS.includes(view) ? `${t(`nav.${view}Tip`)} (Alt+${VIEWS.indexOf(view) + 1})` : t(`nav.${view}Tip`)}
      onClick=${(e) => { if (e.ctrlKey || e.metaKey || e.shiftKey || e.button) return; e.preventDefault(); navigate(remembered[view] || `#/${view}`); }}>
      <${Icon} name=${VIEW_ICON[view]} size=${20} /><span class="rail-label">${t(`nav.${view}`)}</span>
      ${view === 'plans' && waiting > 0 && html`<span class="rail-badge" data-testid="rail-plans-badge" data-count=${waiting}
        title=${t('plans.waitingTip', { n: waiting })}><span aria-hidden="true">${waiting > 99 ? '99+' : waiting}</span>
        <span class="visually-hidden">${t('plans.waitingTip', { n: waiting })}</span></span>`}
      ${view === 'quarantine' && due > 0 && html`<span class="rail-badge" data-testid="rail-quarantine-badge" data-count=${due}
        title=${t('qr.railDue', { n: due })}><span aria-hidden="true">${due > 99 ? '99+' : due}</span>
        <span class="visually-hidden">${t('qr.railDue', { n: due })}</span></span>`}</a>`)}
  </nav>`;
}

/** The read-only views; the workspace is not here: it stays mounted by the App and is only hidden. */
export function ViewHost({ route }) {
  if (route.view === 'clouds') return html`<${Clouds} />`;
  if (route.view === 'free') return html`<${Free} key=${route.remote} remote=${route.remote} params=${route.params} />`;
  if (route.view === 'overview') return html`<${Overview} />`;
  if (route.view === 'explore') return html`<${Explore} params=${route.params} />`;
  if (route.view === 'compare') return html`<${Compare} params=${route.params} />`;
  if (route.view === 'plans') return html`<${Plans} route=${route} />`;
  if (route.view === 'quarantine') return html`<${Quarantine} route=${route} />`;
  return null;
}
