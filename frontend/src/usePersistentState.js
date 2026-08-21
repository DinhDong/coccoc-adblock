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

/** Page sizes are stored as text; reject anything that is not a positive number. */
export const parsePageSize = (raw) => {
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? n : undefined;
};
