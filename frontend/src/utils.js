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
