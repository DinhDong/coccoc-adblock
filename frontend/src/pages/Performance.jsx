import { fmtDur, pct } from "../utils.js";
import { useMetrics } from "../analytics.js";
import { Avatar } from "../components/Avatar.jsx";

export default function Performance({ tickets }) {
  const m = useMetrics(tickets);
  const byReviewed = [...m.perUser].sort((a, b) => b.reviewed - a.reviewed);
  const domains = [...m.domains].sort((a, b) => b.reports - a.reports);
  return (
    <div className="ad-content">
      <div className="ad-pagehead">
        <div><h1>Performance</h1><p>Quality and speed rates for the AI pipeline and the moderation team.</p></div>
      </div>

      <div className="ad-stats" style={{ marginTop: 18 }}>
        <div className="ad-kpi k-green">
          <div className="ad-statnum">{pct(m.approved, m.approved + m.rejected)}</div>
          <div className="ad-statlabel">Moderator acceptance rate</div>
          <div className="ad-kpisub">{m.approved} approved · {m.rejected} rejected</div>
        </div>
        <div className="ad-kpi k-orange">
          <div className="ad-statnum">{pct(m.passed, m.passed + m.failed)}</div>
          <div className="ad-statlabel">Sandbox pass rate</div>
          <div className="ad-kpisub">{m.passed} passed · {m.failed} auto-rejected pre-test</div>
        </div>
        <div className="ad-kpi k-deep">
          <div className="ad-statnum">{fmtDur(m.avgRun)}</div>
          <div className="ad-statlabel">Avg pipeline run time</div>
          <div className="ad-kpisub">crawl → sandbox · {m.runsN} run{m.runsN === 1 ? "" : "s"}</div>
        </div>
        <div className="ad-kpi k-deep">
          <div className="ad-statnum">{fmtDur(m.avgReview)}</div>
          <div className="ad-statlabel">Avg review time</div>
          <div className="ad-kpisub">ready → decision · {m.reviewsN} review{m.reviewsN === 1 ? "" : "s"}</div>
        </div>
        <div className="ad-kpi k-green">
          <div className="ad-statnum">{m.live}</div>
          <div className="ad-statlabel">Rules deployed</div>
          <div className="ad-kpisub">across {m.doneN} completed report{m.doneN === 1 ? "" : "s"}</div>
        </div>
      </div>

      <div className="ad-panel" style={{ marginTop: 16 }}>
        <h3>By moderator</h3>
        <table className="ad-minitable">
          <thead>
            <tr><th>Moderator</th><th>Reviews</th><th>Approved</th><th>Rejected</th><th>Acceptance</th><th>Avg review time</th><th>Reports created</th></tr>
          </thead>
          <tbody>
            {byReviewed.map(({ u, reviewed, uApproved, uRejected, uAvgReview, created }) => (
              <tr key={u.k}>
                <td><span className="ad-person"><Avatar uk={u.k} size={20} />{u.name}</span></td>
                <td>{reviewed}</td>
                <td>{uApproved}</td>
                <td>{uRejected}</td>
                <td>{pct(uApproved, uApproved + uRejected)}</td>
                <td>{fmtDur(uAvgReview)}</td>
                <td>{created}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="ad-panel" style={{ marginTop: 16 }}>
        <h3>By website</h3>
        <table className="ad-minitable">
          <thead>
            <tr><th>Site</th><th>Reports</th><th>Avg run time</th><th>Rules deployed</th><th>Acceptance</th></tr>
          </thead>
          <tbody>
            {domains.map((d) => (
              <tr key={d.domain}>
                <td>{d.domain}</td>
                <td>{d.reports}</td>
                <td>{fmtDur(d.avgRun)}</td>
                <td>{d.live}</td>
                <td>{pct(d.approved, d.approved + d.rejected)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
