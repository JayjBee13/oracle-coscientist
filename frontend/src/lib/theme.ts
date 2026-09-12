/**
 * Which of the two palettes the instrument is painted in, and who decides.
 *
 * Three states, not two, and the third is the important one. `system` is not
 * "dark" with extra steps — it is the absence of a preference, and it keeps
 * tracking the OS as the OS changes, which is what someone whose machine goes
 * light at sunrise actually wants. A two-state toggle has to guess an initial
 * value and then silently stops following; this one can be put back.
 *
 * The mechanism is one attribute. `styles/tokens.css` is dark-first and defines
 * the light palette under `prefers-color-scheme`, with both directions
 * overridable via `:root[data-theme="light" | "dark"]` — so *forcing* a theme
 * means stamping that attribute, and *following the system* means removing it.
 * Nothing here knows a single colour.
 *
 * The stored value is read on every snapshot rather than cached in a module
 * variable. It is one `getItem` of a short string, it cannot go stale against
 * another tab or a test that clears storage, and `useSyncExternalStore` is happy
 * because the snapshot is a primitive that compares equal to itself.
 */

import { useEffect, useSyncExternalStore } from "react";

export type Theme = "system" | "light" | "dark";

/** Same prefix and version scheme as the other stored preferences. */
export const THEME_KEY = "coscientist.theme.v1";

const listeners = new Set<() => void>();

/* --- localStorage, without the exceptions ---------------------------------
   Private windows throw on access, not just on write, and a preference is never
   worth an error boundary. Same shape as `lib/notifications`.
   ------------------------------------------------------------------------- */

function readStore(): string | null {
  try {
    return window.localStorage.getItem(THEME_KEY);
  } catch {
    return null;
  }
}

function writeStore(value: Theme): void {
  try {
    window.localStorage.setItem(THEME_KEY, value);
  } catch {
    /* storage unavailable — the choice holds for this page, not the next one */
  }
}

/**
 * The stored choice, or `system`.
 *
 * Anything that is not one of the three words is `system`: a half-written entry,
 * a value from a future version of this key, a string some other tool put there.
 * Falling back to "follow the OS" is the only failure mode that cannot look like
 * a deliberate choice nobody made.
 */
export function getTheme(): Theme {
  if (typeof window === "undefined") return "system";
  const raw = readStore();
  return raw === "light" || raw === "dark" || raw === "system" ? raw : "system";
}

/** Stamps the choice on the document. `system` removes the attribute entirely. */
export function applyTheme(theme: Theme): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export function setTheme(theme: Theme): void {
  writeStore(theme);
  applyTheme(theme);
  for (const listener of listeners) listener();
}

/** System → light → dark → system. */
export function nextTheme(theme: Theme): Theme {
  if (theme === "system") return "light";
  if (theme === "light") return "dark";
  return "system";
}

/**
 * Changes to the choice, including the ones made in another tab.
 *
 * The `storage` event only fires in the tabs that did *not* write, so it is
 * exactly the case the local notification misses — two windows of the app open
 * side by side should not disagree about which palette they are in.
 */
function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  const onStorage = (event: StorageEvent): void => {
    if (event.key !== null && event.key !== THEME_KEY) return;
    applyTheme(getTheme());
    listener();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

/**
 * The current choice, and a setter.
 *
 * The effect is not how the theme is normally applied — the inline script in
 * `index.html` has already stamped the attribute before this module is parsed,
 * which is what stops the first paint being the wrong colour. It is here so the
 * module is true on its own: a document that never ran that script (a test, a
 * different host page) still ends up matching the stored choice, and re-stamping
 * an attribute that already holds the right value costs nothing.
 */
export function useTheme(): { theme: Theme; setTheme: (theme: Theme) => void } {
  const theme = useSyncExternalStore(subscribe, getTheme, getTheme);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  return { theme, setTheme };
}
