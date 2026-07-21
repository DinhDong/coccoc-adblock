import { useState } from "react";
import { X } from "lucide-react";
import { ENVS, CURRENT_USER } from "../constants.js";

export default function NewReportModal({ nextName, onCreate, onClose }) {
  const [name, setName] = useState(nextName);
  const [url, setUrl] = useState("");
  const [env, setEnv] = useState("desktop");
  const [focus, setFocus] = useState("");
  const [targets, setTargets] = useState("");
  const [notes, setNotes] = useState("");
  const [runNow, setRunNow] = useState(true);
  const [err, setErr] = useState("");

  const submit = () => {
    if (!name.trim()) { setErr("Give the report a name."); return; }
    if (!/^https?:\/\/.+\..+/.test(url.trim())) {
      setErr("Enter the reported page URL, e.g. https://example.vn/article");
      return;
    }
    onCreate(
      {
        name: name.trim(),
        url: url.trim(),
        env,
        focus: focus.trim(),
        targets: targets.split(",").map((s) => s.trim()).filter(Boolean),
        notes: notes.trim(),
      },
      runNow
    );
  };

  return (
    <div className="ad-modal" onMouseDown={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-label="New report">
      <div className="ad-mhead">
        <div>
          <h2>New report</h2>
          <div className="ad-msub">Will be created by {CURRENT_USER.name} · runs crawl → rule generation → sandbox</div>
        </div>
        <button className="ad-close" onClick={onClose} aria-label="Close"><X size={18} /></button>
      </div>

      <div className="ad-mbody">
        <div className="ad-field">
          <label htmlFor="nr-name">Report name</label>
          <input id="nr-name" className="ad-input" value={name} onChange={(e) => setName(e.target.value)} />
        </div>

        <div className="ad-field">
          <label htmlFor="nr-url">Website link</label>
          <input
            id="nr-url" className="ad-input" placeholder="https://…"
            value={url} onChange={(e) => { setUrl(e.target.value); setErr(""); }}
          />
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

        <div className="ad-checkrow">
          <input id="nr-run" type="checkbox" checked={runNow} onChange={(e) => setRunNow(e.target.checked)} />
          <label htmlFor="nr-run" style={{ cursor: "pointer" }}>Send to pipeline right away</label>
        </div>
      </div>

      <div className="ad-mfoot">
        <button className="ad-btn ad-btn-ghost" onClick={onClose}>Cancel</button>
        <button className="ad-btn ad-btn-primary" onClick={submit}>{runNow ? "Create & run" : "Create draft"}</button>
      </div>
    </div>
  );
}
