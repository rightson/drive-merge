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
  if (csrf) headers['X-CSRF-Token'] = csrf;
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
/** Sync (docs/ONBOARDING.md section 5): list the given clouds (default all) into the catalog. Returns {job_id}. */
export const sync = (remotes) => call('POST', '/v1/ui/sync', { body: remotes && remotes.length ? { remotes } : {} });
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

// Plans (docs/PLANS.md section C).
export const plan = (id) => call('GET', `/v1/plans/${id}`);
export const planSummaries = async ({ status, limit = 100 } = {}) =>
  list(await call('GET', '/v1/plans/summaries', { query: { status, limit } }), 'plans');
export const estimatePlan = (kind, params, sample = 50) => call('POST', '/v1/plans/estimate', { body: { kind, params, sample } });
export const createPlan = (body) => call('POST', '/v1/plans', { body });
export const reviewPlan = (id) => call('POST', `/v1/ui/plans/${id}/review`);
export const cancelPlan = (id) => call('POST', `/v1/ui/plans/${id}/cancel`);
export const bridgeCandidates = ({ maxBytes, limit } = {}) => call('GET', '/v1/ui/bridge/candidates', { query: { max_bytes: maxBytes, limit } });
export const startBridge = ({ maxBytes, limit, entryIds } = {}) => call('POST', '/v1/ui/bridge', {
  body: entryIds ? { entry_ids: entryIds } : { max_bytes: maxBytes, limit },
});

// Connections (docs/ONBOARDING.md sections D and G4.5). All of these but `setup` and `importable` are human-channel routes.
// `connectStart`, `connectFinish`, `connectAnswer` and `connectImport` answer with the connection, or with a question result
// ({flow_id, status: 'question', question, retry}) when rclone asks something.
export const setup = () => call('GET', '/v1/setup');
export const connectStart = (body) => call('POST', '/v1/ui/connect/start', { body });
export const connectFlow = (id) => call('GET', `/v1/ui/connect/flows/${encodeURIComponent(id)}`);
export const connectFinish = (id, name) => call('POST', `/v1/ui/connect/flows/${encodeURIComponent(id)}/finish`, { body: name ? { name } : {} });
export const connectAnswer = (id, result) => call('POST', `/v1/ui/connect/flows/${encodeURIComponent(id)}/answer`, { body: { result } });
export const connectCancel = (id) => call('POST', `/v1/ui/connect/flows/${encodeURIComponent(id)}/cancel`, { body: {} });
/** A token pasted from another computer (docs/ONBOARDING.md G4.5): returns a flow that is already succeeded; finish it as usual. */
export const connectPaste = (body) => call('POST', '/v1/ui/connect/paste', { body });
export const importable = () => call('GET', '/v1/ui/connect/importable');
export const connectImport = (body) => call('POST', '/v1/ui/connect/import', { body });
export const testConnection = (name) => call('POST', `/v1/ui/connections/${encodeURIComponent(name)}/test`, { body: {} });
export const disconnect = (name) => call('DELETE', `/v1/ui/connections/${encodeURIComponent(name)}`);
export const installRclone = () => call('POST', '/v1/ui/rclone/install', { body: {} });

// Free space (docs/ONBOARDING.md section 6).
export const release = (remote, { path, keep } = {}) => call('GET', `/v1/release/${encodeURIComponent(remote)}`, {
  query: { path: path || undefined, keep_on: keep && keep.length ? keep.join(',') : undefined },
});
export const purge = (planId, body) => call('POST', `/v1/ui/plans/${planId}/purge`, { body });
