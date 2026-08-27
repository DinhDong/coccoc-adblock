import { useState } from "react";

/**
 * useState that remembers its value in localStorage.
 *
 * Pages unmount when you switch views, so anything held in plain component
 * state is lost — the rows-per-page choice reset to its default every time you
 * navigated away and came back.
 *
 * Storage failures are ignored on purpose: private browsing and disabled
 * storage should degrade to ordinary state, not crash the page.
 */
export function usePersistentState(key, fallback, parse = (v) => v) {
  const [value, setValue] = useState(() => {
    try {
      const stored = window.localStorage.getItem(key);
      if (stored === null) return fallback;
      const parsed = parse(stored);
      return parsed === undefined || parsed === null ? fallback : parsed;
    } catch {
      return fallback;
    }
  });

  const set = (next) => {
    setValue(next);
    try {
      window.localStorage.setItem(key, String(next));
    } catch {
      /* storage unavailable — keep it in memory only */
    }
  };

  return [value, set];
}

/**
 * The same idea for values that are not plain text.
 *
 * usePersistentState writes with String(next), which turns an object into
 * "[object Object]". This one round-trips through JSON instead, so a whole
 * result payload survives a view switch. Kept separate rather than folded into
 * the function above, because JSON.stringify would wrap existing string
 * callers in quotes and change what they read back.
 *
 * Passing null clears the key rather than storing "null".
 */
export function usePersistentJson(key, fallback) {
  const [value, setValue] = useState(() => {
    try {
      const stored = window.localStorage.getItem(key);
      return stored === null ? fallback : JSON.parse(stored);
    } catch {
      // Unparseable leftovers from an older shape must not break the page.
      return fallback;
    }
  });

  const set = (next) => {
    setValue(next);
    try {
      if (next === null || next === undefined) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, JSON.stringify(next));
    } catch {
      /* storage unavailable — keep it in memory only */
    }
  };

  return [value, set];
}

/** Page sizes are stored as text; reject anything that is not a positive number. */
export const parsePageSize = (raw) => {
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? n : undefined;
};
