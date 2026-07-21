import { ENVS } from "../constants.js";
import { hostname, fmtDate, passedRules, approvedRules } from "../utils.js";
import { Person } from "./Avatar.jsx";
import StatusBadge from "./StatusBadge.jsx";

function rulesSummary(t) {
  if (t.state === "draft") return <span className="ad-mute">—</span>;
  if (t.state === "inprocess") return <span className="ad-mute">Generating…</span>;
  if (t.state === "review") {
    const p = passedRules(t).length;
    return <span>{p}/{(t.rules || []).length} passed pre-test</span>;
  }
  const a = approvedRules(t).length;
  return a > 0 ? <span>{a} rule{a === 1 ? "" : "s"} live</span> : <span className="ad-mute">Closed — none deployed</span>;
}

export default function ReportTable({ items, emptyText, onOpen }) {
  return (
    <div className="ad-tablewrap">
      <table className="ad-table">
        <thead>
          <tr>
            <th>Report</th>
            <th>Env</th>
            <th>Created by</th>
            <th>Created</th>
            <th>Rules</th>
            <th>Status</th>
            <th>Reviewed by</th>
          </tr>
        </thead>
        <tbody>
          {items.length === 0 && (
            <tr>
              <td colSpan={7}>
                <div className="ad-empty">{emptyText}</div>
              </td>
            </tr>
          )}
          {items.map((t) => (
            <tr
              key={t.id}
              className={"ad-row" + (t.state === "inprocess" ? " ghost" : "")}
              tabIndex={0}
              onClick={() => onOpen(t.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onOpen(t.id);
                }
              }}
              aria-label={`Open report ${t.name}`}
            >
              <td>
                <div className="ad-rowname">{t.name}</div>
                <div className="ad-rowurl">{hostname(t.url)}</div>
              </td>
              <td><span className="ad-envtag">{ENVS.find((e) => e.k === t.env)?.label || t.env}</span></td>
              <td><Person uk={t.createdBy} /></td>
              <td className="ad-mute">{fmtDate(t.created)}</td>
              <td>{rulesSummary(t)}</td>
              <td><StatusBadge t={t} /></td>
              <td>{t.reviewedBy ? <Person uk={t.reviewedBy} /> : <span className="ad-mute">—</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
