// Plans (docs/PLANS.md): the list of every plan, the review of one, and the two forms that build a new one
// (dedupe, consolidate) with a live estimate. Nothing here changes a cloud: it asks the server for the same plans the
// CLI and Claude make, shows the same simulation, and approves with the same typed code.
import {
  html, useContext, useEffect, useMemo, useRef, useState,
} from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import * as api from './api.js';
import { Icon } from './icons.js';
import { AppCtx } from './ctx.js';
import { CloudDot, CloudName } from './clouds.js';
import { ViewHeader } from './ui.js';
import { parseSize, plansHash } from './query.js';
import {
  ApprovalFields, Card, CliBox, CloudTable, ExecNote, ProgressBox, Problems, SimStatus, useRunner,
} from './report.js';
import {
  ago, baseName, buildText, errorText, fmtBytes, fmtDateTime, fmtNum, parentOf, rootCauses,
} from './util.js';

const WAITING = ['draft', 'simulated'];
/** Plans that wait for a person to review them (a workspace preview is the person's own draft, not a proposal). */
export const isWaiting = (p) => WAITING.includes(p.status) && p.kind !== 'workspace';
const TABS = ['approved', 'executing', 'done', 'failed', 'cancelled'];
const ESTIMATE_MS = 400;
const GROUPS_PAGE = 50;
const OPS_PAGE = 50;

// ------------------------------------------------------------------ small pieces

const cleanPath = (p) => String(p || '').trim().replace(/^\/+|\/+$/g, '');

function Who({ by }) {
  if (by === 'mcp') {
    return html`<span class="badge badge-claude" data-testid="claude-badge" title=${t('plans.byClaudeTip')}>
      <${Icon} name="claude" size=${11} /> ${t('plans.byClaude')}</span>`;
  }
  return html`<span class="pl-who" data-testid="plan-by">${t(`plans.by.${by}`, null, by || '')}</span>`;
}

function KindPill({ kind }) {
  return html`<span class=${'kind kind-' + String(kind).replace(/[^a-z]/g, '-')} data-testid="plan-kind">${t(`kind.${kind}`, null, kind)}</span>`;
}

function StatusPill({ status }) {
  return html`<span class=${'pl-status pl-status-' + status} data-testid="plan-status" data-status=${status}>${t(`pstatus.${status}`, null, status)}</span>`;
}

/** One line about what a plan is for, worded from its kind and params. */
export function planSummary(p) {
  const q = p.params || {};
  if (p.kind === 'dedupe') {
    const parts = [];
    if (q.remote) parts.push(t('plans.scope.on', { cloud: q.remote }));
    if (q.path_prefix) parts.push(t('plans.scope.path', { path: q.path_prefix }));
    if (q.min_size) parts.push(t('plans.scope.min', { size: fmtBytes(q.min_size) }));
    return t('plans.sum.dedupe', {
      mode: t(q.cross_remote ? 'plans.mode.across' : 'plans.mode.within'), scope: parts.length ? parts.join(' · ') : t('plans.scope.all'),
    });
  }
  if (p.kind === 'consolidate' && q.target) {
    const from = q.sources && q.sources.length ? t('plans.sum.from', { sources: q.sources.join(t('common.listSep')) }) : '';
    return t(q.delete_sources ? 'plans.sum.consolidateClean' : 'plans.sum.consolidate', {
      target: `${q.target}:${cleanPath(q.target_root)}`, from,
    });
  }
  if (p.kind === 'workspace') return t('plans.sum.workspace');
  return p.title || t(`kind.${p.kind}`, null, p.kind);
}

function Verdict({ plan }) {
  if (plan.ok === null || plan.ok === undefined) return html`<span class="pl-verdict" data-verdict="none">${t('plans.notSimulated')}</span>`;
  const out = plan.outdated ? html`<span class="pl-verdict warn" data-testid="plan-outdated" title=${t('plans.outdatedTip')}>
    <${Icon} name="alert" size=${12} /> ${t('plans.outdated')}</span>` : null;
  const main = plan.ok
    ? html`<span class="pl-verdict ok" data-verdict="ok"><${Icon} name="check" size=${12} /> ${t('plans.verdictOk')}</span>`
    : html`<span class="pl-verdict bad" data-verdict="bad"><${Icon} name="alert" size=${12} /> ${t('plans.verdictBad', { n: fmtNum(plan.problems || 1) })}</span>`;
  return html`<span class="pl-verdicts">${main}${out}</span>`;
}

/** The estimate's English reason codes in the page's words: "read-only 9 · ambiguous path 3". */
function SkippedSummary({ byReason }) {
  const entries = Object.entries(byReason || {}).filter(([, n]) => n > 0);
  if (!entries.length) return null;
  const total = entries.reduce((a, [, n]) => a + n, 0);
  return html`<p class="pl-skipped" data-testid="est-skipped">${t('plans.skipped', { n: fmtNum(total) })}
    ${entries.map(([code, n]) => html`<span key=${code} class="pl-reason" data-code=${code}>${t(`skip.${code}`, null, code)} <b>${fmtNum(n)}</b></span>`)}</p>`;
}

function SkippedList({ skipped }) {
  if (!skipped || !skipped.length) return null;
  return html`<details class="pl-details" data-testid="est-skipped-list"><summary>${t('plans.skippedShow', { n: fmtNum(skipped.length) })}</summary>
    <ul class="pl-skip-list">${skipped.map((s, i) => html`<li key=${i}><span class="mono">${s.location}</span>
      <span class="muted">${t(`skip.${s.code}`, null, s.reason || s.code)}</span>
      ${s.dest && html`<span class="mono muted">→ ${s.dest}</span>`}</li>`)}</ul></details>`;
}

// ------------------------------------------------------------------ the list

function PlanRow({ plan, onOpen }) {
  const workspace = plan.kind === 'workspace';
  return html`<li class="pl-row" data-testid="plan-row" data-id=${plan.id} data-status=${plan.status} data-kind=${plan.kind}>
    <span class="pl-id">#${plan.id}</span>
    <${KindPill} kind=${plan.kind} />
    <span class="pl-main"><span class="pl-sum" title=${plan.title || ''}>${planSummary(plan)}</span>
      <span class="pl-meta"><${Who} by=${plan.created_by} /> <span title=${fmtDateTime(plan.created_at)}>${ago(plan.created_at)}</span></span></span>
    <span class="pl-ops num" title=${t('plans.opsTip')}>${t('plans.ops', { n: fmtNum(plan.ops) })}</span>
    ${plan.status === 'draft' || plan.status === 'simulated' || plan.status === 'approved' ? html`<${Verdict} plan=${plan} />` : html`<${StatusPill} status=${plan.status} />`}
    <a class="btn btn-small" href=${workspace ? '#/workspace' : plansHash(`/${plan.id}`)} data-testid="plan-open"
      onClick=${(e) => { if (e.ctrlKey || e.metaKey || e.shiftKey || e.button) return; e.preventDefault(); onOpen(plan); }}>
      ${workspace ? t('plans.openWorkspace') : plan.status === 'draft' || plan.status === 'simulated' ? t('plans.review') : t('plans.open')}</a>
  </li>`;
}

function PlanList({ route }) {
  const ctx = useContext(AppCtx);
  const { list, loaded, error } = ctx.plans;
  useEffect(() => { ctx.refreshPlans(); }, []);
  const tab = TABS.includes(route.params.get('tab')) ? route.params.get('tab') : '';
  const waiting = list.filter(isWaiting);
  const workspace = list.filter((p) => p.kind === 'workspace' && WAITING.includes(p.status));
  const byStatus = (s) => list.filter((p) => p.status === s);
  const counts = Object.fromEntries(TABS.map((s) => [s, byStatus(s).length]));
  const firstFull = TABS.find((s) => counts[s] > 0) || 'approved';
  const current = tab || firstFull;
  const open = (plan) => {
    if (plan.kind === 'workspace') ctx.openWorkspacePreview(plan.draft_id);
    else ctx.nav(plansHash(`/${plan.id}`));
  };
  return html`<main class="view view-plans" data-testid="view-plans" aria-label=${t('nav.plans')}>
    <${ViewHeader} title=${t('nav.plans')} hint=${t('plans.hint')}>
      <a class="btn btn-small" href=${plansHash('/new/dedupe')} data-testid="plans-new-dedupe"
        onClick=${(e) => { e.preventDefault(); ctx.nav(plansHash('/new/dedupe')); }}><${Icon} name="copy" size=${14} /> ${t('plans.newDedupe')}</a>
      <a class="btn btn-small" href=${plansHash('/new/consolidate')} data-testid="plans-new-consolidate"
        onClick=${(e) => { e.preventDefault(); ctx.nav(plansHash('/new/consolidate')); }}><${Icon} name="merge" size=${14} /> ${t('plans.newConsolidate')}</a>
    <//>
    <div class="view-body">
      ${error && !loaded && html`<p class="view-error" role="alert" data-testid="plans-error">${errorText(error)}</p>`}
      ${!loaded && !error && html`<p class="empty-hint">${t('common.loading')}</p>`}
      ${loaded && html`<section class="ov-panel pl-panel" aria-labelledby="pl-waiting-h" data-testid="plans-waiting">
        <h2 id="pl-waiting-h">${t('plans.waiting')} <span class="count-pill" data-testid="plans-waiting-count">${waiting.length}</span></h2>
        ${waiting.length === 0 && html`<p class="empty-hint" data-testid="plans-waiting-none">${t('plans.waitingNone')}</p>`}
        <ul class="pl-list">${waiting.map((p) => html`<${PlanRow} key=${p.id} plan=${p} onOpen=${open} />`)}</ul>
      </section>`}
      ${loaded && html`<section class="ov-panel pl-panel" aria-label=${t('plans.others')}>
        <div class="pl-tabs" role="tablist" aria-label=${t('plans.others')}>
          ${TABS.map((s) => html`<button key=${s} role="tab" class=${current === s ? 'on' : ''} aria-selected=${current === s} data-testid=${'plans-tab-' + s}
            data-count=${counts[s]} onClick=${() => ctx.nav(plansHash('', { tab: s }), { replace: true })}>
            ${t(`pstatus.${s}`)} <span class="count-pill">${counts[s]}</span></button>`)}
        </div>
        ${counts[current] === 0 && html`<p class="empty-hint">${t('plans.tabNone')}</p>`}
        <ul class="pl-list" role="tabpanel" data-testid="plans-tabpanel">${byStatus(current).map((p) => html`<${PlanRow} key=${p.id} plan=${p} onOpen=${open} />`)}</ul>
      </section>`}
      ${loaded && workspace.length > 0 && html`<details class="ov-panel pl-panel" data-testid="plans-workspace">
        <summary>${t('plans.workspaceTitle', { n: workspace.length })}</summary>
        <p class="muted">${t('plans.workspaceText')}</p>
        <ul class="pl-list">${workspace.map((p) => html`<${PlanRow} key=${p.id} plan=${p} onOpen=${open} />`)}</ul>
      </details>`}
      ${loaded && list.length === 0 && html`<p class="empty-hint" data-testid="plans-empty">${t('plans.none')}</p>`}
    </div>
  </main>`;
}

// ------------------------------------------------------------------ the review of one plan

function useRemoteNames(ctx) {
  const remotes = ctx.config.remotes || [];
  return { remotes, names: remotes.map((r) => r.name), byId: Object.fromEntries(remotes.map((r) => [r.id, r.name])) };
}

/** An op's place, whether the server sent {remote, path} objects or remote ids. */
function opPlace(op, side, byId) {
  const o = op[side];
  if (o && o.remote !== undefined) return { remote: o.remote, path: o.path || '' };
  const id = op[`${side}_remote_id`];
  const path = op[`${side}_path`];
  if (id === undefined || id === null || path === undefined || path === null) return null;
  return { remote: byId[id] || String(id), path };
}

const OP_ICON = { copy: 'copy', move: 'arrow', delete: 'trash' };

function OpsTable({ plan, byId }) {
  const ops = plan.ops || [];
  const total = plan.ops_total !== undefined ? plan.ops_total : ops.length;
  const [filter, setFilter] = useState('');
  const [shown, setShown] = useState(OPS_PAGE);
  const rows = useMemo(() => ops.map((o) => ({
    o, src: opPlace(o, 'src', byId), dst: opPlace(o, 'dst', byId), witness: opPlace(o, 'witness', byId),
  })), [ops, byId]);
  const needle = filter.trim().toLowerCase();
  const text = (r) => [r.o.op, r.src && `${r.src.remote}:${r.src.path}`, r.dst && `${r.dst.remote}:${r.dst.path}`,
    r.witness && `${r.witness.remote}:${r.witness.path}`, r.o.reason].filter(Boolean).join(' ').toLowerCase();
  const matching = needle ? rows.filter((r) => text(r).includes(needle)) : rows;
  const visible = matching.slice(0, shown);
  // Counts by kind of operation and cloud, from the ops in hand: exact when the page holds all of them.
  const complete = ops.length >= total;
  const groups = {};
  if (complete) {
    rows.forEach((r) => {
      const where = (r.o.op === 'copy' ? r.dst : r.src) || { remote: '?' };
      const key = `${r.o.op}|${where.remote}`;
      const g = groups[key] || (groups[key] = { op: r.o.op, remote: where.remote, n: 0, bytes: 0 });
      g.n += 1;
      g.bytes += r.o.expect_size || 0;
    });
  }
  return html`<section class="ov-panel pl-panel" aria-labelledby="pl-ops-h" data-testid="plan-ops">
    <div class="ov-panel-head"><h2 id="pl-ops-h">${t('plans.opsTitle', { n: fmtNum(total) })}</h2><span class="spacer"></span>
      <label class="visually-hidden" for="pl-ops-filter">${t('plans.opsFilter')}</label>
      <input id="pl-ops-filter" type="search" class="pl-filter" data-testid="ops-filter" placeholder=${t('plans.opsFilter')} value=${filter}
        onInput=${(e) => { setFilter(e.target.value); setShown(OPS_PAGE); }} />
    </div>
    ${complete && Object.keys(groups).length > 0 && html`<ul class="pl-groups" data-testid="ops-groups">
      ${Object.values(groups).map((g) => html`<li key=${g.op + g.remote} data-op=${g.op} data-remote=${g.remote}>
        <span class=${'op op-' + g.op}><${Icon} name=${OP_ICON[g.op] || 'arrow'} size=${12} /> ${t(`plans.op.${g.op}`, null, g.op)}</span>
        <span>${g.remote}</span><b class="num">${fmtNum(g.n)}</b><span class="muted">${g.bytes ? fmtBytes(g.bytes) : ''}</span></li>`)}</ul>`}
    ${!complete && html`<p class="note" data-testid="ops-truncated">${t('plans.opsTruncated', { shown: fmtNum(ops.length), total: fmtNum(total) })}</p>`}
    <div class="pl-ops-scroll"><table class="table pl-ops" data-testid="ops-table">
      <thead><tr><th>#</th><th>${t('plans.col.op')}</th><th>${t('plans.col.from')}</th><th>${t('plans.col.to')}</th><th>${t('plans.col.why')}</th></tr></thead>
      <tbody>${visible.map((r) => html`<tr key=${r.o.seq || r.o.id} data-testid="op-row" data-op=${r.o.op}>
        <td class="num muted">${r.o.seq}</td>
        <td><span class=${'op op-' + r.o.op}><${Icon} name=${OP_ICON[r.o.op] || 'arrow'} size=${12} /> ${t(`plans.op.${r.o.op}`, null, r.o.op)}</span></td>
        <td class="mono pl-path">${r.src ? `${r.src.remote}:${r.src.path}` : ''}</td>
        <td class="mono pl-path">${r.dst ? `${r.dst.remote}:${r.dst.path}` : ''}${r.witness && r.o.op === 'delete'
    ? html`<span class="pl-witness" data-testid="op-witness"><${Icon} name="check" size=${11} /> ${t('plans.witness', { place: `${r.witness.remote}:${r.witness.path}` })}</span>` : null}</td>
        <td class="pl-why muted">${r.o.reason || ''}</td></tr>`)}</tbody></table></div>
    ${matching.length === 0 && html`<p class="empty-hint">${t('plans.opsNone')}</p>`}
    ${matching.length > shown && html`<div class="ex-more"><button class="btn" data-testid="ops-more" onClick=${() => setShown(shown + OPS_PAGE)}>
      ${t('plans.opsMore', { n: fmtNum(matching.length - shown) })}</button></div>`}
  </section>`;
}

function PlanReview({ id }) {
  const ctx = useContext(AppCtx);
  const { config } = ctx;
  const { names, byId } = useRemoteNames(ctx);
  const [data, setData] = useState(null);
  const [closed, setClosed] = useState(null);   // PLAN_CLOSED: the plan, read-only
  const [loadError, setLoadError] = useState('');
  const sim = (data && data.simulation) || (closed && closed.simulation) || {};
  const touched = (sim.remotes || []).filter((r) => r.files_before !== r.files_after || r.bytes_in || r.files_quarantined).map((r) => r.remote);
  const run = useRunner({
    ctx, planId: id, touched, remoteNames: names, initial: 'loading',
  });
  const { phase, setPhase, typed, setTyped } = run;
  const seq = useRef(0);

  async function load() {
    const mine = (seq.current += 1);
    setPhase('loading');
    setLoadError('');
    run.setError('');
    try {
      const r = await api.reviewPlan(id);
      if (mine !== seq.current) return;
      setData(r);
      setClosed(null);
      setPhase(r.status === 'approved' && !r.approval_code ? 'approved' : 'review');
    } catch (e) {
      if (mine !== seq.current) return;
      if (e.code === 'PLAN_CLOSED') {
        try {
          const p = await api.plan(id);
          if (mine !== seq.current) return;
          setClosed(p);
          setData(null);
          setPhase('closed');
          return;
        } catch (e2) { /* fall through to the error */ }
      }
      setLoadError(e.status === 404 ? t('plans.notFound', { id }) : errorText(e));
      setPhase('error');
    }
    ctx.refreshPlans();
  }
  useEffect(() => { load(); return () => { seq.current += 1; }; }, [id]);
  useEffect(() => { if (phase === 'done' || phase === 'failed' || phase === 'approved') ctx.refreshPlans(); }, [phase]);

  const plan = data || closed;
  const violations = rootCauses(sim.violations || [], [], names);
  const warnings = sim.warnings || [];
  const isOk = !!sim.ok;
  const problems = sim.violations_total || violations.length;
  const code = (data && data.approval_code) || '';
  const matches = !!code && typed.trim().toLowerCase() === String(code).toLowerCase();
  const csrfOk = api.hasCsrf();
  const running = phase === 'approving' || phase === 'executing' || phase === 'rescanning';
  const afterRun = ['executing', 'done', 'failed', 'rescanning', 'rescanned'].includes(phase);
  const status = plan ? (phase === 'done' ? 'done' : phase === 'failed' ? 'failed' : plan.status) : '';
  const counts = sim.op_counts || {};
  const opsTotal = plan ? (plan.ops_total !== undefined ? plan.ops_total : (plan.ops || []).length) : 0;
  const canCancel = !!data && ['draft', 'simulated', 'approved'].includes(data.status) && !afterRun && !running;

  const reveal = (loc) => { if (loc) ctx.revealInWorkspace(loc); };
  const copy = (text) => { try { navigator.clipboard.writeText(text); ctx.toast('success', t('common.copied')); } catch (e) { /* no clipboard */ } };
  const back = () => ctx.nav(plansHash());

  async function cancel() {
    const ok = await ctx.confirm({
      title: t('plans.cancelTitle', { id }), message: t('plans.cancelText'), okLabel: t('plans.cancelOk'), danger: true,
    });
    if (!ok) return;
    try {
      await api.cancelPlan(id);
      ctx.toast('success', t('plans.cancelled', { id }));
      ctx.refreshPlans();
      back();
    } catch (e) {
      ctx.toast('error', errorText(e));
      if (e.code === 'PLAN_CLOSED') load();
    }
  }

  const bar = (() => {
    if (!plan || phase === 'loading' || phase === 'error') return null;
    if (phase === 'done' || phase === 'failed') {
      return html`<button class="btn btn-primary" data-testid="rescan-button" onClick=${run.rescan}><${Icon} name="refresh" size=${15} /> ${t('prog.rescan')}</button>
        <button class="btn" data-testid="plan-back" onClick=${back}>${t('plans.back')}</button>`;
    }
    if (phase === 'rescanned') return html`<button class="btn btn-primary" data-testid="plan-back" onClick=${back}>${t('plans.back')}</button>`;
    if (running) {
      return html`<span class="spinner" aria-hidden="true"></span><span class="muted">${phase === 'approving' ? t('prev.approving') : t('prog.wait')}</span>`;
    }
    if (phase === 'closed') return html`<span class="muted" data-testid="plan-closed-note">${t('plans.closedNote', { status: t(`pstatus.${plan.status}`, null, plan.status) })}</span>`;
    if (phase === 'approved') {
      return html`<span class="pl-approved" data-testid="plan-approved"><${Icon} name="check" size=${16} /> ${t('plans.approvedNote')}</span>
        <button class="btn btn-primary" data-testid="plan-run" disabled=${config.execution_enabled === false || !csrfOk} onClick=${() => run.execute('approved')}>
          <${Icon} name="arrow" size=${15} /> ${t('plans.run')}</button>`;
    }
    if (config.approvals_enabled === false) return html`<span class="muted">${t('plans.cliOnly')}</span>`;
    const canApprove = config.approvals_enabled && config.execution_enabled !== false && isOk && !!code && !ctx.outdated;
    if (canApprove) {
      return html`<div class="approval" data-testid="approval-area">
        <${ApprovalFields} code=${code} typed=${typed} setTyped=${setTyped} matches=${matches} disabled=${!csrfOk}
          onApprove=${() => run.approve({ stay: true })} label=${t('plans.approve')} />
      </div>${!csrfOk && html`<p class="note error" data-testid="no-csrf">${t('prev.noCsrf')}</p>`}`;
    }
    if (!isOk) return html`<span class="muted" data-testid="plan-cannot-approve">${t('plans.cannotApprove')}</span>`;
    return null;
  })();

  const headActions = html`
    ${canCancel && html`<button class="btn btn-small btn-danger-ghost" data-testid="plan-cancel" onClick=${cancel}><${Icon} name="x" size=${14} /> ${t('plans.cancel')}</button>`}
    ${(phase === 'review' || phase === 'approved') && html`<button class="btn btn-small" data-testid="plan-resimulate" onClick=${load}><${Icon} name="refresh" size=${14} /> ${t('plans.resimulate')}</button>`}
    <button class="btn btn-small" data-testid="plan-back-top" onClick=${back}>${t('plans.back')}</button>`;

  return html`<main class="view view-plans view-review" data-testid="view-plan-review" data-id=${id} aria-label=${t('plans.reviewTitle', { id })}>
    <${ViewHeader} title=${t('plans.reviewTitle', { id })} hint=${plan ? planSummary(plan) : ''}>${headActions}<//>
    <div class="view-body">
      ${phase === 'loading' && html`<div class="center-msg" data-testid="plan-loading"><span class="spinner"></span> ${t('plans.loading')}</div>`}
      ${phase === 'error' && html`<div class="note error" data-testid="plan-error">${loadError} <button class="btn btn-small" onClick=${load}>${t('common.retry')}</button></div>`}
      ${plan && html`<div class="pl-facts" data-testid="plan-facts">
        <${KindPill} kind=${plan.kind} /><${StatusPill} status=${status} /><${Who} by=${plan.created_by} />
        <span class="muted">${t('plans.created', { when: fmtDateTime(plan.created_at) })}</span>
        ${plan.simulated_at && html`<span class="muted">${t('plans.simulatedAt', { when: fmtDateTime(plan.simulated_at) })}</span>`}
      </div>`}
      ${phase === 'closed' && html`<div class="banner banner-info pl-banner" data-testid="plan-closed" role="note"><${Icon} name="info" size=${16} />
        <span>${t('err.PLAN_CLOSED', { plan: id, plan_status: t(`pstatus.${closed.status}`, null, closed.status) })}
          ${closed.status === 'failed' ? ` ${t('prog.failedHelp', { id })}` : ''}</span></div>`}
      ${phase === 'review' && ctx.outdated && html`<div class="banner banner-warn" data-testid="outdated-note">
        <${Icon} name="alert" size=${16} /><span>${t('catalog.blocked')}</span>
        ${config.approvals_enabled && html`<button class="btn btn-small" data-testid="outdated-rescan" onClick=${run.rescan}>
          <${Icon} name="refresh" size=${14} /> ${t('catalog.rescan')}</button>`}</div>`}
      ${afterRun && html`<${ProgressBox} phase=${phase} job=${run.job} prog=${run.prog} planId=${id} error=${run.error} />`}
      ${plan && sim && Object.keys(sim).length > 0 && html`
        <section class="cards" data-testid="plan-summary">
          <${Card} id="ops" label=${t('plans.card.ops')} value=${fmtNum(opsTotal)} />
          <${Card} id="copy" label=${t('plans.op.copy')} value=${fmtNum(counts.copy || 0)} />
          <${Card} id="move" label=${t('plans.op.move')} value=${fmtNum(counts.move || 0)} />
          <${Card} id="delete" label=${t('plans.op.delete')} value=${fmtNum(counts.delete || 0)} sub=${counts.delete ? t('plans.card.deleteSub') : ''} />
          <${Card} id="cross" label=${t('prev.cross')} value=${fmtBytes(sim.bytes_cross_remote)}
            tone=${sim.bytes_cross_remote > 0 ? 'warn' : ''} sub=${sim.bytes_cross_remote > 0 ? t('prev.crossSub') : ''} />
          <${Card} id="contents" label=${t('plans.card.contents')} value=${`${fmtNum(sim.contents_before)} → ${fmtNum(sim.contents_after)}`}
            tone=${sim.contents_lost > 0 ? 'bad' : ''} sub=${sim.contents_lost > 0 ? t('plans.card.lost', { n: fmtNum(sim.contents_lost) }) : t('plans.card.nolost')} />
        </section>
        ${phase !== 'closed' && html`<${SimStatus} isOk=${isOk} problems=${problems} />`}
        <${Problems} violations=${violations} conflicts=${[]} warnings=${warnings} remoteNames=${names} onReveal=${reveal} />
        <${CloudTable} remotes=${sim.remotes} />`}
      ${plan && html`<${OpsTable} plan=${plan} byId=${byId} />`}
      ${plan && phase !== 'closed' && html`<${CliBox} config=${config} planId=${id} onCopy=${copy} /><${ExecNote} config=${config} isOk=${isOk} />`}
      ${run.error && !afterRun && html`<p class="note error" data-testid="approval-error">${run.error}</p>`}
    </div>
    ${bar && html`<footer class="plan-bar" data-testid="plan-bar">${bar}</footer>`}
  </main>`;
}

// ------------------------------------------------------------------ forms

/**
 * Form state that lives in the address bar: parsed from the route, written back (replacing the history entry) a
 * moment after each edit, and re-read when the address changes by Back, Forward or a link.
 */
function useUrlForm(ctx, route, kind, parse, serialize) {
  const [form, setForm] = useState(() => parse(route.params));
  const written = useRef(route.hash);
  useEffect(() => {
    if (route.hash !== written.current) { written.current = route.hash; setForm(parse(route.params)); }
  }, [route.hash]);
  useEffect(() => {
    const timer = setTimeout(() => {
      const hash = plansHash(`/new/${kind}`, serialize(form));
      if (hash === window.location.hash) return;
      written.current = hash;
      ctx.nav(hash, { replace: true });
    }, 250);
    return () => clearTimeout(timer);
  }, [form]);
  const update = (patch) => setForm((f) => ({ ...f, ...(typeof patch === 'function' ? patch(f) : patch) }));
  return [form, update, setForm];
}

/**
 * Debounced estimate: one request after the form settles, stale answers dropped. `extra` (a ref) is read when the
 * request goes out, so that paging state does not by itself start a new estimate.
 */
function useEstimate(kind, params, extra) {
  const [state, setState] = useState({ loading: !!params, error: null, data: null, sent: null });
  const seq = useRef(0);
  const first = useRef(true);
  const key = params ? JSON.stringify(params) : '';
  useEffect(() => {
    if (!params) { setState({ loading: false, error: null, data: null, sent: null }); return undefined; }
    const mine = (seq.current += 1);
    setState((s) => ({ ...s, loading: true, error: null }));
    const delay = first.current ? 0 : ESTIMATE_MS;
    first.current = false;
    const timer = setTimeout(async () => {
      const sent = extra ? extra.current : null;
      try {
        const data = await api.estimatePlan(kind, { ...params, ...(sent || {}) }, 50);
        if (mine === seq.current) setState({ loading: false, error: null, data, sent });
      } catch (e) {
        if (mine === seq.current) setState({ loading: false, error: e, data: null, sent });
      }
    }, delay);
    return () => { clearTimeout(timer); };
  }, [key]);
  return [state, setState];
}

function sizeError(text) {
  return text.trim() !== '' && parseSize(text) === null;
}

function PreferList({ remotes, value, onChange }) {
  const names = remotes.map((r) => r.name);
  const order = value.length ? [...value, ...names.filter((n) => !value.includes(n))] : names;
  const move = (i, d) => {
    const next = [...order];
    const j = i + d;
    if (j < 0 || j >= next.length) return;
    [next[i], next[j]] = [next[j], next[i]];
    onChange(next.join(',') === names.join(',') ? [] : next);
  };
  return html`<ol class="pl-prefer" data-testid="prefer-list" aria-label=${t('plans.prefer')}>
    ${order.map((n, i) => html`<li key=${n} data-cloud=${n}>
      <${CloudName} remotes=${remotes} name=${n} />
      <button class="icon-btn" aria-label=${t('plans.preferUp', { cloud: n })} disabled=${i === 0} data-testid=${'prefer-up-' + n}
        onClick=${() => move(i, -1)}>▲</button>
      <button class="icon-btn" aria-label=${t('plans.preferDown', { cloud: n })} disabled=${i === order.length - 1} data-testid=${'prefer-down-' + n}
        onClick=${() => move(i, 1)}>▼</button></li>`)}
  </ol>
  <p class="pl-help">${value.length ? html`${t('plans.preferHint')} <button class="link" data-testid="prefer-reset" onClick=${() => onChange([])}>${t('plans.reset')}</button>` : t('plans.preferHint')}</p>`;
}

const listParam = (s) => (s ? s.split(',').filter(Boolean) : []);

// ---- dedupe

function parseDedupe(p) {
  const keep = {};
  listParam(p.get('keep')).forEach((pair) => {
    const i = pair.lastIndexOf('=');
    if (i > 0 && /^\d+$/.test(pair.slice(i + 1))) keep[pair.slice(0, i)] = Number(pair.slice(i + 1));
  });
  return {
    remote: p.get('remote') || '', across: p.get('across') === '1', path: p.get('path') || '', min: p.get('min') || '',
    prefer: listParam(p.get('prefer')), keep, skip: listParam(p.get('skip')),
  };
}

function serializeDedupe(f) {
  return {
    remote: f.remote, across: f.across ? '1' : '', path: f.path, min: f.min, prefer: f.prefer.join(','),
    keep: Object.entries(f.keep).map(([g, id]) => `${g}=${id}`).join(','), skip: f.skip.join(','),
  };
}

function dedupeParams(f) {
  if (sizeError(f.min)) return null;
  const p = { cross_remote: f.across };
  if (f.remote) p.remote = f.remote;
  if (cleanPath(f.path)) p.path_prefix = cleanPath(f.path);
  if (f.min.trim()) p.min_size = parseSize(f.min);
  if (f.prefer.length) p.prefer_remotes = f.prefer;
  if (Object.keys(f.keep).length) p.keep = f.keep;
  if (f.skip.length) p.skip_groups = f.skip;
  return p;
}

function GroupCard({ g, remotes, onKeep, onSkip, form }) {
  const first = g.copies[0];
  const name = first ? baseName(first.path) : g.key;
  const removed = g.copies.filter((c) => c.remove).length;
  const mine = form.keep[g.key];
  return html`<article class=${'dd-group' + (g.skipped ? ' skipped' : '')} data-testid="dd-group" data-key=${g.key}
    data-skipped=${String(!!g.skipped)}>
    <header><strong class="dd-name" title=${name}>${name}</strong>
      <span class="muted">${fmtBytes(g.size)} · ${t('dedupe.copies', { n: g.copies.length })}</span>
      <span class="spacer"></span>
      ${mine !== undefined && html`<span class="badge badge-new" data-testid="dd-overridden">${t('dedupe.yourChoice')}</span>`}
      ${!g.skipped && html`<span class="dd-reclaim" title=${t('dedupe.reclaimTip')}>−${fmtBytes(g.reclaim)} (${removed})</span>`}
      <button class="btn btn-tiny" data-testid=${g.skipped ? 'dd-unskip' : 'dd-skip'} onClick=${() => onSkip(g.key, !g.skipped)}>
        ${g.skipped ? t('dedupe.unskip') : t('dedupe.skip')}</button>
    </header>
    ${g.skipped ? html`<p class="muted dd-skipped-note">${t('dedupe.skippedNote')}</p>`
    : html`<ul class="dd-copies" role="radiogroup" aria-label=${t('dedupe.whichStays', { name })}>
      ${g.copies.map((c) => {
    const role = c.keep ? 'keep' : c.remove ? 'remove' : 'stay';
    const pickable = c.usable && !c.keep;
    return html`<li key=${c.entry_id} class=${'dd-copy dd-' + role + (c.usable ? '' : ' locked')} data-testid="dd-copy" data-entry=${c.entry_id}
      data-role=${role} data-usable=${String(c.usable)}>
      <button role="radio" aria-checked=${c.keep} aria-disabled=${!pickable && !c.keep} class="dd-pick" data-testid="dd-pick"
        title=${c.usable ? (c.keep ? t('dedupe.keepTip') : t('dedupe.pickTip')) : t(`skip.${c.why_not}`, null, c.why_not || '')}
        onClick=${() => { if (pickable) onKeep(g.key, c.entry_id); }}>
        <span class="dd-mark" aria-hidden="true">${c.keep ? '★' : c.usable ? '○' : html`<${Icon} name="lock" size=${12} />`}</span>
        <${CloudDot} remotes=${remotes} name=${c.remote} size=${8} /><span class="mono dd-path">${c.remote}:${c.path}</span></button>
      <span class=${'dd-tag dd-tag-' + role}>${c.keep ? t('dedupe.keep') : c.remove ? t('dedupe.remove') : t('dedupe.stays')}</span>
      ${!c.usable && html`<span class="dd-why" data-testid="dd-why" data-code=${c.why_not}>${t(`skip.${c.why_not}`, null, c.why_not || '')}</span>`}
    </li>`;
  })}</ul>`}
  </article>`;
}

function Dedupe({ route }) {
  const ctx = useContext(AppCtx);
  const remotes = ctx.config.remotes || [];
  const [form, update, setForm] = useUrlForm(ctx, route, 'dedupe', parseDedupe, serializeDedupe);
  const params = dedupeParams(form);
  const [more, setMore] = useState({ groups: [], busy: false, last: null });
  const extra = useRef({});
  const [est] = useEstimate('dedupe', params, extra);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState(null);
  useEffect(() => { setMore({ groups: [], busy: false, last: null }); }, [est.data]);
  const data = est.data;
  const stats = (data && data.stats) || {};
  const groups = data ? [...(data.groups || []), ...more.groups] : [];
  // A refresh asks for as many groups as are showing, so a change of the form does not lose the pages opened.
  extra.current = { group_offset: 0, group_limit: Math.max(GROUPS_PAGE, groups.length) };
  const asked = est.sent ? est.sent.group_limit : GROUPS_PAGE;
  const lastFull = more.last === null ? data && (data.groups || []).length >= asked : more.last >= GROUPS_PAGE;
  const total = data && data.groups_total !== undefined ? data.groups_total : stats.groups_examined;
  const hasMore = !!data && lastFull && (total === undefined || groups.length < total);
  const keepCount = Object.keys(form.keep).length;
  const resetOverrides = () => update({ keep: {}, skip: [] });
  const scope = (patch) => update({ ...patch, keep: {}, skip: [] });  // overrides belong to the scope they were made in

  async function loadMore() {
    setMore((m) => ({ ...m, busy: true }));
    try {
      const r = await api.estimatePlan('dedupe', { ...params, group_offset: groups.length, group_limit: GROUPS_PAGE }, 1);
      setMore((m) => ({ groups: [...m.groups, ...(r.groups || [])], busy: false, last: (r.groups || []).length }));
    } catch (e) {
      setMore((m) => ({ ...m, busy: false }));
      ctx.toast('error', buildText(e));
    }
  }
  async function create() {
    setCreating(true);
    setCreateError(null);
    try {
      const r = await api.createPlan({ kind: 'dedupe', params });
      if (!r.plan_id) { setCreateError({ code: r.code || 'NOTHING_TO_DO' }); return; }
      ctx.refreshPlans();
      ctx.nav(plansHash(`/${r.plan_id}`));
    } catch (e) {
      setCreateError(e);
    } finally {
      setCreating(false);
    }
  }
  const writable = remotes.filter((r) => r.writable !== false);
  const readOnly = remotes.filter((r) => r.writable === false);
  const ops = data ? data.ops : 0;
  const nothing = data && (data.nothing_to_do || ops === 0);

  return html`<main class="view view-plans view-form" data-testid="view-plan-new" data-kind="dedupe" aria-label=${t('dedupe.title')}>
    <${ViewHeader} title=${t('dedupe.title')} hint=${t('dedupe.hint')}>
      <button class="btn btn-small" data-testid="plan-back-top" onClick=${() => ctx.nav(plansHash())}>${t('plans.back')}</button><//>
    <div class="pl-form-layout">
      <form class="pl-form" onSubmit=${(e) => e.preventDefault()} aria-label=${t('dedupe.scope')}>
        <h2>${t('dedupe.scope')}</h2>
        <label class="field"><span>${t('dedupe.clouds')}</span>
          <select class="select" data-testid="dd-remote" value=${form.remote} onChange=${(e) => scope({ remote: e.target.value })}>
            <option value="">${t('dedupe.allClouds')}</option>
            ${writable.map((r) => html`<option key=${r.name} value=${r.name}>${t('dedupe.onlyCloud', { cloud: r.name })}</option>`)}
          </select></label>
        <fieldset class="pl-radios"><legend>${t('dedupe.mode')}</legend>
          <label class="check"><input type="radio" name="dd-mode" data-testid="dd-mode-each" checked=${!form.across} onChange=${() => scope({ across: false })} />
            ${t('plans.mode.within')}</label>
          <label class="check"><input type="radio" name="dd-mode" data-testid="dd-mode-across" checked=${form.across} onChange=${() => scope({ across: true })} />
            ${t('plans.mode.across')}</label>
          ${form.across && html`<p class="pl-warn" data-testid="dd-across-warning" role="note"><${Icon} name="alert" size=${14} /> ${t('dedupe.acrossWarning')}</p>`}
          ${!form.across && html`<p class="pl-help">${t('dedupe.eachHelp')}</p>`}
        </fieldset>
        <label class="field"><span>${t('dedupe.path')}</span>
          <input type="text" data-testid="dd-path" value=${form.path} placeholder=${t('dedupe.pathPlaceholder')} autocomplete="off" spellcheck="false"
            onInput=${(e) => scope({ path: e.target.value })} /></label>
        <label class="field"><span>${t('dedupe.minSize')}</span>
          <input type="text" data-testid="dd-min" value=${form.min} placeholder=${t('dedupe.minPlaceholder')} autocomplete="off" spellcheck="false"
            aria-invalid=${sizeError(form.min)} onInput=${(e) => scope({ min: e.target.value })} />
          ${sizeError(form.min) && html`<span class="pl-error" role="alert" data-testid="dd-min-error">${t('dedupe.minBad')}</span>`}</label>
        <div class="field"><span>${t('plans.prefer')}</span>
          <${PreferList} remotes=${remotes} value=${form.prefer} onChange=${(prefer) => update({ prefer })} /></div>
        ${readOnly.length > 0 && html`<p class="pl-help" data-testid="dd-readonly-note">${t('dedupe.readOnlyNote', { clouds: readOnly.map((r) => r.name).join(t('common.listSep')) })}</p>`}
        ${(keepCount > 0 || form.skip.length > 0) && html`<p class="pl-overrides" data-testid="dd-overrides">
          ${t('dedupe.overrides', { keeps: keepCount, skips: form.skip.length })}
          <button type="button" class="link" data-testid="dd-reset" onClick=${resetOverrides}>${t('plans.reset')}</button></p>`}
        <div class="pl-form-foot">
          ${createError && html`<p class="pl-error" role="alert" data-testid="create-error">${createError.code === 'NOTHING_TO_DO' ? t('plans.NOTHING_TO_DO') : buildText(createError)}</p>`}
          <button type="button" class="btn btn-primary" data-testid="create-plan" disabled=${creating || est.loading || !data || !(ops > 0)} onClick=${create}>
            <${Icon} name="plus" size=${15} /> ${t('plans.create')}</button>
        </div>
      </form>
      <section class="pl-estimate" aria-live="polite" aria-label=${t('plans.estimate')} data-testid="estimate">
        <h2>${t('plans.estimate')} ${est.loading && html`<span class="pl-working" data-testid="estimating"><span class="spinner" aria-hidden="true"></span> ${t('plans.estimating')}</span>`}</h2>
        ${est.error && html`<p class="view-error pl-est-error" role="alert" data-testid="estimate-error">${buildText(est.error)}
          ${(est.error.code === 'BAD_KEEPER' || est.error.code === 'BAD_GROUP') && html` <button class="link" onClick=${resetOverrides}>${t('plans.reset')}</button>`}</p>`}
        ${!params && html`<p class="empty-hint">${t('dedupe.fixForm')}</p>`}
        ${data && html`<div class=${est.loading ? 'pl-stale' : ''}>
          ${nothing ? html`<p class="empty-hint" data-testid="estimate-nothing">${t('plans.NOTHING_TO_DO')}</p>`
    : html`<p class="pl-headline" data-testid="estimate-headline">${t('dedupe.headline', {
      copies: fmtNum(stats.deletes !== undefined ? stats.deletes : ops), groups: fmtNum(stats.groups || 0), bytes: fmtBytes(stats.bytes_reclaimed || 0),
    })}</p>`}
          <${SkippedSummary} byReason=${stats.skipped_by_reason} /><${SkippedList} skipped=${data.skipped} />
          ${groups.length > 0 && html`<div class="dd-groups" data-testid="dd-groups">
            ${groups.map((g) => html`<${GroupCard} key=${g.key} g=${g} remotes=${remotes} form=${form}
              onKeep=${(group, id) => update((f) => ({ keep: { ...f.keep, [group]: id }, skip: f.skip.filter((k) => k !== group) }))}
              onSkip=${(group, skip) => update((f) => ({
    skip: skip ? [...f.skip.filter((k) => k !== group), group] : f.skip.filter((k) => k !== group),
    keep: skip ? Object.fromEntries(Object.entries(f.keep).filter(([k]) => k !== group)) : f.keep,
  }))} />`)}
          </div>`}
          ${hasMore && html`<div class="ex-more"><button class="btn" data-testid="dd-more" disabled=${more.busy} onClick=${loadMore}>
            ${more.busy ? t('common.loading') : t('dedupe.more')}</button></div>`}
        </div>`}
        ${!data && !est.error && est.loading && html`<p class="empty-hint">${t('plans.estimating')}</p>`}
      </section>
    </div>
  </main>`;
}

// ---- consolidate

function parseConsolidate(p) {
  return {
    target: p.get('target') || '', root: p.get('root') || '', sources: p.has('src') ? listParam(p.get('src')) : null,
    reorg: p.get('reorg') === '1', probable: p.get('probable') === '1', path: p.get('path') || '', prefer: listParam(p.get('prefer')),
    del: p.get('del') === '1',
  };
}

function serializeConsolidate(f) {
  return {
    target: f.target, root: f.root, src: f.sources === null ? '' : f.sources.join(','), reorg: f.reorg ? '1' : '', probable: f.probable ? '1' : '',
    path: f.path, prefer: f.prefer.join(','), del: f.del ? '1' : '',
  };
}

function consolidateParams(f, target, sources) {
  if (!target || sources.length === 0) return null;
  const p = { target, reorganize_target: f.reorg, include_probable: f.probable, delete_sources: f.del };
  if (cleanPath(f.root)) p.target_root = cleanPath(f.root);
  if (f.sources !== null) p.sources = sources;
  if (cleanPath(f.path)) p.path_prefix = cleanPath(f.path);
  if (f.prefer.length) p.prefer_remotes = f.prefer;
  return p;
}

/** What the sources hold that the target does not, from the coverage bars: {contents, bytes, approx}. */
export function notOnTarget(cov, target, sources) {
  if (!cov || !target) return null;
  let contents = 0;
  let bytes = 0;
  (cov.intersections || []).forEach((b) => {
    if (b.remotes.includes(target) || !b.remotes.some((r) => sources.includes(r))) return;
    contents += b.contents;
    bytes += b.bytes;
  });
  return { contents, bytes, approx: !!cov.other };
}

function Placements({ sample, target }) {
  const placed = (sample || []).filter((o) => (o.op === 'copy' || o.op === 'move') && o.dst);
  if (!placed.length) return null;
  const folders = new Map();
  placed.forEach((o) => {
    const dir = `${o.dst.remote}:${parentOf(o.dst.path)}`;
    if (!folders.has(dir)) folders.set(dir, []);
    folders.get(dir).push(o);
  });
  return html`<section class="pl-placements" data-testid="placements"><h3 class="section-title">${t('cons.placements', { n: placed.length, target })}</h3>
    ${[...folders.entries()].map(([dir, ops]) => html`<div key=${dir} class="pl-folder" data-testid="placement-folder" data-dir=${dir}>
      <div class="pl-folder-name mono"><${Icon} name="folder" size=${13} /> ${dir.endsWith(':') ? `${dir}/` : dir}</div>
      <ul>${ops.map((o, i) => html`<li key=${i} data-testid="placement"><span class=${'op op-' + o.op}>${t(`plans.op.${o.op}`, null, o.op)}</span>
        <span class="mono pl-from" title=${`${o.src.remote}:${o.src.path}`}>${o.src.remote}:${o.src.path}</span>
        <${Icon} name="arrow" size=${11} /><span class="mono">${baseName(o.dst.path)}</span></li>`)}</ul></div>`)}</section>`;
}

function Consolidate({ route }) {
  const ctx = useContext(AppCtx);
  const remotes = ctx.config.remotes || [];
  const writable = remotes.filter((r) => r.writable !== false);
  const [form, update] = useUrlForm(ctx, route, 'consolidate', parseConsolidate, serializeConsolidate);
  const target = writable.some((r) => r.name === form.target) ? form.target : (writable[0] && writable[0].name) || '';
  const others = remotes.filter((r) => r.name !== target).map((r) => r.name);
  const sources = (form.sources === null ? others : form.sources.filter((s) => others.includes(s)));
  const params = consolidateParams(form, target, sources);
  const [est] = useEstimate('consolidate', params, null);
  const [cov, setCov] = useState(null);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState(null);
  useEffect(() => { api.coverage({ maxBars: 64 }).then(setCov, () => setCov(null)); }, []);
  const missing = notOnTarget(cov, target, sources);
  const data = est.data;
  const stats = (data && data.stats) || {};
  const ops = data ? data.ops : 0;
  const nothing = data && (data.nothing_to_do || ops === 0);
  const toggleSource = (name) => update(() => {
    const now = new Set(sources);
    if (now.has(name)) now.delete(name); else now.add(name);
    const next = others.filter((o) => now.has(o));
    return { sources: next.length === others.length ? null : next };
  });

  async function create() {
    setCreating(true);
    setCreateError(null);
    try {
      const r = await api.createPlan({ kind: 'consolidate', params });
      if (!r.plan_id) { setCreateError({ code: r.code || 'NOTHING_TO_DO' }); return; }
      ctx.refreshPlans();
      ctx.nav(plansHash(`/${r.plan_id}`));
    } catch (e) {
      setCreateError(e);
    } finally {
      setCreating(false);
    }
  }

  return html`<main class="view view-plans view-form" data-testid="view-plan-new" data-kind="consolidate" aria-label=${t('cons.title')}>
    <${ViewHeader} title=${t('cons.title')} hint=${t('cons.hint')}>
      <button class="btn btn-small" data-testid="plan-back-top" onClick=${() => ctx.nav(plansHash())}>${t('plans.back')}</button><//>
    <div class="pl-form-layout">
      <form class="pl-form" onSubmit=${(e) => e.preventDefault()} aria-label=${t('cons.title')}>
        <h2>${t('cons.target')}</h2>
        ${writable.length === 0 && html`<p class="pl-warn" role="alert"><${Icon} name="alert" size=${14} /> ${t('cons.noWritable')}</p>`}
        <label class="field"><span>${t('cons.targetCloud')}</span>
          <select class="select" data-testid="cons-target" value=${target} onChange=${(e) => update({ target: e.target.value, sources: null })}>
            ${writable.map((r) => html`<option key=${r.name} value=${r.name}>${r.name}</option>`)}
          </select></label>
        <label class="field"><span>${t('cons.root')}</span>
          <input type="text" data-testid="cons-root" value=${form.root} placeholder=${t('cons.rootPlaceholder')} autocomplete="off" spellcheck="false"
            onInput=${(e) => update({ root: e.target.value })} /></label>
        <fieldset class="pl-radios"><legend>${t('cons.sources')}</legend>
          ${others.length === 0 && html`<p class="pl-help">${t('cons.noSources')}</p>`}
          ${others.map((n) => html`<label key=${n} class="check"><input type="checkbox" data-testid=${'cons-source-' + n} checked=${sources.includes(n)}
            onChange=${() => toggleSource(n)} /> <${CloudName} remotes=${remotes} name=${n} /></label>`)}
        </fieldset>
        <label class="check"><input type="checkbox" data-testid="cons-reorg" checked=${form.reorg} onChange=${(e) => update({ reorg: e.target.checked })} />
          ${t('cons.reorg')}</label>
        <p class="pl-help">${t('cons.reorgHelp')}</p>
        <label class="check"><input type="checkbox" data-testid="cons-probable" checked=${form.probable} onChange=${(e) => update({ probable: e.target.checked })} />
          ${t('cons.probable')}</label>
        ${form.probable && html`<p class="pl-warn" data-testid="cons-probable-warning" role="note"><${Icon} name="alert" size=${14} /> ${t('cons.probableWarning')}</p>`}
        <label class="field"><span>${t('dedupe.path')}</span>
          <input type="text" data-testid="cons-path" value=${form.path} placeholder=${t('dedupe.pathPlaceholder')} autocomplete="off" spellcheck="false"
            onInput=${(e) => update({ path: e.target.value })} /></label>
        <div class="field"><span>${t('plans.prefer')}</span>
          <${PreferList} remotes=${remotes} value=${form.prefer} onChange=${(prefer) => update({ prefer })} /></div>
        <p class="pl-help">${t('cons.rulesHelp')}</p>
        <details class=${'pl-details pl-cleanup' + (form.del ? ' on' : '')} data-testid="cons-cleanup" open=${form.del || undefined}>
          <summary>${t('cons.cleanup')}</summary>
          <p>${t('cons.cleanupText')}</p>
          ${missing && html`<p class=${'pl-coverage' + (missing.contents > 0 ? ' warn' : '')} data-testid="cons-missing" data-contents=${missing.contents}>
            <${Icon} name=${missing.contents > 0 ? 'alert' : 'check'} size=${14} />
            ${missing.contents > 0 ? t('cons.missing', { n: `${missing.approx ? '≥ ' : ''}${fmtNum(missing.contents)}`, bytes: fmtBytes(missing.bytes), target })
    : t('cons.allThere', { target })}</p>`}
          <label class="check"><input type="checkbox" data-testid="cons-delete" checked=${form.del} onChange=${(e) => update({ del: e.target.checked })} />
            ${t('cons.deleteSources')}</label>
          ${form.del && missing && missing.contents > 0 && html`<p class="pl-warn" data-testid="cons-delete-warning" role="alert"><${Icon} name="alert" size=${14} />
            ${t('cons.deleteWarning', { n: fmtNum(missing.contents), target })}</p>`}
        </details>
        <div class="pl-form-foot">
          ${createError && html`<p class="pl-error" role="alert" data-testid="create-error">${createError.code === 'NOTHING_TO_DO' ? t('plans.NOTHING_TO_DO') : buildText(createError)}</p>`}
          <button type="button" class="btn btn-primary" data-testid="create-plan" disabled=${creating || est.loading || !data || !(ops > 0)} onClick=${create}>
            <${Icon} name="plus" size=${15} /> ${t('plans.create')}</button>
        </div>
      </form>
      <section class="pl-estimate" aria-live="polite" aria-label=${t('plans.estimate')} data-testid="estimate">
        <h2>${t('plans.estimate')} ${est.loading && html`<span class="pl-working" data-testid="estimating"><span class="spinner" aria-hidden="true"></span> ${t('plans.estimating')}</span>`}</h2>
        ${est.error && html`<p class="view-error pl-est-error" role="alert" data-testid="estimate-error">${buildText(est.error)}</p>`}
        ${!params && html`<p class="empty-hint">${t('cons.fixForm')}</p>`}
        ${data && html`<div class=${est.loading ? 'pl-stale' : ''}>
          ${nothing ? html`<p class="empty-hint" data-testid="estimate-nothing">${t('plans.NOTHING_TO_DO')}</p>` : null}
          <section class="cards pl-cards" data-testid="cons-stats">
            <${Card} id="already" label=${t('cons.already')} value=${fmtNum(stats.already_on_target || 0)} />
            <${Card} id="copy" label=${t('cons.toCopy')} value=${fmtNum(stats.to_copy || 0)} sub=${fmtBytes(stats.bytes_to_copy || 0)} />
            <${Card} id="cross" label=${t('cons.cross')} value=${fmtBytes(stats.bytes_cross_remote || 0)}
              tone=${stats.bytes_cross_remote > 0 ? 'warn' : ''} sub=${stats.bytes_cross_remote > 0 ? t('prev.crossSub') : ''} />
            <${Card} id="moves" label=${t('cons.moves')} value=${fmtNum(stats.moves_on_target || 0)} />
            <${Card} id="deletes" label=${t('cons.deletes')} value=${fmtNum(stats.deletes || 0)} tone=${stats.deletes > 0 ? 'warn' : ''} />
          </section>
          <${SkippedSummary} byReason=${stats.skipped_by_reason} /><${SkippedList} skipped=${data.skipped} />
          <${Placements} sample=${data.sample} target=${target} />
        </div>`}
        ${!data && !est.error && est.loading && html`<p class="empty-hint">${t('plans.estimating')}</p>`}
      </section>
    </div>
  </main>`;
}

// ------------------------------------------------------------------ the route

export function Plans({ route }) {
  if (route.page === 'review') return html`<${PlanReview} key=${route.id} id=${route.id} />`;
  if (route.page === 'new' && route.kind === 'dedupe') return html`<${Dedupe} route=${route} />`;
  if (route.page === 'new' && route.kind === 'consolidate') return html`<${Consolidate} route=${route} />`;
  return html`<${PlanList} route=${route} />`;
}

