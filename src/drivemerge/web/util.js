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

export function errorText(err) {
  if (!err) return '';
  if (err.status === 0) return t('err.network');
  const known = err.code && t(`err.${err.code}`, null, '');
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
  const detail = names.length > 3 ? `${names.slice(0, 3).join('、')} …` : names.join('、');
  if (a.new_name && items.length === 1 && first.remote === a.dst.remote && parentOf(first.path) === a.dst.path) {
    return { title: t('act.rename', { from: names[0], to: a.new_name }), detail: '' };
  }
  return { title: t('act.move', { n: items.length, dst }), detail };
}

/** Why a staged step no longer fits the catalog, in words: its `stale_reason` ({code, message, items}). */
export function staleText(reason) {
  if (!reason) return '';
  const items = (reason.items || []).map(fmtLoc).join('、');
  return t(`stale.${reason.code}`, { items }, reason.message);
}

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

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
