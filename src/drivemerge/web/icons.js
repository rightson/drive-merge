// Inline SVG icons (24x24, stroke). Inline so the app needs no font, CDN or image requests.
import { html } from './vendor/htm-preact-standalone.mjs';

const PATHS = {
  folder: ['M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z'],
  file: ['M6 3h8l4 4v14H6z', 'M14 3v4h4'],
  document: ['M6 3h8l4 4v14H6z', 'M14 3v4h4', 'M9 12h6M9 16h6'],
  pdf: ['M6 3h8l4 4v14H6z', 'M14 3v4h4', 'M9 13h2.5a1.5 1.5 0 0 0 0-3H9v6'],
  photo: ['M4 5h16v14H4z', 'M4 16l5-5 4 4 3-3 4 4', 'M9 9.5h.01'],
  video: ['M4 6h11v12H4z', 'M15 10l5-3v10l-5-3'],
  audio: ['M9 18V6l10-2v12', 'M9 18a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0z', 'M19 16a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0z'],
  spreadsheet: ['M5 4h14v16H5z', 'M5 10h14M5 15h14M11 4v16'],
  presentation: ['M4 5h16v11H4z', 'M12 16v4M8 20h8'],
  archive: ['M4 8h16v12H4z', 'M3 4h18v4H3z', 'M10 12h4'],
  code: ['M9 8l-4 4 4 4', 'M15 8l4 4-4 4'],
  design: ['M12 3l2.6 5.4 5.9.8-4.3 4.1 1 5.9L12 16.4 6.8 19.2l1-5.9L3.5 9.2l5.9-.8z'],
  lock: ['M6 11h12v9H6z', 'M8.5 11V8a3.5 3.5 0 0 1 7 0v3', 'M12 14.5v2'],
  cloud: ['M7 18h10a4 4 0 0 0 .6-7.95A6 6 0 0 0 6.2 9.6 4.2 4.2 0 0 0 7 18z'],
  chevron: ['M9 6l6 6-6 6'],
  undo: ['M9 14L4 9l5-5', 'M4 9h10a6 6 0 0 1 0 12h-3'],
  redo: ['M15 14l5-5-5-5', 'M20 9H10a6 6 0 0 0 0 12h3'],
  plus: ['M12 5v14M5 12h14'],
  search: ['M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z', 'M21 21l-5-5'],
  x: ['M6 6l12 12M18 6L6 18'],
  eye: ['M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z', 'M12 9.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z'],
  list: ['M4 6h16M4 12h16M4 18h10'],
  sparkle: ['M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z', 'M19 17v4M17 19h4'],
  check: ['M5 12l5 5 9-10'],
  alert: ['M12 4l9 16H3z', 'M12 10v4M12 17.5h.01'],
  info: ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z', 'M12 11v5M12 8h.01'],
  folderPlus: ['M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z', 'M12 10.5v5M9.5 13h5'],
  arrow: ['M5 12h14M13 6l6 6-6 6'],
  copy: ['M9 9h10v11H9z', 'M5 15V4h10'],
  jump: ['M10 14L21 3', 'M15 3h6v6', 'M19 14v6H4V5h6'],
  refresh: ['M20 12a8 8 0 1 1-2.3-5.7', 'M20 4v5h-5'],
  trash: ['M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13'],
  ghost: ['M5 20V10a7 7 0 0 1 14 0v10l-3.5-2-3.5 2-3.5-2z', 'M9.5 10h.01M14.5 10h.01'],
  merge: ['M6 4v6a6 6 0 0 0 6 6h0a6 6 0 0 0 6-6V4', 'M12 16v5'],
  chart: ['M4 20V10M10 20V4M16 20v-7M22 20H2'],
  swap: ['M7 7h13M16 3l4 4-4 4', 'M17 17H4M8 13l-4 4 4 4'],
  columns: ['M4 5h16v14H4z', 'M12 5v14'],
  more: ['M5 12h.01M12 12h.01M19 12h.01'],
  plans: ['M9 4h6v3H9z', 'M7 5.5H5V21h14V5.5h-2', 'M9 12h6M9 16h4'],
  rules: ['M4 6h16M4 12h16M4 18h16', 'M9 4v4M15 10v4M7 16v4'],
  claude: ['M12 3v6M12 15v6M3 12h6M15 12h6M5.6 5.6l4.2 4.2M14.2 14.2l4.2 4.2M18.4 5.6l-4.2 4.2M9.8 14.2l-4.2 4.2'],
};

export function Icon({ name, size = 16, class: cls = '' }) {
  const paths = PATHS[name] || PATHS.file;
  return html`<svg class=${'icon ' + cls} viewBox="0 0 24 24" width=${size} height=${size} fill="none"
    stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"
    focusable="false">${paths.map((d) => html`<path d=${d} />`)}</svg>`;
}

/** Icon name for a catalog category (the categories.py names) or a folder. */
export function iconFor(item) {
  if (item.is_dir) return item.path === '' ? 'cloud' : 'folder';
  const map = {
    photo: 'photo', video: 'video', audio: 'audio', document: 'document', pdf: 'pdf',
    spreadsheet: 'spreadsheet', presentation: 'presentation', archive: 'archive', code: 'code', design: 'design',
  };
  return map[item.category] || 'file';
}
