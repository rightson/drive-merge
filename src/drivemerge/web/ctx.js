// Shared context object, in its own module so app.js and the panes do not import each other.
import { createContext } from './vendor/htm-preact-standalone.mjs';

export const AppCtx = createContext(null);

/** One-shot requests between the shell and a view that is not mounted yet ("/" asks for the Explore box). */
export const pending = { exploreFocus: false };
