// What the Quarantine view and the places that mention it share (docs/QUARANTINE.md section F): the last
// `GET /v1/quarantine` report, kept in one place so that the rail badge, the Overview and Free space cards and the view
// itself agree, and the route string of the view. Nothing here polls: the report is read at start and again whenever
// something changes it (a run, a restore, a purge), through `refreshQuarantine()`.
import { useEffect, useState } from './vendor/htm-preact-standalone.mjs';
import * as api from './api.js';

let state = { loaded: false, data: null, error: null };
let inflight = null;
let again = false;
const subs = new Set();
const publish = (next) => { state = next; subs.forEach((fn) => fn(state)); };

/** `#/quarantine`, `#/quarantine/17`, `#/quarantine?remote=dropbox`, `#/quarantine/17?state=all&path=Photos`. */
export function quarantineHash(id, obj = {}) {
  const q = Object.entries(obj).filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${k}=${encodeURIComponent(String(v)).replace(/%2F/gi, '/').replace(/%3A/gi, ':')}`).join('&');
  return '#/quarantine' + (id ? `/${id}` : '') + (q ? `?${q}` : '');
}

/** Read the report again (a read already under way is followed by one more, so a change made meanwhile is seen). */
export function refreshQuarantine() {
  if (inflight) { again = true; return inflight; }
  inflight = api.quarantine().then(
    (data) => publish({ loaded: true, data, error: null }),
    (error) => publish({ loaded: true, data: state.data, error }),
  ).then(() => {
    inflight = null;
    if (again) { again = false; return refreshQuarantine(); }
    return state;
  });
  return inflight;
}

/** The shared report; the first user starts the read. */
export function useQuarantine() {
  const [s, setS] = useState(state);
  useEffect(() => {
    subs.add(setS);
    setS(state);
    if (!state.loaded && !inflight) refreshQuarantine();
    return () => { subs.delete(setS); };
  }, []);
  return s;
}

/** Plans that hold something past its retention (the rail badge). */
export const duePlans = (data) => ((data && data.plans) || []).filter((p) => p.due && p.due.files > 0).length;

/** What one cloud holds, from the report: {files, bytes, due_files, due_bytes} or null when the report has no such cloud. */
export const cloudHolds = (data, name) => ((data && data.remotes) || []).find((r) => r.remote === name) || null;
