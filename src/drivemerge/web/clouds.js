// A stable colour per cloud and the small components that carry it. The colours are CSS custom properties
// (--cloud-0 ... --cloud-7, defined in styles.css for the light and the dark scheme, each at least 3:1 against the
// surface), assigned by configuration order, so a cloud keeps its colour in every view and after a reload.
import { html } from './vendor/htm-preact-standalone.mjs';
import { t } from './i18n.js';
import { Icon } from './icons.js';
import { ago, fmtDateTime, isStaleAge } from './util.js';

export const CLOUD_COLORS = 8;

/** Position of a cloud in the configuration order (-1 when unknown). */
export const cloudIndex = (remotes, name) => remotes.findIndex((r) => r.name === name);
export const cloudColor = (remotes, name) => {
  const i = cloudIndex(remotes, name);
  return `var(--cloud-${(i < 0 ? CLOUD_COLORS - 1 : i) % CLOUD_COLORS})`;
};

/** A filled dot in the cloud's colour. */
export function CloudDot({ remotes, name, size = 10 }) {
  return html`<span class="cloud-dot" aria-hidden="true" style=${{ background: cloudColor(remotes, name), width: `${size}px`, height: `${size}px` }}></span>`;
}

/** A cloud's name with its dot. */
export function CloudName({ remotes, name }) {
  return html`<span class="cloud-name"><${CloudDot} remotes=${remotes} name=${name} /><span>${name}</span></span>`;
}

/**
 * The presence columns of one result: a cell per cloud in configuration order, a filled dot (with the count when
 * the cloud holds more than one copy) or an empty ring. `verified` false draws every cell with a dashed outline:
 * the content is not hash-identified, so copies elsewhere may exist under a different hash.
 */
export function PresenceCells({ remotes, counts, verified = true }) {
  return remotes.map((r) => {
    const n = counts[r.name] || 0;
    const label = n > 0 ? t('ex.presence.on', { cloud: r.name, n }) : t('ex.presence.off', { cloud: r.name });
    return html`<span key=${r.name} class=${'presence' + (n > 0 ? ' on' : '') + (verified ? '' : ' unproven')} role="cell"
      aria-label=${label} title=${verified ? label : `${label} — ${t('ex.unverified')}`} data-cloud=${r.name} data-count=${n}>
      <span class="presence-dot" style=${{ '--c': cloudColor(remotes, r.name) }}>${n > 1 ? html`<b>${n}</b>` : null}</span></span>`;
  });
}

/** Column heads for PresenceCells: the cloud's short name in its colour. */
export function PresenceHeads({ remotes }) {
  return remotes.map((r) => html`<span key=${r.name} class="presence-head" role="columnheader" title=${t('cloud.column', { cloud: r.name })} style=${{ '--c': cloudColor(remotes, r.name) }}>
    <${CloudDot} remotes=${remotes} name=${r.name} size=${7} /><span>${r.name.slice(0, 3)}</span></span>`);
}

/** "Synced 3 min ago" / "Never synced": the data age of a cloud, in a warning tone after 24 hours or when never synced. */
export function AgeChip({ iso, testid }) {
  const stale = isStaleAge(iso);
  const text = iso ? t('age.synced', { when: ago(iso) }) : t('age.never');
  return html`<span class=${'age-chip' + (stale ? ' stale' : '')} data-testid=${testid} data-stale=${stale ? 'true' : 'false'}
    title=${iso ? t('age.exact', { when: fmtDateTime(iso) }) : t('age.neverTip')}>
    <${Icon} name=${stale ? 'alert' : 'check'} size=${11} />${text}</span>`;
}
