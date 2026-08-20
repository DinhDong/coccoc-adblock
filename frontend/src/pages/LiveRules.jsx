import { useMemo, useState } from "react";
import { RefreshCw, ChevronRight, Copy, Check, Search } from "lucide-react";
import { fmtDate } from "../utils.js";

// A rule counts as live once a moderator approved it AND the report it came
// from was closed. Approved rules on a still-open report are shown separately
// rather than hidden, so nothing a moderator approved silently disappears.
export default function LiveRules({ rules, loading, onRefresh, onOpenReport }) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(() => new Set());
  const [copied, setCopied] = useState("");

  const { sites, liveCount, pendingCount } = useMemo(() => {
    const live = rules.filter((r) => r.deployed);
    const pending = rules.filter((r) => r.decision === "approve" && !r.deployed);

    const q = query.trim().toLowerCase();
    const match = (r) =>
      !q || (r.text || "").toLowerCase().includes(q) || (r.domain || "").toLowerCase().includes(q);

    const grouped = {};
    for (const r of live) {
      const key = r.domain || "unknown";
      (grouped[key] ||= { site: key, live: [], pending: 0 }).live.push(r);
    }
    for (const r of pending) {
      const key = r.domain || "unknown";
      (grouped[key] ||= { site: key, live: [], pending: 0 }).pending += 1;
    }

    const list = Object.values(grouped)
      .map((s) => ({ ...s, shown: s.live.filter(match) }))
      .filter((s) => (q ? s.shown.length > 0 || s.site.toLowerCase().includes(q) : true))
      .sort((a, b) => b.live.length - a.live.length || a.site.localeCompare(b.site));

    return { sites: list, liveCount: live.length, pendingCount: pending.length };
  }, [rules, query]);

  const toggle = (site) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(site)) next.delete(site);
      else next.add(site);
      return next;
    });

  const copy = async (label, text) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(label);
      setTimeout(() => setCopied(""), 1800);
    } catch {
      window.alert("Could not copy — your browser blocked clipboard access.");
    }
  };

  const allLiveText = sites.flatMap((s) => s.live.map((r) => r.text)).join("\n");

  return (
    <div className="ad-content">
      <div className="ad-pagehead">
        <div>
          <h1>Live rules</h1>
          <p>Rules approved by a moderator on a closed report — what is actually blocking today.</p>
        </div>
        <button className="ad-refresh" onClick={onRefresh} disabled={loading} aria-label="Refresh live rules">
          <RefreshCw className={loading ? "ad-spin" : ""} />
        </button>
      </div>

      <div className="ad-stats" style={{ marginTop: 18 }}>
        <div className="ad-kpi k-green">
          <div className="ad-statnum">{liveCount}</div>
          <div className="ad-statlabel">Live rules</div>
          <div className="ad-kpisub">approved and deployed</div>
        </div>
        <div className="ad-kpi k-deep">
          <div className="ad-statnum">{sites.filter((s) => s.live.length > 0).length}</div>
          <div className="ad-statlabel">Sites covered</div>
          <div className="ad-kpisub">with at least one live rule</div>
        </div>
        <div className="ad-kpi k-orange">
          <div className="ad-statnum">{pendingCount}</div>
          <div className="ad-statlabel">Approved, not live yet</div>
          <div className="ad-kpisub">waiting on their report to be finished</div>
        </div>
      </div>

      <div className="ad-card">
        <div className="ad-toolbar">
          <div className="ad-tabs">
            <span className="ad-pageinfo">
              {sites.length} site{sites.length === 1 ? "" : "s"}
            </span>
          </div>
          <div className="ad-tools">
            <span className="ad-search">
              <Search aria-hidden="true" />
              <input
                placeholder="Search rule or site…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                aria-label="Search live rules"
              />
            </span>
            <button
              className="ad-btn ad-btn-ghost"
              onClick={() => copy("all", allLiveText)}
              disabled={liveCount === 0}
              title="Copy every live rule as a filter list"
            >
              {copied === "all" ? <Check /> : <Copy />} Copy all
            </button>
          </div>
        </div>

        {sites.length === 0 ? (
          <div className="ad-empty">
            {rules.length === 0
              ? "No rules in the database yet."
              : "No rules are live. Approve rules and finish their report to deploy them."}
          </div>
        ) : (
          <div className="ad-sites">
            {sites.map((s) => {
              const isOpen = open.has(s.site);
              return (
                <div className={"ad-site" + (isOpen ? " open" : "")} key={s.site}>
                  <button
                    className="ad-sitehead"
                    onClick={() => toggle(s.site)}
                    aria-expanded={isOpen}
                  >
                    <ChevronRight className="ad-sitechev" />
                    <span className="ad-sitename">{s.site}</span>
                    <span className="ad-sitecount">
                      {s.live.length} live
                      {s.pending > 0 && (
                        <span className="ad-sitepending" title="Approved but their report is not finished">
                          +{s.pending} pending
                        </span>
                      )}
                    </span>
                  </button>

                  {isOpen && (
                    <div className="ad-sitebody">
                      {s.live.length === 0 ? (
                        <div className="ad-panelabel">
                          Nothing live here yet — {s.pending} approved rule
                          {s.pending === 1 ? "" : "s"} waiting on their report to close.
                        </div>
                      ) : (
                        <>
                          <table className="ad-minitable">
                            <thead>
                              <tr><th>Rule</th><th>Type</th><th>From</th><th>Since</th></tr>
                            </thead>
                            <tbody>
                              {(query ? s.shown : s.live).map((r, i) => (
                                <tr key={`${r.reportId}-${r.text}-${i}`}>
                                  <td><span className="ad-ruletext">{r.text}</span></td>
                                  <td><span className="ad-chip">{r.rule_type}</span></td>
                                  <td>
                                    <button className="ad-linkbtn" onClick={() => onOpenReport(r.reportId)}>
                                      {r.reportId}
                                    </button>
                                  </td>
                                  <td>{fmtDate(r.updatedAt)}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                          <button
                            className="ad-btn ad-btn-ghost"
                            style={{ marginTop: 10 }}
                            onClick={() => copy(s.site, s.live.map((r) => r.text).join("\n"))}
                          >
                            {copied === s.site ? <Check /> : <Copy />} Copy {s.site} rules
                          </button>
                        </>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
