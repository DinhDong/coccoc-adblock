import { useMemo } from "react";
import { RefreshCw } from "lucide-react";
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, Legend, CartesianGrid,
} from "recharts";
import { fmtDur, hostname, pct } from "../utils.js";

const CHART = {
  crawl: "#1D3829",
  generate: "#88C646",
  validate: "#FF7439",
  tick: { fontSize: 11, fill: "#5C6B63" },
};

const n = (v) => (typeof v === "number" ? v.toLocaleString() : "—");
const avg = (xs) => (xs.length ? Math.round(xs.reduce((a, b) => a + b, 0) / xs.length) : 0);

export default function Performance({ tickets, onRefresh, refreshing }) {
  // Everything here comes from values the backend persists. An earlier
  // version read per-review timestamps that only ever existed in React
  // state and vanished on reload, so the panels rendered zeros against
  // real data.
  const m = useMemo(() => {
    const ran = tickets.filter((t) => t.metrics && t.metrics.totalMs);
    const allRules = tickets.flatMap((t) => t.rules || []);

    const passed = allRules.filter((r) => r.status === "passed").length;
    const failed = allRules.filter((r) => r.status === "failed").length;
    const untested = allRules.filter((r) => r.status === "pending").length;

    const approved = allRules.filter((r) => r.decision === "approve").length;
    const rejectedByHand = allRules.filter(
      (r) => r.decision === "reject" && !r.autoRejected
    ).length;
    const autoRejected = allRules.filter((r) => r.autoRejected).length;
    const undecided = allRules.filter((r) => !r.decision).length;
    const deployed = tickets
      .filter((t) => t.state === "done")
      .flatMap((t) => t.rules || [])
      .filter((r) => r.decision === "approve").length;

    const dupes = tickets.reduce((sum, t) => sum + (t.duplicates?.total || 0), 0);
    const graded = passed + failed + untested;

    const sites = {};
    for (const t of tickets) {
      const key = hostname(t.url || "");
      if (!key) continue;
      const s = (sites[key] ||= {
        site: key, reports: 0, runs: [], rules: 0, passed: 0, deployed: 0, tokens: 0,
      });
      s.reports += 1;
      if (t.metrics?.totalMs) s.runs.push(t.metrics.totalMs);
      s.tokens += t.metrics?.totalTokens || 0;
      for (const r of t.rules || []) {
        s.rules += 1;
        if (r.status === "passed") s.passed += 1;
        if (r.decision === "approve" && t.state === "done") s.deployed += 1;
      }
    }

    return {
      runsN: ran.length,
      passed, failed, untested, graded,
      approved, rejectedByHand, autoRejected, undecided, deployed, dupes,
      avgCrawl: avg(ran.map((t) => t.metrics.crawlMs).filter(Boolean)),
      avgGenerate: avg(ran.map((t) => t.metrics.generationMs).filter(Boolean)),
      avgValidate: avg(ran.map((t) => t.metrics.validationMs).filter(Boolean)),
      avgTotal: avg(ran.map((t) => t.metrics.totalMs).filter(Boolean)),
      avgTokens: avg(ran.map((t) => t.metrics.totalTokens).filter(Boolean)),
      totalTokens: ran.reduce((sum, t) => sum + (t.metrics.totalTokens || 0), 0),
      stageRows: ran
        .slice()
        .sort((a, b) => (b.metrics.totalMs || 0) - (a.metrics.totalMs || 0))
        .slice(0, 12)
        .map((t) => ({
          name: t.id,
          Crawl: +((t.metrics.crawlMs || 0) / 1000).toFixed(1),
          Generate: +((t.metrics.generationMs || 0) / 1000).toFixed(1),
          Validate: +((t.metrics.validationMs || 0) / 1000).toFixed(1),
        })),
      sites: Object.values(sites).sort((a, b) => b.rules - a.rules),
    };
  }, [tickets]);

  return (
    <div className="ad-content">
      <div className="ad-pagehead">
        <div>
          <h1>Performance</h1>
          <p>How the pipeline spends its time, and what moderators do with what it produces.</p>
        </div>
        {onRefresh && (
          <button className="ad-refresh" onClick={onRefresh} disabled={refreshing} aria-label="Refresh">
            <RefreshCw className={refreshing ? "ad-spin" : ""} />
          </button>
        )}
      </div>

      <div className="ad-stats" style={{ marginTop: 18 }}>
        <div className="ad-kpi k-deep">
          <div className="ad-statnum">{m.runsN}</div>
          <div className="ad-statlabel">Completed runs</div>
          <div className="ad-kpisub">reports with a recorded pipeline time</div>
        </div>
        <div className="ad-kpi k-orange">
          <div className="ad-statnum">{pct(m.passed, m.passed + m.failed)}</div>
          <div className="ad-statlabel">Sandbox pass rate</div>
          <div className="ad-kpisub">{m.passed} passed · {m.failed} auto-rejected</div>
        </div>
        <div className="ad-kpi k-green">
          <div className="ad-statnum">{m.deployed}</div>
          <div className="ad-statlabel">Rules deployed</div>
          <div className="ad-kpisub">approved on a closed report</div>
        </div>
        <div className="ad-kpi k-deep">
          <div className="ad-statnum">{fmtDur(m.avgTotal)}</div>
          <div className="ad-statlabel">Average run</div>
          <div className="ad-kpisub">crawl + generate + validate</div>
        </div>
        <div className="ad-kpi k-deep">
          <div className="ad-statnum">{n(m.avgTokens)}</div>
          <div className="ad-statlabel">Tokens per run</div>
          <div className="ad-kpisub">{n(m.totalTokens)} total</div>
        </div>
      </div>

      <div className="ad-panel" style={{ marginTop: 16 }}>
        <h3>Where the time goes</h3>
        <p className="ad-notes">
          Averaged over {m.runsN} run{m.runsN === 1 ? "" : "s"}: crawl {fmtDur(m.avgCrawl)},
          generation {fmtDur(m.avgGenerate)}, sandbox {fmtDur(m.avgValidate)}. Sandbox
          validation dominates because it reloads the page once per rule.
        </p>
        {m.stageRows.length === 0 ? (
          <div className="ad-empty">No completed runs yet.</div>
        ) : (
          <div style={{ height: 300, marginTop: 10 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={m.stageRows} margin={{ top: 5, right: 8, bottom: 5, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#ECF0EC" vertical={false} />
                <XAxis
                  dataKey="name" tick={CHART.tick} tickLine={false}
                  interval={0} angle={-25} textAnchor="end" height={70}
                />
                <YAxis tick={CHART.tick} tickLine={false} unit="s" />
                <Tooltip formatter={(v) => `${v}s`} />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Bar dataKey="Crawl" stackId="t" fill={CHART.crawl} />
                <Bar dataKey="Generate" stackId="t" fill={CHART.generate} />
                <Bar dataKey="Validate" stackId="t" fill={CHART.validate} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>

      <div className="ad-panel" style={{ marginTop: 16 }}>
        <h3>What happens to a generated rule</h3>
        <table className="ad-minitable">
          <thead>
            <tr><th>Outcome</th><th>Rules</th><th>Share</th></tr>
          </thead>
          <tbody>
            <tr><td>Passed the sandbox</td><td>{m.passed}</td><td>{pct(m.passed, m.graded)}</td></tr>
            <tr><td>Auto-rejected by the sandbox</td><td>{m.autoRejected}</td><td>{pct(m.autoRejected, m.graded)}</td></tr>
            <tr><td>Hand-written or not yet tested</td><td>{m.untested}</td><td>{pct(m.untested, m.graded)}</td></tr>
            <tr><td>Approved by a moderator</td><td>{m.approved}</td><td>{pct(m.approved, m.approved + m.rejectedByHand)}</td></tr>
            <tr><td>Rejected by a moderator</td><td>{m.rejectedByHand}</td><td>{pct(m.rejectedByHand, m.approved + m.rejectedByHand)}</td></tr>
            <tr><td>Still undecided</td><td>{m.undecided}</td><td>—</td></tr>
            <tr><td>Skipped as duplicates before validation</td><td>{m.dupes}</td><td>—</td></tr>
          </tbody>
        </table>
      </div>

      <div className="ad-panel" style={{ marginTop: 16 }}>
        <h3>By website</h3>
        <table className="ad-minitable">
          <thead>
            <tr>
              <th>Site</th><th>Reports</th><th>Avg run</th>
              <th>Rules</th><th>Passed</th><th>Deployed</th><th>Tokens</th>
            </tr>
          </thead>
          <tbody>
            {m.sites.map((s) => (
              <tr key={s.site}>
                <td>{s.site}</td>
                <td>{s.reports}</td>
                <td>{s.runs.length ? fmtDur(avg(s.runs)) : "—"}</td>
                <td>{s.rules}</td>
                <td>{s.passed}</td>
                <td>{s.deployed}</td>
                <td>{n(s.tokens)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {m.sites.length === 0 && <div className="ad-empty">No reports yet.</div>}
      </div>
    </div>
  );
}
