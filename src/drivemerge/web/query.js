// The Explore query language (docs/EXPLORE.md section 4) and the route strings of the new views. Pure functions: no DOM,
// no state, no strings for people (the page words chips and errors itself from the codes returned here).

const FILTER_KEYS = ['in', 'on', 'not', 'only', 'copies', 'type', 'cat', 'size', 'modified', 'path'];
export const QUERY_KEYS = FILTER_KEYS;

// ---------------------------------------------------------------- routes
const loose = (s) => encodeURIComponent(s).replace(/%3A/gi, ':').replace(/%2F/gi, '/').replace(/%2C/gi, ',');
const pairs = (obj) => Object.entries(obj).filter(([, v]) => v !== undefined && v !== null && v !== '')
  .map(([k, v]) => `${k}=${loose(String(v))}`).join('&');

/** `#/explore?q=...` for a query text (and a sort other than the default). */
export function exploreHash(text, sort) {
  const s = pairs({ q: (text || '').trim(), sort });
  return '#/explore' + (s ? '?' + s : '');
}

/** `#/compare?l=remote:path&r=remote:path`; a side that is not set yet is left out. */
export function compareHash(left, right) {
  const place = (p) => (p ? `${p.remote}:${p.path || ''}` : '');
  const s = pairs({ l: place(left), r: place(right) });
  return '#/compare' + (s ? '?' + s : '');
}

/** `#/plans`, `#/plans/14`, `#/plans/new/dedupe?...` (docs/PLANS.md section E); `sub` is the part after `/plans`. */
export function plansHash(sub = '', obj = {}) {
  const s = pairs(obj);
  return '#/plans' + sub + (s ? '?' + s : '');
}

/** `#/free/<remote>?path=...&keep=a,b` (docs/ONBOARDING.md section 6): the free-space view of one cloud. */
export function freeHash(remote, { path, keep } = {}) {
  const s = pairs({ path: (path || '').replace(/^\/+|\/+$/g, ''), keep: Array.isArray(keep) ? keep.join(',') : keep });
  return `#/free/${encodeURIComponent(remote)}` + (s ? '?' + s : '');
}

/** "remote:path" -> {remote, path}; null when it names no remote. Remote names cannot contain ':'. */
export function parsePlace(text) {
  if (!text) return null;
  const i = text.indexOf(':');
  if (i <= 0) return null;
  return { remote: text.slice(0, i), path: text.slice(i + 1).replace(/^\/+|\/+$/g, '') };
}

// ---------------------------------------------------------------- tokenizer
/** Split on whitespace outside double quotes; every token keeps its place in the text (for removing a chip). */
function tokenize(text) {
  const tokens = [];
  let i = 0;
  while (i < text.length) {
    while (i < text.length && /\s/.test(text[i])) i += 1;
    if (i >= text.length) break;
    const start = i;
    let quoted = false;
    while (i < text.length && (quoted || !/\s/.test(text[i]))) {
      if (text[i] === '"') quoted = !quoted;
      i += 1;
    }
    tokens.push({ raw: text.slice(start, i), start, end: i });
  }
  return tokens;
}

const unquote = (s) => s.replace(/"/g, '');
const wrap = (s) => (/[\s"]/.test(s) ? `"${s.replace(/"/g, '')}"` : s);

// ---------------------------------------------------------------- numbers: sizes, counts, dates
const UNIT = { '': 0, k: 1, m: 2, g: 3, t: 4 };
const UNIT_LETTER = ['', 'K', 'M', 'G', 'T'];

export function parseSize(s) {
  const m = /^(\d+(?:\.\d+)?)\s*([kmgt]?)b?$/i.exec(s.trim());
  if (!m) return null;
  return Math.round(Number(m[1]) * 1024 ** UNIT[m[2].toLowerCase()]);
}

/** 10485760 -> "10M"; the largest unit that divides the byte count exactly. */
export function formatSize(bytes) {
  if (!bytes) return '0';
  for (let u = 4; u > 0; u -= 1) if (bytes % 1024 ** u === 0) return `${bytes / 1024 ** u}${UNIT_LETTER[u]}`;
  return String(bytes);
}

const parseCount = (s) => (/^\d+$/.test(s.trim()) ? Number(s) : null);

const pad = (n, w = 2) => String(n).padStart(w, '0');
const isoDay = (d) => `${pad(d.getUTCFullYear(), 4)}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
const dayAfter = (iso) => { const d = new Date(`${iso}T00:00:00Z`); d.setUTCDate(d.getUTCDate() + 1); return isoDay(d); };
const dayBefore = (iso) => { const d = new Date(`${iso}T00:00:00Z`); d.setUTCDate(d.getUTCDate() - 1); return isoDay(d); };

/** "2023" / "2023-06" / "2023-06-15" -> {start, end} as ISO days, end exclusive; null when it is not a date. */
function period(s) {
  const m = /^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?$/.exec(s.trim());
  if (!m) return null;
  const y = Number(m[1]);
  const mo = m[2] === undefined ? null : Number(m[2]);
  const d = m[3] === undefined ? null : Number(m[3]);
  if (mo !== null && (mo < 1 || mo > 12)) return null;
  if (d !== null) {
    const probe = new Date(Date.UTC(y, mo - 1, d));
    if (probe.getUTCMonth() !== mo - 1 || d < 1) return null;
    const start = isoDay(probe);
    return { start, end: dayAfter(start) };
  }
  if (mo !== null) return { start: `${pad(y, 4)}-${pad(mo)}-01`, end: mo === 12 ? `${pad(y + 1, 4)}-01-01` : `${pad(y, 4)}-${pad(mo + 1)}-01` };
  return { start: `${pad(y, 4)}-01-01`, end: `${pad(y + 1, 4)}-01-01` };
}

/** Which of year, month, day does [after, before) cover exactly? Null when it is none of them. */
function exactPeriod(after, before) {
  const y = /^(\d{4})-01-01$/.exec(after);
  if (y && before === `${pad(Number(y[1]) + 1, 4)}-01-01`) return y[1];
  const m = /^(\d{4})-(\d{2})-01$/.exec(after);
  if (m) {
    const next = Number(m[2]) === 12 ? `${pad(Number(m[1]) + 1, 4)}-01-01` : `${m[1]}-${pad(Number(m[2]) + 1)}-01`;
    if (before === next) return `${m[1]}-${m[2]}`;
  }
  if (/^\d{4}-\d{2}-\d{2}$/.test(after) && before === dayAfter(after)) return after;
  return null;
}

/**
 * One value of a number-like filter: `5`, `>5`, `>=5`, `<5`, `<=5`, `3..8`, `3..`, `..8`. `parse(text)` reads one
 * operand; `strict` turns `>` and `<` into an inclusive bound (`one` says how: +1 / -1 or a day).
 * Returns {min, max} (operands as parsed, either may be null) or null when malformed.
 */
function bounds(value, parse, strictLow, strictHigh, exact) {
  let m;
  if ((m = /^(.*)\.\.(.*)$/.exec(value)) && (m[1] || m[2])) {
    const a = m[1] ? parse(m[1]) : undefined;
    const b = m[2] ? parse(m[2]) : undefined;
    if (a === null || b === null) return null;
    return { low: a === undefined ? null : a.low, high: b === undefined ? null : b.high };
  }
  if ((m = /^(>=|<=|>|<)(.+)$/.exec(value))) {
    const v = parse(m[2]);
    if (v === null) return null;
    if (m[1] === '>') return { low: strictLow(v), high: null };
    if (m[1] === '>=') return { low: v.low, high: null };
    if (m[1] === '<') return { low: null, high: strictHigh(v) };
    return { low: null, high: v.high };
  }
  const v = parse(value);
  return v === null ? null : exact(v);
}

function sizeBounds(value) {
  const one = (s) => { const n = parseSize(s); return n === null ? null : { low: n, high: n }; };
  const r = bounds(value, one, (v) => v.low + 1, (v) => Math.max(0, v.high - 1), (v) => v);
  return r && (r.low !== null || r.high !== null) ? r : null;
}

function countBounds(value) {
  const one = (s) => { const n = parseCount(s); return n === null ? null : { low: n, high: n }; };
  const r = bounds(value, one, (v) => v.low + 1, (v) => Math.max(0, v.high - 1), (v) => v);
  return r && (r.low !== null || r.high !== null) ? r : null;
}

function dateBounds(value) {
  const one = (s) => { const p = period(s); return p === null ? null : { low: p.start, high: p.end }; };
  // `>P` is after the whole period; `<P` before its start; `<=P` up to the end of it; a bare period covers it.
  const r = bounds(value, one, (v) => v.high, (v) => v.low, (v) => v);
  return r && (r.low !== null || r.high !== null) ? r : null;
}

// ---------------------------------------------------------------- parse
/**
 * parseQuery(text, remoteNames) -> {params, chips, errors}
 * `params` are the request parameters of GET /v1/explore (only valid tokens); `chips` one per key:value token, in
 * order, {key, value, raw, start, end, error}; `errors` the chips that did not parse ({chip, code, detail}): the page
 * shows them red with a tooltip and does not send them. Free words stay words (`params.q`).
 */
export function parseQuery(text, remoteNames = []) {
  const params = {};
  const chips = [];
  const errors = [];
  const words = [];
  const remotes = remoteNames;
  const push = (name, v) => { params[name] = [...(params[name] || []), ...v.filter((x) => !(params[name] || []).includes(x))]; };

  for (const tok of tokenize(text || '')) {
    const m = /^([A-Za-z]+):(.*)$/.exec(tok.raw);
    if (!m) { words.push(unquote(tok.raw)); continue; }
    const key = m[1].toLowerCase();
    const value = unquote(m[2]);
    const chip = { key, value, raw: tok.raw, start: tok.start, end: tok.end, error: null };
    chips.push(chip);
    const fail = (code, detail) => { chip.error = { code, detail }; errors.push({ chip, code, detail }); };
    if (!FILTER_KEYS.includes(key)) { fail('unknownKey', key); continue; }
    if (value === '') { fail('empty', key); continue; }

    if (key === 'in') {
      if (!remotes.includes(value)) fail('unknownCloud', value);
      else if (params.in !== undefined && params.in !== value) fail('repeated', key);
      else params.in = value;
    } else if (key === 'on' || key === 'not' || key === 'only') {
      const list = value.split(',').filter(Boolean);
      const bad = list.find((n) => !remotes.includes(n));
      if (!list.length || bad !== undefined) { fail('unknownCloud', bad === undefined ? value : bad); continue; }
      if (key === 'only') {
        if (params.only_on !== undefined) { fail('repeated', key); continue; }
        params.only_on = remotes.filter((n) => list.includes(n));
      } else push(key === 'on' ? 'on' : 'not_on', list);
    } else if (key === 'copies') {
      const b = countBounds(value);
      if (!b) { fail('badValue', value); continue; }
      if (b.low !== null) params.copies_min = b.low;
      if (b.high !== null) params.copies_max = b.high;
    } else if (key === 'type') {
      const ext = value.toLowerCase().replace(/^\./, '');
      if (!ext || /[\s/]/.test(ext)) fail('badValue', value); else params.ext = ext;
    } else if (key === 'cat') {
      if (!/^[A-Za-z_]+$/.test(value)) fail('badValue', value); else params.category = value.toLowerCase();
    } else if (key === 'size') {
      const b = sizeBounds(value);
      if (!b) { fail('badValue', value); continue; }
      if (b.low !== null) params.min_size = b.low;
      if (b.high !== null) params.max_size = b.high;
    } else if (key === 'modified') {
      const b = dateBounds(value);
      if (!b) { fail('badValue', value); continue; }
      if (b.low !== null) params.modified_after = b.low;
      if (b.high !== null) params.modified_before = b.high;
    } else if (key === 'path') {
      const p = value.replace(/^\/+|\/+$/g, '');
      if (!p) fail('badValue', value); else params.path_prefix = p;
    }
  }
  if (words.length) params.q = words.join(' ');
  return { params, chips, errors };
}

// ---------------------------------------------------------------- format
/** The text that parses back to `params`: formatQuery(parseQuery(x).params) names the same query as x. */
export function formatQuery(p) {
  const out = [];
  if (p.q) out.push(p.q);
  if (p.in) out.push(`in:${wrap(p.in)}`);
  if ((p.on || []).length) out.push(`on:${p.on.map(wrap).join(',')}`);
  if ((p.not_on || []).length) out.push(`not:${p.not_on.map(wrap).join(',')}`);
  if ((p.only_on || []).length) out.push(`only:${p.only_on.map(wrap).join(',')}`);
  const lo = p.copies_min;
  const hi = p.copies_max;
  if (lo !== undefined && hi !== undefined) out.push(`copies:${lo === hi ? lo : `${lo}..${hi}`}`);
  else if (lo !== undefined) out.push(`copies:>=${lo}`);
  else if (hi !== undefined) out.push(`copies:<=${hi}`);
  if (p.ext) out.push(`type:${wrap(p.ext)}`);
  if (p.category) out.push(`cat:${wrap(p.category)}`);
  const smin = p.min_size;
  const smax = p.max_size;
  if (smin !== undefined && smax !== undefined) out.push(`size:${smin === smax ? formatSize(smin) : `${formatSize(smin)}..${formatSize(smax)}`}`);
  else if (smin !== undefined) out.push(`size:>=${formatSize(smin)}`);
  else if (smax !== undefined) out.push(`size:<=${formatSize(smax)}`);
  const a = p.modified_after;
  const b = p.modified_before;
  if (a && b) {
    const exact = exactPeriod(a, b);
    out.push(`modified:${exact || `${a}..${dayBefore(b)}`}`);
  } else if (a) out.push(`modified:>=${a}`);
  else if (b) out.push(`modified:<${b}`);
  if (p.path_prefix) out.push(`path:${wrap(p.path_prefix)}`);
  return out.join(' ');
}

// ---------------------------------------------------------------- editing the text (facets, chips)
/** Text without the span of one chip, whitespace tidied. */
export function removeChip(text, chip) {
  return (text.slice(0, chip.start) + ' ' + text.slice(chip.end)).replace(/\s+/g, ' ').trim();
}

/** Does the query hold the token `key:value` (a comma list counts for each of its values)? */
export function hasToken(chips, key, value) {
  return chips.some((c) => !c.error && c.key === key && c.value.split(',').includes(value));
}

/** Add `key:value` to the text, or take it out when it is there (one value of a comma list leaves the rest). */
export function toggleToken(text, remoteNames, key, value) {
  const { chips } = parseQuery(text, remoteNames);
  const hit = chips.find((c) => !c.error && c.key === key && c.value.split(',').includes(value));
  if (!hit) return `${text.trim()} ${key}:${wrap(value)}`.trim();
  const rest = hit.value.split(',').filter((v) => v !== value);
  if (!rest.length) return removeChip(text, hit);
  return (text.slice(0, hit.start) + `${key}:${rest.join(',')}` + text.slice(hit.end)).trim();
}

/** Replace every chip of `key` by one `key:value` (single-valued filters such as copies), or none for value null. */
export function setToken(text, remoteNames, key, value) {
  const { chips } = parseQuery(text, remoteNames);
  let out = text;
  chips.filter((c) => c.key === key).sort((x, y) => y.start - x.start).forEach((c) => { out = removeChip(out, c); });
  return value === null ? out : `${out.trim()} ${key}:${wrap(value)}`.trim();
}
