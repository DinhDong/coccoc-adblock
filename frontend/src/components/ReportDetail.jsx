import { useState, useEffect, useLayoutEffect, useRef } from "react";
import { X, ExternalLink, CheckCircle2, XCircle, AlertTriangle, Pencil, Trash2, Plus, Check, Combine, ZoomIn, ZoomOut, RotateCcw } from "lucide-react";
import { STAGES, ENVS } from "../constants.js";
import { fmtDate, fmtDur } from "../utils.js";
import StatusBadge from "./StatusBadge.jsx";

const STAGE_MS = { crawl: "crawlMs", generate: "generationMs", validate: "validationMs" };

/**
 * How long the current run has been going, ticking every second.
 *
 * `baseMs` is measured by the database, not derived from a timestamp here:
 * MySQL runs on UTC and serialises without an offset, so parsing it locally
 * in UTC+7 made a run that started seconds ago look seven hours old. Each
 * poll re-anchors the counter, so it stays honest and cannot drift.
 *
 * Its own component so the one-second interval re-renders this line rather
 * than the whole report modal.
 */
function ElapsedTime({ baseMs }) {
  const [sinceAnchor, setSinceAnchor] = useState(0);
  const anchor = useRef({ base: baseMs, at: Date.now() });

  useEffect(() => {
    anchor.current = { base: baseMs, at: Date.now() };
    setSinceAnchor(0);
  }, [baseMs]);

  useEffect(() => {
    const t = setInterval(() => setSinceAnchor(Date.now() - anchor.current.at), 1000);
    return () => clearInterval(t);
  }, []);

  if (typeof baseMs !== "number") return null;
  return (
    <span className="ad-steptime ad-steplive">
      {fmtDur(Math.max(0, anchor.current.base + sinceAnchor))}
    </span>
  );
}

function StepList({ current, metrics, estimates, runElapsedMs }) {
  const idx = STAGES.findIndex((s) => s.k === current);
  const running = idx !== -1;
  return (
    <div>
      {STAGES.map((s, i) => {
        const done = idx > i || idx === -1;
        const cur = idx === i;
        const ms = metrics?.[STAGE_MS[s.k]];
        // Estimates come from how long this stage has actually taken on past
        // runs, so they only appear once there is history to average.
        const eta = cur ? estimates?.[s.k] : null;
        return (
          <div className={"ad-step" + (done ? " donestep" : cur ? " current" : " pending")} key={s.k}>
            <span className="ad-stepdot" />
            <span className="ad-steplabel">{s.label}{cur ? "…" : done ? " — completed" : ""}</span>
            {typeof ms === "number" ? (
              <span className="ad-steptime">{fmtDur(ms)}</span>
            ) : (
              typeof eta === "number" && (
                <span className="ad-steptime ad-stepeta" title="Average of previous runs">
                  ~{fmtDur(eta)}
                </span>
              )
            )}
          </div>
        );
      })}
      {/* Keyed on whether the run is in flight, not on totalMs being present:
          the stage blobs fill in as the pipeline goes, so mid-run totalMs is a
          real number — just a partial one — and showing it would freeze the
          clock at however far the run had got. */}
      {running ? (
        <div className="ad-step ad-steptotal">
          <span className="ad-stepdot" />
          <span className="ad-steplabel">
            Running for
            {typeof estimates?.total === "number" && (
              <span className="ad-stepest">
                {" "}· {fmtDur(estimates.total)} typical
              </span>
            )}
          </span>
          {typeof runElapsedMs === "number" ? (
            <ElapsedTime baseMs={runElapsedMs} />
          ) : (
            <span className="ad-steptime ad-stepeta">—</span>
          )}
        </div>
      ) : (
        typeof metrics?.totalMs === "number" && (
          <div className="ad-step ad-steptotal">
            <span className="ad-stepdot" />
            <span className="ad-steplabel">Total pipeline time</span>
            <span className="ad-steptime">{fmtDur(metrics.totalMs)}</span>
          </div>
        )
      )}
    </div>
  );
}

/**
 * How many rules this run produced, and what happened to the rest.
 *
 * A run whose every candidate was already registered finishes with an empty
 * rule list. Without this the ticket looked broken; the counts say plainly
 * that the generator did its job and dedup took the output.
 */
function RuleYield({ t }) {
  const kept = (t.rules || []).length;
  const generated = typeof t.generatedCount === "number" ? t.generatedCount : kept;
  const dupes = t.duplicates?.total || 0;
  if (!generated && !dupes) return null;

  // Reports run before duplicates became reviewable had them removed from the
  // list, so the two cases have to read differently — otherwise an old report
  // would claim it kept rules it does not actually contain.
  const wereDropped = t.duplicates?.dropped;

  return (
    <div className="ad-yield">
      <span className="ad-yieldmain">
        <b>{generated}</b> rule{generated === 1 ? "" : "s"} generated
      </span>
      {dupes > 0 && (
        <span className="ad-yieldnote">
          {wereDropped
            ? `${kept} kept · ${dupes} dropped as already known`
            : `${dupes} flagged as duplicate — still yours to approve or reject`}
        </span>
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

// Each step multiplies rather than adds. A full-page mobile capture fits at
// ~15px wide, so the ceiling (width fills the stage) lands near 80x — additive
// steps of 0.5 would have taken about 160 clicks to cross that. Multiplying by
// 1.6 gets there in roughly ten, and keeps the steps feeling even at both ends
// because each one is the same proportional change.
const ZOOM_FACTOR = 1.6;

export function Lightbox({ image, onClose }) {
  const [zoom, setZoom] = useState(1);
  // Size the image occupies at 100%, measured once it has loaded. Zoom
  // multiplies this into a real width/height rather than a CSS transform:
  // transform scales the already-rasterised element, so a 16000px screenshot
  // laid out at ~700px would be magnified from that 700px bitmap and look
  // mushy. Changing the layout size makes the browser resample the source.
  const [fit, setFit] = useState(null);
  const [stageW, setStageW] = useState(0);
  const stage = useRef(null);
  // Where to put the scroll position after a zoom step, so the point you
  // clicked stays under the cursor instead of jumping to the top.
  const anchor = useRef(null);

  // Zooming stops where the image width fills the stage. Past that you would
  // have to pan sideways to read a single line — and these captures are tall
  // and narrow, so there is nothing out there to pan to.
  const maxZoom = fit && stageW ? Math.max(1, stageW / fit.w) : 1;
  const clamp = (z) => Math.min(maxZoom, Math.max(1, z));

  const measureStage = () => {
    if (stage.current) setStageW(stage.current.clientWidth);
  };

  const onImageLoad = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    if (r.width && r.height) setFit({ w: r.width, h: r.height });
    measureStage();
  };

  useEffect(() => {
    measureStage();
    window.addEventListener("resize", measureStage);
    return () => window.removeEventListener("resize", measureStage);
  }, []);

  const zoomBy = (factor, clientY) => {
    const node = stage.current;
    if (node && fit) {
      const rect = node.getBoundingClientRect();
      const y = clientY == null ? rect.height / 2 : clientY - rect.top;
      anchor.current = { ratio: (node.scrollTop + y) / (fit.h * zoom), y };
    }
    setZoom((z) => clamp(z * factor));
  };

  const reset = () => { anchor.current = null; setZoom(1); };

  // Restore the anchored position before paint, so the jump is never visible.
  // Re-measuring here as well keeps maxZoom honest if the usable width does
  // change underneath us (a browser without scrollbar-gutter support, say).
  useLayoutEffect(() => {
    const node = stage.current;
    if (!node || !fit) return;
    if (anchor.current) {
      const { ratio, y } = anchor.current;
      anchor.current = null;
      node.scrollTop = ratio * fit.h * zoom - y;
    }
    if (node.clientWidth && node.clientWidth !== stageW) setStageW(node.clientWidth);
  }, [zoom, fit, stageW]);

  // If the ceiling drops (window resized smaller, gutter appeared), bring an
  // already-applied zoom back under it rather than leaving the image wider
  // than the stage.
  useEffect(() => {
    setZoom((z) => Math.min(z, maxZoom));
  }, [maxZoom]);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") return onClose();
      if (e.key === "+" || e.key === "=") zoomBy(ZOOM_FACTOR);
      if (e.key === "-" || e.key === "_") zoomBy(1 / ZOOM_FACTOR);
      if (e.key === "0") reset();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // Clicking the backdrop closes; clicking the image zooms. The wheel is left
  // alone entirely so the stage scrolls natively — no preventDefault, no
  // custom handler, and the scrollbar behaves the way every other page does.
  const onStageClick = (e) => {
    if (e.target === stage.current) {
      if (zoom === 1) onClose();
      return;
    }
    zoomBy(ZOOM_FACTOR, e.clientY);
  };

  const onStageContextMenu = (e) => {
    e.preventDefault();
    zoomBy(1 / ZOOM_FACTOR, e.clientY);
  };

  const atMax = zoom >= maxZoom - 0.001;

  return (
    <div className="ad-lightbox" role="dialog" aria-modal="true" aria-label={image.label}>
      <div className="ad-lightbar" onClick={(e) => e.stopPropagation()}>
        <button className="ad-lightbtn" onClick={() => zoomBy(1 / ZOOM_FACTOR)} disabled={zoom <= 1} aria-label="Zoom out" title="Zoom out (right-click or −)">
          <ZoomOut size={17} />
        </button>
        <span className="ad-zoomlevel">{Math.round(zoom * 100)}%</span>
        <button className="ad-lightbtn" onClick={() => zoomBy(ZOOM_FACTOR)} disabled={atMax} aria-label="Zoom in" title="Zoom in (left-click or +)">
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
        onClick={onStageClick}
        onContextMenu={onStageContextMenu}
        style={{ cursor: atMax ? "zoom-out" : "zoom-in" }}
      >
        <img
          src={image.url}
          alt={image.label}
          draggable={false}
          onLoad={onImageLoad}
          style={
            zoom === 1 || !fit
              ? undefined
              : {
                  // Explicit size, not scale() — this is what keeps it sharp.
                  width: `${fit.w * zoom}px`,
                  height: `${fit.h * zoom}px`,
                  maxWidth: "none",
                  maxHeight: "none",
                }
          }
        />
      </div>

      <figcaption className="ad-lightcap">
        {image.label} · left-click to zoom in · right-click to zoom out
        {zoom > 1 ? " · scroll to move down the page" : ""}
      </figcaption>
    </div>
  );
}

function ReportImages({ reportId, backendUrl }) {
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
    fetch(`${backendUrl}/api/tickets/${encodeURIComponent(reportId)}/images`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => {
        const list = d.images || [];
        imageCache.set(reportId, { at: Date.now(), images: list });
        if (!cancelled) setImages(list);
      })
      .catch((e) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [reportId, backendUrl]);

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
              {r.duplicate && (
                <span className="ad-dupchip" title={r.duplicate.source}>
                  <AlertTriangle aria-hidden="true" /> duplicate
                </span>
              )}
            </div>
            {/* A warning, not a verdict — the approve/reject buttons below stay
                enabled so a moderator can deploy it anyway. */}
            {r.duplicate && (
              <div className="ad-rulewarn">
                {r.duplicate.source.charAt(0).toUpperCase() + r.duplicate.source.slice(1)}.
              </div>
            )}
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
            <button className={"ad-tgl" + (r.decision === "approve" ? " onA" : "")} onClick={() => onDecide(r.text, "approve")}>Approve</button>
            <button className={"ad-tgl" + (r.decision === "reject" ? " onR" : "")} onClick={() => onDecide(r.text, "reject")}>Reject</button>
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
  backendUrl, estimates,
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
        {t.state !== "draft" && t.state !== "failed" && t.state !== "queued" && (
          <>
            <div className="ad-msection">
              <h3>Pipeline</h3>
              <StepList
                current={t.state === "inprocess" ? t.stage : null}
                metrics={t.metrics}
                estimates={estimates}
                runElapsedMs={t.runElapsedMs}
              />
              <RuleYield t={t} />
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
              <ReportImages reportId={t.id} backendUrl={backendUrl} />
            </div>

            <div className="ad-msection">
              <h3>{t.state === "review" ? "Candidate rules — decide each one" : "Rules"}</h3>
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
        {t.state === "queued" && (
          <>
            <span className="ad-tally">
              Waiting for the worker
              {t.queuePosition ? ` — ${t.queuePosition} of ${t.queueLength} in the queue` : ""}.
              Runs happen one at a time.
            </span>
            <button className="ad-btn ad-btn-ghost" onClick={onCancelRun}>Remove from queue</button>
          </>
        )}
        {t.state === "inprocess" && (
          <>
            <span className="ad-tally">
              {stalled
                ? "No progress for over 10 minutes — the backend may have restarted mid-run. Cancel to put this back in Draft, then run it again."
                : "The pipeline is running this report in the background. Status updates automatically."}
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
              {approved} approved · {rejected} rejected · {undecided} pending
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
            </span>
            <button className="ad-btn ad-btn-danger" onClick={onDelete}>Delete report</button>
          </>
        )}
      </div>
    </div>
  );
}
