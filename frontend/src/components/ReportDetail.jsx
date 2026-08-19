import { useState, useEffect, useRef } from "react";
import { X, ExternalLink, CheckCircle2, XCircle, AlertTriangle, Pencil, Trash2, Plus, Check, Combine, ZoomIn, ZoomOut, RotateCcw } from "lucide-react";
import { STAGES, ENVS, CURRENT_USER, userOf } from "../constants.js";
import { fmtDate, fmtDur } from "../utils.js";
import { Person } from "./Avatar.jsx";
import StatusBadge from "./StatusBadge.jsx";

const STAGE_MS = { crawl: "crawlMs", generate: "generationMs", validate: "validationMs" };

function StepList({ current, metrics }) {
  const idx = STAGES.findIndex((s) => s.k === current);
  return (
    <div>
      {STAGES.map((s, i) => {
        const done = idx > i || idx === -1;
        const cur = idx === i;
        const ms = metrics?.[STAGE_MS[s.k]];
        return (
          <div className={"ad-step" + (done ? " donestep" : cur ? " current" : " pending")} key={s.k}>
            <span className="ad-stepdot" />
            <span className="ad-steplabel">{s.label}{cur ? "…" : done ? " — completed" : ""}</span>
            {typeof ms === "number" && <span className="ad-steptime">{fmtDur(ms)}</span>}
          </div>
        );
      })}
      {typeof metrics?.totalMs === "number" && (
        <div className="ad-step ad-steptotal">
          <span className="ad-stepdot" />
          <span className="ad-steplabel">Total pipeline time</span>
          <span className="ad-steptime">{fmtDur(metrics.totalMs)}</span>
        </div>
      )}
    </div>
  );
}

function TokenUsage({ metrics }) {
  if (!metrics?.totalTokens) return null;
  return (
    <div className="ad-msection">
      <h3>Token usage</h3>
      <div className="ad-tokenrow">
        <div className="ad-tokenstat">
          <div className="ad-tokennum">{metrics.totalTokens.toLocaleString()}</div>
          <div className="ad-tokenlabel">Total tokens</div>
        </div>
        <div className="ad-tokenstat">
          <div className="ad-tokennum">{metrics.promptTokens.toLocaleString()}</div>
          <div className="ad-tokenlabel">Prompt</div>
        </div>
        <div className="ad-tokenstat">
          <div className="ad-tokennum">{metrics.completionTokens.toLocaleString()}</div>
          <div className="ad-tokenlabel">Completion</div>
        </div>
        <div className="ad-tokenstat">
          <div className="ad-tokennum ad-tokenmodel">{metrics.model || "—"}</div>
          <div className="ad-tokenlabel">
            {metrics.fallbackUsed ? "Model (fallback used)" : "Model"}
          </div>
        </div>
      </div>
    </div>
  );
}

// Presigned URLs survive reopening a report, so they are cached per report
// instead of refetched every time the modal mounts. The TTL sits well under
// the backend's PRESIGNED_URL_EXPIRES_SECONDS (900s) so a cached URL can
// never be handed out after it has expired.
const IMAGE_CACHE_MS = 10 * 60 * 1000;
const imageCache = new Map();

export function clearReportImageCache(reportId) {
  if (reportId) imageCache.delete(reportId);
  else imageCache.clear();
}

const ZOOM_MIN = 1;
const ZOOM_MAX = 8;
const ZOOM_STEP = 0.25;

export function Lightbox({ image, onClose }) {
  const [zoom, setZoom] = useState(1);
  // Pan offset in screen pixels, so a zoomed-in crawl can be read top to bottom.
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  // Size the image fills at 100%, measured once it has loaded. Zoom multiplies
  // this into a real width/height rather than a CSS transform: transform
  // scales the already-rasterised element, so a 4000px screenshot laid out at
  // ~900px would be magnified from that 900px bitmap and look mushy. Changing
  // the layout size makes the browser resample from the full-resolution source.
  const [fit, setFit] = useState(null);
  const drag = useRef(null);
  const stage = useRef(null);

  const onImageLoad = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    if (r.width && r.height) setFit({ w: r.width, h: r.height });
  };

  const clamp = (z) => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z));
  const reset = () => { setZoom(1); setOffset({ x: 0, y: 0 }); };

  const zoomBy = (delta) =>
    setZoom((z) => {
      const next = clamp(z + delta);
      if (next === 1) setOffset({ x: 0, y: 0 });
      return next;
    });

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") return onClose();
      if (e.key === "+" || e.key === "=") zoomBy(ZOOM_STEP);
      if (e.key === "-" || e.key === "_") zoomBy(-ZOOM_STEP);
      if (e.key === "0") reset();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // React registers onWheel passively, so preventDefault there is ignored and
  // the page scrolls behind the overlay. Bind natively with passive:false.
  useEffect(() => {
    const node = stage.current;
    if (!node) return;
    const onWheel = (e) => {
      e.preventDefault();
      zoomBy(e.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP);
    };
    node.addEventListener("wheel", onWheel, { passive: false });
    return () => node.removeEventListener("wheel", onWheel);
  }, []);

  const onPointerDown = (e) => {
    if (zoom === 1) return;
    drag.current = { x: e.clientX - offset.x, y: e.clientY - offset.y };
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e) => {
    if (!drag.current) return;
    setOffset({ x: e.clientX - drag.current.x, y: e.clientY - drag.current.y });
  };
  const onPointerUp = () => { drag.current = null; };

  return (
    <div className="ad-lightbox" role="dialog" aria-modal="true" aria-label={image.label}>
      <div className="ad-lightbar" onClick={(e) => e.stopPropagation()}>
        <button className="ad-lightbtn" onClick={() => zoomBy(-ZOOM_STEP)} disabled={zoom <= ZOOM_MIN} aria-label="Zoom out" title="Zoom out (−)">
          <ZoomOut size={17} />
        </button>
        <span className="ad-zoomlevel">{Math.round(zoom * 100)}%</span>
        <button className="ad-lightbtn" onClick={() => zoomBy(ZOOM_STEP)} disabled={zoom >= ZOOM_MAX} aria-label="Zoom in" title="Zoom in (+)">
          <ZoomIn size={17} />
        </button>
        <button className="ad-lightbtn" onClick={reset} disabled={zoom === 1} aria-label="Reset zoom" title="Reset (0)">
          <RotateCcw size={16} />
        </button>
        <button className="ad-lightbtn" onClick={onClose} aria-label="Close image" title="Close (Esc)">
          <X size={18} />
        </button>
      </div>

      <div
        className="ad-lightstage"
        ref={stage}
        onClick={(e) => { if (e.target === e.currentTarget && zoom === 1) onClose(); }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        style={{ cursor: zoom > 1 ? (drag.current ? "grabbing" : "grab") : "default" }}
      >
        <img
          src={image.url}
          alt={image.label}
          draggable={false}
          onLoad={onImageLoad}
          style={
            zoom === 1 || !fit
              ? { transform: `translate(${offset.x}px, ${offset.y}px)` }
              : {
                  // Explicit size, not scale() — this is what keeps it sharp.
                  width: `${fit.w * zoom}px`,
                  height: `${fit.h * zoom}px`,
                  maxWidth: "none",
                  maxHeight: "none",
                  transform: `translate(${offset.x}px, ${offset.y}px)`,
                }
          }
        />
      </div>

      <figcaption className="ad-lightcap">
        {image.label} · scroll or +/− to zoom{zoom > 1 ? " · drag to pan" : ""}
      </figcaption>
    </div>
  );
}

function ReportImages({ reportId }) {
  const cached = imageCache.get(reportId);
  const fresh = cached && Date.now() - cached.at < IMAGE_CACHE_MS;
  const [images, setImages] = useState(fresh ? cached.images : null);
  const [error, setError] = useState("");
  const [zoomed, setZoomed] = useState(null);

  useEffect(() => {
    const hit = imageCache.get(reportId);
    if (hit && Date.now() - hit.at < IMAGE_CACHE_MS) {
      setImages(hit.images);
      return;
    }

    let cancelled = false;
    fetch(`http://127.0.0.1:5000/api/tickets/${encodeURIComponent(reportId)}/images`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => {
        const list = d.images || [];
        imageCache.set(reportId, { at: Date.now(), images: list });
        if (!cancelled) setImages(list);
      })
      .catch((e) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [reportId]);

  if (error) {
    return <div className="ad-panelabel">Could not load screenshots — {error}</div>;
  }
  if (!images) return <div className="ad-panelabel">Loading screenshots…</div>;

  return (
    <>
      <div className="ad-shots">
        {images.map((img) => (
          <figure className="ad-shot" key={img.kind}>
            {img.url ? (
              <button
                type="button"
                className="ad-shotbtn"
                onClick={() => setZoomed(img)}
                title="Click to enlarge"
              >
                {/* decoding=async keeps a large PNG off the main thread; the
                    browser caches the bytes so reopening is instant. */}
                <img src={img.url} alt={img.label} loading="lazy" decoding="async" />
              </button>
            ) : (
              <div className="ad-shotmissing">Not produced for this run</div>
            )}
            <figcaption>{img.label}</figcaption>
          </figure>
        ))}
      </div>
      {zoomed && <Lightbox image={zoomed} onClose={() => setZoomed(null)} />}
    </>
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

function EditTicketButton({ onEdit }) {
  return (
    <button className="ad-btn ad-btn-ghost" onClick={onEdit}>
      <Pencil /> Edit report
    </button>
  );
}

function AddRuleRow({ onAdd }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    const rule = text.trim();
    if (!rule || busy) return;
    setBusy(true);
    const ok = await onAdd(rule);
    setBusy(false);
    if (ok) setText("");
  };

  return (
    <div className="ad-addrule">
      <input
        className="ad-input"
        placeholder="Type a rule, e.g. example.com##div.ad-box or ||ads.example.com^"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") submit(); }}
        aria-label="New rule"
      />
      <button className="ad-btn ad-btn-ghost" onClick={submit} disabled={!text.trim() || busy}>
        <Plus /> Add rule
      </button>
    </div>
  );
}

// Pre-test result, or the reason there isn't one. A hand-written rule never
// went through the sandbox, so it reports its origin instead of a verdict.
export function PreTestCell({ r }) {
  if (r.source === "manual") return <span className="ad-pill manual">Manual</span>;
  if (r.status === "pending") return <span className="ad-mute">not run</span>;
  return <span className={"ad-pill " + (r.status === "passed" ? "pass" : "fail")}>{r.status}</span>;
}

export function RuleActions({ editing, setEditing, onDelete, label }) {
  return (
    <span className="ad-rowactions">
      <button
        className="ad-iconbtn"
        title="Edit this rule"
        aria-label={`Edit rule ${label}`}
        onClick={() => setEditing(!editing)}
      >
        <Pencil />
      </button>
      <button
        className="ad-iconbtn danger"
        title="Delete this rule"
        aria-label={`Delete rule ${label}`}
        onClick={onDelete}
      >
        <Trash2 />
      </button>
    </span>
  );
}

export function RuleEditor({ value, original, onChange, onSave, onCancel }) {
  return (
    <div className="ad-addrule">
      <input
        className="ad-input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && value.trim() && value.trim() !== original) onSave();
          if (e.key === "Escape") onCancel();
        }}
        aria-label="Edit rule text"
        autoFocus
      />
      <button
        className="ad-iconbtn"
        title="Save"
        aria-label="Save rule"
        disabled={!value.trim() || value.trim() === original}
        onClick={onSave}
      >
        <Check />
      </button>
      <button className="ad-iconbtn" title="Cancel" aria-label="Cancel edit" onClick={onCancel}>
        <X />
      </button>
    </div>
  );
}

function RuleRow({ r, index, state, selected, onToggle, onDecide, onEditRule, onDeleteRule }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(r.text);

  const save = () => { onEditRule(r.text, text.trim()); setEditing(false); };
  const startEditing = (next) => { setText(r.text); setEditing(next); };

  return (
    <tr className={(r.status === "failed" ? "rulefail" : "") + (selected ? " ad-rowsel" : "")}>
      <td>
        <input
          type="checkbox"
          className="ad-check"
          checked={selected}
          onChange={onToggle}
          aria-label={`Select rule ${r.text}`}
        />
      </td>
      <td>
        {editing ? (
          <RuleEditor
            value={text}
            original={r.text}
            onChange={setText}
            onSave={save}
            onCancel={() => setEditing(false)}
          />
        ) : (
          <>
            <div className="ad-ruletext">
              {r.text}
              {r.source === "edited" && <span className="ad-srcchip">edited</span>}
            </div>
            {r.status === "failed" && r.reason && (
              <div className="ad-rulereason">Auto-rejected — {r.reason}</div>
            )}
          </>
        )}
      </td>
      <td><PreTestCell r={r} /></td>
      <td>
        <span className="ad-conf">
          {typeof r.conf === "number" ? `${Math.round(r.conf * 100)}%` : "—"}
        </span>
      </td>
      <td>
        {/* Hand-written rules arrive unvalidated, so gate on "not auto-rejected"
            rather than "passed" — otherwise a rule you just typed could never
            be approved. */}
        {state === "review" && r.status !== "failed" && (
          <span className="ad-decide">
            <button className={"ad-tgl" + (r.decision === "approve" ? " onA" : "")} onClick={() => onDecide(index, "approve")}>Approve</button>
            <button className={"ad-tgl" + (r.decision === "reject" ? " onR" : "")} onClick={() => onDecide(index, "reject")}>Reject</button>
          </span>
        )}
        {/* A closed report can still hold rules nobody ruled on. Reporting
            those as "Rejected" claims a decision that was never made. */}
        {state === "done" && r.status !== "failed" && !r.decision && (
          <span className="ad-mute">No decision</span>
        )}
        {state === "done" && r.status !== "failed" && r.decision && (
          <span className={"ad-decided " + (r.decision === "approve" ? "a" : "r")}>
            {r.decision === "approve" ? <><CheckCircle2 /> Deployed</> : <><XCircle /> Rejected</>}
          </span>
        )}
        {r.status === "failed" && (
          <span className="ad-decided r" title="The sandbox rejected this rule; no moderator decision needed">
            <XCircle /> Auto-rejected
          </span>
        )}
      </td>
      <td>
        <RuleActions
          editing={editing}
          setEditing={startEditing}
          label={r.text}
          onDelete={() => {
            if (window.confirm(`Delete this rule?\n\n${r.text}`)) onDeleteRule(r.text);
          }}
        />
      </td>
    </tr>
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

export default function ReportDetail({
  t, onClose, onRun, onCancelRun, onDelete, onDecide, onFinish,
  onEdit, onAddRule, onEditRule, onDeleteRule, onMergeRules, onMergePreview,
}) {
  const [selected, setSelected] = useState(() => new Set());

  const toggleRule = (text) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(text)) next.delete(text);
      else next.add(text);
      return next;
    });

  // Same contract as the library: exactly two rules, previewed before commit
  // so the moderator confirms the real merged text.
  const mergeSelected = async () => {
    const picked = [...selected];
    if (picked.length !== 2) return;
    const items = picked.map((rule) => ({ reportId: t.id, rule }));
    const preview = await onMergePreview(items);
    if (!preview) return;
    if (
      !window.confirm(
        `Merge these two rules into one?\n\n  ${picked[0]}\n  ${picked[1]}\n\nResult:\n  ${preview.merged}`
      )
    ) {
      return;
    }
    await onMergeRules(items);
    setSelected(new Set());
  };
  // Anything the sandbox did not auto-reject is the moderator's to rule on,
  // including hand-written rules that were never validated.
  // A run lives on a worker thread now, so a backend restart mid-run leaves
  // the ticket stuck in-process with nothing to move it on. Flag it rather
  // than resetting it automatically — a teammate's run may still be alive.
  const stalled =
    t.state === "inprocess" &&
    t.updatedAt &&
    Date.now() - new Date(t.updatedAt).getTime() > 10 * 60 * 1000;

  const decidable = (t.rules || []).filter((r) => r.status !== "failed");
  const undecided = decidable.filter((r) => !r.decision).length;
  const approved = decidable.filter((r) => r.decision === "approve").length;
  const rejected = decidable.filter((r) => r.decision === "reject").length;

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
          <>
            <div className="ad-msection">
              <h3>Pipeline</h3>
              <StepList current={t.state === "inprocess" ? t.stage : null} metrics={t.metrics} />
            </div>
            <TokenUsage metrics={t.metrics} />
          </>
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
              <h3>Screenshots</h3>
              <ReportImages reportId={t.id} />
            </div>

            <div className="ad-msection">
              <h3>{t.state === "review" ? "Candidate rules — decide each one" : "Rules"}</h3>
              <DuplicateWarning dupes={t.duplicates} ruleCount={(t.rules || []).length} />
              {selected.size > 0 && (
                <div className="ad-bulkbar" style={{ margin: "8px 0" }}>
                  <b>{selected.size} selected</b>
                  <button
                    className="ad-btn ad-btn-ghost"
                    onClick={mergeSelected}
                    disabled={selected.size !== 2}
                    title={selected.size === 2 ? "Combine into one rule" : "Select exactly two rules to merge"}
                  >
                    <Combine /> Merge
                  </button>
                  <button className="ad-btn ad-btn-ghost" onClick={() => setSelected(new Set())}>Clear</button>
                </div>
              )}
              <table className="ad-ruletable">
                <thead>
                  <tr>
                    <th style={{ width: 24 }} aria-label="Select" />
                    <th>Rule</th>
                    <th>Pre-test</th>
                    <th>Conf.</th>
                    <th>Decision</th>
                    <th aria-label="Actions" />
                  </tr>
                </thead>
                <tbody>
                  {(t.rules || []).map((r, i) => (
                    <RuleRow
                      key={`${r.text}-${i}`}
                      r={r}
                      index={i}
                      state={t.state}
                      selected={selected.has(r.text)}
                      onToggle={() => toggleRule(r.text)}
                      onDecide={onDecide}
                      onEditRule={onEditRule}
                      onDeleteRule={onDeleteRule}
                    />
                  ))}
                </tbody>
              </table>
              <AddRuleRow onAdd={onAddRule} />
            </div>
          </>
        )}
      </div>

      <div className="ad-mfoot">
        {t.state === "draft" && (
          <>
            <button className="ad-btn ad-btn-danger" onClick={onDelete}>Delete draft</button>
            <EditTicketButton onEdit={onEdit} />
            <button className="ad-btn ad-btn-primary" onClick={onRun}>Send to pipeline</button>
          </>
        )}
        {t.state === "inprocess" && (
          <>
            <span className="ad-tally">
              {stalled
                ? "No progress for over 10 minutes — the backend may have restarted mid-run. Cancel to put this back in Draft, then run it again."
                : "The pipeline is running this report in the background. You can close this and come back."}
            </span>
            <button className="ad-btn ad-btn-ghost" onClick={onCancelRun}>Cancel run</button>
          </>
        )}
        {t.state === "failed" && (
          <>
            <button className="ad-btn ad-btn-danger" onClick={onDelete}>Delete report</button>
            <EditTicketButton onEdit={onEdit} />
            <button className="ad-btn ad-btn-primary" onClick={onRun}>Run again</button>
          </>
        )}
        {t.state === "review" && (
          <>
            <span className="ad-tally">
              {approved} approved · {rejected} rejected · {undecided} pending — reviewer: {CURRENT_USER.name}
            </span>
            {/* No "Edit report" here on purpose: once the pipeline has run,
                changing the URL or targets would leave the rules below
                describing a page the ticket no longer points at. */}
            <button className="ad-btn ad-btn-danger" onClick={onDelete}>Delete report</button>
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
          <>
            <span className="ad-tally">
              Locked — {approved > 0 ? `${approved} rule${approved === 1 ? "" : "s"} deployed.` : "closed with no rules deployed."}
              {t.reviewedBy ? ` Reviewed by ${userOf(t.reviewedBy)?.name}.` : ""}
            </span>
            <button className="ad-btn ad-btn-danger" onClick={onDelete}>Delete report</button>
          </>
        )}
      </div>
    </div>
  );
}
