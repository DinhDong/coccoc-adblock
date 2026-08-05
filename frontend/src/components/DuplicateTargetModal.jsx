import { X, AlertTriangle } from "lucide-react";
import { fmtDate } from "../utils.js";

// Mirrors the three choices the CLI pipeline offers when a domain already has
// rules in the registry: discard them, keep them, or abort.
export default function DuplicateTargetModal({ url, duplicates, onChoose, onClose }) {
  return (
    <div className="ad-modal" onMouseDown={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-label="Duplicate link">
      <div className="ad-mhead">
        <div>
          <h2>This link has been run before</h2>
          <div className="ad-msub">{url}</div>
        </div>
        <button className="ad-close" onClick={onClose} aria-label="Close"><X size={18} /></button>
      </div>

      <div className="ad-mbody">
        <div className="ad-warnbox">
          <AlertTriangle className="ad-warnicon" />
          <div>
            <div className="ad-warntitle">
              {duplicates.length} existing report{duplicates.length === 1 ? "" : "s"} target{duplicates.length === 1 ? "s" : ""} this link
            </div>
            <div className="ad-warnbody">
              Rules already generated for this domain are in the registry. Unless you discard
              them, this run will skip anything it has proposed before — which can leave the
              new report with nothing to review.
            </div>
          </div>
        </div>

        <table className="ad-minitable" style={{ marginTop: 12 }}>
          <thead>
            <tr><th>Report</th><th>State</th><th>Rules</th><th>Created</th></tr>
          </thead>
          <tbody>
            {duplicates.map((d) => (
              <tr key={d.reportId}>
                <td>{d.reportId}</td>
                <td>{d.state}</td>
                <td>{d.ruleCount}</td>
                <td>{fmtDate(d.createdAt)}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <div className="ad-choices">
          <button className="ad-choice" onClick={() => onChoose("discard")}>
            <b>Discard old rules and run new ones</b>
            <span>
              Clears this domain from the rule registry first, so the model can propose the
              same rules again. Rules already saved on the older reports are left alone.
            </span>
          </button>
          <button className="ad-choice" onClick={() => onChoose("keep")}>
            <b>Run anyway</b>
            <span>
              Keeps the registry as it is. Anything already known for this domain is skipped as
              a duplicate, so only genuinely new rules appear.
            </span>
          </button>
          <button className="ad-choice danger" onClick={onClose}>
            <b>Abort — don’t run</b>
            <span>Leaves the report exactly as it is. Nothing is crawled and no tokens are spent.</span>
          </button>
        </div>
      </div>
    </div>
  );
}
