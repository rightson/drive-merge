// Pure helpers: paths, formatting, action descriptions. No DOM, no state.
import { t } from './i18n.js';

// A tree node is identified by "remote:path" (path '' is the remote root). Remote names cannot contain ':'.
export const keyOf = (remote, path) => `${remote}:${path}`;
export const splitKey = (key) => {
  const i = key.indexOf(':');
  return { remote: key.slice(0, i), path: key.slice(i + 1) };
};
export const fmtLoc = (loc) => keyOf(loc.remote, loc.path);

export const joinPath = (base, name) => (base ? `${base}/${name}` : name);
export const parentOf = (path) => (path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '');
export const baseName = (path) => path.slice(path.lastIndexOf('/') + 1);
/** True when `path` is `base` or lies below it. Whole segments only: a/b does not contain a/bc. */
export const isUnder = (path, base) => base === '' || path === base || path.startsWith(base + '/');

export function ancestorsOf(path) {
  const parts = path ? path.split('/') : [];
  return parts.map((_, i) => parts.slice(0, i + 1).join('/'));
}

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
export function fmtBytes(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '';
  let v = Number(n);
  let i = 0;
  while (v >= 1024 && i < UNITS.length - 1) { v /= 1024; i += 1; }
  return i === 0 ? `${v} B` : `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${UNITS[i]}`;
}

const NUM = new Intl.NumberFormat('en-US');
export const fmtNum = (n) => NUM.format(Number(n) || 0);

/** "report.pdf" -> "report (2).pdf"; folders and extension-less names just get the suffix. */
export function keepBothName(name, n, isDir) {
  const dot = name.lastIndexOf('.');
  if (isDir || dot <= 0) return `${name} (${n})`;
  return `${name.slice(0, dot)} (${n})${name.slice(dot)}`;
}

// Message parameters that are byte counts, and those that name a short kind with a sentence of its own (`why: "moved"`
// is worded by `<ns>.why.moved`).
const BYTE_PARAMS = new Set(['bytes', 'free', 'size', 'expected']);
const KIND_PARAMS = ['why', 'problem', 'what', 'role'];

/**
 * The `params` a server message came with, ready for t(): a place ({remote, path}) becomes "remote:path", a byte count
 * is written with its unit, and a kind (why / problem / what) is replaced by its sentence in namespace `ns`.
 */
export function wordParams(ns, params) {
  const out = {};
  for (const [k, v] of Object.entries(params || {})) {
    if (v && typeof v === 'object' && !Array.isArray(v) && v.remote !== undefined) out[k] = fmtLoc({ remote: v.remote, path: v.path || '' });
    else if (BYTE_PARAMS.has(k) && typeof v === 'number') out[k] = fmtBytes(v);
    else out[k] = v;
  }
  for (const k of KIND_PARAMS) {
    if (typeof out[k] === 'string') out[k] = t(`${ns}.${k}.${out[k]}`, out, out[k]);
  }
  // The form field a refusal is about (MISSING_FIELD: the Apple ID or its password).
  if (out.field === 'apple_id' || out.field === 'password') out.field = t(`conn.field.${out.field}`);
  // Which side of a comparison a refusal is about ("left" | "right").
  if (out.side === 'left' || out.side === 'right') out.side = t(`cmp.side.${out.side}`);
  // The state a plan is in ('done', 'cancelled'), as a word of the page's language.
  if (typeof out.plan_status === 'string') out.plan_status = t(`pstatus.${out.plan_status}`, null, out.plan_status);
  if (typeof out.files === 'number') out.files = fmtNum(out.files);
  if (typeof out.limit === 'number') out.limit = fmtNum(out.limit);
  return out;
}

/**
 * The detail line under a violation, warning or conflict, in the page's language: `<ns>.<CODE>.detail`, or
 * `<ns>.<CODE>.detail.<variant>` for a code with several sentences. A message the page has no sentence for (a newer
 * server, a report stored before `params` existed) is shown as the server wrote it, in English.
 */
export function problemDetail(ns, item) {
  const code = item.code;
  const params = item.params || {};
  const key = `${ns}.${code}.detail${params.variant ? `.${params.variant}` : ''}`;
  return t(key, wordParams(ns, params), item.message !== undefined ? item.message : item.reason);
}

export function errorText(err) {
  if (!err) return '';
  if (err.status === 0) return t('err.network');
  const known = err.code && t(`err.${err.code}`, wordParams('err', err.params), '');
  return known || err.message || t('err.unknown');
}

/** Title and detail line of one staged action, for the drawer. */
export function describeAction(a) {
  const dst = fmtLoc(a.dst);
  if (a.kind === 'mkdir') {
    return { title: t('act.mkdir', { name: a.new_name, dst }), detail: '' };
  }
  const items = a.items || [];
  const names = items.map((i) => baseName(i.path) || i.remote);
  const first = items[0];
  const sep = t('common.listSep');
  const detail = names.length > 3 ? `${names.slice(0, 3).join(sep)} …` : names.join(sep);
  if (a.new_name && items.length === 1 && first.remote === a.dst.remote && parentOf(first.path) === a.dst.path) {
    return { title: t('act.rename', { from: names[0], to: a.new_name }), detail: '' };
  }
  return { title: t('act.move', { n: items.length, dst }), detail };
}

/** Why a staged step no longer fits the catalog, in words: its `stale_reason` ({code, message, items}). */
export function staleText(reason) {
  if (!reason) return '';
  const items = (reason.items || []).map(fmtLoc).join(t('common.listSep'));
  return t(`stale.${reason.code}`, { items }, reason.message);
}

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** What a rescan job is doing, in words (its `progress` is {stage} or {remote, remote_index, remotes_total, files}). */
export function rescanText(job) {
  const p = (job && job.progress) || {};
  if (p.stage === 'identity' || p.stage === 'dir_stats') return t(`prog.stage.${p.stage}`);
  if (!p.remote) return t('prog.wait');
  return t('prog.scanning', { remote: p.remote, i: p.remote_index || 1, n: p.remotes_total || 1, files: fmtNum(p.files || 0) });
}

/**
 * Replay the staged steps on the file the catalog has at `origin` (a step moves everything at or below its items, as
 * the server's forward map does) and return the last step that moved it, with where it was just before that step:
 * the step a name clash can be taken back from. Null when no step moved it.
 */
export function lastMove(actions, origin) {
  let at = { remote: origin.remote, path: origin.path };
  let found = null;
  for (const a of [...actions].sort((x, y) => x.seq - y.seq)) {
    if (a.state !== 'active' || a.kind !== 'move') continue;
    const items = a.items || [];
    const hit = items.find((it) => it.path && it.remote === at.remote && isUnder(at.path, it.path));
    if (!hit) continue;
    const name = (items.length === 1 && a.new_name) || baseName(hit.path);
    const next = { remote: a.dst.remote, path: joinPath(a.dst.path, name) + at.path.slice(hit.path.length) };
    if (next.remote !== at.remote || next.path !== at.path) found = { step: a, before: at };
    at = next;
  }
  return found;
}

export function todayStamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export function safeStorage(op, key, value) {
  try {
    if (op === 'get') return window.localStorage.getItem(key);
    if (op === 'set') window.localStorage.setItem(key, value);
  } catch (e) { /* private window or blocked storage: the page works without it */ }
  return null;
}

/** First "remote:path" location quoted in a simulator message ('gdrive:a/b' or "gdrive:a/b"). */
export function locationInMessage(message, remoteNames) {
  if (!message) return null;
  for (const name of remoteNames) {
    for (const q of ["'", '"']) {
      const start = message.indexOf(`${q}${name}:`);
      if (start < 0) continue;
      const from = start + 1 + name.length + 1;
      const end = message.indexOf(q, from);
      if (end > from - 1) return { remote: name, path: message.slice(from, end) };
    }
  }
  return null;
}

/** Where a simulator violation happened: its `at` parameter, else the first quoted place in its English message. */
export function violationLoc(v, remoteNames) {
  const at = v.params && v.params.at;
  if (at && at.remote !== undefined) return { remote: at.remote, path: at.path || '' };
  return locationInMessage(v.message, remoteNames);
}

/** True for the NO_WITNESS that says the witness file is not there (not: none given, or the file itself). */
const missingWitness = (v) => (v.params && v.params.variant ? v.params.variant === 'missing' : /no live file at the witness/.test(v.message || ''));

/**
 * The problems worth showing. The simulator and the workspace both report some failures, and one cause can set off
 * another: a DST_EXISTS on the very spot a conflict already explains, or a NO_WITNESS for a copy that a read-only
 * cloud (READ_ONLY_REMOTE) already prevents, are the same problem again, so the page counts and draws it once. So is
 * a sentence the simulator says again for every op (it names no op, only the place).
 */
export function rootCauses(violations, conflicts, remoteNames) {
  const spot = (loc) => (loc ? fmtLoc({ remote: loc.remote, path: loc.path || '' }) : '');
  const explained = new Set(conflicts.map((c) => spot(c.to)));
  const readOnly = new Set(violations.filter((v) => v.code === 'READ_ONLY_REMOTE')
    .map((v) => (violationLoc(v, remoteNames) || {}).remote));
  const said = new Set();
  return violations.filter((v) => {
    const again = `${v.code}|${v.message}`;
    if (said.has(again)) return false;
    said.add(again);
    const at = violationLoc(v, remoteNames);
    if (!at) return true;
    if (v.code === 'DST_EXISTS' && explained.has(spot(at))) return false;
    if (v.code === 'NO_WITNESS' && readOnly.has(at.remote) && missingWitness(v)) return false;
    return true;
  });
}

/** Words for each suggestion `code`: the placeholders its `sug.<CODE>.reason` needs, made from the server's `params`. */
const SUGGESTION_REASON = {
  RELATED: (p) => {
    const quote = (terms) => terms.map((term) => t('sug.q', { term })).join(t('sug.sep'));
    const why = [];
    if ((p.name_terms || []).length) why.push(t('sug.RELATED.why.names', { terms: quote(p.name_terms) }));
    if ((p.content_terms || []).length) why.push(t('sug.RELATED.why.files', { terms: quote(p.content_terms) }));
    if (p.same) why.push(t('sug.RELATED.why.same', { n: p.same }));
    return { why: why.join(t('sug.joinReasons')) || t('sug.RELATED.why.alike'), anchor: p.anchor };
  },
  DUPLICATES: (p) => ({
    pct: `${Math.round((p.similarity || 0) * 100)}%`, shared: fmtNum(p.shared || 0), kept: p.kept, saved: fmtBytes(p.saved_bytes || 0),
  }),
};

/**
 * Title and reason of a suggestion in the page's language. The server sends them in English together with a `code`
 * and the `params` they were made from; the page words those itself, and shows the server's English text only for a
 * code it does not know.
 */
export function suggestionText(s) {
  const p = s.params;
  const reason = SUGGESTION_REASON[s.code];
  if (!p || !reason) return { title: s.title, reason: s.reason };
  const names = p.names || [];
  const shown = names.slice(0, 3).join(t('common.listSep')) + (names.length > 3 ? '…' : '');
  return {
    title: t(`sug.${s.code}.title`, { n: p.count, names: shown }, s.title),
    reason: t(`sug.${s.code}.reason`, reason(p), s.reason),
  };
}

/** "2024-03-01T10:00:00Z" -> "2024-03-01"; "" for a missing time. */
export const fmtDay = (iso) => (iso ? String(iso).slice(0, 10) : '');
/** "2024-03-01T10:00:00Z" -> "2024-03-01 10:00" (as the catalog wrote it, not converted). */
export const fmtDateTime = (iso) => (iso ? String(iso).slice(0, 16).replace('T', ' ') : '');

/** A refusal of a plan builder (`build.<CODE>`, worded from its params), else the usual refusal text. */
export function buildText(err) {
  if (err && err.code) {
    const known = t(`build.${err.code}`, wordParams('build', err.params), '');
    if (known) return known;
  }
  return errorText(err);
}

/** "3 min ago", "5 h ago", "2 d ago" for a catalog time ("2024-03-01T10:00:00Z"); "" when there is none. */
export function ago(iso, now = Date.now()) {
  if (!iso) return '';
  const then = Date.parse(String(iso).endsWith('Z') || /[+-]\d\d:?\d\d$/.test(String(iso)) ? iso : `${iso}Z`);
  if (Number.isNaN(then)) return '';
  const min = Math.max(0, Math.round((now - then) / 60000));
  if (min < 1) return t('ago.now');
  if (min < 60) return t('ago.min', { n: min });
  if (min < 2880) return t('ago.hour', { n: Math.round(min / 60) });
  return t('ago.day', { n: Math.round(min / 1440) });
}

/** A cloud's data is "old" after this many hours without a complete sync (docs/ONBOARDING.md section 5). */
export const STALE_HOURS = 24;
/** True when `iso` (the end of the last complete sync) is missing or older than STALE_HOURS. */
export function isStaleAge(iso, now = Date.now()) {
  if (!iso) return true;
  const then = Date.parse(String(iso).endsWith('Z') || /[+-]\d\d:?\d\d$/.test(String(iso)) ? iso : `${iso}Z`);
  return Number.isNaN(then) || now - then > STALE_HOURS * 3600 * 1000;
}
