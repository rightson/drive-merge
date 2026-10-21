// drive-merge visual workspace: two tree panes, a draft of staged moves, preview and approval.
// All state that matters lives on the server (the draft); this file only drives it.
import {
  html, render, useEffect, useRef, useState,
} from './vendor/htm-preact-standalone.mjs';
import { getLang, htmlLang, setLang, t } from './i18n.js';
import * as api from './api.js';
import { Icon } from './icons.js';
import { AppCtx } from './ctx.js';
import { Pane } from './tree.js';
import { CatalogBanner, StagedDrawer, SuggestionsDrawer } from './panels.js';
import { PreviewModal } from './preview.js';
import { DialogHost, Toasts } from './ui.js';
import {
  Rail, ViewHost, currentView, navigate, useRoute, useShortcuts,
} from './shell.js';
import { compareHash, plansHash } from './query.js';
import { isWaiting } from './plans.js';
import { SyncControl, useSyncJob } from './sync.js';
import {
  baseName, errorText, fmtBytes, fmtLoc, fmtNum, keepBothName, lastMove, parentOf, safeStorage, todayStamp, wordParams,
} from './util.js';

const POLL_MS = 3000;
const PLANS_POLL_MS = 10000;
const TOAST_MS = 6000;
const TOAST_ACTION_MS = 12000;
const SPLIT_KEY = 'dm-split';
const DRAFT_KEY = 'dm-draft';

const defaultTitle = () => t('draft.defaultTitle', { date: todayStamp() });
const staleCount = (d) => (d.summary && d.summary.stale_actions) || 0;

function Splitter({ frac, setFrac, boxRef }) {
  const onPointerDown = (e) => { e.preventDefault(); e.currentTarget.setPointerCapture(e.pointerId); };
  const onPointerMove = (e) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId) || !boxRef.current) return;
    const r = boxRef.current.getBoundingClientRect();
    setFrac(Math.min(0.75, Math.max(0.25, (e.clientX - r.left) / r.width)));
  };
  const onKeyDown = (e) => {
    const step = e.shiftKey ? 0.1 : 0.03;
    if (e.key === 'ArrowLeft') { e.preventDefault(); setFrac(Math.max(0.25, frac - step)); }
    if (e.key === 'ArrowRight') { e.preventDefault(); setFrac(Math.min(0.75, frac + step)); }
  };
  return html`<div class="splitter" role="separator" aria-orientation="vertical" aria-label=${t('pane.resize')}
    aria-valuenow=${Math.round(frac * 100)} aria-valuemin="25" aria-valuemax="75" tabindex="0" data-testid="splitter"
    onPointerDown=${onPointerDown} onPointerMove=${onPointerMove} onKeyDown=${onKeyDown}
    onDblClick=${() => setFrac(0.5)}></div>`;
}

function App() {
  const [lang, setLangState] = useState(getLang());
  const [fatal, setFatal] = useState('');
  const [config, setConfig] = useState(null);
  const [draft, setDraft] = useState(null);
  const [drafts, setDrafts] = useState([]);
  const [tick, setTick] = useState(0);
  const [drawer, setDrawer] = useState(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [toasts, setToasts] = useState([]);
  const [dialog, setDialog] = useState(null);
  // The clouds the last run changed while the catalog still shows what they held before ({remotes}); null once rescanned.
  const [outdated, setOutdated] = useState(null);
  // Connections (docs/ONBOARDING.md): what /v1/setup says, and a counter that moves whenever a sync has changed the catalog.
  const [setup, setSetup] = useState({ loaded: false, data: null, error: null });
  const [syncEpoch, setSyncEpoch] = useState(0);
  // The plan list (docs/PLANS.md section 7): polled while the page is visible, for the rail badge, the list view and
  // the "proposed by Claude" toast. `seen` holds the ids the page already knows, so only a new plan raises the toast.
  const [plansState, setPlansState] = useState({ list: [], loaded: false, error: null });
  const seenPlans = useRef(null);
  const [frac, setFrac] = useState(() => {
    const v = parseFloat(safeStorage('get', SPLIT_KEY));
    return v >= 0.25 && v <= 0.75 ? v : 0.5;
  });
  const draftRef = useRef(null);
  const chain = useRef(Promise.resolve());
  const inFlight = useRef(0);
  const toastId = useRef(0);
  const leftRef = useRef(null);
  const rightRef = useRef(null);
  const panesBox = useRef(null);
  const modalOpen = useRef(false);
  const wsRef = useRef(null);
  const replacing = useRef(false);
  const ranHere = useRef(new Set());
  modalOpen.current = previewOpen || !!dialog;
  // The rail and the read-only views (shell.js): the workspace below stays mounted whichever view shows.
  const route = useRoute();
  useShortcuts(route);
  const onWorkspace = route.view === 'workspace';
  const sync = useSyncJob({
    names: ((config && config.remotes) || []).map((r) => r.name),
    onFinished: (r) => {
      setSyncEpoch((n) => n + 1);
      setTick((x) => x + 1);
      if (r.all && r.ok) setOutdated(null);
      refreshSetupRef.current();
      if (r.ok) toast('success', t('sync.toast.done'));
      else if (r.failed.length) toast('warn', t('sync.toast.failed', { clouds: r.failed.join(', ') }));
      else toast('error', r.error || t('sync.pill.failed'));
    },
  });
  const refreshSetupRef = useRef(() => {});

  useEffect(() => { safeStorage('set', SPLIT_KEY, String(frac)); }, [frac]);

  // ---------------------------------------------------------------- toasts and dialogs
  const dismiss = (id) => setToasts((xs) => xs.filter((x) => x.id !== id));
  /** Errors stay until closed; everything else leaves by itself (longer when it carries a button). */
  function toast(kind, text, action) {
    toastId.current += 1;
    const id = toastId.current;
    const ttl = kind === 'error' ? 0 : action ? TOAST_ACTION_MS : TOAST_MS;
    // The same message again replaces its earlier copy instead of piling up under it.
    setToasts((xs) => [...xs.filter((x) => x.kind !== kind || x.text !== text).slice(-3), { id, kind, text, action, ttl }]);
    return id;
  }
  const ask = (type, opts) => new Promise((resolve) => {
    setDialog({ type, ...opts, resolve: (v) => { setDialog(null); resolve(v); } });
  });
  const confirmDialog = (opts) => ask('confirm', opts);
  const promptDialog = (opts) => ask('prompt', opts);

  // ---------------------------------------------------------------- draft state
  function adopt(d) {
    const prev = draftRef.current;
    // Applied by someone else (a terminal): the clouds changed and this catalog has not heard of it.
    if (prev && prev.id === d.id && prev.status === 'open' && d.status === 'applied' && !ranHere.current.has(d.id)) {
      markOutdated((d.summary || prev.summary || {}).remotes_touched, d.id);
    }
    draftRef.current = d;
    setDraft(d);
    safeStorage('set', DRAFT_KEY, String(d.id));
    return d;
  }

  async function refreshDraftList() {
    try { setDrafts(await api.listDrafts()); } catch (e) { /* the select keeps its old options */ }
  }

  async function syncDraft() {
    const d = adopt(await api.getDraft(draftRef.current.id));
    setTick((x) => x + 1);
    refreshDraftList();
    return d;
  }

  async function startDraft(title) {
    const d = adopt(await api.createDraft(title || defaultTitle()));
    setTick((x) => x + 1);
    refreshDraftList();
    return d;
  }

  /** The open draft was applied, discarded or lost: continue on a fresh one. */
  async function replaceDraft(message) {
    if (replacing.current) return;
    replacing.current = true;
    try {
      const list = await api.listDrafts();
      const open = list.find((x) => x.id !== (draftRef.current && draftRef.current.id));
      if (open) adopt(await api.getDraft(open.id)); else await startDraft();
      setTick((x) => x + 1);
      refreshDraftList();
      if (message) toast('info', message);
    } catch (e) {
      toast('error', errorText(e));
    } finally {
      replacing.current = false;
    }
  }

  // Everything that changes the draft runs one at a time, in the order the user did it.
  const enqueue = (fn) => {
    inFlight.current += 1;
    const p = chain.current.then(fn, fn);
    chain.current = p.catch(() => {}).then(() => { inFlight.current -= 1; });
    return p;
  };

  function showWarnings(warnings, dst) {
    const seen = new Set();
    (warnings || []).forEach((w) => {
      if (seen.has(w.code)) return;
      seen.add(w.code);
      toast('warn', t(`warn.${w.code}`, { dst: dst ? `${dst.remote}:${dst.path}` : '', ...wordParams('warn', w.params) }, w.message));
    });
  }

  async function failStaging(e, items, dst, opts) {
    if (e.code === 'NOT_OPEN' || (e.code === 'NOT_FOUND' && (e.params ? e.params.draft !== undefined && e.params.action === undefined : /draft/.test(e.message)))) {
      await replaceDraft(t('draft.closed'));
      return;
    }
    if (e.code === 'NAME_EXISTS' && opts.keepBoth !== false) {
      toast('error', errorText(e), { label: t('act.keepBoth'), run: () => keepBoth(items, dst) });
    } else {
      toast('error', errorText(e));
    }
  }

  const stageMove = (items, dst, opts = {}) => enqueue(async () => {
    if (!draftRef.current) return false;
    const body = { kind: 'move', items, dst: { remote: dst.remote, path: dst.path } };
    if (opts.newName) body.new_name = opts.newName;
    try {
      const r = await api.addAction(draftRef.current.id, body);
      showWarnings(r.warnings, dst);
      await syncDraft();
      return true;
    } catch (e) {
      await failStaging(e, items, dst, opts);
      return false;
    }
  });

  const stageMkdir = (dst, name) => enqueue(async () => {
    if (!draftRef.current) return false;
    try {
      const r = await api.addAction(draftRef.current.id, { kind: 'mkdir', dst: { remote: dst.remote, path: dst.path }, new_name: name });
      showWarnings(r.warnings, dst);
      await syncDraft();
      return true;
    } catch (e) {
      await failStaging(e, [], dst, { keepBoth: false });
      return false;
    }
  });

  const stageMany = (actions) => enqueue(async () => {
    let staged = 0;
    for (const a of actions) {
      try {
        const r = await api.addAction(draftRef.current.id, a);
        showWarnings(r.warnings, a.dst);
        staged += 1;
      } catch (e) {
        toast('error', errorText(e));
        break;
      }
    }
    if (staged) { await syncDraft(); toast('success', t('sug.addedN', { n: staged })); }
    return staged;
  });

  /** Retry a move that hit NAME_EXISTS, giving each clashing item "name (2).ext". */
  const keepBoth = (items, dst) => enqueue(async () => {
    let staged = 0;
    for (const it of items) {
      const name = baseName(it.path);
      const isDir = !!it.is_dir;
      for (let n = items.length === 1 ? 2 : 1; n < 40; n += 1) {
        const body = { kind: 'move', items: [{ remote: it.remote, path: it.path }], dst: { remote: dst.remote, path: dst.path } };
        if (n > 1) body.new_name = keepBothName(name, n, isDir);
        try {
          const r = await api.addAction(draftRef.current.id, body);
          showWarnings(r.warnings, dst);
          staged += 1;
          break;
        } catch (e) {
          if (e.code === 'NAME_EXISTS') continue;
          toast('error', errorText(e));
          n = 99;
        }
      }
    }
    if (staged) { await syncDraft(); toast('success', t('act.keptBoth', { n: staged })); }
    return staged;
  });

  const renameItem = (item, newName) => stageMove(
    [{ remote: item.remote, path: item.path }],
    { remote: item.remote, path: parentOf(item.path) },
    { newName, keepBoth: false },
  );

  const stepHistory = (kind) => enqueue(async () => {
    const d = draftRef.current;
    if (!d) return;
    if (kind === 'undo' ? !d.undo_available : !d.redo_available) {
      toast('info', t(kind === 'undo' ? 'err.NOTHING_TO_UNDO' : 'err.NOTHING_TO_REDO'));
      return;
    }
    try {
      adopt(await (kind === 'undo' ? api.undo(d.id) : api.redo(d.id)));
      setTick((x) => x + 1);
      refreshDraftList();
    } catch (e) {
      toast('error', errorText(e));
      try { await syncDraft(); } catch (e2) { /* keep the toast */ }
    }
  });

  const removeAction = (id) => enqueue(async () => {
    try {
      const r = await api.removeAction(draftRef.current.id, id);
      await syncDraft();
      if (r.removed && r.removed.length > 1) toast('info', t('staged.alsoRemoved', { n: r.removed.length - 1 }));
      return true;
    } catch (e) {
      toast('error', errorText(e));
      return false;
    }
  });

  /**
   * One row holds two different files under one name (a merge clash). Renaming the row renames both, so the clash
   * would stay; each file can only be addressed before the step that put them together. Offer taking that step back.
   */
  async function resolveClash(side, origins, name) {
    const steps = draftRef.current.actions;
    const last = origins.map((o) => ({ o, hit: lastMove(steps, o) })).filter((x) => x.hit)
      .sort((a, b) => b.hit.step.seq - a.hit.step.seq)[0];
    if (!last) { toast('warn', t('clash.explain')); return; }
    const { step, before } = last.hit;
    const ok = await confirmDialog({
      title: t('clash.title'), message: t('clash.text', { name, seq: step.seq, from: fmtLoc(before) }),
      okLabel: t('clash.remove', { seq: step.seq }),
    });
    if (!ok || !(await removeAction(step.id))) return;
    toast('info', t('clash.removed', { name }), { label: t('clash.show'), run: () => reveal(side, before) });
  }

  /**
   * A run changed these clouds (or failed part way): the catalog is behind them until a scan finishes. `ranDraft` is
   * the draft this page ran itself, so that the poll does not announce the same run a second time after a rescan.
   */
  const markOutdated = (remotes, ranDraft) => {
    if (ranDraft) ranHere.current.add(ranDraft);
    setOutdated((o) => ({
      remotes: [...new Set([...(o ? o.remotes : []), ...(remotes && remotes.length ? remotes : (config.remotes || []).map((r) => r.name))])],
    }));
  };
  const catalogRefreshed = () => { setOutdated(null); setTick((x) => x + 1); };

  async function clearDraft() {
    const ok = await confirmDialog({
      title: t('staged.clearTitle'), message: t('staged.clearText'), okLabel: t('staged.clearOk'), danger: true,
    });
    if (!ok) return;
    try {
      await api.discardDraft(draftRef.current.id);
      await startDraft();
      toast('success', t('staged.cleared'));
    } catch (e) {
      toast('error', errorText(e));
    }
  }

  async function newDraft() {
    const title = await promptDialog({
      title: t('draft.newTitle'), label: t('draft.titleLabel'), value: defaultTitle(), okLabel: t('draft.create'),
    });
    if (!title) return;
    try { await startDraft(title); } catch (e) { toast('error', errorText(e)); }
  }

  async function selectDraft(id) {
    try {
      adopt(await api.getDraft(id));
      setTick((x) => x + 1);
    } catch (e) {
      toast('error', errorText(e));
      refreshDraftList();
    }
  }

  async function afterExecution() {
    setTick((x) => x + 1);
    try {
      const d = await api.getDraft(draftRef.current.id);
      if (d.status !== 'open') await replaceDraft(t('draft.autoNew')); else adopt(d);
    } catch (e) {
      await replaceDraft(t('draft.autoNew'));
    }
  }

  const reveal = (side, loc, opts) => {
    const pane = (side === 'left' ? leftRef : rightRef).current;
    return pane ? pane.reveal(loc, opts) : undefined;
  };

  /** Show the workspace and focus an item in the left pane (the other views' Reveal). */
  const revealInWorkspace = async (loc) => {
    navigate('#/workspace');
    // The panes are laid out only once the workspace is un-hidden; scrolling to the row needs that.
    for (let n = 0; n < 40 && (!wsRef.current || wsRef.current.hidden); n += 1) await new Promise((r) => requestAnimationFrame(r));
    return reveal('left', loc);
  };

  /** A workspace preview listed among the plans: open the workspace on its draft with the preview dialog. */
  async function openWorkspacePreview(draftId) {
    navigate('#/workspace');
    if (draftId && draftRef.current && draftRef.current.id !== draftId) await selectDraft(draftId);
    setToasts([]);
    setPreviewOpen(true);
  }

  /** Re-read the connections and the cloud list (a connection made or removed while the server runs is visible at once). */
  async function refreshSetup() {
    try {
      const [data, cfg] = await Promise.all([api.setup(), api.config()]);
      setSetup({ loaded: true, data, error: null });
      setConfig((old) => ({ ...cfg, csrf: undefined, approvals_enabled: cfg.approvals_enabled === undefined && old ? old.approvals_enabled : cfg.approvals_enabled }));
      return data;
    } catch (e) {
      setSetup((x) => ({ ...x, loaded: true, error: e }));
      return null;
    }
  }
  refreshSetupRef.current = refreshSetup;

  // ---------------------------------------------------------------- start up
  useEffect(() => {
    (async () => {
      const hash = new URLSearchParams(window.location.hash.replace(/^#/, ''));
      const token = hash.get('token');
      // The one-time token must not stay in the address bar, history or a bookmark.
      if (token) window.history.replaceState(null, '', window.location.pathname + window.location.search);
      api.setUnauthorizedHandler(() => setFatal(t('fatal.session')));
      try {
        let approvals;
        if (token) {
          const session = await api.login(token);
          api.setCsrf(session.csrf);
          approvals = session.approvals_enabled;
        }
        const cfg = await api.config();
        // A reloaded page has no URL token; the server hands a live session its CSRF token again.
        if (cfg.csrf && !api.hasCsrf()) api.setCsrf(cfg.csrf);
        if (cfg.approvals_enabled === undefined) cfg.approvals_enabled = !!approvals;
        setConfig(cfg);
        // A run that changed a cloud since its last scan: say so again after a reload, as the page did right after the run.
        if (cfg.outdated && cfg.outdated.length) setOutdated({ remotes: cfg.outdated });
        // The connections decide where the page opens (no route, nothing connected: the Welcome screen). A server that
        // does not know the route (older, or a test double) leaves the page as it was.
        try {
          const data = await api.setup();
          setSetup({ loaded: true, data, error: null });
          if (!window.location.hash && (data.first_run || (!(data.connections || []).length && !(cfg.remotes || []).length))) navigate('#/clouds', { replace: true });
        } catch (e) { setSetup({ loaded: true, data: null, error: e }); }
        const list = await api.listDrafts();
        setDrafts(list);
        const saved = Number(safeStorage('get', DRAFT_KEY));
        const pick = list.find((x) => x.id === saved) || list[0];
        if (pick) adopt(await api.getDraft(pick.id)); else await startDraft();
      } catch (e) {
        setFatal(e.status === 401 || e.status === 403 ? (token ? t('fatal.login') : t('fatal.session')) : errorText(e));
      }
    })();
  }, []);

  // Poll the open draft: another tab or Claude (through MCP) may have staged something.
  const draftId = draft ? draft.id : null;
  useEffect(() => {
    if (!draftId) return undefined;
    const timer = setInterval(async () => {
      const mine = draftRef.current;
      if (!mine || mine.id !== draftId || document.hidden || inFlight.current > 0) return;
      try {
        const d = await api.getDraft(draftId);
        // Our own change may have landed while this request was out; that is not news.
        if (draftRef.current !== mine || inFlight.current > 0) return;
        if (d.status !== 'open') {
          adopt(d);
          if (!modalOpen.current) replaceDraft(t('draft.closed'));
          return;
        }
        const stale = staleCount(d);
        if (d.version === mine.version) {
          if (stale === staleCount(mine)) return;
          // Only the catalog moved under the draft (a rescan): the trees are refreshed and the steps it broke are named.
          adopt(d);
          setTick((x) => x + 1);
          if (stale > staleCount(mine)) toast('warn', t('staged.staleTip', { n: stale }));
          return;
        }
        const known = new Set(mine.actions.map((a) => a.id));
        const fresh = d.actions.filter((a) => a.state === 'active' && !known.has(a.id));
        adopt(d);
        setTick((x) => x + 1);
        refreshDraftList();
        if (fresh.some((a) => a.created_by === 'mcp')) toast('claude', t('draft.fromClaude'));
        else toast('info', t('draft.changedElsewhere'));
      } catch (e) { /* offline for a moment: try again next time */ }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [draftId]);

  async function refreshPlans() {
    try {
      const list = await api.planSummaries({ limit: 100 });
      setPlansState({ list, loaded: true, error: null });
      if (seenPlans.current === null) {
        seenPlans.current = new Set(list.map((p) => p.id));
        return list;
      }
      list.filter((p) => !seenPlans.current.has(p.id)).forEach((p) => {
        seenPlans.current.add(p.id);
        if (p.created_by === 'mcp' && isWaiting(p)) {
          toast('claude', t('plans.fromClaude', { id: p.id, title: p.title || '' }),
            { label: t('plans.review'), run: () => navigate(plansHash(`/${p.id}`)) });
        }
      });
      return list;
    } catch (e) {
      setPlansState((s) => ({ ...s, loaded: true, error: e }));  // the next poll tries again
      return null;
    }
  }
  const refreshPlansRef = useRef(refreshPlans);
  refreshPlansRef.current = refreshPlans;
  const ready = !!config && !!draft;
  useEffect(() => { if (ready) sync.resume(); }, [ready]);
  useEffect(() => {
    if (!ready) return undefined;
    refreshPlansRef.current();
    const tick = () => { if (!document.hidden) refreshPlansRef.current(); };
    const timer = setInterval(tick, PLANS_POLL_MS);
    document.addEventListener('visibilitychange', tick);
    return () => { clearInterval(timer); document.removeEventListener('visibilitychange', tick); };
  }, [ready]);

  // Undo / redo from the keyboard, unless the user is typing in a field or a dialog is open.
  useEffect(() => {
    const onKey = (e) => {
      const el = e.target;
      if (el && (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) || el.isContentEditable)) return;
      if (modalOpen.current || !(e.ctrlKey || e.metaKey) || e.altKey || currentView() !== 'workspace') return;
      const k = e.key.toLowerCase();
      if (k === 'z') { e.preventDefault(); stepHistory(e.shiftKey ? 'redo' : 'undo'); }
      else if (k === 'y') { e.preventDefault(); stepHistory('redo'); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const switchLang = () => {
    const next = getLang() === 'zh-TW' ? 'en' : 'zh-TW';
    setLang(next);
    setLangState(next);
    document.documentElement.lang = htmlLang(next);
    document.title = t('app.title');
  };

  // ---------------------------------------------------------------- render
  if (fatal) {
    return html`<main class="fatal" data-testid="fatal" role="alert">
      <${Icon} name="alert" size=${28} /><h1>${t('fatal.title')}</h1><p>${fatal}</p></main>`;
  }
  if (!config || !draft) return html`<main class="fatal" data-testid="loading"><span class="spinner"></span> ${t('common.loading')}</main>`;

  const active = draft.actions.filter((a) => a.state === 'active');
  const sum = draft.summary || {};
  const ctx = {
    config, draft, toast, confirm: confirmDialog, prompt: promptDialog, reveal,
    stageMove, stageMkdir, stageMany, rename: renameItem, afterExecution, resolveClash,
    outdated, markOutdated, catalogRefreshed,
    setup, refreshSetup, sync, syncEpoch,
    plans: plansState, refreshPlans, openWorkspacePreview,
    nav: navigate, lang, switchLang, revealInWorkspace, openWorkspace: () => navigate('#/workspace'),
    compare: (a, b) => navigate(compareHash({ remote: a.remote, path: a.path }, { remote: b.remote, path: b.path })),
    compareWith: (a) => navigate(compareHash({ remote: a.remote, path: a.path }, null)),
    undo: () => stepHistory('undo'), redo: () => stepHistory('redo'), removeAction, clearDraft,
  };
  const toggleDrawer = (name) => setDrawer((d) => (d === name ? null : name));

  return html`<${AppCtx.Provider} value=${ctx}>
    <div class="app" data-lang=${lang} data-drawer=${onWorkspace ? drawer || undefined : undefined} data-view=${route.view}>
      <${Rail} route=${route} waiting=${plansState.list.filter(isWaiting).length} />
      <div class="shell-main">
      ${config.approvals_enabled === false && html`<div class="banner banner-info" role="note" data-testid="approvals-banner">
        <${Icon} name="info" size=${17} />
        <span><strong>${t('banner.title')}</strong> ${t('banner.text')}
          <code>drivemerge plan approve ${'<id>'}</code> ${t('banner.and')} <code>drivemerge plan execute ${'<id>'}</code> ${t('banner.after')}</span>
      </div>`}
      ${outdated && html`<${CatalogBanner} remotes=${outdated.remotes} onFresh=${() => { catalogRefreshed(); toast('success', t('prog.rescanned')); }}
        onDismiss=${() => setOutdated(null)} />`}
      <div class="ws" ref=${wsRef} hidden=${!onWorkspace} data-testid="workspace-host">
      <header class="topbar">
        <div class="brand"><${Icon} name="folder" size=${20} /><span>drive-merge</span>
          ${config.version && html`<small title=${t('app.version')}>${config.version}</small>`}</div>
        <div class="group draft-group">
          <label class="visually-hidden" for="draft-select">${t('draft.select')}</label>
          <select id="draft-select" class="select" data-testid="draft-select" value=${draft.id}
            onChange=${(e) => selectDraft(Number(e.target.value))}>
            ${(drafts.some((x) => x.id === draft.id) ? drafts : [draft, ...drafts]).map((x) => html`<option key=${x.id} value=${x.id}>
              #${x.id} ${x.title}</option>`)}
          </select>
          <button class="btn btn-small" data-testid="draft-new" onClick=${newDraft} title=${t('draft.new')}>
            <${Icon} name="plus" size=${15} /><span class="btn-text">${t('draft.new')}</span></button>
        </div>
        <div class="group">
          <button class="btn" data-testid="undo-button" disabled=${!draft.undo_available} onClick=${() => stepHistory('undo')}
            title=${t('draft.undoTip')}><${Icon} name="undo" size=${16} /><span class="btn-text">${t('draft.undo')}</span></button>
          <button class="btn" data-testid="redo-button" disabled=${!draft.redo_available} onClick=${() => stepHistory('redo')}
            title=${t('draft.redoTip')}><${Icon} name="redo" size=${16} /><span class="btn-text">${t('draft.redo')}</span></button>
        </div>
        <div class="group">
          <button class=${'btn' + (drawer === 'staged' ? ' on' : '')} data-testid="staged-toggle" aria-pressed=${drawer === 'staged'}
            title=${t('staged.button')} onClick=${() => toggleDrawer('staged')}><${Icon} name="list" size=${16} />
            <span class="btn-text">${t('staged.button')}</span>
            <span class="count-pill" data-testid="staged-count">${active.length}</span>
            ${sum.stale_actions > 0 && html`<span class="count-pill count-pill-warn" data-testid="staged-stale-count"
              title=${t('staged.staleTip', { n: sum.stale_actions })}><${Icon} name="alert" size=${11} />${sum.stale_actions}</span>`}</button>
          <button class=${'btn' + (drawer === 'suggestions' ? ' on' : '')} data-testid="suggestions-toggle" aria-pressed=${drawer === 'suggestions'}
            title=${t('sug.button')} onClick=${() => toggleDrawer('suggestions')}><${Icon} name="sparkle" size=${16} />
            <span class="btn-text">${t('sug.button')}</span></button>
        </div>
        <span class="spacer"></span>
        ${sum.moved_files > 0 && html`<span class="topbar-summary" data-testid="draft-summary">
          ${t('draft.summary', { files: fmtNum(sum.moved_files), bytes: fmtBytes(sum.moved_bytes) })}</span>`}
        ${onWorkspace && html`<${SyncControl} />`}
        <button class="btn btn-small" data-testid="lang-toggle" onClick=${switchLang} title=${t('app.language')}>
          ${lang === 'zh-TW' ? 'EN' : '中文'}</button>
        <button class="btn btn-primary" data-testid="preview-button" disabled=${active.length === 0}
          onClick=${() => { setToasts([]); setPreviewOpen(true); }}><${Icon} name="eye" size=${16} /> ${t('draft.preview')}</button>
      </header>
      <div class="workspace">
        <div class="panes" ref=${panesBox} style=${{ gridTemplateColumns: `minmax(260px, ${frac}fr) 10px minmax(260px, ${1 - frac}fr)` }}>
          <${Pane} side="left" draft=${draft} tick=${tick} handle=${leftRef} />
          <${Splitter} frac=${frac} setFrac=${setFrac} boxRef=${panesBox} />
          <${Pane} side="right" draft=${draft} tick=${tick} handle=${rightRef} />
        </div>
        ${drawer === 'staged' && html`<${StagedDrawer} draft=${draft} onClose=${() => setDrawer(null)} />`}
        ${drawer === 'suggestions' && html`<${SuggestionsDrawer} draft=${draft} onClose=${() => setDrawer(null)} />`}
      </div>
      </div>
      ${!onWorkspace && html`<${ViewHost} route=${route} />`}
      </div>
      ${previewOpen && html`<${PreviewModal} draft=${draft} onClose=${() => setPreviewOpen(false)} />`}
      <${DialogHost} request=${dialog} />
      <${Toasts} toasts=${toasts} dismiss=${dismiss} />
    </div>
  <//>`;
}

document.title = t('app.title');
document.documentElement.lang = htmlLang(getLang());
render(html`<${App} />`, document.getElementById('root'));
