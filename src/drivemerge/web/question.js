// The generic form for one question of rclone's configuration (docs/ONBOARDING.md section G5): a radio group for
// exclusive examples, a select-or-type field, a password input, yes/no buttons, a retry line, and rclone's own help
// text in a box that says it is rclone's.
//
// Secrets: a password or token typed here lives only in this form's state. It is sent in the body of one POST (never
// in a URL, never in storage, never in a toast) and the field is emptied before the request goes out.
import {
  html, useEffect, useRef, useState,
} from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import { Icon } from './icons.js';

/** rclone's own words (English, dynamic) in a box that says so; shown exactly as received. */
export function RcloneText({ text, children, testid }) {
  return html`<div class="su-rclone" data-testid=${testid}>
    <div class="su-rclone-tag"><${Icon} name="info" size=${13} /> ${t('conn.q.fromRclone')}</div>
    ${text ? html`<pre class="su-rclone-text" lang="en">${text}</pre>` : null}
    ${children}
  </div>`;
}

/** One question of rclone's configuration, whatever it is: radio group, buttons, text, or a secret. */
export function QuestionForm({ q, busy, error, retry, onSubmit, cancel }) {
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
    <h3 class="su-q-title">${t('conn.q.title')} <code data-testid="connect-q-name">${q.name}</code></h3>
    ${retry && html`<p class="su-retry" role="alert" data-testid="connect-q-retry"><${Icon} name="alert" size=${14} /> ${t('conn.q.retry')}</p>`}
    <${RcloneText} text=${q.help} testid="connect-q-help">
      ${radios && html`<div role="radiogroup" aria-label=${q.name} class="su-options" data-testid="connect-q-options">
        ${examples.map((e, i) => html`<label key=${i} class=${'su-option' + (pick === i ? ' on' : '')}>
          <input type="radio" name="su-pick" checked=${pick === i} ref=${i === (pick === null ? 0 : pick) ? first : undefined}
            onChange=${() => setPick(i)} data-testid=${'connect-q-option-' + i} />
          <span class="su-option-main"><span class="su-option-value"><code>${String(e.value) === '' ? t('conn.q.empty') : String(e.value)}</code>
            ${String(e.value) === dflt && dflt !== '' && html`<span class="badge badge-new">${t('conn.q.isDefault')}</span>`}</span>
            ${e.help && html`<span class="su-option-help" lang="en">${e.help}</span>`}</span></label>`)}
      </div>`}
    <//>
    ${isBool && html`<div class="su-bool" role="group" aria-label=${q.name}>
      <button type="button" class=${'btn' + (dflt !== 'false' ? ' btn-primary' : '')} disabled=${busy} data-testid="connect-q-yes" ref=${first}
        onClick=${() => finish('true')}>${t('conn.q.yes')}</button>
      <button type="button" class=${'btn' + (dflt === 'false' ? ' btn-primary' : '')} disabled=${busy} data-testid="connect-q-no"
        onClick=${() => finish('false')}>${t('conn.q.no')}</button>
    </div>`}
    ${!isBool && !radios && html`<div class="su-q-input">
      ${!secret && examples.length > 0 && html`<label class="field"><span>${t('conn.q.pick')}</span>
        <select class="select" data-testid="connect-q-select" value="" onChange=${(e) => { if (e.target.value !== '') setText(examples[Number(e.target.value)].value); }}>
          <option value="">${t('conn.q.custom')}</option>
          ${examples.map((e, i) => html`<option key=${i} value=${i} lang="en">${String(e.value) === '' ? t('conn.q.empty') : e.value}${e.help ? ` — ${String(e.help).split('\n')[0].slice(0, 80)}` : ''}</option>`)}
        </select></label>`}
      <label class="field"><span>${t('conn.q.answer')}${q.required ? ' *' : ''}</span>
        <input type=${secret ? 'password' : 'text'} value=${text} ref=${first} autocomplete=${secret ? 'new-password' : 'off'} spellcheck="false"
          autocapitalize="off" data-testid="connect-q-input" aria-required=${!!q.required} onInput=${(e) => setText(e.target.value)} /></label>
      ${secret && html`<p class="pl-help">${t('conn.q.secret')}</p>`}
      ${hasDefault && html`<p class="pl-help">${t('conn.q.default', { value: dflt })}</p>`}
      ${!q.required && html`<p class="pl-help">${t('conn.q.optional')}</p>`}
    </div>`}
    ${error && html`<p class="note error" role="alert" data-testid="connect-q-error">${error}</p>`}
    <div class="su-q-actions">
      ${cancel}
      ${!isBool && html`<button type="submit" class="btn btn-primary" disabled=${busy || missing} data-testid="connect-q-continue">${t('conn.q.continue')}</button>`}
    </div>
  </form>`;
}
