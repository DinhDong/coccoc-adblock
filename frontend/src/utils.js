/* time helpers — seed dates are relative to "now" so demo metrics stay plausible */
export const nowISO = () => new Date().toISOString();
export const dAgo = (days, h = 9, m = 0) => { const d = new Date(); d.setDate(d.getDate() - days); d.setHours(h, m, 0, 0); return d; };
export const dateStr = (d) => d.toISOString().slice(0, 10);
export const iso = (d) => d.toISOString();
export const minLater = (d, mins) => new Date(d.getTime() + mins * 60000);

export const fmtDur = (ms) => {
  if (ms == null || !isFinite(ms) || ms < 0) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return m < 10 && s % 60 ? `${m}m ${s % 60}s` : `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d}d ${h % 24}h` : `${d}d`;
};
export const pct = (num, den) => (den > 0 ? Math.round((num / den) * 100) + "%" : "—");

export const agoText = (d) => {
  const s = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
};

// A scheme followed by "//" — the shape a web address actually has. Matching
// only this (rather than any "word:") keeps "example.com:8080/path" readable
// as a bare host with a port, which the looser pattern would mistake for a
// scheme and refuse to complete.
const HAS_AUTHORITY_SCHEME = /^[a-z][a-z0-9+.-]*:\/\//i;

// Schemes that carry no host at all. These must be left exactly as typed so
// isWebUrl below rejects them; completing "javascript:…" to
// "https://javascript:…" would dress up something dangerous as an ordinary
// link and hand it to the crawler.
const OPAQUE_SCHEME = /^(?:javascript|data|vbscript|file|blob|mailto):/i;

/**
 * Fill in the scheme a moderator did not type.
 *
 * "vnexpress.net" and "vnexpress.net/the-thao" are how people actually write
 * a site down, and rejecting them for a missing "https://" was busywork.
 * Anything already carrying a scheme is returned untouched, so an explicit
 * http:// is never silently upgraded.
 */
export const normalizeUrl = (raw) => {
  const text = (raw || "").trim();
  if (!text) return "";
  if (text.startsWith("//")) return "https:" + text;          // protocol-relative
  if (HAS_AUTHORITY_SCHEME.test(text) || OPAQUE_SCHEME.test(text)) return text;
  return "https://" + text;
};

/**
 * Whether a typed value is a usable web address once the scheme is filled in.
 *
 * Parsed rather than pattern-matched, so ports, paths, query strings and
 * unicode hosts all behave. The host must contain a dot: a bare word is not
 * something the crawler can resolve, and accepting it would turn a typo into
 * a failed run several minutes later.
 */
export const isWebUrl = (raw) => {
  try {
    const u = new URL(normalizeUrl(raw));
    if (u.protocol !== "http:" && u.protocol !== "https:") return false;
    return u.hostname.includes(".") && !u.hostname.endsWith(".");
  } catch {
    return false;
  }
};

export const hostname = (url) => {
  try { return new URL(url).hostname.replace(/^www\./, ""); }
  catch { return url.replace(/^https?:\/\//, "").split("/")[0]; }
};

export const fmtDate = (iso) => {
  if (!iso) return "—";
  // Handle both date-only strings (YYYY-MM-DD) and full ISO datetimes.
  const d = iso.includes("T") ? new Date(iso) : new Date(iso + "T00:00:00");
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
};

// Clock time for a timestamp, empty when the value carries no time at all.
// Reports created in one batch land in the same second, so seconds are shown
// rather than just hours and minutes — without them a run of reports reads as
// having been created at the identical moment. Rows written before the API
// returned a real timestamp hold a date-only string; those get "" instead of
// a fabricated midnight.
export const fmtTime = (iso) => {
  if (!iso || !iso.includes("T")) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? ""
    : d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
};

export const todayISO = () => new Date().toISOString().slice(0, 10);

export function makeRules(t) {
  const host = hostname(t.url);
  const root = host.split(".").slice(-2).join(".");
  const rules = [
    { text: `${host}##div[id^="zone-ad"]`, status: "passed", conf: 0.88 },
    { text: `${host}##.banner-wrap`, status: "passed", conf: 0.81 },
    { text: `||ads.${root}^$third-party`, status: "passed", conf: 0.92 },
  ];
  if ((t.targets || []).some((x) => /popup|overlay/i.test(x)) || /popup/i.test(t.notes || "")) {
    rules.push({ text: `${host}##.popup-backdrop`, status: "passed", conf: 0.86 });
  } else {
    rules.push({ text: `${host}##.sticky-footer-ad`, status: "passed", conf: 0.79 });
  }
  rules.push({
    text: `${host}##[class*="ads"]`, status: "failed", conf: 0.38,
    reason: "Too broad — matched 3 non-ad elements in the sandbox.",
  });
  return rules;
}

export const passedRules = (t) => (t.rules || []).filter((r) => r.status === "passed");
export const approvedRules = (t) => passedRules(t).filter((r) => r.decision === "approve");
