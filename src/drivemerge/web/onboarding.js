// Clouds (docs/ONBOARDING.md sections 4, 5 and G): the Welcome screen and the cloud cards, the connect dialogs (Google
// Drive with its step-by-step guide, OneDrive and Dropbox with one button, iCloud with Apple ID and password, a token
// pasted from another computer, rclone's own questions), the import of remotes the person already has in rclone,
// and the header's Sync button with its progress pill and per-cloud popover.
//
// Secrets: tokens never reach this page (it sees an account name and a quota). What a person types into a form here
// (client secret, Apple ID password, a pasted token, the answer to a password question) lives only in that form's
// state: it is sent in the body of one POST, the field is emptied before the request goes out, and it is never put in
// the address, in storage or in a toast.
import {
  html, useContext, useEffect, useRef, useState,
} from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import * as api from './api.js';
import { Icon } from './icons.js';
import { AppCtx } from './ctx.js';
import { AgeChip, CloudDot } from './clouds.js';
import { Modal, ViewHeader } from './ui.js';
import { QuestionForm, RcloneText } from './question.js';
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
  { key: 'iclouddrive', label: 'iCloud Drive', needs_client: false, kind: 'config', fields: ['apple_id', 'password'] },
];
const DEFAULT_NAME = { drive: 'gdrive', onedrive: 'onedrive', dropbox: 'dropbox', iclouddrive: 'icloud' };
// Only rclone's own helper on this machine is ever opened or offered as a sign-in link.
const AUTH_URL_RE = /^http:\/\/127\.0\.0\.1:53682\/auth\?/;
const safeAuthUrl = (u) => (typeof u === 'string' && AUTH_URL_RE.test(u) ? u : '');
const isQuestion = (r) => !!r && !!r.flow_id && r.status === 'question' && !!r.question;

/** Copy text and say so; a browser that refuses (no permission, no clipboard) leaves the selectable text as it was. */
function copyText(ctx, text) {
  try {
    Promise.resolve(navigator.clipboard.writeText(text)).then(() => ctx.toast('success', t('common.copied')), () => {});
  } catch (e) { /* no clipboard: the text is selectable */ }
}

/** One argument as a shell would need it (placeholders such as CLIENT_SECRET stay as they are). */
const shellWord = (a) => (/^[A-Za-z0-9_@%+=:,./-]+$/.test(a) ? a : `'${a.replace(/'/g, `'\\''`)}'`);

/** The `rclone authorize` command to run on the other computer, with the client id the person typed filled in. */
export function authorizeCommand(provider, purpose, clientId) {
  const base = (provider.authorize && provider.authorize[purpose]) || ['rclone', 'authorize', provider.key];
  const argv = base.slice(0, 3);
  if (clientId || provider.needs_client) argv.push(clientId || 'CLIENT_ID', 'CLIENT_SECRET');
  argv.push(...base.slice(3));
  return argv.map(shellWord).join(' ');
}
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
 * One sign-in. `provider` is {key, label, needs_client, kind?, fields?, authorize?}; `purpose` 'read' connects a new
 * cloud, 'write' asks the second consent for `conn` (docs/ONBOARDING.md section 3). The auth page opens in a new tab
 * and the flow is polled every second; cancelling or closing the dialog cancels the flow. `resume` is a question
 * result (an import, or a finish that rclone interrupted with a question): the dialog opens on that question.
 * Phases: form, starting, waiting, finishing, rename, question.
 */
export function ConnectDialog({ provider, purpose = 'read', conn = null, taken, resume = null, onClose, onDone }) {
  const ctx = useContext(AppCtx);
  const write = purpose === 'write';
  const isGoogle = provider.key === 'drive';
  const isConfig = provider.kind === 'config';
  const [phase, setPhase] = useState(isQuestion(resume) ? 'question' : 'form');
  const [clientId, setClientId] = useState('');
  const [secret, setSecret] = useState('');
  const [prod, setProd] = useState(false);
  const [name, setName] = useState('');
  const [touched, setTouched] = useState({});
  const [needClient, setNeedClient] = useState(false);
  const [own, setOwn] = useState(false);
  const [error, setError] = useState(null);
  const [authUrl, setAuthUrl] = useState('');
  const [appleId, setAppleId] = useState('');
  const [applePw, setApplePw] = useState('');
  const [pasteToken, setPasteToken] = useState('');
  const [question, setQuestion] = useState(isQuestion(resume) ? { q: resume.question, retry: !!resume.retry, n: 0 } : null);
  const [answerBusy, setAnswerBusy] = useState(false);
  const [answerError, setAnswerError] = useState('');
  const flow = useRef(isQuestion(resume) ? resume.flow_id : null);
  const asked = useRef(0);
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
  const appleOk = !isConfig || (appleId.trim() !== '' && applePw !== '');
  const valid = idOk && secretOk && nameOk && prodOk && appleOk;
  const canPaste = !isConfig && valid && pasteToken.trim() !== '';

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
  const close = () => { if (phase === 'waiting' || phase === 'question') cancelFlow(); onClose(); };
  // Focus can be on the page body once the clicked button is gone; Escape must still close the dialog.
  // The handler reads the latest render through a ref: a listener re-bound in an effect would still hold the
  // previous phase (e.g. 'starting', busy) for a moment after the waiting view is already on screen.
  const keyRef = useRef(null);
  keyRef.current = { busy: busy || answerBusy, close };
  useEffect(() => {
    const onKey = (e) => { const k = keyRef.current; if (e.key === 'Escape' && !k.busy) k.close(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  /** rclone asked something the page cannot answer by itself: show its question. */
  function ask(res) {
    asked.current += 1;
    flow.current = res.flow_id;
    setQuestion({ q: res.question, retry: !!res.retry, n: asked.current });
    setAnswerError('');
    setPhase('question');
  }
  /** The outcome of finish / answer / start: another question, or the connection itself. */
  function settle(res) {
    if (isQuestion(res)) { ask(res); return; }
    flow.current = null;
    if (alive.current) onDone(res);
  }
  function failTo(e, back = 'form') {
    flow.current = null;
    const words = describeError(e, provider.key);
    if (resume) { ctx.toast('error', words.text); onClose(); return; }      // an import has no form to go back to
    setError(words);
    setPhase(back);
  }

  async function finish(id) {
    setPhase('finishing');
    try {
      settle(await api.connectFinish(id, write ? conn.name : nameText || undefined));
    } catch (e) {
      if (!alive.current) return;
      if (e.code === 'NAME_TAKEN' || e.code === 'BAD_NAME') { setError(describeError(e, provider.key)); setPhase('rename'); return; }
      failTo(e);
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
        if (failures > 5) { failTo(e); return; }
        continue;
      }
      if (!alive.current || flow.current !== id) return;
      if (f.status === 'succeeded') { await finish(id); return; }
      if (f.status === 'question' && f.question) { ask({ ...f, flow_id: id }); return; }
      if (f.status === 'done' && f.connection) { settle(f.connection); return; }
      if (f.status === 'cancelled') { flow.current = null; setPhase('form'); return; }
      if (f.status === 'failed' || f.status === 'timeout') {
        failTo({ code: f.code || (f.status === 'timeout' ? 'FLOW_TIMEOUT' : 'FLOW_FAILED'), params: f.params, message: f.error || '' });
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
    if (isConfig) {
      body.apple_id = appleId.trim();
      body.password = applePw;
      if (nameText) body.name = nameText;
      setApplePw('');                        // the password leaves this component's state before the request goes out
    }
    let made;
    try {
      made = await api.connectStart(body);
    } catch (e) {
      if (!alive.current) return;
      if (e.code === 'NEEDS_CLIENT') setNeedClient(true);
      failTo(e);
      return;
    }
    if (!alive.current) { if (made && made.flow_id) api.connectCancel(made.flow_id).catch(() => {}); return; }
    if (!made.flow_id) { settle(made); return; }          // iCloud without a second factor: the connection itself
    flow.current = made.flow_id;
    if (isQuestion(made)) { ask(made); return; }
    if (made.status === 'succeeded' && !made.auth_url) { await finish(made.flow_id); return; }
    const url = safeAuthUrl(made.auth_url);
    setAuthUrl(url);
    if (url) window.open(url, '_blank', 'noopener');
    setPhase('waiting');
    poll(made.flow_id);
  }

  async function submitPaste() {
    const token = pasteToken.trim();
    setPasteToken('');                       // the token leaves this component's state before the request goes out
    setError(null);
    setPhase('starting');
    const body = { provider: provider.key, purpose, token };
    if (showClient && idText && secretText) { body.client_id = idText; body.client_secret = secretText; }
    if (write) body.name = conn.name; else if (nameText) body.name = nameText;
    let made;
    try {
      made = await api.connectPaste(body);
    } catch (e) {
      if (!alive.current) return;
      if (e.code === 'NEEDS_CLIENT') setNeedClient(true);
      failTo(e);
      return;
    }
    if (!alive.current) { if (made && made.flow_id) api.connectCancel(made.flow_id).catch(() => {}); return; }
    flow.current = made.flow_id;
    await finish(made.flow_id);
  }

  async function submitAnswer(value) {
    setAnswerBusy(true);
    setAnswerError('');
    try {
      settle(await api.connectAnswer(flow.current, value));
    } catch (e) {
      if (!alive.current) return;
      if (e.status === 0 || e.code === 'BAD_REQUEST') setAnswerError(errorText(e));
      else failTo(e);
    } finally {
      if (alive.current) setAnswerBusy(false);
    }
  }

  async function cancelWaiting() {
    await cancelFlow();
    setPhase('form');
  }
  const copy = (text) => copyText(ctx, text);

  const title = write ? t('conn.allowTitle', { name: conn.name }) : t('conn.title', { provider: provider.label });
  const command = !isConfig ? authorizeCommand(provider, purpose, showClient ? idText : '') : '';
  const cancelQuestion = html`<button type="button" class="btn" data-testid="connect-cancel" disabled=${answerBusy} onClick=${cancelWaiting}>${t('common.cancel')}</button>`;
  return html`<${Modal} label=${title} onClose=${close} testid="connect-dialog" wide=${isGoogle && !write} dismissable=${!busy && !answerBusy}>
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
      ${phase === 'question' && question && html`<div data-testid="connect-asking">
        <${QuestionForm} key=${`${question.n}:${JSON.stringify(question.q)}`} q=${question.q} busy=${answerBusy} error=${answerError}
          retry=${question.retry} onSubmit=${submitAnswer} cancel=${cancelQuestion} />
        ${answerBusy && html`<p class="muted connect-busy" data-testid="connect-answering"><span class="spinner" aria-hidden="true"></span> ${t('conn.finishing')}</p>`}</div>`}
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
        ${isConfig && html`<div class="su-icloud" data-testid="connect-icloud">
          <p>${t('conn.icloud.intro')}</p>
          <label class="field"><span>${t('conn.icloud.appleId')}</span>
            <input type="text" data-testid="connect-apple-id" value=${appleId} autocomplete="off" spellcheck="false" autocapitalize="off"
              onInput=${(e) => setAppleId(e.target.value)} /></label>
          <label class="field"><span>${t('conn.icloud.password')}</span>
            <input type="password" data-testid="connect-apple-password" value=${applePw} autocomplete="new-password" spellcheck="false"
              onInput=${(e) => setApplePw(e.target.value)} /></label>
          <p class="muted" data-testid="icloud-twofactor">${t('conn.icloud.twoFactor')}</p>
          <p class="muted" data-testid="icloud-processlist">${t('conn.icloud.processList')}</p></div>`}
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
        ${!isConfig && html`<details class="own-app conn-paste" data-testid="conn-paste">
          <summary data-testid="conn-paste-toggle">${t('conn.paste.toggle')}</summary>
          <p class="muted">${t('conn.paste.text')}</p>
          <p class="conn-paste-label">${t('conn.paste.command')}</p>
          <div class="su-code"><pre data-testid="conn-paste-command" tabindex="0" aria-label=${t('conn.paste.command')}>${command}</pre>
            <button type="button" class="btn btn-small su-copy" data-testid="conn-paste-copy" onClick=${() => copy(command)}>
              <${Icon} name="copy" size=${14} /> ${t('common.copy')}</button></div>
          <div class="banner-warn guide-warn" role="note" data-testid="conn-paste-scope"><${Icon} name="alert" size=${15} /><span>${t('conn.paste.scope')}</span></div>
          <label class="field"><span>${t('conn.paste.token')}</span>
            <textarea class="conn-paste-token" data-testid="conn-paste-token" rows="4" value=${pasteToken} autocomplete="off" spellcheck="false"
              onInput=${(e) => setPasteToken(e.target.value)}></textarea></label>
          <button type="button" class="btn" data-testid="conn-paste-submit" disabled=${!canPaste || busy} onClick=${submitPaste}>${t('conn.paste.submit')}</button>
        </details>`}
        <${ErrorBox} error=${error} />
      </div>`}
    </div>
    ${phase !== 'question' && html`<footer class="modal-foot">
      ${phase === 'waiting'
    ? html`<button class="btn" data-testid="connect-cancel" onClick=${cancelWaiting}>${t('common.cancel')}</button>`
    : html`<button class="btn" data-testid="connect-cancel" onClick=${close} disabled=${busy}>${t('common.cancel')}</button>`}
      ${(phase === 'form' || phase === 'starting') && html`<button class="btn btn-primary" data-testid="connect-start" disabled=${!valid || busy} onClick=${start}>
        ${busy ? html`<span class="spinner" aria-hidden="true"></span>` : html`<${Icon} name=${write ? 'lock' : 'cloud'} size=${15} />`}
        ${write ? t('conn.allowButton') : isGoogle ? t('conn.google.button') : isConfig ? t('conn.connect') : t('conn.signIn', { provider: provider.label })}</button>`}
      ${phase === 'rename' && html`<button class="btn btn-primary" data-testid="connect-finish" disabled=${!nameOk || !nameText}
        onClick=${() => finish(flow.current)}>${t('conn.finish')}</button>`}
    </footer>`}
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

/** A failed Test in words: the sentence of its code, else what rclone said; rclone's own detail goes in its box. */
function testFailure(result) {
  const sentence = result.code ? errorText({ code: result.code, params: result.params, message: result.error }) : '';
  const detail = result.code === 'RCLONE_ERROR' && result.params && typeof result.params.detail === 'string' ? result.params.detail : '';
  return { text: sentence || result.error || t('err.unknown'), detail };
}

function ConnCard({
  conn, providers, test, scan, canAct, manual = false, onTest, onAllow, onDisconnect, onSyncOne, syncing,
}) {
  const ctx = useContext(AppCtx);
  const result = test && !test.loading ? test : null;
  // The server sends no per-connection state: the Test decides ("relogin" when the sign-in has expired).
  const expired = !!result && result.ok === false && result.code === 'AUTH_EXPIRED';
  const state = expired ? 'relogin' : result && result.ok === false ? 'error' : conn.writable ? 'rw' : 'ro';
  const account = (result && result.account) || conn.account;
  const quota = (result && result.quota) || conn.quota;
  const spec = providers.find((p) => p.key === conn.provider);
  const failure = result && result.ok === false ? testFailure(result) : null;
  return html`<article class="conn-card" data-testid=${'conn-card-' + conn.name} data-state=${state} data-provider=${conn.provider}
    data-source=${manual ? 'config' : 'connection'}>
    <header><${ProviderMark} provider=${conn.provider} />
      <span class="conn-name">${conn.name}</span><span class="cloud-provider">${spec ? spec.label : conn.provider}</span></header>
    <p><span class=${'conn-state tone-' + STATE_TONE[state]} data-testid=${'conn-state-' + conn.name}>
      <${Icon} name=${state === 'ro' || state === 'rw' ? 'check' : 'alert'} size=${12} />${t(`conn.state.${state}`)}</span></p>
    <p class="conn-account" data-testid=${'conn-account-' + conn.name}>${account || html`<span class="muted">${t('conn.accountUnknown')}</span>`}</p>
    ${manual && html`<p class="muted conn-note" data-testid=${'conn-manual-' + conn.name}>${t('conn.manual')}</p>`}
    ${!manual && conn.scoped === false && html`<p class="muted conn-note" data-testid=${'conn-unscoped-' + conn.name}>${t('conn.unscoped')}</p>`}
    ${test && test.loading ? html`<p class="muted"><span class="spinner" aria-hidden="true"></span> ${t('conn.testing')}</p>`
    : html`<${Quota} quota=${quota} name=${conn.name} />`}
    <p class="conn-sync"><${AgeChip} iso=${scan} testid=${'conn-age-' + conn.name} /></p>
    ${failure && html`<div class="note error" data-testid=${'conn-error-' + conn.name} data-code=${result.code || ''} role="alert">
      <p>${t('conn.testFailed', { error: failure.text })}</p>
      ${failure.detail && html`<${RcloneText} text=${failure.detail} testid=${'conn-error-detail-' + conn.name} />`}</div>`}
    <footer class="conn-actions">
      <button class="btn btn-small" data-testid=${'conn-test-' + conn.name} onClick=${onTest} disabled=${!canAct || (test && test.loading)}>${t('conn.test')}</button>
      <button class="btn btn-small" data-testid=${'conn-sync-' + conn.name} onClick=${onSyncOne} disabled=${!canAct || syncing}>${t('conn.syncOne')}</button>
      ${!manual && !conn.writable && html`<button class="btn btn-small" data-testid=${'conn-allow-' + conn.name} onClick=${onAllow}
        disabled=${!canAct} title=${t('conn.allowTip')}><${Icon} name="lock" size=${13} /> ${t('conn.allow')}</button>`}
      ${!manual && html`<a class="btn btn-small" href=${freeHash(conn.name)} data-testid=${'conn-free-' + conn.name}
        onClick=${(e) => { e.preventDefault(); ctx.nav(freeHash(conn.name)); }}>${t('conn.free')}</a>
      <span class="spacer"></span>
      <button class="btn btn-small btn-danger-ghost" data-testid=${'conn-disconnect-' + conn.name} onClick=${onDisconnect}
        disabled=${!canAct}>${t('conn.disconnect')}</button>`}
    </footer>
  </article>`;
}

function ProviderTile({ spec, connected, onConnect, disabled }) {
  const google = spec.key === 'drive';
  return html`<article class="conn-card conn-tile" data-testid=${'provider-' + spec.key} data-connected=${connected ? 'true' : 'false'}>
    <header><${ProviderMark} provider=${spec.key} /><span class="conn-name">${spec.label}</span></header>
    <p class="muted">${connected ? t('conn.another') : t(`conn.tile.${spec.key}`, null, t('conn.tile.other'))}</p>
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

const READONLY_COMMANDS = {
  connect: 'drivemerge connect drive\ndrivemerge connect onedrive\ndrivemerge connect dropbox\ndrivemerge connect iclouddrive',
  scan: 'drivemerge scan --all\ndrivemerge identity rebuild',
};

function CodeBlock({ text, label, testid, onCopy }) {
  return html`<div class="su-code">
    <pre data-testid=${testid} tabindex="0" aria-label=${label}>${text}</pre>
    <button class="btn btn-small su-copy" onClick=${() => onCopy(text)} data-testid=${testid + '-copy'}>
      <${Icon} name="copy" size=${14} /> ${t('common.copy')}</button>
  </div>`;
}

/** Approvals are disabled (the server was not started from a person's terminal): look, but connect in a terminal. */
function ReadOnlyPanel({ onCopy }) {
  return html`<section class="su-ro" data-testid="clouds-readonly" aria-labelledby="clouds-ro-h">
    <h2 id="clouds-ro-h"><${Icon} name="lock" size=${16} /> ${t('conn.ro.title')}</h2>
    <p>${t('conn.ro.text')}</p>
    <ol class="su-ro-steps">
      <li><p>${t('conn.ro.connect')}</p><${CodeBlock} text=${READONLY_COMMANDS.connect} label="drivemerge connect" testid="clouds-cmd-connect" onCopy=${onCopy} /></li>
      <li><p>${t('conn.ro.scan')}</p><${CodeBlock} text=${READONLY_COMMANDS.scan} label="drivemerge scan" testid="clouds-cmd-scan" onCopy=${onCopy} /></li>
    </ol>
  </section>`;
}

/** "Already use rclone?": the remotes of the person's own rclone config that can be copied in (docs/ONBOARDING.md G4.4). */
/** The name `import_remote` derives from a source (connections._import_name): lowercase, runs of anything but a-z 0-9 - become `-`. */
export function importName(source) {
  return String(source).toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '');
}

/** First `base-2`, `base-3` ... that passes the name rule and is not in `taken` (the fallback when the server sent no suggested_name). */
function suggestFree(base, taken) {
  for (let n = 2; n < 1000; n += 1) {
    const suffix = `-${n}`;
    const cand = base.slice(0, 31 - suffix.length).replace(/-+$/, '') + suffix;
    if (NAME_RE.test(cand) && !taken.has(cand)) return cand;
  }
  return base;
}

/** The import row turned into a rename form: the derived name is taken (or unusable), so the person picks another one. */
function ImportRenameForm({ row, initial, code, taken, busy, onSubmit, onCancel }) {
  const [text, setText] = useState(initial);
  const [serverCode, setServerCode] = useState(code);
  const input = useRef(null);
  useEffect(() => { if (input.current) { input.current.focus(); input.current.select(); } }, []);
  const name = text.trim();
  const clientCode = !NAME_RE.test(name) ? 'BAD_NAME' : taken.has(name) ? 'NAME_TAKEN' : '';
  const reason = clientCode || serverCode;
  const submit = async () => {
    if (clientCode || busy) return;
    const result = await onSubmit(name);
    if (result) setServerCode(result);
  };
  const onKey = (e) => {
    if (e.key === 'Enter') {
      if (e.isComposing || e.keyCode === 229) return;
      e.preventDefault();
      submit();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      if (!busy) onCancel();
    }
  };
  return html`<form class="import-rename" data-testid=${'import-rename-' + row.name} onSubmit=${(e) => e.preventDefault()}>
    <label class="field"><span>${t('conn.import.renameLabel', { name: row.name })}</span>
      <input type="text" ref=${input} data-testid=${'import-rename-input-' + row.name} value=${text} autocomplete="off" spellcheck="false"
        aria-invalid=${!!reason} aria-describedby=${reason ? 'import-rename-err-' + row.name : undefined}
        onInput=${(e) => { setText(e.target.value); setServerCode(''); }} onKeyDown=${onKey} /></label>
    ${reason && html`<p class="field-error" id=${'import-rename-err-' + row.name} role="alert" data-testid=${'import-rename-error-' + row.name}
      data-code=${reason}>${reason === 'NAME_TAKEN' ? t('err.NAME_TAKEN') : t('err.BAD_NAME')}</p>`}
    <div class="import-rename-actions">
      <button type="button" class="btn btn-small btn-primary" data-testid=${'import-rename-ok-' + row.name} disabled=${!!clientCode || !!busy}
        onClick=${submit}>${busy ? html`<span class="spinner" aria-hidden="true"></span>` : null} ${t('conn.import.renameOk')}</button>
      <button type="button" class="btn btn-small" data-testid=${'import-rename-cancel-' + row.name} disabled=${!!busy}
        onClick=${onCancel}>${t('common.cancel')}</button>
    </div>
  </form>`;
}

function ImportList({ info, canAct, busy, renaming, taken, onImport, onRename, onCancelRename }) {
  const remotes = (info && info.remotes) || [];
  if (!info || (!remotes.length && !info.encrypted)) return null;
  return html`<section aria-labelledby="clouds-import-h" data-testid="import-section">
    <h2 id="clouds-import-h" class="section-h">${t('conn.import.heading')}</h2>
    <p class="muted">${t('conn.import.text')}</p>
    ${info.encrypted && html`<p class="note" data-testid="import-encrypted">${t('conn.import.encrypted')}</p>`}
    ${remotes.length > 0 && html`<ul class="import-list" data-testid="import-list">
      ${remotes.map((r) => html`<li key=${r.name} class="import-row" data-testid=${'import-' + r.name} data-supported=${r.supported ? 'true' : 'false'}
        data-imported=${r.imported ? 'true' : 'false'}>
        <${ProviderMark} provider=${r.provider} />
        <span class="conn-name">${r.name}</span><span class="cloud-provider">${r.type}</span>
        <span class="spacer"></span>
        ${renaming && renaming.source === r.name ? html`<${ImportRenameForm} row=${r} initial=${renaming.suggestion} code=${renaming.code} taken=${taken}
          busy=${busy === r.name} onSubmit=${(name) => onRename(r, name)} onCancel=${onCancelRename} />`
    : r.imported ? html`<span class="muted" data-testid=${'import-done-' + r.name}><${Icon} name="check" size=${12} /> ${t('conn.import.done', { name: r.name })}</span>`
    : html`${!r.supported && html`<span class="muted" data-testid=${'import-unsupported-' + r.name}>${t('conn.import.unsupported')}</span>`}
          <button class="btn btn-small" data-testid=${'import-add-' + r.name} disabled=${!canAct || !r.supported || busy}
            onClick=${() => onImport(r)}>${busy === r.name ? html`<span class="spinner" aria-hidden="true"></span>` : null} ${t('conn.import.add')}</button>`}
      </li>`)}</ul>`}
  </section>`;
}

export function Clouds() {
  const ctx = useContext(AppCtx);
  const setup = ctx.setup.data;
  const canAct = ctx.config.approvals_enabled !== false;
  const conns = (setup && setup.connections) || [];
  const manual = (setup && setup.manual_remotes) || [];
  const providers = setup && setup.providers && setup.providers.length ? setup.providers : PROVIDERS;
  const rclone = setup && setup.rclone;
  const remotes = ctx.config.remotes || [];
  const [tests, setTests] = useState({});
  const [scans, setScans] = useState({});
  const [dialog, setDialog] = useState(null);
  const [importable, setImportable] = useState(null);
  const [importing, setImporting] = useState('');
  const [renaming, setRenaming] = useState(null);          // { source, suggestion, code }: the import row is a rename form
  const [focusBack, setFocusBack] = useState('');          // the source whose Import button gets the focus after Cancel
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  const names = [...conns, ...manual].map((c) => c.name).join(',');

  async function runTest(name) {
    if (!canAct) return;
    setTests((x) => ({ ...x, [name]: { loading: true } }));
    let result;
    try {
      result = await api.testConnection(name);
    } catch (e) {
      result = { ok: false, code: e.code, params: e.params, error: errorText(e) };
    }
    if (alive.current) setTests((x) => ({ ...x, [name]: result }));
  }
  // A page that cannot act (approvals disabled) never runs a Test by itself: it would only be refused.
  useEffect(() => { if (canAct) [...conns, ...manual].forEach((c) => { if (!tests[c.name]) runTest(c.name); }); }, [names, canAct]);
  useEffect(() => {
    let live = true;
    api.coverage({ maxBars: 1 }).then((cov) => {
      if (live) setScans(Object.fromEntries((cov.remotes || []).map((r) => [r.name, r.last_scan && r.last_scan.finished_at])));
    }, () => {});
    return () => { live = false; };
  }, [names, ctx.syncEpoch]);
  const loadImportable = () => api.importable().then((info) => { if (alive.current) setImportable(info); }, () => {});
  useEffect(() => { loadImportable(); }, [names]);

  const taken = new Set([...remotes.map((r) => r.name), ...conns.map((c) => c.name), ...manual.map((c) => c.name)]);
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

  /** Allow changes: a second sign-in, or (iCloud, an imported remote: the one sign-in can write already) a confirmation. */
  async function allow(conn) {
    if (conn.scoped !== false) {
      setDialog({ provider: providers.find((p) => p.key === conn.provider) || { key: conn.provider, label: conn.provider }, purpose: 'write', conn });
      return;
    }
    const ok = await ctx.confirm({
      title: t('conn.allowSameTitle', { name: conn.name }), message: t('conn.allowSameText', { name: conn.name }), okLabel: t('conn.allow'),
    });
    if (!ok) return;
    try {
      const flow = await api.connectStart({ provider: conn.provider, purpose: 'write', name: conn.name });
      await done(await api.connectFinish(flow.flow_id, conn.name), true);
    } catch (e) {
      ctx.toast('error', errorText(e));
    }
  }

  /** Import `r` (under `name` when the person chose one). Returns the NAME_TAKEN / BAD_NAME code that opened or kept the rename form. */
  async function importRemote(r, name) {
    setImporting(r.name);
    try {
      const res = await api.connectImport(name ? { source: r.name, name } : { source: r.name });
      if (!alive.current) return '';
      setRenaming(null);
      if (res && res.flow_id && res.status === 'question') {
        setDialog({ provider: { key: r.provider || 'other', label: r.name }, purpose: 'read', resume: res });
      } else {
        await done(res, false);
        loadImportable();
      }
    } catch (e) {
      if (e.code === 'NAME_TAKEN' || e.code === 'BAD_NAME') {
        if (alive.current) {
          const base = importName(r.name);
          const suggestion = (e.params && e.params.suggested_name) || (NAME_RE.test(base) ? suggestFree(base, taken) : base);
          setRenaming((prev) => (prev && prev.source === r.name ? { ...prev, code: e.code } : { source: r.name, suggestion, code: e.code }));
        }
        return e.code;
      }
      ctx.toast('error', errorText(e));
    } finally {
      if (alive.current) setImporting('');
    }
    return '';
  }
  const cancelRename = () => {
    if (renaming) setFocusBack(renaming.source);
    setRenaming(null);
  };
  useEffect(() => {
    if (!focusBack || renaming) return;
    const btn = document.querySelector(`[data-testid="import-add-${CSS.escape(focusBack)}"]`);
    if (btn) btn.focus();
    setFocusBack('');
  }, [focusBack, renaming]);

  const copy = (text) => copyText(ctx, text);

  async function syncAndContinue() {
    const r = await ctx.sync.start();
    if (r.ok) ctx.nav('#/overview');
  }

  const empty = conns.length === 0 && manual.length === 0 && remotes.length === 0;      // clouds configured by hand count as connected
  const blocked = !!rclone && !rclone.ok;
  return html`<main class="view view-clouds" data-testid="view-clouds" aria-label=${t('nav.clouds')}>
    <${ViewHeader} title=${empty ? t('welcome.title') : t('nav.clouds')} hint=${empty ? '' : t('clouds.hint')}>
      <${RcloneBanner} rclone=${rclone && rclone.ok ? rclone : null} onChanged=${ctx.refreshSetup} />
    <//>
    <div class="view-body">
      ${!canAct && html`<${ReadOnlyPanel} onCopy=${copy} />`}
      ${rclone && !rclone.ok && html`<${RcloneBanner} rclone=${rclone} onChanged=${ctx.refreshSetup} />`}
      ${empty && html`<section class="welcome" data-testid="welcome">
        <h2>${t('welcome.heading')}</h2>
        <p>${t('welcome.text')}</p>
        <ul class="welcome-steps"><li>${t('welcome.step1')}</li><li>${t('welcome.step2')}</li><li>${t('welcome.step3')}</li></ul></section>`}
      ${ctx.setup.error && !setup && html`<p class="view-error" role="alert" data-testid="setup-error">${errorText(ctx.setup.error)}</p>`}
      ${(conns.length > 0 || manual.length > 0) && html`<div class="conn-grid" data-testid="conn-cards">
        ${conns.map((c) => html`<${ConnCard} key=${c.name} conn=${c} providers=${providers} test=${tests[c.name]} scan=${scans[c.name]} canAct=${canAct}
          syncing=${ctx.sync.running} onTest=${() => runTest(c.name)} onSyncOne=${() => ctx.sync.start([c.name])}
          onAllow=${() => allow(c)} onDisconnect=${() => disconnect(c)} />`)}
        ${manual.map((c) => html`<${ConnCard} key=${'manual-' + c.name} conn=${c} manual providers=${providers} test=${tests[c.name]} scan=${scans[c.name]}
          canAct=${canAct} syncing=${ctx.sync.running} onTest=${() => runTest(c.name)} onSyncOne=${() => ctx.sync.start([c.name])} />`)}</div>`}
      <section aria-labelledby="clouds-add-h">
        <h2 id="clouds-add-h" class="section-h">${empty ? t('clouds.connectHeading') : t('clouds.addHeading')}</h2>
        <div class="conn-grid" data-testid="provider-tiles">
          ${providers.map((p) => html`<${ProviderTile} key=${p.key} spec=${p} connected=${conns.some((c) => c.provider === p.key)}
            disabled=${blocked || !canAct} onConnect=${() => setDialog({ provider: p, purpose: 'read' })} />`)}</div>
      </section>
      <${ImportList} info=${importable} canAct=${canAct && !blocked} busy=${importing} renaming=${renaming} taken=${taken}
        onImport=${(r) => importRemote(r)} onRename=${importRemote} onCancelRename=${cancelRename} />
      ${(conns.length > 0 || manual.length > 0) && html`<div class="welcome-actions">
        <button class="btn btn-primary" data-testid="welcome-sync" disabled=${!canAct || ctx.sync.running} onClick=${syncAndContinue}>
          ${ctx.sync.running ? html`<span class="spinner" aria-hidden="true"></span>` : html`<${Icon} name="refresh" size=${15} />`} ${t('welcome.sync')}</button>
        <span class="muted">${t('welcome.syncHelp')}</span></div>`}
    </div>
    ${dialog && html`<${ConnectDialog} provider=${dialog.provider} purpose=${dialog.purpose} conn=${dialog.conn} resume=${dialog.resume} taken=${taken}
      onClose=${() => setDialog(null)} onDone=${(made) => done(made, dialog.purpose === 'write')} />`}
  </main>`;
}
