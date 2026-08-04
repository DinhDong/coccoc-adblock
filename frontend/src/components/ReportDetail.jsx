import { X, ExternalLink, CheckCircle2, XCircle, AlertTriangle } from "lucide-react";
import { STAGES, ENVS, CURRENT_USER, userOf } from "../constants.js";
import { fmtDate, passedRules, approvedRules } from "../utils.js";
import { Person } from "./Avatar.jsx";
import StatusBadge from "./StatusBadge.jsx";

function StepList({ current }) {
  const idx = STAGES.findIndex((s) => s.k === current);
  return (
    <div>
      {STAGES.map((s, i) => {
        const done = idx > i || idx === -1;
        const cur = idx === i;
        return (
          <div className={"ad-step" + (done ? " donestep" : cur ? " current" : " pending")} key={s.k}>
            <span className="ad-stepdot" />
            {s.label}{cur ? "…" : done ? " — completed" : ""}
          </div>
        );
      })}
    </div>
  );
}

function DuplicateWarning({ dupes, ruleCount }) {
  const total = dupes?.total || 0;
  if (!total) return null;

  const parts = [];
  if (dupes.internal) parts.push(`${dupes.internal} already in the rule registry for this domain`);
  if (dupes.external) parts.push(`${dupes.external} already covered by a public filter list`);

  return (
    <div className="ad-warnbox">
      <AlertTriangle className="ad-warnicon" />
      <div>
        <div className="ad-warntitle">
          {total} duplicate rule{total === 1 ? "" : "s"} skipped
          {ruleCount === 0 && " — nothing left to review"}
        </div>
        <div className="ad-warnbody">
          The model proposed {total + ruleCount} rule{total + ruleCount === 1 ? "" : "s"}, but{" "}
          {parts.join(" and ")}. Duplicates are dropped before validation.
          {ruleCount === 0 && " Clear this domain from the rule registry to re-propose them."}
        </div>
        {dupes.rules?.length > 0 && (
          <details className="ad-warndetails">
            <summary>Show skipped rules</summary>
            <ul>
              {dupes.rules.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </details>
        )}
      </div>
    </div>
  );
}

function MiniSandbox() {
  const Lines = () => (
    <>
      <div className="line" style={{ width: "88%" }} />
      <div className="line" style={{ width: "72%" }} />
      <div className="line" style={{ width: "80%" }} />
    </>
  );
  return (
    <div className="ad-sandbox">
      <div className="ad-pane">
        <div className="ad-mini">
          <div className="bar" />
          <div className="adblk">AD</div>
          <Lines />
          <div className="adblk">AD</div>
        </div>
        <div className="ad-panelabel">Before</div>
      </div>
      <div className="ad-pane">
        <div className="ad-mini">
          <div className="bar" />
          <div className="goneblk" />
          <Lines />
          <div className="goneblk" />
        </div>
        <div className="ad-panelabel">After rules (sandbox)</div>
      </div>
    </div>
  );
}

export default function ReportDetail({ t, onClose, onRun, onCancelRun, onDelete, onDecide, onFinish }) {
  const passed = passedRules(t);
  const undecided = passed.filter((r) => !r.decision).length;
  const approved = approvedRules(t).length;
  const rejected = passed.filter((r) => r.decision === "reject").length;

  return (
    <div className="ad-modal" onMouseDown={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-label={`Report ${t.name}`}>
      <div className="ad-mhead">
        <div>
          <h2>{t.name} <StatusBadge t={t} /></h2>
          <div className="ad-msub">
            Created {fmtDate(t.created)}{t.doneAt ? ` · Finished ${fmtDate(t.doneAt)}` : ""}
          </div>
        </div>
        <button className="ad-close" onClick={onClose} aria-label="Close"><X size={18} /></button>
      </div>

      <div className="ad-mbody">
        <div className="ad-defs">
          <div className="ad-def">
            <label>Website</label>
            <a href={t.url} target="_blank" rel="noreferrer">{t.url} <ExternalLink /></a>
          </div>
          <div className="ad-def">
            <label>Environment</label>
            <div className="ad-defval">{ENVS.find((e) => e.k === t.env)?.label || t.env}</div>
          </div>
          <div className="ad-def">
            <label>Created by</label>
            <div className="ad-defval"><Person uk={t.createdBy} /></div>
          </div>
          <div className="ad-def">
            <label>Reviewed by</label>
            <div className="ad-defval">{t.reviewedBy ? <Person uk={t.reviewedBy} /> : <span className="ad-mute">—</span>}</div>
          </div>
          <div className="ad-def">
            <label>Problem type</label>
            <div className="ad-defval">{(t.targets || []).length ? "Specific ads reported" : "General ad clutter"}</div>
          </div>
        </div>

        {(t.focus || (t.targets || []).length > 0 || t.notes) && (
          <div className="ad-msection">
            <h3>Ticket details</h3>
            <div className="ad-defs">
              {t.focus && (
                <div className="ad-def">
                  <label>Focus region (crawl scope)</label>
                  <div className="ad-defval">{t.focus}</div>
                </div>
              )}
              {(t.targets || []).length > 0 && (
                <div className="ad-def">
                  <label>Block these ads only</label>
                  <div className="ad-chips">{t.targets.map((x) => <span className="ad-chip" key={x}>{x}</span>)}</div>
                </div>
              )}
            </div>
            {t.notes && <p className="ad-notes" style={{ marginTop: 10 }}>{t.notes}</p>}
          </div>
        )}

        {/* StepList marks every stage complete when `current` is null, so a
            failed run must not render it — the failure box below says what
            actually happened. */}
        {t.state !== "draft" && t.state !== "failed" && (
          <div className="ad-msection">
            <h3>Pipeline</h3>
            <StepList current={t.state === "inprocess" ? t.stage : null} />
          </div>
        )}

        {t.state === "failed" && (
          <div className="ad-msection">
            <h3>Run failed</h3>
            <div className="ad-warnbox ad-errbox">
              <AlertTriangle className="ad-warnicon" />
              <div>
                <div className="ad-warntitle">The pipeline stopped before producing rules</div>
                <div className="ad-warnbody">
                  {t.errorMessage || "No error detail was recorded for this run."}
                </div>
                <div className="ad-warnbody">Fix the cause, then run the report again.</div>
              </div>
            </div>
          </div>
        )}

        {(t.state === "review" || t.state === "done") && (
          <>
            <div className="ad-msection">
              <h3>Sandbox result (mock)</h3>
              <MiniSandbox />
            </div>

            <div className="ad-msection">
              <h3>{t.state === "review" ? "Candidate rules — decide each one" : "Rules"}</h3>
              <DuplicateWarning dupes={t.duplicates} ruleCount={(t.rules || []).length} />
              <table className="ad-ruletable">
                <thead>
                  <tr>
                    <th>Rule</th>
                    <th>Pre-test</th>
                    <th>Conf.</th>
                    <th>Decision</th>
                  </tr>
                </thead>
                <tbody>
                  {(t.rules || []).map((r, i) => (
                    <tr key={i} className={r.status === "failed" ? "rulefail" : ""}>
                      <td>
                        <div className="ad-ruletext">{r.text}</div>
                        {r.status === "failed" && <div className="ad-rulereason">Auto-rejected — {r.reason}</div>}
                      </td>
                      <td><span className={"ad-pill " + (r.status === "passed" ? "pass" : "fail")}>{r.status}</span></td>
                      <td>
                        <span className="ad-conf">
                          {typeof r.conf === "number" ? `${Math.round(r.conf * 100)}%` : "—"}
                        </span>
                      </td>
                      <td>
                        {t.state === "review" && r.status === "passed" && (
                          <span className="ad-decide">
                            <button className={"ad-tgl" + (r.decision === "approve" ? " onA" : "")} onClick={() => onDecide(i, "approve")}>Approve</button>
                            <button className={"ad-tgl" + (r.decision === "reject" ? " onR" : "")} onClick={() => onDecide(i, "reject")}>Reject</button>
                          </span>
                        )}
                        {t.state === "done" && r.status === "passed" && (
                          <span className={"ad-decided " + (r.decision === "approve" ? "a" : "r")}>
                            {r.decision === "approve" ? <><CheckCircle2 /> Deployed</> : <><XCircle /> Rejected</>}
                          </span>
                        )}
                        {r.status === "failed" && <span className="ad-mute">—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>

      <div className="ad-mfoot">
        {t.state === "draft" && (
          <>
            <button className="ad-btn ad-btn-danger" onClick={onDelete}>Delete draft</button>
            <button className="ad-btn ad-btn-primary" onClick={onRun}>Send to pipeline</button>
          </>
        )}
        {t.state === "inprocess" && (
          <>
            <span className="ad-tally">The pipeline is processing this report. Refresh to see updates.</span>
            <button className="ad-btn ad-btn-ghost" onClick={onCancelRun}>Cancel run</button>
          </>
        )}
        {t.state === "failed" && (
          <>
            <button className="ad-btn ad-btn-danger" onClick={onDelete}>Delete report</button>
            <button className="ad-btn ad-btn-primary" onClick={onRun}>Run again</button>
          </>
        )}
        {t.state === "review" && (
          <>
            <span className="ad-tally">
              {approved} approved · {rejected} rejected · {undecided} pending — reviewer: {CURRENT_USER.name}
            </span>
            <button className="ad-btn ad-btn-primary" disabled={undecided > 0} onClick={onFinish}>
              {undecided > 0
                ? "Decide all rules to finish"
                : approved > 0
                ? `Finish review — deploy ${approved} rule${approved === 1 ? "" : "s"}`
                : "Finish review — deploy nothing"}
            </button>
          </>
        )}
        {t.state === "done" && (
          <span className="ad-tally">
            Locked — {approved > 0 ? `${approved} rule${approved === 1 ? "" : "s"} deployed.` : "closed with no rules deployed."}
            {t.reviewedBy ? ` Reviewed by ${userOf(t.reviewedBy)?.name}.` : ""}
          </span>
        )}
      </div>
    </div>
  );
}
