// Setup: check rclone, connect a cloud by signing in through rclone's helper, adopt remotes rclone already has,
// test them, run the first scan and say when the person can start (docs/SETUP.md sections 2 to 6).
//
// Secrets: a password or token typed into rclone's question form lives only in that form's state. It is sent in the
// body of one POST (never in a URL, never in storage, never in a toast) and the field is emptied before the request.
import {
  html, useContext, useEffect, useRef, useState,
} from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import * as api from './api.js';
import { Icon } from './icons.js';
import { AppCtx } from './ctx.js';
import { CloudName } from './clouds.js';
import { Modal, ViewHeader } from './ui.js';
import {
  errorText, fmtBytes, fmtDateTime, fmtNum, sleep, wordParams,
} from './util.js';

const PROVIDERS = ['drive', 'dropbox', 'onedrive', 'iclouddrive'];
const SUGGESTED_NAME = { drive: 'gdrive', dropbox: 'dropbox', onedrive: 'onedrive', iclouddrive: 'icloud' };
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/;
// Only rclone's own helper on this machine is ever offered as a sign-in link.
const AUTH_URL_RE = /^http:\/\/127\.0\.0\.1:53682\//;
const POLL_MS = 600;
const HOLD_MS = 4000;       // after an answer, the same question is not shown again for this long (rclone is still thinking)

const providerLabel = (p) => t(`setup.provider.${p}`, null, p);

/** The scan this page started (kept across visits to another view, so coming back finds it still running). */
const activeScan = { current: null };

/** One failure of a job or an API call in words: the sentence of its code, else what the server wrote. */
function failureOf(job) {
  const e = job && job.error;
  let code = null;
  let params = null;
  let message = '';
  if (e && typeof e === 'object') { code = e.code || null; params = e.params || null; message = e.message || ''; }
  else if (typeof e === 'string') message = e;
  if (!code && job && job.code) { code = job.code; params = job.params || null; }
  const known = code ? t(`err.${code}`, wordParams('err', params), '') : '';
  const extra = params && [params.detail, params.stderr, params.message].find((v) => typeof v === 'string' && v.trim());
  return { code, text: known || message || t('err.unknown'), detail: extra || '' };
}

/** A sentence for an error that came back from a call, never echoing more than the server's own message. */
const refusalText = (e) => (e && e.code === 'JOB_BUSY' ? t('setup.busy') : errorText(e));

// ---------------------------------------------------------------------------------------------- small pieces

/** rclone's own words (English, dynamic) in a box that says so; shown exactly as received. */
function RcloneText({ text, children, testid }) {
  return html`<div class="su-rclone" data-testid=${testid}>
    <div class="su-rclone-tag"><${Icon} name="info" size=${13} /> ${t('connect.q.fromRclone')}</div>
    ${text ? html`<pre class="su-rclone-text" lang="en">${text}</pre>` : null}
    ${children}
  </div>`;
}

function Mark({ state, n }) {
  return html`<span class=${'su-mark su-mark-' + state} aria-hidden="true">
    ${state === 'done' ? html`<${Icon} name="check" size=${16} />` : state === 'attention' ? html`<${Icon} name="alert" size=${15} />` : n}</span>`;
}

function Step({ id, n, state, title, children, aside }) {
  const label = state === 'done' ? t('setup.state.done') : state === 'attention' ? t('setup.state.attention') : t('setup.state.todo');
  return html`<section class="su-step" data-testid=${'setup-step-' + id} data-state=${state} aria-labelledby=${'su-h-' + id}>
    <${Mark} state=${state} n=${n} />
    <div class="su-main">
      <header class="su-step-head">
        <h2 id=${'su-h-' + id}>${title}</h2>
        <span class=${'su-state su-state-' + state} data-testid=${'setup-state-' + id}>${label}</span>
        ${aside}
      </header>
      ${children}
    </div>
  </section>`;
}

function CodeBlock({ text, label, testid, onCopy }) {
  return html`<div class="su-code">
    <pre data-testid=${testid} tabindex="0" aria-label=${label}>${text}</pre>
    <button class="btn btn-small su-copy" onClick=${() => onCopy(text)} data-testid=${testid ? testid + '-copy' : undefined}>
      <${Icon} name="copy" size=${14} /> ${t('common.copy')}</button>
  </div>`;
}

// ---------------------------------------------------------------------------------------------- read-only fallback

function remotesBlock(setup) {
  const pending = (setup.rclone_remotes || []).filter((r) => !r.in_config);
  const items = pending.length ? pending.map((r) => ({ name: r.name, provider: r.provider || 'other' })) : [{ name: 'gdrive', provider: 'drive' }];
  return items.map((x) => `[[remotes]]\nname = ${JSON.stringify(x.name)}\nprovider = ${JSON.stringify(x.provider)}\nread = ${JSON.stringify(`${x.name}:`)}`
    + (x.provider === 'drive' ? '\nextra_args = ["--fast-list", "--drive-skip-shortcuts"]' : '')).join('\n\n');
}

function ReadOnlyPanel({ setup, onCopy }) {
  return html`<section class="su-ro" data-testid="setup-readonly" aria-labelledby="su-ro-h">
    <h2 id="su-ro-h"><${Icon} name="lock" size=${16} /> ${t('setup.ro.title')}</h2>
    <p>${t('setup.ro.text')}</p>
    <ol class="su-ro-steps">
      <li><p>${t('setup.ro.s1')}</p><${CodeBlock} text="rclone config" label="rclone config" testid="setup-cmd-rclone" onCopy=${onCopy} /></li>
      <li><p>${t('setup.ro.s2', { path: (setup && setup.config_path) || 'config.toml' })}</p>
        <${CodeBlock} text=${remotesBlock(setup || {})} label="[[remotes]]" testid="setup-cmd-remotes" onCopy=${onCopy} /></li>
      <li><p>${t('setup.ro.s3')}</p>
        <${CodeBlock} text=${'drivemerge scan --all\ndrivemerge identity rebuild'} label="drivemerge scan" testid="setup-cmd-scan" onCopy=${onCopy} /></li>
    </ol>
  </section>`;
}

// ---------------------------------------------------------------------------------------------- rclone's question

/** One question of rclone's configuration, whatever it is: radio group, buttons, text, or a secret. */
function QuestionForm({ q, busy, error, retry, onSubmit, cancel }) {
  const examples = Array.isArray(q.examples) ? q.examples : [];
  const secret = !!q.password;
  const isBool = !secret && (q.type === 'bool' || (q.exclusive && examples.length === 2 && examples.every((e) => /^(true|false)$/.test(String(e.value)))));
  const radios = !secret && !isBool && !!q.exclusive && examples.length > 0;
  const initialPick = () => {
    const i = examples.findIndex((e) => String(e.value) === String(q.default === undefined || q.default === null ? '' : q.default));
    return i >= 0 ? i : null;
  };
  const [pick, setPick] = useState(initialPick);
  const [text, setText] = useState(secret || q.default === undefined || q.default === null ? '' : String(q.default));
  const first = useRef(null);
  useEffect(() => { if (first.current) first.current.focus(); }, []);

  const finish = (value) => {
    setText('');       // the secret leaves this component's state before the request goes out
    onSubmit(value);
  };
  const answer = () => (radios ? (pick === null ? String(q.default === undefined || q.default === null ? '' : q.default) : String(examples[pick].value)) : text);
  const missing = !!q.required && (radios ? pick === null : text.trim() === '');
  const submit = (e) => {
    e.preventDefault();       // never a navigation: nothing typed here may end up in the address bar
    if (busy || missing) return;
    finish(answer());
  };
  const dflt = q.default === undefined || q.default === null ? '' : String(q.default);
  const hasDefault = dflt !== '' && !secret;

  return html`<form class="su-question" onSubmit=${submit} noValidate autocomplete="off" data-testid="connect-question" data-name=${q.name}>
    <h3 class="su-q-title">${t('connect.q.title')} <code data-testid="connect-q-name">${q.name}</code></h3>
    ${retry && html`<p class="su-retry" role="alert" data-testid="connect-q-retry"><${Icon} name="alert" size=${14} /> ${t('connect.q.retry')}</p>`}
    <${RcloneText} text=${q.help} testid="connect-q-help">
      ${radios && html`<div role="radiogroup" aria-label=${q.name} class="su-options" data-testid="connect-q-options">
        ${examples.map((e, i) => html`<label key=${i} class=${'su-option' + (pick === i ? ' on' : '')}>
          <input type="radio" name="su-pick" checked=${pick === i} ref=${i === (pick === null ? 0 : pick) ? first : undefined}
            onChange=${() => setPick(i)} data-testid=${'connect-q-option-' + i} />
          <span class="su-option-main"><span class="su-option-value"><code>${String(e.value) === '' ? t('connect.q.empty') : String(e.value)}</code>
            ${String(e.value) === dflt && dflt !== '' && html`<span class="badge badge-new">${t('connect.q.isDefault')}</span>`}</span>
            ${e.help && html`<span class="su-option-help" lang="en">${e.help}</span>`}</span></label>`)}
      </div>`}
    <//>
    ${isBool && html`<div class="su-bool" role="group" aria-label=${q.name}>
      <button type="button" class=${'btn' + (dflt !== 'false' ? ' btn-primary' : '')} disabled=${busy} data-testid="connect-q-yes" ref=${first}
        onClick=${() => finish('true')}>${t('connect.q.yes')}</button>
      <button type="button" class=${'btn' + (dflt === 'false' ? ' btn-primary' : '')} disabled=${busy} data-testid="connect-q-no"
        onClick=${() => finish('false')}>${t('connect.q.no')}</button>
    </div>`}
    ${!isBool && !radios && html`<div class="su-q-input">
      ${!secret && examples.length > 0 && html`<label class="field"><span>${t('connect.q.pick')}</span>
        <select class="select" data-testid="connect-q-select" value="" onChange=${(e) => { if (e.target.value !== '') setText(examples[Number(e.target.value)].value); }}>
          <option value="">${t('connect.q.custom')}</option>
          ${examples.map((e, i) => html`<option key=${i} value=${i} lang="en">${String(e.value) === '' ? t('connect.q.empty') : e.value}${e.help ? ` — ${String(e.help).split('\n')[0].slice(0, 80)}` : ''}</option>`)}
        </select></label>`}
      <label class="field"><span>${t('connect.q.answer')}${q.required ? ' *' : ''}</span>
        <input type=${secret ? 'password' : 'text'} value=${text} ref=${first} autocomplete=${secret ? 'new-password' : 'off'} spellcheck="false"
          autocapitalize="off" data-testid="connect-q-input" aria-required=${!!q.required} onInput=${(e) => setText(e.target.value)} /></label>
      ${secret && html`<p class="pl-help">${t('connect.q.secret')}</p>`}
      ${hasDefault && html`<p class="pl-help">${t('connect.q.default', { value: dflt })}</p>`}
      ${!q.required && html`<p class="pl-help">${t('connect.q.optional')}</p>`}
    </div>`}
    ${error && html`<p class="note error" role="alert" data-testid="connect-q-error">${error}</p>`}
    <div class="su-q-actions">
      ${cancel}
      ${!isBool && html`<button type="submit" class="btn btn-primary" disabled=${busy || missing} data-testid="connect-q-continue">${t('connect.q.continue')}</button>`}
    </div>
  </form>`;
}

// ---------------------------------------------------------------------------------------------- connect dialog

/**
 * Pick a cloud and a name, sign in, answer rclone's questions, done. With `preset` ({jobId, name, title, enable}) the
 * job already exists (enabling changes on Drive starts one) and the dialog goes straight to its progress.
 */
function ConnectDialog({ setup, preset, onClose, onConnected, onScan }) {
  const taken = new Set([...(setup.remotes || []).map((r) => r.name), ...(setup.rclone_remotes || []).map((r) => r.name)]);
  const suggest = (p) => {
    const base = SUGGESTED_NAME[p];
    let n = base;
    for (let i = 2; taken.has(n); i += 1) n = `${base}${i}`;
    return n;
  };
  const [provider, setProvider] = useState('drive');
  const [name, setName] = useState(() => suggest('drive'));
  const [touched, setTouched] = useState(false);
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [appleId, setAppleId] = useState('');
  const [applePassword, setApplePassword] = useState('');
  const [remoteAuth, setRemoteAuth] = useState(false);
  const [phase, setPhase] = useState(preset ? 'run' : 'form');     // form | run | done | failed
  const [jobId, setJobId] = useState(preset ? preset.jobId : null);
  const [job, setJob] = useState(null);
  const [error, setError] = useState('');
  const [fail, setFail] = useState(null);
  const [done, setDone] = useState(null);
  const [held, setHeld] = useState(null);           // {sig, until}: the question the person just answered
  const [busy, setBusy] = useState(false);
  const [answerError, setAnswerError] = useState('');
  const [opened, setOpened] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const nameError = !NAME_RE.test(name) ? t('err.BAD_NAME') : taken.has(name) ? t('err.REMOTE_EXISTS', { name }) : '';
  const writeName = preset ? preset.name : name;
  const icloud = provider === 'iclouddrive';
  const missing = icloud && (!appleId.trim() || !applePassword);

  useEffect(() => {
    if (!jobId || phase !== 'run') return undefined;
    let live = true;
    (async () => {
      let misses = 0;
      while (live && alive.current) {
        try {
          const j = await api.job(jobId);
          if (!live || !alive.current) return;
          misses = 0;
          setJob(j);
          if (j.status === 'done') {
            const remote = (j.result && j.result.remote) || { name: writeName };
            setDone(remote);
            setPhase('done');
            onConnected(remote);
            return;
          }
          if (j.status === 'failed') {
            const f = failureOf(j);
            if (f.code === 'CANCELLED') { onClose(true); return; }
            setFail(f);
            setPhase('failed');
            return;
          }
        } catch (e) {
          misses += 1;
          if (misses > 10) { setFail({ code: null, text: refusalText(e), detail: '' }); setPhase('failed'); return; }
        }
        await sleep(POLL_MS);
      }
    })();
    return () => { live = false; };
  }, [jobId, phase]);

  async function start(e) {
    if (e) e.preventDefault();
    if (nameError) { setTouched(true); return; }
    if (missing) return;
    setError('');
    const body = { provider, name };
    if (provider === 'drive' && clientId.trim()) {
      body.client_id = clientId.trim();
      if (clientSecret) body.client_secret = clientSecret;
    }
    if (provider === 'iclouddrive') { body.apple_id = appleId.trim(); body.password = applePassword; }
    if (remoteAuth) body.remote_auth = true;
    try {
      const r = await api.connect(body);
      setClientSecret('');
      setApplePassword('');
      setJobId(r.job_id);
      setPhase('run');
    } catch (err) {
      setError(refusalText(err));
    }
  }

  async function answer(value) {
    const q = job && job.progress && job.progress.question;
    setBusy(true);
    setAnswerError('');
    setHeld({ sig: JSON.stringify(q), until: Date.now() + HOLD_MS });
    try {
      await api.answer(jobId, value);
    } catch (err) {
      setHeld(null);
      setAnswerError(refusalText(err));
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    setCancelling(true);
    try { await api.cancelConnect(jobId); } catch (err) { setCancelling(false); setAnswerError(refusalText(err)); }
  }

  const p = (job && job.progress) || {};
  const q = p.stage === 'question' && p.question ? p.question : null;
  const heldNow = q && held && held.sig === JSON.stringify(q) && Date.now() < held.until;
  const authUrl = p.stage === 'signin' && typeof p.auth_url === 'string' && AUTH_URL_RE.test(p.auth_url) ? p.auth_url : '';
  const running = phase === 'run';
  const cancelButton = html`<button type="button" class="btn" data-testid="connect-cancel" disabled=${cancelling} onClick=${cancel}>
    ${cancelling ? t('connect.cancelling') : t('common.cancel')}</button>`;
  const title = preset ? preset.title : t('connect.title');

  return html`<${Modal} label=${title} onClose=${() => onClose(false)} testid="connect-dialog" dismissable=${!running}
    initialFocus=${phase === 'form' ? '[data-testid="connect-name"]' : undefined}>
    <div class="dialog su-dialog" data-phase=${phase}>
      <h2 class="dialog-title">${title}</h2>

      ${phase === 'form' && html`<form class="su-connect-form" onSubmit=${start} noValidate autocomplete="off">
        <fieldset class="pl-radios su-providers">
          <legend>${t('connect.provider')}</legend>
          ${PROVIDERS.map((id) => html`<label key=${id} class=${'su-provider' + (provider === id ? ' on' : '')}>
            <input type="radio" name="provider" value=${id} checked=${provider === id} data-testid=${'connect-provider-' + id}
              onChange=${() => { setProvider(id); if (!touched) setName(suggest(id)); }} />
            <span>${providerLabel(id)}</span></label>`)}
        </fieldset>
        <label class="field"><span>${t('connect.name')}</span>
          <input type="text" value=${name} data-testid="connect-name" autocomplete="off" spellcheck="false" maxlength="32"
            aria-invalid=${!!nameError} aria-describedby="su-name-help"
            onInput=${(e) => { setName(e.target.value); setTouched(true); }} /></label>
        <p class=${'pl-help' + (nameError && touched ? ' su-bad' : '')} id="su-name-help" data-testid="connect-name-help" aria-live="polite">
          ${nameError && touched ? nameError : t('connect.nameHelp')}</p>
        ${icloud && html`<div class="su-icloud" data-testid="connect-icloud">
          <p class="pl-help">${t('connect.icloudHelp')}</p>
          <label class="field"><span>${t('connect.appleId')} *</span>
            <input type="text" value=${appleId} data-testid="connect-apple-id" autocomplete="off" spellcheck="false" autocapitalize="off"
              onInput=${(e) => setAppleId(e.target.value)} /></label>
          <label class="field"><span>${t('connect.applePassword')} *</span>
            <input type="password" value=${applePassword} data-testid="connect-apple-password" autocomplete="new-password" spellcheck="false"
              onInput=${(e) => setApplePassword(e.target.value)} /></label>
          <p class="pl-help">${t('connect.q.secret')}</p>
        </div>`}
        ${provider === 'drive' && html`<details class="pl-details su-advanced" data-testid="connect-advanced">
          <summary>${t('connect.advanced')}</summary>
          <p class="pl-help">${t('connect.advancedHelp')}</p>
          <label class="field"><span>${t('connect.clientId')}</span>
            <input type="text" value=${clientId} data-testid="connect-client-id" autocomplete="off" spellcheck="false"
              onInput=${(e) => setClientId(e.target.value)} /></label>
          <label class="field"><span>${t('connect.clientSecret')}</span>
            <input type="password" value=${clientSecret} data-testid="connect-client-secret" autocomplete="new-password" spellcheck="false"
              onInput=${(e) => setClientSecret(e.target.value)} /></label>
        </details>`}
        <label class="check su-remote-auth"><input type="checkbox" checked=${remoteAuth} data-testid="connect-remote-auth"
          onChange=${(e) => setRemoteAuth(e.target.checked)} /> <span>${t('connect.remoteAuth')}</span></label>
        <p class="su-note"><${Icon} name="lock" size=${14} /> <span>${t('connect.readOnlyNote')}</span></p>
        ${error && html`<p class="note error" role="alert" data-testid="connect-error">${error}</p>`}
        <div class="dialog-actions">
          <button type="button" class="btn" data-testid="connect-cancel" onClick=${() => onClose(false)}>${t('common.cancel')}</button>
          <button type="submit" class="btn btn-primary" data-testid="connect-start" disabled=${!!nameError || missing}>${t('connect.signIn')}</button>
        </div>
      </form>`}

      ${running && html`<div class="su-run" data-testid="connect-run" aria-live="polite">
        ${authUrl && html`<div class="su-signin" data-testid="connect-signin">
          <p>${t('connect.signinText')}</p>
          <p><a class="btn btn-primary su-link-btn" href=${authUrl} target="_blank" rel="noopener" data-testid="connect-signin-link"
              onClick=${() => setOpened(true)}><${Icon} name="jump" size=${15} /> ${opened ? t('connect.openAgain') : t('connect.open')}</a></p>
          <p class="pl-help"><span class="spinner" aria-hidden="true"></span> ${t('connect.waitingNote')}</p>
        </div>`}
        ${q && !heldNow && html`<${QuestionForm} key=${JSON.stringify(q)} q=${q} busy=${busy} error=${answerError} retry=${!!p.retry} onSubmit=${answer} cancel=${cancelButton} />`}
        ${!authUrl && (!q || heldNow) && html`<p class="su-working" data-testid="connect-working"><span class="spinner" aria-hidden="true"></span>
          ${job ? t('connect.working') : t('connect.starting')}</p>`}
        ${!q && answerError && html`<p class="note error" role="alert">${answerError}</p>`}
        ${!(q && !heldNow) && html`<div class="dialog-actions">${cancelButton}</div>`}
      </div>`}

      ${phase === 'done' && html`<div class="su-done" data-testid="connect-done" role="status">
        <p class="su-done-line"><${Icon} name="check" size=${18} />
          <span>${preset && preset.enable ? t('connect.doneRw', { name: (done && done.name) || (preset && preset.target) || writeName })
    : t('connect.done', { name: (done && done.name) || writeName })}</span></p>
        <div class="dialog-actions">
          <button type="button" class="btn" data-testid="connect-close" onClick=${() => onClose(false)}>${t('common.close')}</button>
          ${!(preset && preset.enable) && html`<button type="button" class="btn btn-primary" data-testid="connect-scan"
            onClick=${() => { onClose(false); onScan([(done && done.name) || writeName]); }}>${t('connect.scanNow')}</button>`}
        </div>
      </div>`}

      ${phase === 'failed' && fail && html`<div class="su-failed" data-testid="connect-failed" role="alert">
        <p class="su-failed-line"><${Icon} name="alert" size=${18} /> <strong>${t('connect.failed')}</strong></p>
        <p data-testid="connect-failed-text" data-code=${fail.code || ''}>${fail.text}</p>
        ${fail.detail && html`<${RcloneText} text=${fail.detail} testid="connect-failed-detail" />`}
        <div class="dialog-actions">
          <button type="button" class="btn" data-testid="connect-close" onClick=${() => onClose(false)}>${t('common.close')}</button>
          ${!preset && html`<button type="button" class="btn btn-primary" data-testid="connect-retry"
            onClick=${() => { setPhase('form'); setJob(null); setJobId(null); setFail(null); setHeld(null); }}>${t('connect.retry')}</button>`}
        </div>
      </div>`}
    </div>
  <//>`;
}

// ---------------------------------------------------------------------------------------------- the view

const scanned = (r) => !!(r.last_scan && r.last_scan.finished_at && !/fail|run|part|cancel/i.test(String(r.last_scan.status || '')));

export function Setup() {
  const ctx = useContext(AppCtx);
  const canAct = ctx.config.approvals_enabled !== false;
  const [state, setState] = useState({ loading: true, data: null, error: null });
  const [dialog, setDialog] = useState(null);                 // {preset?}
  const [tests, setTests] = useState({});
  const [adopting, setAdopting] = useState('');
  const [scan, setScan] = useState(activeScan.current);       // {id, names, job, finished, error}
  const [scanError, setScanError] = useState('');
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  async function load({ quiet = false } = {}) {
    if (!quiet) setState((s) => ({ ...s, loading: true }));
    try {
      const data = await api.setupState();
      if (alive.current) setState({ loading: false, data, error: null });
      return data;
    } catch (e) {
      if (alive.current) setState((s) => ({ loading: false, data: s.data, error: e }));
      return null;
    }
  }
  useEffect(() => { load(); }, []);

  const copy = (text) => {
    try { navigator.clipboard.writeText(text); ctx.toast('success', t('common.copied')); } catch (e) { /* no clipboard: the text is selectable */ }
  };

  // ---- scanning
  useEffect(() => {
    if (!scan || scan.finished) return undefined;
    let live = true;
    (async () => {
      let misses = 0;
      let lastRemote = '';
      while (live && alive.current) {
        try {
          const j = await api.job(scan.id);
          if (!live || !alive.current) return;
          misses = 0;
          const p = j.progress || {};
          const next = { ...scan, job: j };
          if (j.status === 'done' || j.status === 'failed') {
            next.finished = true;
            next.status = j.status;
            next.failure = j.status === 'failed' ? failureOf(j).text : '';
            activeScan.current = null;
            setScan(next);
            await load({ quiet: true });
            ctx.reloadConfig();
            if (j.status === 'done') ctx.toast('success', t('setup.scan.done'));
            return;
          }
          activeScan.current = next;
          setScan(next);
          const key = `${p.remote || ''}|${p.stage || ''}`;
          if (key !== lastRemote) { lastRemote = key; load({ quiet: true }); }       // a cloud finished: its count is real now
        } catch (e) {
          misses += 1;
          if (misses > 10) {
            activeScan.current = null;
            setScan({ ...scan, finished: true, status: 'failed', failure: refusalText(e) });
            return;
          }
        }
        await sleep(POLL_MS);
      }
    })();
    return () => { live = false; };
  }, [scan && scan.id, scan && scan.finished]);

  async function startScan(names) {
    setScanError('');
    try {
      const { job_id: id } = await api.rescan(names);
      const next = { id, names, job: null, finished: false };
      activeScan.current = next;
      setScan(next);
    } catch (e) {
      setScanError(refusalText(e));
    }
  }

  // ---- connect, adopt, test, enable changes
  const connected = async () => { await load({ quiet: true }); ctx.reloadConfig(); };

  async function adopt(name) {
    setAdopting(name);
    try {
      await api.adopt(name);
      ctx.toast('success', t('setup.adopt.added', { name }));
      await connected();
    } catch (e) {
      ctx.toast('error', refusalText(e));
    } finally {
      setAdopting('');
    }
  }

  async function test(name) {
    setTests((x) => ({ ...x, [name]: { busy: true } }));
    let result;
    try {
      const r = await api.testRemote(name);
      result = r.ok ? { ok: true } : { ok: false, code: r.code || 'RCLONE_ERROR', params: r.params || null };
    } catch (e) {
      result = { ok: false, code: e.code || null, params: e.params || null, text: refusalText(e) };
    }
    if (alive.current) setTests((x) => ({ ...x, [name]: result }));
  }

  async function enable(r) {
    const ok = await ctx.confirm({
      title: t('setup.enable.title', { name: r.name }),
      message: t(r.provider === 'drive' ? 'setup.enable.textDrive' : 'setup.enable.textOther', { name: r.name }),
      okLabel: t('setup.enable.ok'),
    });
    if (!ok) return;
    try {
      const res = await api.enableWrite(r.name);
      if (res.job_id) {
        setDialog({ preset: { jobId: res.job_id, name: r.name, target: `${r.name}-rw`, enable: true, title: t('setup.enable.runTitle', { name: r.name }) } });
      } else {
        ctx.toast('success', t('setup.enable.done', { name: r.name }));
        await connected();
      }
    } catch (e) {
      ctx.toast('error', refusalText(e));
    }
  }

  const data = state.data;
  const remotes = (data && data.remotes) || [];
  const rcloneRemotes = (data && data.rclone_remotes) || [];
  const loose = rcloneRemotes.filter((r) => !r.in_config);
  const rc = (data && data.rclone) || {};
  const running = !!scan && !scan.finished;

  const rcloneState = !data ? 'todo' : rc.found && rc.ok !== false ? 'done' : 'attention';
  const cloudsState = remotes.length > 0 ? 'done' : 'todo';
  const allScanned = remotes.length > 0 && remotes.every(scanned) && data && data.scanned !== false;
  const scanState = remotes.length === 0 ? 'todo' : allScanned && data.identity_built !== false ? 'done' : 'attention';
  const readyState = rcloneState === 'done' && cloudsState === 'done' && scanState === 'done' ? 'done' : 'todo';

  // What each cloud shows in the scan step: its stored result, or the live count while the running job is on it.
  function scanCell(r) {
    const p = (scan && scan.job && scan.job.progress) || {};
    if (running && scan.names.includes(r.name)) {
      const at = scan.names.indexOf(p.remote);
      const mine = scan.names.indexOf(r.name);
      if (p.stage || (at >= 0 && mine < at)) return { kind: 'done', files: r.files, bytes: r.bytes };
      if (p.remote === r.name) return { kind: 'live', files: p.files || 0 };
      return { kind: 'waiting' };
    }
    if (scan && scan.finished && scan.job && scan.job.result && Array.isArray(scan.job.result.scans)) {
      const mine = scan.job.result.scans.find((s) => s.remote === r.name);
      if (mine && (mine.error || (mine.status && mine.status !== 'complete' && mine.status !== 'done'))) return { kind: 'failed', error: mine.error };
    }
    if (scanned(r)) return { kind: 'done', files: r.files, bytes: r.bytes, when: r.last_scan.finished_at };
    return { kind: 'none' };
  }

  const sp = (scan && scan.job && scan.job.progress) || {};
  const total = sp.remotes_total || (scan && scan.names.length) || 0;
  const pct = !running ? 0 : sp.stage ? 96 : total ? Math.min(95, Math.round((((sp.remote_index || 1) - 1) / total) * 100)) : 0;
  const rcloneAside = data && html`<span class="su-aside" data-testid="setup-rclone-version">${rc.found ? rc.version : ''}</span>`;

  return html`<main class="view view-setup" data-testid="view-setup" aria-label=${t('nav.setup')}>
    <${ViewHeader} title=${t('nav.setup')} hint=${t('setup.hint')}>
      <button class="btn btn-small" data-testid="setup-refresh" onClick=${() => load()} disabled=${state.loading}>
        <${Icon} name="refresh" size=${14} /> ${t('common.refresh')}</button>
    <//>
    <div class="view-body su-body">
      ${state.error && html`<p class="view-error" role="alert" data-testid="setup-error">${refusalText(state.error)}</p>`}
      ${!data && state.loading && html`<p class="empty-hint">${t('common.loading')}</p>`}
      ${data && !canAct && html`<${ReadOnlyPanel} setup=${data} onCopy=${copy} />`}
      ${data && html`<div class="su-steps">

        <${Step} id="rclone" n=${1} state=${rcloneState} title=${t('setup.step.rclone')} aside=${rcloneAside}>
          ${rc.found && rc.ok !== false && html`<p class="su-line" data-testid="setup-rclone-text"><${Icon} name="check" size=${15} /> ${t('setup.rclone.found', { version: rc.version })}</p>`}
          ${rc.found && rc.ok === false && html`<p class="su-line warn" data-testid="setup-rclone-text"><${Icon} name="alert" size=${15} /> ${t('setup.rclone.old', { version: rc.version })}</p>`}
          ${!rc.found && html`<p class="su-line warn" data-testid="setup-rclone-text"><${Icon} name="alert" size=${15} /> ${t('setup.rclone.missing', { binary: rc.binary || 'rclone' })}</p>`}
          ${!(rc.found && rc.ok !== false) && html`<p class="su-line"><a href="https://rclone.org/install/" target="_blank" rel="noopener" data-testid="setup-rclone-install">
            ${t('setup.rclone.install')}</a></p>`}
          <p class="su-sub">${t('setup.rclone.where', { binary: rc.binary || 'rclone', path: data.config_path || '' })}</p>
        <//>

        <${Step} id="clouds" n=${2} state=${cloudsState} title=${t('setup.step.clouds')}>
          ${remotes.length === 0 && html`<p class="su-line" data-testid="setup-no-clouds">${t('setup.clouds.none')}</p>`}
          <ul class="su-clouds" data-testid="setup-clouds">
            ${remotes.map((r) => {
    const tr = tests[r.name];
    const writable = r.writable !== false && !!r.write;
    return html`<li key=${r.name} class="su-cloud" data-testid=${'setup-cloud-' + r.name} data-writable=${writable ? 'true' : 'false'}>
              <div class="su-cloud-row">
                <${CloudName} remotes=${remotes} name=${r.name} />
                <span class="cloud-provider">${providerLabel(r.provider)}</span>
                ${writable
    ? html`<span class="badge badge-dedupe" title=${t('setup.cloud.writableTip')}>${t('setup.cloud.writable')}</span>`
    : html`<span class="badge badge-readonly" title=${t('ov.readOnlyTip')}>${t('ov.readOnly')}</span>`}
                <span class="su-ok"><${Icon} name="check" size=${14} /> ${t('setup.cloud.connected')}</span>
                <span class="spacer"></span>
                <button class="btn btn-small" data-testid=${'setup-test-' + r.name} disabled=${!!(tr && tr.busy)} onClick=${() => test(r.name)}>
                  ${tr && tr.busy ? html`<span class="spinner" aria-hidden="true"></span>` : null} ${t('setup.test')}</button>
                ${canAct && !writable && html`<button class="btn btn-small" data-testid=${'setup-enable-' + r.name} onClick=${() => enable(r)}>${t('setup.enable')}</button>`}
              </div>
              ${tr && !tr.busy && html`<p class=${'su-test ' + (tr.ok ? 'ok' : 'bad')} role="status" data-testid=${'setup-test-result-' + r.name} data-ok=${tr.ok ? 'true' : 'false'}>
                <${Icon} name=${tr.ok ? 'check' : 'alert'} size=${14} />
                <span>${tr.ok ? t('setup.test.ok', { name: r.name })
    : t('setup.test.fail', { reason: tr.text || t(`err.${tr.code}`, wordParams('err', { name: r.name, ...(tr.params || {}) }), t('err.RCLONE_ERROR')) })}
                  ${!tr.ok && tr.code === 'AUTH_EXPIRED' && html` <code>rclone config reconnect ${r.name}:</code>`}</span></p>`}
            </li>`;
  })}
          </ul>
          ${canAct && html`<div class="su-actions">
            <button class="btn btn-primary" data-testid="setup-connect" disabled=${!rc.found} title=${rc.found ? undefined : t('err.RCLONE_MISSING', { binary: rc.binary || 'rclone' })}
              onClick=${() => setDialog({})}><${Icon} name="plus" size=${15} /> ${t('setup.connect')}</button>
          </div>`}
          ${canAct && loose.length > 0 && html`<div class="su-adopt" data-testid="setup-adopt">
            <h3>${t('setup.adopt.title')}</h3>
            <ul>${loose.map((r) => html`<li key=${r.name} data-testid=${'setup-adopt-' + r.name}>
              <span>${t('setup.adopt.found', { name: r.name, type: r.type || r.provider || '' })}</span>
              <span class="spacer"></span>
              <button class="btn btn-small" data-testid=${'setup-adopt-add-' + r.name} disabled=${adopting === r.name} onClick=${() => adopt(r.name)}>
                <${Icon} name="plus" size=${14} /> ${t('setup.adopt.add')}</button></li>`)}</ul>
          </div>`}
          ${!canAct && loose.length > 0 && html`<p class="su-sub" data-testid="setup-adopt-ro">${t('setup.adopt.ro', { names: loose.map((r) => r.name).join(t('common.listSep')) })}</p>`}
        <//>

        <${Step} id="scan" n=${3} state=${scanState} title=${t('setup.step.scan')}>
          ${remotes.length === 0 && html`<p class="su-line muted">${t('setup.scan.needCloud')}</p>`}
          <ul class="su-scans" data-testid="setup-scans">
            ${remotes.map((r) => {
    const c = scanCell(r);
    return html`<li key=${r.name} class="su-scan" data-testid=${'setup-scan-' + r.name} data-kind=${c.kind}>
              <${CloudName} remotes=${remotes} name=${r.name} />
              <span class="su-scan-state">
                ${c.kind === 'done' && html`<${Icon} name="check" size=${14} /> <strong data-testid=${'setup-files-' + r.name}>${t('setup.scan.files', { n: fmtNum(c.files) })}</strong> · ${fmtBytes(c.bytes)}
                  ${c.when && html`<span class="muted"> · ${t('ov.scanned', { when: fmtDateTime(c.when) })}</span>`}`}
                ${c.kind === 'live' && html`<span class="spinner" aria-hidden="true"></span> <strong data-testid=${'setup-files-' + r.name}>${t('setup.scan.files', { n: fmtNum(c.files) })}</strong>…`}
                ${c.kind === 'waiting' && html`<span class="muted">${t('setup.scan.waiting')}</span>`}
                ${c.kind === 'failed' && html`<span class="su-bad" title=${c.error || ''}><${Icon} name="alert" size=${14} /> ${t('setup.scan.cloudFailed')}</span>`}
                ${c.kind === 'none' && html`<span class="muted">${t('ov.neverScanned')}</span>`}
              </span>
              ${canAct && html`<button class="btn btn-small" data-testid=${'setup-scan-one-' + r.name} disabled=${running} onClick=${() => startScan([r.name])}>
                <${Icon} name="refresh" size=${14} /> ${t('setup.scan.one')}</button>`}
            </li>`;
  })}
          </ul>
          ${running && html`<div class="su-progress" data-testid="setup-scan-progress" role="status">
            <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow=${pct} aria-label=${t('prog.label')}><span style=${{ width: `${pct}%` }}></span></div>
            <p class="progress-nums" data-testid="setup-scan-text">${sp.stage ? t(`prog.stage.${sp.stage === 'identity' ? 'identity' : 'dir_stats'}`)
    : sp.remote ? t('prog.scanning', { remote: sp.remote, i: sp.remote_index || 1, n: sp.remotes_total || total || 1, files: fmtNum(sp.files || 0) }) : t('prog.wait')}</p>
          </div>`}
          ${scan && scan.finished && scan.failure && html`<p class="note error" role="alert" data-testid="setup-scan-failed">${t('setup.scan.failed', { reason: scan.failure })}</p>`}
          ${scanError && html`<p class="note error" role="alert" data-testid="setup-scan-error">${scanError}</p>`}
          ${canAct && html`<div class="su-actions">
            <button class="btn btn-primary" data-testid="setup-scan-all" disabled=${running || remotes.length === 0}
              onClick=${() => startScan(remotes.map((r) => r.name))}><${Icon} name="refresh" size=${15} /> ${t('setup.scan.all')}</button>
            <span class="pl-help">${t('setup.scan.help')}</span>
          </div>`}
        <//>

        <${Step} id="ready" n=${4} state=${readyState} title=${t('setup.step.ready')}>
          ${readyState === 'done'
    ? html`<p class="su-line">${t('setup.ready.text')}</p>`
    : html`<p class="su-line muted">${t('setup.ready.notYet')}</p>`}
          <p class="su-sub" data-testid="setup-execution" data-enabled=${data.execution_enabled ? 'true' : 'false'}>
            <${Icon} name=${data.execution_enabled ? 'check' : 'lock'} size=${14} /> ${data.execution_enabled ? t('setup.ready.execOn') : t('setup.ready.execOff')}</p>
          <div class="su-actions">
            <button class="btn btn-primary" data-testid="setup-open-overview" disabled=${readyState !== 'done'}
              onClick=${() => ctx.nav('#/overview')}>${t('setup.ready.open')}</button>
          </div>
        <//>
      </div>`}
    </div>
    ${dialog && html`<${ConnectDialog} setup=${data || {}} preset=${dialog.preset} onConnected=${connected} onScan=${startScan}
      onClose=${(cancelled) => { setDialog(null); if (cancelled) ctx.toast('info', t('connect.cancelled')); load({ quiet: true }); }} />`}
  </main>`;
}
