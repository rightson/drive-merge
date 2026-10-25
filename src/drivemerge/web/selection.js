// The selection model of Explore (docs/BULK.md sections 3 and G): pure and testable, no DOM, no network.
//
// A selection is either hand-picked ("keys": a set of content keys) or "all matching" the query ("query": every content
// the query finds, minus the ones the person unchecked). It becomes the JSON every route takes (`body()`), and it
// travels to the dedupe form in the address bar (`toDedupeParams()` / `fromDedupeParams()`).
import { parseQuery } from './query.js';

export const PICK_LIMIT = 500;      // hand-picked contents, and exclusions of "all matching"
export const SELECT_LIMIT = 5000;   // contents of "all matching"
const KEY = /^(g|e):[1-9][0-9]{0,17}$/;

const get = (p, name) => {
  const v = p && typeof p.get === 'function' ? p.get(name) : p && p[name];
  return v === undefined || v === null ? '' : String(v);
};
const list = (s) => (s ? s.split(',').filter(Boolean) : []);

/** The query as the routes take it: the explore parameters only (no sort, limit, offset), empty values left out. */
export function queryOf(params) {
  const out = {};
  for (const [k, v] of Object.entries(params || {})) {
    if (['sort', 'limit', 'offset'].includes(k) || v === undefined || v === null || v === '') continue;
    out[k] = Array.isArray(v) ? [...v] : v;
  }
  return out;
}

export class Selection {
  /** `text` is the query text (kept for the link back to Explore); `params` its explore parameters. */
  constructor(text = '', params = {}) {
    this.text = text;
    this.query = queryOf(params);
    this.mode = 'keys';
    this.keys = new Set();
    this.except = new Set();
    this.seen = 0;          // how many contents "all matching" held when it was chosen
  }

  clone() {
    const c = new Selection(this.text, this.query);
    c.mode = this.mode;
    c.keys = new Set(this.keys);
    c.except = new Set(this.except);
    c.seen = this.seen;
    return c;
  }

  /** How many contents the selection holds, as far as the page knows (the server's count is the exact one). */
  get count() {
    return this.mode === 'query' ? Math.max(0, this.seen - this.except.size) : this.keys.size;
  }

  get empty() { return this.count === 0; }

  has(key) { return this.mode === 'query' ? !this.except.has(key) : this.keys.has(key); }

  /** Check or uncheck one result. Returns '' or the key of the sentence that says why it was refused ('sel.limit'). */
  toggle(key, on = !this.has(key)) {
    if (this.mode === 'query') {
      if (on) { this.except.delete(key); return ''; }
      if (!this.except.has(key) && this.except.size >= PICK_LIMIT) return 'sel.limit';
      this.except.add(key);
      return '';
    }
    if (!on) { this.keys.delete(key); return ''; }
    if (!this.keys.has(key) && this.keys.size >= PICK_LIMIT) return 'sel.limit';
    this.keys.add(key);
    return '';
  }

  /** Check every key of the loaded page, all or nothing (a page that does not fit is refused as a whole). */
  selectPage(keys) {
    if (this.mode === 'query') { keys.forEach((k) => this.except.delete(k)); return ''; }
    const fresh = keys.filter((k) => !this.keys.has(k));
    if (this.keys.size + fresh.length > PICK_LIMIT) return 'sel.limit';
    fresh.forEach((k) => this.keys.add(k));
    return '';
  }

  /** Uncheck the loaded page. "All matching" cannot lose a page by exclusions, so it ends. */
  deselectPage(keys) {
    if (this.mode === 'query') { this.clear(); return; }
    keys.forEach((k) => this.keys.delete(k));
  }

  /** Every content the query finds (`total` of them, as the results said). */
  selectAllMatching(params, total, text = this.text) {
    this.text = text;
    this.query = queryOf(params);
    this.mode = 'query';
    this.keys = new Set();
    this.except = new Set();
    this.seen = Number(total) || 0;
  }

  /** The server's count after a rescan changed the answer ("Use the current N"): `total` is the count after exclusions. */
  useCurrent(total) {
    if (this.mode === 'query') this.seen = Number(total) + this.except.size;
  }

  clear() {
    this.mode = 'keys';
    this.keys = new Set();
    this.except = new Set();
    this.seen = 0;
  }

  /** The selection as every route and tool takes it (docs/BULK.md section A). */
  body() {
    if (this.mode === 'query') {
      const out = { query: this.query };
      if (this.except.size) out.except = [...this.except].sort();
      out.expect_total = this.count;
      return out;
    }
    return { keys: [...this.keys].sort() };
  }

  /** The dedupe form's address-bar parameters: selq / selx / seln for "all matching", selk for hand-picked contents. */
  toDedupeParams() {
    if (this.empty) return {};
    if (this.mode === 'query') return { selq: this.text, selx: [...this.except].sort().join(','), seln: String(this.count) };
    return { selq: this.text, selk: [...this.keys].sort().join(',') };
  }

  /** A signature that changes whenever what the selection holds changes. */
  get signature() { return JSON.stringify(this.empty ? null : this.body()); }
}

/**
 * The selection a dedupe form's address carries (null when it has none). `urlParams` is a URLSearchParams or a plain
 * object; `remoteNames` lets the query text be read again, so the form can send the same query the page showed.
 */
export function fromDedupeParams(urlParams, remoteNames = []) {
  const text = get(urlParams, 'selq');
  const keys = list(get(urlParams, 'selk')).filter((k) => KEY.test(k));
  const s = new Selection(text, parseQuery(text, remoteNames).params);
  if (keys.length) {
    keys.forEach((k) => s.keys.add(k));
    return s;
  }
  const n = get(urlParams, 'seln');
  if (!text.trim() && !n) return null;
  if (!/^\d+$/.test(n)) return null;
  const except = list(get(urlParams, 'selx')).filter((k) => KEY.test(k));
  s.mode = 'query';
  s.except = new Set(except);
  s.seen = Number(n) + s.except.size;
  return s;
}

// What each query showed last, so that Back to the same query finds its selection (for the visit: not stored anywhere).
const memory = new Map();
/** Keep `sel` for the query `key`; an empty selection forgets it. */
export function remember(key, sel) {
  if (sel && !sel.empty) memory.set(key, sel); else memory.delete(key);
}
/** The selection kept for `key`, or a new empty one. */
export function recall(key, text, params) {
  return memory.get(key) || new Selection(text, params);
}
export const forgetAll = () => memory.clear();
