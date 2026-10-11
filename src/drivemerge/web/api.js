// Thin fetch wrapper over the /v1 HTTP contract (docs/WORKSPACE.md section D).
// The CSRF token lives only in this module's memory; it is never written to storage or the URL.

let csrf = '';
let onUnauthorized = null;

export const setCsrf = (value) => { csrf = value || ''; };
export const hasCsrf = () => csrf !== '';
export const setUnauthorizedHandler = (fn) => { onUnauthorized = fn; };

export class ApiError extends Error {
  constructor(status, body, fallback) {
    super((body && (body.error || body.detail || body.message)) || fallback || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.code = (body && body.code) || null;
    this.params = (body && body.params) || null;
    this.body = body;
  }
}

async function call(method, path, { query, body, quiet401 = false } = {}) {
  let url = path;
  if (query) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null) q.set(k, String(v));
    const s = q.toString();
    if (s) url += '?' + s;
  }
  const headers = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (method !== 'GET' && csrf) headers['X-CSRF-Token'] = csrf;
  let res;
  try {
    res = await fetch(url, {
      method, headers, credentials: 'same-origin',
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new ApiError(0, null, 'network');
  }
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (e) { data = { error: text.slice(0, 200) }; }
  }
  if (!res.ok) {
    if (res.status === 401 && !quiet401 && onUnauthorized) onUnauthorized();
    throw new ApiError(res.status, data);
  }
  return data;
}

const list = (r, key) => (Array.isArray(r) ? r : (r && r[key]) || []);

export const login = (token) => call('POST', '/v1/ui/session', { body: { token }, quiet401: true });
export const config = () => call('GET', '/v1/ui/config');

export const roots = async (draft) => list(await call('GET', '/v1/tree/roots', { query: { draft } }), 'roots');
export const tree = ({ remote, path, draft, view, ghosts, limit, offset }) =>
  call('GET', '/v1/tree', { query: { remote, path, draft, view, ghosts, limit, offset } });
export const search = async (q, { remote, limit = 20 } = {}) =>
  list(await call('GET', '/v1/search', { query: { q, include_dirs: 'true', remote, limit } }), 'results');

export const listDrafts = async () => list(await call('GET', '/v1/drafts'), 'drafts');
export const createDraft = async (title) => {
  const r = await call('POST', '/v1/drafts', { body: { title } });
  return r.draft || r;
};
export const getDraft = async (id) => {
  const r = await call('GET', `/v1/drafts/${id}`);
  return r.draft || r;
};
export const discardDraft = (id) => call('DELETE', `/v1/drafts/${id}`);
export const addAction = (id, action) => call('POST', `/v1/drafts/${id}/actions`, { body: action });
export const removeAction = (id, actionId) => call('DELETE', `/v1/drafts/${id}/actions/${actionId}`);
export const undo = async (id) => {
  const r = await call('POST', `/v1/drafts/${id}/undo`);
  return r.draft || r;
};
export const redo = async (id) => {
  const r = await call('POST', `/v1/drafts/${id}/redo`);
  return r.draft || r;
};
export const preview = (id) => call('POST', `/v1/drafts/${id}/preview`);

export const suggestions = async (kind, { remote, limit = 20 } = {}) =>
  list(await call('GET', '/v1/suggestions', { query: { kind, remote, limit } }), 'suggestions');

// The human channel: UI session cookie + CSRF header only.
export const approve = (planId, code) => call('POST', `/v1/ui/plans/${planId}/approve`, { body: { code } });
export const execute = (planId) => call('POST', `/v1/ui/plans/${planId}/execute`);
export const rescan = (remotes) => call('POST', '/v1/ui/rescan', { body: { remotes } });
export const job = (id) => call('GET', `/v1/jobs/${id}`);
export const progress = (planId) => call('GET', `/v1/plans/${planId}/progress`);

// Read-only views (docs/EXPLORE.md section E).
export const compare = ({ left, right, maxFiles }) => call('GET', '/v1/compare', {
  query: {
    left_remote: left.remote, left_path: left.path || '', right_remote: right.remote, right_path: right.path || '', max_files: maxFiles,
  },
});
export const coverage = ({ category, maxBars } = {}) => call('GET', '/v1/coverage', { query: { category, max_bars: maxBars } });
/** `params` are the request parameters of the query language; list parameters go comma-separated. */
export const explore = (params) => {
  const query = {};
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue;
    query[k] = Array.isArray(v) ? v.join(',') : v;
  }
  return call('GET', '/v1/explore', { query });
};
