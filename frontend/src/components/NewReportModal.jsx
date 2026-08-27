import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { ENVS } from "../constants.js";
import { normalizeUrl, isWebUrl } from "../utils.js";

// Doubles as the edit form: pass `ticket` to prefill and switch to save mode.
export default function NewReportModal({ nextName, ticket, onCreate, onSave, onClose }) {
  const editing = Boolean(ticket);
  const [name, setName] = useState(ticket?.name ?? nextName);
  const [url, setUrl] = useState(ticket?.url ?? "");
  const [env, setEnv] = useState(ticket?.env ?? "desktop");
  const [focus, setFocus] = useState(ticket?.focus ?? "");
  const [targets, setTargets] = useState((ticket?.targets ?? []).join(", "));
  const [notes, setNotes] = useState(ticket?.notes ?? "");
  const [runNow, setRunNow] = useState(true);
  const [err, setErr] = useState("");

  // The suggested id is fetched from the backend, so it usually arrives a
  // moment after this form mounts and the initial useState above has already
  // run with an empty string. Adopt it when it lands, unless the moderator
  // has started typing a name of their own.
  const typed = useRef(false);
  useEffect(() => {
    if (editing || typed.current || !nextName) return;
    setName(nextName);
  }, [nextName, editing]);

  const submit = () => {
    if (!name.trim()) { setErr("Give the report a name."); return; }
    if (!isWebUrl(url)) {
      setErr("Enter the reported page, e.g. example.vn/article or https://example.vn/article");
      return;
    }
    const data = {
      name: name.trim(),
      // Stored with the scheme filled in, so everything downstream — the
      // crawler, the domain the registry keys on, the duplicate check — sees
      // one consistent form no matter how it was typed.
      url: normalizeUrl(url),
      env,
      focus: focus.trim(),
      targets: targets.split(",").map((s) => s.trim()).filter(Boolean),
      notes: notes.trim(),
    };
    if (editing) onSave(data);
    else onCreate(data, runNow);
  };

  return (
    <div className="ad-modal" onMouseDown={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-label={editing ? "Edit report" : "New report"}>
      <div className="ad-mhead">
        <div>
          <h2>{editing ? `Edit ${ticket.id}` : "New report"}</h2>
          <div className="ad-msub">
            {editing
              ? "Changes apply to the next run — rules already generated are left as they are."
              : "Runs crawl → rule generation → sandbox"}
          </div>
        </div>
        <button className="ad-close" onClick={onClose} aria-label="Close"><X size={18} /></button>
      </div>

      <div className="ad-mbody">
        <div className="ad-field">
          <label htmlFor="nr-name">Report name</label>
          <input
            id="nr-name" className="ad-input" value={name}
            onChange={(e) => { typed.current = true; setName(e.target.value); }}
            disabled={editing}
          />
          {editing && (
            <div className="ad-hint">
              The name is the report id — rules, crawl files and decisions are filed under it, so it cannot change.
            </div>
          )}
        </div>

        <div className="ad-field">
          <label htmlFor="nr-url">Website link</label>
          <input
            id="nr-url" className="ad-input" placeholder="example.vn/article"
            value={url} onChange={(e) => { setUrl(e.target.value); setErr(""); }}
          />
          {/* Only shown when the scheme was filled in for them, so it reads as
              confirmation of what will be crawled rather than noise. */}
          {!err && normalizeUrl(url) !== url.trim() && isWebUrl(url) && (
            <div className="ad-hint">Will crawl {normalizeUrl(url)}</div>
          )}
          {err && <div className="ad-err">{err}</div>}
        </div>

        <div className="ad-field">
          <label>Environment</label>
          <div className="ad-envtabs" role="group" aria-label="Crawl environment">
            {ENVS.map((e) => (
              <button key={e.k} type="button" className={"ad-envtab" + (env === e.k ? " on" : "")} onClick={() => setEnv(e.k)}>
                {e.label}
              </button>
            ))}
          </div>
          <div className="ad-hint">The crawler and sandbox reuse this viewport and user agent.</div>
        </div>

        <div className="ad-optlabel">Optional</div>

        <div className="ad-field">
          <label htmlFor="nr-focus">Focus on a specific part of the page</label>
          <input id="nr-focus" className="ad-input" placeholder="e.g. right sidebar, article footer" value={focus} onChange={(e) => setFocus(e.target.value)} />
        </div>

        <div className="ad-field">
          <label htmlFor="nr-targets">Block these ads only</label>
          <input id="nr-targets" className="ad-input" placeholder="e.g. popup overlay, video preroll (comma-separated)" value={targets} onChange={(e) => setTargets(e.target.value)} />
        </div>

        <div className="ad-field">
          <label htmlFor="nr-notes">Notes for the pipeline</label>
          <textarea id="nr-notes" className="ad-textarea" placeholder="Anything the reporter mentioned…" value={notes} onChange={(e) => setNotes(e.target.value)} />
        </div>

        {!editing && (
          <div className="ad-checkrow">
            <input id="nr-run" type="checkbox" checked={runNow} onChange={(e) => setRunNow(e.target.checked)} />
            <label htmlFor="nr-run" style={{ cursor: "pointer" }}>Send to pipeline right away</label>
          </div>
        )}
      </div>

      <div className="ad-mfoot">
        <button className="ad-btn ad-btn-ghost" onClick={onClose}>Cancel</button>
        <button className="ad-btn ad-btn-primary" onClick={submit}>
          {editing ? "Save changes" : runNow ? "Create & run" : "Create draft"}
        </button>
      </div>
    </div>
  );
}
