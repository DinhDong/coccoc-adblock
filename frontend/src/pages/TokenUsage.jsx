import { RefreshCw, AlertTriangle } from "lucide-react";
import { fmtDate, fmtDur } from "../utils.js";

const n = (v) => (typeof v === "number" ? v.toLocaleString() : "—");

function BudgetBar({ used, budget }) {
  // No provider quota is wired into this project, so the budget is whatever
  // ceiling the team opts into via TOKEN_BUDGET.
  if (!budget) {
    return (
      <div className="ad-panel" style={{ marginTop: 16 }}>
        <h3>Budget</h3>
        <p className="ad-notes">
          No token limit is configured. The pipeline caps each response at 1,024 completion
          tokens, but there is no cap on total spend and nothing here is enforced against the
          provider account.
        </p>
        <p className="ad-notes" style={{ marginTop: 8 }}>
          To track against a ceiling, set <code className="ad-code">TOKEN_BUDGET</code> in
          <code className="ad-code">.env.local</code> and restart the backend.
        </p>
      </div>
    );
  }

  const pctUsed = Math.min(100, Math.round((used / budget) * 100));
  const over = used > budget;
  const near = !over && pctUsed >= 80;

  return (
    <div className="ad-panel" style={{ marginTop: 16 }}>
      <h3>Budget</h3>
      {(over || near) && (
        <div className={"ad-warnbox" + (over ? " ad-errbox" : "")} style={{ marginBottom: 12 }}>
          <AlertTriangle className="ad-warnicon" />
          <div>
            <div className="ad-warntitle">
              {over ? "Over the configured budget" : "Approaching the configured budget"}
            </div>
            <div className="ad-warnbody">
              {n(used)} of {n(budget)} tokens used ({pctUsed}%).
              {over && ` That is ${n(used - budget)} over.`}
            </div>
          </div>
        </div>
      )}
      <div className="ad-progress">
        <div
          className={"ad-progressfill" + (over ? " over" : near ? " near" : "")}
          style={{ width: `${pctUsed}%` }}
        />
      </div>
      <div className="ad-progresslabel">
        {n(used)} / {n(budget)} tokens · {n(Math.max(0, budget - used))} remaining
      </div>
    </div>
  );
}

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

      <BudgetBar used={totals.totalTokens} budget={usage?.budget} />

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
