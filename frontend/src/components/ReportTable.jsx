import { ENVS } from "../constants.js";
import { hostname, fmtDate, passedRules, approvedRules } from "../utils.js";
import StatusBadge from "./StatusBadge.jsx";

function rulesSummary(t) {
  if (t.state === "draft") return <span className="ad-mute">—</span>;
  if (t.state === "queued") return <span className="ad-mute">Waiting for the worker</span>;
  if (t.state === "inprocess") return <span className="ad-mute">Generating…</span>;
  if (t.state === "failed") return <span className="ad-failtext">Run failed — no rules produced</span>;
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
      <table className="ad-table ad-table-fixed">
        {/* Percentages rather than pixels so the table still fills its
            container, while each column's share stays constant whatever the
            cells contain. Status is the widest because it has to hold
            "Running · Sandbox validation" without the row reflowing. */}
        <colgroup>
          <col style={{ width: "24%" }} />
          <col style={{ width: "10%" }} />
          <col style={{ width: "13%" }} />
          <col style={{ width: "25%" }} />
          <col style={{ width: "28%" }} />
        </colgroup>
        <thead>
          <tr>
            <th>Report</th>
            <th>Env</th>
            <th>Created</th>
            <th>Rules</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {items.length === 0 && (
            <tr>
              <td colSpan={5}>
                <div className="ad-empty">{emptyText}</div>
              </td>
            </tr>
          )}
          {items.map((t) => (
            <tr
              key={t.id}
              className={"ad-row" + (t.state === "inprocess" || t.state === "queued" ? " ghost" : "")}
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
              <td className="ad-mute">{fmtDate(t.created)}</td>
              <td>{rulesSummary(t)}</td>
              <td><StatusBadge t={t} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
