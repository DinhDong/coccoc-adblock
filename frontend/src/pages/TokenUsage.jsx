import { RefreshCw } from "lucide-react";
import { fmtDate, fmtDur } from "../utils.js";

const n = (v) => (typeof v === "number" ? v.toLocaleString() : "—");

export default function TokenUsage({ usage, loading, onRefresh }) {
  const runs = usage?.runs || [];
  const totals = usage?.totals || { runs: 0, promptTokens: 0, completionTokens: 0, totalTokens: 0 };
  const byModel = usage?.byModel || [];
  const avgPerRun = totals.runs ? Math.round(totals.totalTokens / totals.runs) : 0;

  return (
    <div className="ad-content">
      <div className="ad-pagehead">
        <div>
          <h1>Token usage</h1>
          <p>What the rule generator has spent on the OpenAI API, per report and in total.</p>
        </div>
        <button
          className="ad-refresh"
          onClick={onRefresh}
          disabled={loading}
          title="Reload usage from the database"
          aria-label="Refresh token usage"
        >
          <RefreshCw className={loading ? "ad-spin" : ""} />
        </button>
      </div>

      <div className="ad-stats" style={{ marginTop: 18 }}>
        <div className="ad-kpi k-deep">
          <div className="ad-statnum">{n(totals.totalTokens)}</div>
          <div className="ad-statlabel">Total tokens</div>
          <div className="ad-kpisub">across {totals.runs} billed run{totals.runs === 1 ? "" : "s"}</div>
        </div>
        <div className="ad-kpi k-orange">
          <div className="ad-statnum">{n(totals.promptTokens)}</div>
          <div className="ad-statlabel">Prompt tokens</div>
          <div className="ad-kpisub">
            {totals.totalTokens
              ? `${Math.round((totals.promptTokens / totals.totalTokens) * 100)}% of spend`
              : "—"}
          </div>
        </div>
        <div className="ad-kpi k-green">
          <div className="ad-statnum">{n(totals.completionTokens)}</div>
          <div className="ad-statlabel">Completion tokens</div>
          <div className="ad-kpisub">rules the model wrote back</div>
        </div>
        <div className="ad-kpi k-deep">
          <div className="ad-statnum">{n(avgPerRun)}</div>
          <div className="ad-statlabel">Average per run</div>
          <div className="ad-kpisub">prompt + completion</div>
        </div>
      </div>

      <div className="ad-panel" style={{ marginTop: 16 }}>
        <h3>By model</h3>
        <table className="ad-minitable">
          <thead>
            <tr><th>Model</th><th>Runs</th><th>Prompt</th><th>Completion</th><th>Total</th></tr>
          </thead>
          <tbody>
            {byModel.map((m) => (
              <tr key={m.model}>
                <td>{m.model}</td>
                <td>{n(m.runs)}</td>
                <td>{n(m.prompt)}</td>
                <td>{n(m.completion)}</td>
                <td>{n(m.total)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {byModel.length === 0 && <div className="ad-empty">No billed runs yet.</div>}
      </div>

      <div className="ad-panel" style={{ marginTop: 16 }}>
        <h3>By report</h3>
        <table className="ad-minitable">
          <thead>
            <tr>
              <th>Report</th><th>Site</th><th>Model</th>
              <th>Prompt</th><th>Completion</th><th>Total</th>
              <th>Run time</th><th>Created</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.reportId}>
                <td>{r.reportId}</td>
                <td>{r.domain}</td>
                <td>
                  {r.model || "—"}
                  {r.fallbackUsed && <span className="ad-srcchip">fallback</span>}
                </td>
                <td>{n(r.promptTokens)}</td>
                <td>{n(r.completionTokens)}</td>
                <td>{n(r.totalTokens)}</td>
                <td>{typeof r.totalMs === "number" ? fmtDur(r.totalMs) : "—"}</td>
                <td>{fmtDate(r.createdAt)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {runs.length === 0 && (
          <div className="ad-empty">
            No report has made a billed LLM call yet. Runs that failed before rule generation are
            not listed.
          </div>
        )}
      </div>
    </div>
  );
}
