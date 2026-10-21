// Clouds (docs/ONBOARDING.md sections 4 and 5): the Welcome screen and the cloud cards, the connect dialogs (Google
// Drive with its step-by-step guide, OneDrive and Dropbox with one button), and the header's Sync button with its
// progress pill and per-cloud popover. Tokens never reach this page: it sees an account name and a quota.
import {
  html, useContext, useEffect, useRef, useState,
} from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import * as api from './api.js';
import { Icon } from './icons.js';
import { AppCtx } from './ctx.js';
import { AgeChip, CloudDot } from './clouds.js';
import { Modal, ViewHeader } from './ui.js';
import { freeHash } from './query.js';
import {
  errorText, fmtBytes, sleep, wordParams,
} from './util.js';

const FLOW_POLL_MS = 1000;
const NAME_RE = /^[a-z][a-z0-9-]{0,30}$/;
const GOOGLE_ID = /^[0-9]+-[a-z0-9_]+\.apps\.googleusercontent\.com$/i;
const PROVIDERS = [
  { key: 'drive', label: 'Google Drive', needs_client: true },
  { key: 'onedrive', label: 'OneDrive', needs_client: false },
  { key: 'dropbox', label: 'Dropbox', needs_client: false },
];
const DEFAULT_NAME = { drive: 'gdrive', onedrive: 'onedrive', dropbox: 'dropbox' };
const GOOGLE_LINKS = {
  project: 'https://console.cloud.google.com/projectcreate',
  api: 'https://console.cloud.google.com/apis/library/drive.googleapis.com',
  consent: 'https://console.cloud.google.com/apis/credentials/consent',
  client: 'https://console.cloud.google.com/apis/credentials/oauthclient',
};

/** "gdrive", then "gdrive-2", "gdrive-3" ... for a second account of the same kind. */
export function defaultName(provider, taken) {
  const base = DEFAULT_NAME[provider] || provider;
  if (!taken.has(base)) return base;
  for (let n = 2; n < 100; n += 1) if (!taken.has(`${base}-${n}`)) return `${base}-${n}`;
  return base;
}

// ------------------------------------------------------------------ connect dialogs

/** Words for a failed connect: the code's sentence, a hint for the Google mistakes people make, and the raw text. */
function describeError(e, provider) {
  const code = e.code || null;
  const known = code === 'BAD_NAME' ? t('conn.nameBad') : code ? t(`err.${code}`, wordParams('err', e.params), '') : '';
  const raw = e.message || e.error || '';
  const hints = [];
  if (provider === 'drive' || provider === 'dropbox') {
    if (/invalid_client|unauthorized_client|client_id|client secret/i.test(raw)) hints.push('badClient');
    if (/access_denied|verif|testing|test user/i.test(raw)) hints.push('testing');
    if (/accessNotConfigured|has not been used|SERVICE_DISABLED|API.*(not|isn.t).*enabled/i.test(raw)) hints.push('api');
  }
  return { text: known || raw || t('err.unknown'), hints, raw: known && raw && raw !== known ? raw : '', code };
}

function ErrorBox({ error }) {
  if (!error) return null;
  return html`<div class="note error connect-error" role="alert" data-testid="connect-error" data-code=${error.code || ''}>
    <p><strong>${error.text}</strong></p>
    ${error.hints.map((h) => html`<p key=${h} data-testid=${'connect-hint-' + h}>${t(`conn.hint.${h}`)}</p>`)}
    ${error.raw && html`<details><summary>${t('conn.rawDetails')}</summary><code class="raw">${error.raw}</code></details>`}
  </div>`;
}

function GoogleGuide() {
  const step = (n, title, text, link, href, extra) => html`<li class="guide-step" data-testid=${'google-step-' + n}>
    <div><strong>${title}</strong><p>${text}</p>${extra}</div>
    <a class="btn btn-small" href=${href} target="_blank" rel="noopener noreferrer" data-testid=${'google-link-' + n}>
      <${Icon} name="jump" size=${13} /> ${link}</a></li>`;
  return html`<ol class="guide" data-testid="google-guide">
    ${step(1, t('conn.google.s1'), t('conn.google.s1t'), t('conn.google.open'), GOOGLE_LINKS.project)}
    ${step(2, t('conn.google.s2'), t('conn.google.s2t'), t('conn.google.open'), GOOGLE_LINKS.api)}
    ${step(3, t('conn.google.s3'), t('conn.google.s3t'), t('conn.google.open'), GOOGLE_LINKS.consent, html`
      <div class="banner-warn guide-warn" role="note" data-testid="google-testing-warning">
        <${Icon} name="alert" size=${15} /><span><strong>${t('conn.google.prodTitle')}</strong> ${t('conn.google.prodText')}</span></div>`)}
    ${step(4, t('conn.google.s4'), t('conn.google.s4t'), t('conn.google.open'), GOOGLE_LINKS.client)}
    <li class="guide-step" data-testid="google-step-5"><div><strong>${t('conn.google.s5')}</strong><p>${t('conn.google.s5t')}</p></div></li>
  </ol>`;
}

/**
 * One sign-in. `provider` is {key, label, needs_client}; `purpose` 'read' connects a new cloud, 'write' asks the
 * second consent for `conn` (docs/ONBOARDING.md section 3). The auth page opens in a new tab and the flow is polled
 * every second; cancelling or closing the dialog cancels the flow.
 */
export function ConnectDialog({ provider, purpose = 'read', conn = null, taken, onClose, onDone }) {
  const write = purpose === 'write';
  const isGoogle = provider.key === 'drive';
  const [phase, setPhase] = useState('form');          // form | starting | waiting | finishing | rename
  const [clientId, setClientId] = useState('');
  const [secret, setSecret] = useState('');
  const [prod, setProd] = useState(false);
  const [name, setName] = useState('');
  const [touched, setTouched] = useState({});
  const [needClient, setNeedClient] = useState(false);
  const [own, setOwn] = useState(false);
  const [error, setError] = useState(null);
  const [authUrl, setAuthUrl] = useState('');
  const flow = useRef(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const showClient = (isGoogle && !write) || needClient || (provider.key === 'dropbox' && own);
  const idText = clientId.trim();
  const secretText = secret.trim();
  const optional = showClient && !isGoogle && !needClient;      // Dropbox's own app: both empty means rclone's app
  const unused = optional && !idText && !secretText;
  const idOk = !showClient || unused || (isGoogle ? GOOGLE_ID.test(idText) : idText.length > 0 && !/\s/.test(idText));
  const secretOk = !showClient || unused || (secretText.length >= 8 && !/\s/.test(secretText) && !/googleusercontent\.com/.test(secretText));
  const nameText = name.trim();
  const nameOk = write || nameText === '' || (NAME_RE.test(nameText) && !taken.has(nameText));
  const prodOk = !(isGoogle && !write) || prod;
  const valid = idOk && secretOk && nameOk && prodOk;

  const idError = !idOk && touched.id ? (/^GOCSPX-/.test(idText) ? 'conn.google.idIsSecret' : /\s/.test(idText) ? 'conn.google.spaces' : 'conn.google.badId') : '';
  const secretError = !secretOk && touched.secret
    ? (/googleusercontent\.com/.test(secretText) ? 'conn.google.secretIsId' : /\s/.test(secretText) ? 'conn.google.spaces' : 'conn.google.badSecret') : '';
  const nameError = !nameOk ? (taken.has(nameText) ? 'conn.nameTaken' : 'conn.nameBad') : '';

  const busy = phase === 'starting' || phase === 'finishing';

  async function cancelFlow() {
    const id = flow.current;
    flow.current = null;
    if (id) { try { await api.connectCancel(id); } catch (e) { /* the flow is gone already */ } }
  }
  const close = () => { if (phase === 'waiting') cancelFlow(); onClose(); };
  // Focus can be on the page body once the clicked button is gone; Escape must still close the dialog.
  // The handler reads the latest render through a ref: a listener re-bound in an effect would still hold the
  // previous phase (e.g. 'starting', busy) for a moment after the waiting view is already on screen.
  const keyRef = useRef(null);
  keyRef.current = { busy, close };
  useEffect(() => {
    const onKey = (e) => { const k = keyRef.current; if (e.key === 'Escape' && !k.busy) k.close(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  async function finish(id) {
    setPhase('finishing');
    try {
      const made = await api.connectFinish(id, write ? conn.name : nameText || undefined);
      flow.current = null;
      if (alive.current) onDone(made);
    } catch (e) {
      if (!alive.current) return;
      if (e.code === 'NAME_TAKEN' || e.code === 'BAD_NAME') { setError(describeError(e, provider.key)); setPhase('rename'); return; }
      flow.current = null;
      setError(describeError(e, provider.key));
      setPhase('form');
    }
  }

  async function poll(id) {
    let failures = 0;
    for (;;) {
      await sleep(FLOW_POLL_MS);
      if (!alive.current || flow.current !== id) return;
      let f;
      try {
        f = await api.connectFlow(id);
        failures = 0;
      } catch (e) {
        failures += 1;
        if (failures > 5) { setError(describeError(e, provider.key)); setPhase('form'); flow.current = null; return; }
        continue;
      }
      if (!alive.current || flow.current !== id) return;
      if (f.status === 'succeeded') { await finish(id); return; }
      if (f.status === 'cancelled') { flow.current = null; setPhase('form'); return; }
      if (f.status === 'failed' || f.status === 'timeout') {
        flow.current = null;
        setError(describeError({ code: f.code || (f.status === 'timeout' ? 'FLOW_TIMEOUT' : 'FLOW_FAILED'), params: f.params, message: f.error || '' }, provider.key));
        setPhase('form');
        return;
      }
    }
  }

  async function start() {
    setError(null);
    setPhase('starting');
    const body = { provider: provider.key, purpose };
    if (showClient && idText && secretText) { body.client_id = idText; body.client_secret = secretText; }
    if (write) body.name = conn.name;
    let made;
    try {
      made = await api.connectStart(body);
    } catch (e) {
      if (!alive.current) return;
      if (e.code === 'NEEDS_CLIENT') setNeedClient(true);
      setError(describeError(e, provider.key));
      setPhase('form');
      return;
    }
    if (!alive.current) { api.connectCancel(made.flow_id).catch(() => {}); return; }
    flow.current = made.flow_id;
    setAuthUrl(made.auth_url || '');
    if (made.auth_url) window.open(made.auth_url, '_blank', 'noopener');
    setPhase('waiting');
    poll(made.flow_id);
  }

  async function cancelWaiting() {
    await cancelFlow();
    setPhase('form');
  }

  const title = write ? t('conn.allowTitle', { name: conn.name }) : t('conn.title', { provider: provider.label });
  return html`<${Modal} label=${title} onClose=${close} testid="connect-dialog" wide=${isGoogle && !write} dismissable=${!busy}>
    <header class="modal-head"><h2>${title}</h2></header>
    <div class="modal-body connect-body" data-phase=${phase} data-provider=${provider.key} data-purpose=${purpose}>
      ${phase === 'waiting' && html`<div class="connect-waiting" data-testid="connect-waiting">
        <span class="spinner" aria-hidden="true"></span>
        <div><strong>${t('conn.waiting')}</strong>
          <p>${t('conn.waitingText')}</p>
          ${authUrl && html`<p><a href=${authUrl} target="_blank" rel="noopener noreferrer" data-testid="auth-link">
            <${Icon} name="jump" size=${13} /> ${t('conn.reopen')}</a></p>`}
          <p class="muted">${t('conn.sameMachine')}</p></div></div>`}
      ${phase === 'finishing' && html`<div class="connect-waiting" data-testid="connect-finishing"><span class="spinner" aria-hidden="true"></span>
        <strong>${t('conn.finishing')}</strong></div>`}
      ${phase === 'rename' && html`<div data-testid="connect-rename">
        <${ErrorBox} error=${error} />
        <label class="field"><span>${t('conn.nameLabel')}</span>
          <input type="text" data-testid="conn-name" value=${name} autocomplete="off" spellcheck="false" aria-invalid=${!nameOk}
            placeholder=${defaultName(provider.key, taken)} onInput=${(e) => setName(e.target.value)} /></label>
        ${nameError && html`<p class="field-error" data-testid="conn-name-error">${t(nameError)}</p>`}</div>`}
      ${(phase === 'form' || phase === 'starting') && html`<div>
        ${write && html`<p data-testid="allow-text">${t('conn.allowText', { name: conn.name })}</p>
          <div class="banner-warn guide-warn" role="note"><${Icon} name="alert" size=${15} /><span>${t('conn.allowWarn')}</span></div>`}
        ${!write && provider.key === 'onedrive' && html`<p data-testid="onedrive-text">${t('conn.onedrive')}</p>`}
        ${!write && provider.key === 'dropbox' && html`<p data-testid="dropbox-text">${t('conn.dropbox')}</p>
          <p class="muted">${t('conn.dropboxScope')}</p>`}
        ${!write && isGoogle && html`<p>${t('conn.google.intro')}</p><${GoogleGuide} />`}
        ${needClient && !isGoogle && html`<p class="note" data-testid="need-client-note">${t('err.NEEDS_CLIENT')}</p>`}
        ${provider.key === 'dropbox' && !write && html`<details class="own-app" data-testid="dropbox-own" open=${own}
          onToggle=${(e) => setOwn(e.currentTarget.open)}>
          <summary data-testid="dropbox-own-toggle">${t('conn.dropboxOwn')}</summary>
          <p class="muted">${t('conn.dropboxOwnText')}</p>
          <code class="scopes">files.metadata.read files.content.read account_info.read</code></details>`}
        ${showClient && html`<div class="client-fields" data-testid="client-fields">
          <label class="field"><span>${isGoogle ? t('conn.google.clientId') : t('conn.appKey')}</span>
            <input type="text" data-testid="client-id" value=${clientId} autocomplete="off" spellcheck="false" aria-invalid=${!!idError}
              placeholder=${isGoogle ? '1234567890-abc123.apps.googleusercontent.com' : ''}
              onInput=${(e) => setClientId(e.target.value)} onBlur=${() => setTouched((x) => ({ ...x, id: true }))} /></label>
          ${idError && html`<p class="field-error" data-testid="client-id-error">${t(idError)}</p>`}
          <label class="field"><span>${isGoogle ? t('conn.google.clientSecret') : t('conn.appSecret')}</span>
            <input type="password" data-testid="client-secret" value=${secret} autocomplete="off" spellcheck="false" aria-invalid=${!!secretError}
              placeholder=${isGoogle ? 'GOCSPX-…' : ''}
              onInput=${(e) => setSecret(e.target.value)} onBlur=${() => setTouched((x) => ({ ...x, secret: true }))} /></label>
          ${secretError && html`<p class="field-error" data-testid="client-secret-error">${t(secretError)}</p>`}
        </div>`}
        ${isGoogle && !write && html`<label class="check guide-confirm"><input type="checkbox" data-testid="google-confirm-prod" checked=${prod}
          onChange=${(e) => setProd(e.target.checked)} /><span>${t('conn.google.confirmProd')}</span></label>`}
        ${!write && html`<details class="own-app"><summary data-testid="conn-name-toggle">${t('conn.nameToggle')}</summary>
          <label class="field"><span>${t('conn.nameLabel')}</span>
            <input type="text" data-testid="conn-name" value=${name} autocomplete="off" spellcheck="false" aria-invalid=${!nameOk}
              placeholder=${defaultName(provider.key, taken)} onInput=${(e) => setName(e.target.value)} /></label>
          ${nameError && html`<p class="field-error" data-testid="conn-name-error">${t(nameError)}</p>`}</details>`}
        <${ErrorBox} error=${error} />
      </div>`}
    </div>
    <footer class="modal-foot">
      ${phase === 'waiting'
    ? html`<button class="btn" data-testid="connect-cancel" onClick=${cancelWaiting}>${t('common.cancel')}</button>`
    : html`<button class="btn" data-testid="connect-cancel" onClick=${close} disabled=${busy}>${t('common.cancel')}</button>`}
      ${(phase === 'form' || phase === 'starting') && html`<button class="btn btn-primary" data-testid="connect-start" disabled=${!valid || busy} onClick=${start}>
        ${busy ? html`<span class="spinner" aria-hidden="true"></span>` : html`<${Icon} name=${write ? 'lock' : 'cloud'} size=${15} />`}
        ${write ? t('conn.allowButton') : isGoogle ? t('conn.google.button') : t('conn.signIn', { provider: provider.label })}</button>`}
      ${phase === 'rename' && html`<button class="btn btn-primary" data-testid="connect-finish" disabled=${!nameOk || !nameText}
        onClick=${() => finish(flow.current)}>${t('conn.finish')}</button>`}
    </footer>
  <//>`;
}

// ------------------------------------------------------------------ cards

function ProviderMark({ provider }) {
  return html`<span class=${'prov-mark prov-' + provider} aria-hidden="true"><${Icon} name="cloud" size=${16} /></span>`;
}

const STATE_TONE = { ro: 'ok', rw: 'warn', signing: 'info', relogin: 'warn', error: 'bad' };

function Quota({ quota, name }) {
  if (!quota || quota.used === undefined || quota.used === null) return html`<p class="conn-quota muted" data-testid=${'conn-quota-' + name}>${t('conn.quotaUnknown')}</p>`;
  const pct = quota.total ? Math.min(100, Math.round((quota.used / quota.total) * 100)) : 0;
  return html`<div class="conn-quota" data-testid=${'conn-quota-' + name}>
    <span>${quota.total ? t('conn.quota', { used: fmtBytes(quota.used), total: fmtBytes(quota.total) }) : t('conn.quotaUsed', { used: fmtBytes(quota.used) })}</span>
    ${quota.total ? html`<div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow=${pct}
      aria-label=${t('conn.quotaBar')}><span style=${{ width: `${pct}%` }}></span></div>` : null}</div>`;
}

function ConnCard({
  conn, remotes, test, scan, onTest, onAllow, onDisconnect, onSyncOne, syncing,
}) {
  const ctx = useContext(AppCtx);
  const result = test && !test.loading ? test : null;
  const rawState = conn.state === 'needs_login' ? 'relogin' : conn.state;
  const state = ['signing', 'relogin', 'error'].includes(rawState) ? rawState : result && result.ok === false ? 'error' : conn.writable ? 'rw' : 'ro';
  const account = (result && result.account) || conn.account;
  const quota = (result && result.quota) || conn.quota;
  const spec = PROVIDERS.find((p) => p.key === conn.provider);
  return html`<article class="conn-card" data-testid=${'conn-card-' + conn.name} data-state=${state} data-provider=${conn.provider}>
    <header><${ProviderMark} provider=${conn.provider} />
      <span class="conn-name">${conn.name}</span><span class="cloud-provider">${spec ? spec.label : conn.provider}</span></header>
    <p><span class=${'conn-state tone-' + STATE_TONE[state]} data-testid=${'conn-state-' + conn.name}>
      <${Icon} name=${state === 'ro' || state === 'rw' ? 'check' : 'alert'} size=${12} />${t(`conn.state.${state}`)}</span></p>
    <p class="conn-account" data-testid=${'conn-account-' + conn.name}>${account || html`<span class="muted">${t('conn.accountUnknown')}</span>`}</p>
    ${test && test.loading ? html`<p class="muted"><span class="spinner" aria-hidden="true"></span> ${t('conn.testing')}</p>`
    : html`<${Quota} quota=${quota} name=${conn.name} />`}
    <p class="conn-sync"><${AgeChip} iso=${scan} testid=${'conn-age-' + conn.name} /></p>
    ${result && result.ok === false && html`<p class="note error" data-testid=${'conn-error-' + conn.name} role="alert">
      ${t('conn.testFailed', { error: result.error || (result.code ? errorText({ code: result.code, params: result.params }) : '') })}</p>`}
    <footer class="conn-actions">
      <button class="btn btn-small" data-testid=${'conn-test-' + conn.name} onClick=${onTest} disabled=${test && test.loading}>${t('conn.test')}</button>
      <button class="btn btn-small" data-testid=${'conn-sync-' + conn.name} onClick=${onSyncOne} disabled=${syncing}>${t('conn.syncOne')}</button>
      ${!conn.writable && html`<button class="btn btn-small" data-testid=${'conn-allow-' + conn.name} onClick=${onAllow}
        title=${t('conn.allowTip')}><${Icon} name="lock" size=${13} /> ${t('conn.allow')}</button>`}
      <a class="btn btn-small" href=${freeHash(conn.name)} data-testid=${'conn-free-' + conn.name}
        onClick=${(e) => { e.preventDefault(); ctx.nav(freeHash(conn.name)); }}>${t('conn.free')}</a>
      <span class="spacer"></span>
      <button class="btn btn-small btn-danger-ghost" data-testid=${'conn-disconnect-' + conn.name} onClick=${onDisconnect}>${t('conn.disconnect')}</button>
    </footer>
  </article>`;
}

function ProviderTile({ spec, connected, onConnect, disabled }) {
  const google = spec.key === 'drive';
  return html`<article class="conn-card conn-tile" data-testid=${'provider-' + spec.key} data-connected=${connected ? 'true' : 'false'}>
    <header><${ProviderMark} provider=${spec.key} /><span class="conn-name">${spec.label}</span></header>
    <p class="muted">${connected ? t('conn.another') : google ? t('conn.tile.drive') : t(`conn.tile.${spec.key}`, null, t('conn.tile.other'))}</p>
    <footer class="conn-actions"><button class=${'btn ' + (connected ? 'btn-small' : 'btn-primary')} data-testid=${'connect-' + spec.key}
      disabled=${disabled} onClick=${onConnect}>${google && !connected ? t('conn.setup') : connected ? t('conn.addAnother') : t('conn.connect')}</button></footer>
  </article>`;
}

function RcloneBanner({ rclone, onChanged }) {
  const ctx = useContext(AppCtx);
  const [phase, setPhase] = useState('idle');   // idle | installing
  const [error, setError] = useState('');
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  if (!rclone) return null;
  if (rclone.ok) {
    return html`<span class="rclone-chip" data-testid="rclone-chip" title=${rclone.path || ''}>
      <${Icon} name="check" size=${12} />rclone ${rclone.version}</span>`;
  }
  async function install() {
    setError('');
    setPhase('installing');
    try {
      const { job_id: id } = await api.installRclone();
      for (let fails = 0; alive.current;) {
        try {
          const j = await api.job(id);
          if (j.status === 'done') break;
          if (j.status === 'failed') throw new Error(j.error || t('rclone.failed'));
          fails = 0;
        } catch (e) {
          if (e.message && !(e instanceof api.ApiError)) throw e;
          fails += 1;
          if (fails > 10) throw e;
        }
        await sleep(700);
      }
      if (alive.current) { await onChanged(); ctx.toast('success', t('rclone.installed')); }
    } catch (e) {
      if (alive.current) setError(e instanceof api.ApiError ? errorText(e) : e.message);
    }
    if (alive.current) setPhase('idle');
  }
  const old = rclone.found && !rclone.ok;
  return html`<section class="banner-warn rclone-banner" data-testid="rclone-banner" data-state=${old ? 'old' : 'missing'} role="note">
    <${Icon} name="alert" size=${16} />
    <span>${old ? t('rclone.old', { version: rclone.version }) : t('rclone.missing')} ${t('rclone.help')}</span>
    <button class="btn btn-small btn-primary" data-testid="rclone-install" disabled=${phase === 'installing'} onClick=${install}>
      ${phase === 'installing' ? html`<span class="spinner" aria-hidden="true"></span>` : html`<${Icon} name="refresh" size=${14} />`} ${t('rclone.install')}</button>
    <button class="btn btn-small" data-testid="rclone-recheck" disabled=${phase === 'installing'} onClick=${onChanged}>${t('rclone.recheck')}</button>
    ${error && html`<p class="note error" data-testid="rclone-error" role="alert">${error}</p>`}
  </section>`;
}

export function Clouds() {
  const ctx = useContext(AppCtx);
  const setup = ctx.setup.data;
  const conns = (setup && setup.connections) || [];
  const providers = setup && setup.providers && setup.providers.length ? setup.providers : PROVIDERS;
  const rclone = setup && setup.rclone;
  const remotes = ctx.config.remotes || [];
  const [tests, setTests] = useState({});
  const [scans, setScans] = useState({});
  const [dialog, setDialog] = useState(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  const names = conns.map((c) => c.name).join(',');

  async function runTest(name) {
    setTests((x) => ({ ...x, [name]: { loading: true } }));
    let result;
    try {
      result = await api.testConnection(name);
    } catch (e) {
      result = { ok: false, code: e.code, params: e.params, error: errorText(e) };
    }
    if (alive.current) setTests((x) => ({ ...x, [name]: result }));
  }
  useEffect(() => { conns.forEach((c) => { if (!tests[c.name]) runTest(c.name); }); }, [names]);
  useEffect(() => {
    let live = true;
    api.coverage({ maxBars: 1 }).then((cov) => {
      if (live) setScans(Object.fromEntries((cov.remotes || []).map((r) => [r.name, r.last_scan && r.last_scan.finished_at])));
    }, () => {});
    return () => { live = false; };
  }, [names, ctx.syncEpoch]);

  const taken = new Set([...remotes.map((r) => r.name), ...conns.map((c) => c.name)]);
  const done = async (made, write) => {
    setDialog(null);
    await ctx.refreshSetup();
    ctx.toast('success', write ? t('conn.toast.allowed', { name: made.name }) : t('conn.toast.connected', { name: made.name, account: made.account ? ` (${made.account})` : '' }));
    if (made.name) runTest(made.name);
  };

  async function disconnect(conn) {
    const ok = await ctx.confirm({
      title: t('conn.disconnectTitle', { name: conn.name }), message: t('conn.disconnectText', { name: conn.name }),
      okLabel: t('conn.disconnect'), danger: true,
    });
    if (!ok) return;
    try {
      await api.disconnect(conn.name);
      await ctx.refreshSetup();
      ctx.toast('success', t('conn.toast.disconnected', { name: conn.name }));
    } catch (e) {
      ctx.toast('error', errorText(e));
    }
  }

  async function syncAndContinue() {
    const r = await ctx.sync.start();
    if (r.ok) ctx.nav('#/overview');
  }

  const empty = conns.length === 0 && remotes.length === 0;      // clouds configured by hand count as connected
  const blocked = !!rclone && !rclone.ok;
  return html`<main class="view view-clouds" data-testid="view-clouds" aria-label=${t('nav.clouds')}>
    <${ViewHeader} title=${empty ? t('welcome.title') : t('nav.clouds')} hint=${empty ? '' : t('clouds.hint')}>
      <${RcloneBanner} rclone=${rclone && rclone.ok ? rclone : null} onChanged=${ctx.refreshSetup} />
    <//>
    <div class="view-body">
      ${rclone && !rclone.ok && html`<${RcloneBanner} rclone=${rclone} onChanged=${ctx.refreshSetup} />`}
      ${empty && html`<section class="welcome" data-testid="welcome">
        <h2>${t('welcome.heading')}</h2>
        <p>${t('welcome.text')}</p>
        <ul class="welcome-steps"><li>${t('welcome.step1')}</li><li>${t('welcome.step2')}</li><li>${t('welcome.step3')}</li></ul></section>`}
      ${ctx.setup.error && !setup && html`<p class="view-error" role="alert" data-testid="setup-error">${errorText(ctx.setup.error)}</p>`}
      ${conns.length > 0 && html`<div class="conn-grid" data-testid="conn-cards">
        ${conns.map((c) => html`<${ConnCard} key=${c.name} conn=${c} remotes=${remotes} test=${tests[c.name]} scan=${scans[c.name]}
          syncing=${ctx.sync.running} onTest=${() => runTest(c.name)} onSyncOne=${() => ctx.sync.start([c.name])}
          onAllow=${() => setDialog({ provider: providers.find((p) => p.key === c.provider) || { key: c.provider, label: c.provider }, purpose: 'write', conn: c })}
          onDisconnect=${() => disconnect(c)} />`)}</div>`}
      <section aria-labelledby="clouds-add-h">
        <h2 id="clouds-add-h" class="section-h">${empty ? t('clouds.connectHeading') : t('clouds.addHeading')}</h2>
        <div class="conn-grid" data-testid="provider-tiles">
          ${providers.map((p) => html`<${ProviderTile} key=${p.key} spec=${p} connected=${conns.some((c) => c.provider === p.key)}
            disabled=${blocked} onConnect=${() => setDialog({ provider: p, purpose: 'read' })} />`)}</div>
      </section>
      ${conns.length > 0 && html`<div class="welcome-actions">
        <button class="btn btn-primary" data-testid="welcome-sync" disabled=${ctx.sync.running} onClick=${syncAndContinue}>
          ${ctx.sync.running ? html`<span class="spinner" aria-hidden="true"></span>` : html`<${Icon} name="refresh" size=${15} />`} ${t('welcome.sync')}</button>
        <span class="muted">${t('welcome.syncHelp')}</span></div>`}
    </div>
    ${dialog && html`<${ConnectDialog} provider=${dialog.provider} purpose=${dialog.purpose} conn=${dialog.conn} taken=${taken}
      onClose=${() => setDialog(null)} onDone=${(made) => done(made, dialog.purpose === 'write')} />`}
  </main>`;
}
