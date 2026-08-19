import { useState, useEffect } from "react";
import { Play, AlertTriangle } from "lucide-react";
import { ENVS } from "../constants.js";
import { fmtDur } from "../utils.js";
import { Lightbox } from "../components/ReportDetail.jsx";

const BACKEND = "http://127.0.0.1:5000";

const SHOT_LABELS = {
  crawl: "Page as loaded",
  before_boxed: "Ad candidates detected",
  after_rules: "With your rules applied",
};

export default function Playground() {
  const [url, setUrl] = useState("");
  const [env, setEnv] = useState("desktop");
  const [rulesText, setRulesText] = useState("");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [zoomed, setZoomed] = useState(null);

  // One rule per line; blank lines and # comments are ignored so a rule list
  // can be pasted straight from a filter file.
  const parsedRules = rulesText
    .split("\n")
    .map((r) => r.trim())
    .filter((r) => r && !r.startsWith("!") && !r.startsWith("#"));

  const canRun = url.trim() && parsedRules.length > 0 && !running;

  const run = async () => {
    if (!canRun) return;
    setRunning(true);
    setError("");
    setResult(null);
    try {
      const response = await fetch(`${BACKEND}/api/playground/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url.trim(), rules: parsedRules, environment: env }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      setResult(body.result);
    } catch (e) {
      setError(e.message);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="ad-content">
      <div className="ad-pagehead">
        <div>
          <h1>Rule playground</h1>
          <p>
            Load any page with rules of your choosing applied, and see what they block —
            without creating a report.
          </p>
        </div>
      </div>

      <div className="ad-card" style={{ padding: 16, marginTop: 18 }}>
        <div className="ad-field">
          <label htmlFor="pg-url">Page to load</label>
          <input
            id="pg-url"
            className="ad-input"
            placeholder="https://example.com/"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
        </div>

        <div className="ad-field">
          <label>Environment</label>
          <div className="ad-envtabs" role="group" aria-label="Environment">
            {ENVS.map((e) => (
              <button
                key={e.k}
                type="button"
                className={"ad-envtab" + (env === e.k ? " on" : "")}
                onClick={() => setEnv(e.k)}
              >
                {e.label}
              </button>
            ))}
          </div>
        </div>

        <div className="ad-field">
          <label htmlFor="pg-rules">
            Rules to test <span className="ad-mute">— one per line</span>
          </label>
          <textarea
            id="pg-rules"
            className="ad-textarea ad-rulesbox"
            rows={7}
            placeholder={"example.com##div.ad-box\n||ads.example.com^$third-party"}
            value={rulesText}
            onChange={(e) => setRulesText(e.target.value)}
          />
          <div className="ad-hint">
            {parsedRules.length} rule{parsedRules.length === 1 ? "" : "s"} ready ·
            lines starting with ! or # are ignored
          </div>
        </div>

        <div className="ad-mfoot" style={{ paddingRight: 0 }}>
          <span className="ad-tally">
            Nothing is saved — no report is created and no rule enters the library.
          </span>
          <button className="ad-btn ad-btn-primary" onClick={run} disabled={!canRun}>
            <Play /> {running ? "Running sandbox…" : "Run"}
          </button>
        </div>

        {running && (
          <div className="ad-panelabel">
            Loading the page once per rule plus a combined pass — this usually takes
            30–90 seconds.
          </div>
        )}

        {error && (
          <div className="ad-warnbox ad-errbox" style={{ marginTop: 12 }}>
            <AlertTriangle className="ad-warnicon" />
            <div>
              <div className="ad-warntitle">Could not run this test</div>
              <div className="ad-warnbody">{error}</div>
            </div>
          </div>
        )}
      </div>

      {result && (
        <>
          <div className="ad-panel" style={{ marginTop: 16 }}>
            <h3>
              {result.passed}/{result.total} passed on {result.url}
              {typeof result.validationMs === "number" && (
                <span className="ad-steptime"> · {fmtDur(result.validationMs)}</span>
              )}
            </h3>
            <table className="ad-minitable" style={{ marginTop: 10 }}>
              <thead><tr><th>Rule</th><th>Result</th><th>Reason</th></tr></thead>
              <tbody>
                {result.outcomes.map((o, i) => (
                  <tr key={i}>
                    <td><span className="ad-ruletext">{o.rule}</span></td>
                    <td>
                      <span className={"ad-pill " + (o.passed ? "pass" : "fail")}>
                        {o.passed ? "passed" : "failed"}
                      </span>
                    </td>
                    <td>{o.reason || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="ad-panel" style={{ marginTop: 16 }}>
            <h3>Screenshots</h3>
            {result.images.length === 0 ? (
              <div className="ad-panelabel">This run produced no screenshots.</div>
            ) : (
              <div className="ad-shots ad-shots-tall">
                {result.images.map((kind) => {
                  const src = `${BACKEND}/api/playground/${result.runId}/screenshot/${kind}`;
                  return (
                    <figure className="ad-shot" key={kind}>
                      <button
                        type="button"
                        className="ad-shotbtn"
                        onClick={() => setZoomed({ url: src, label: SHOT_LABELS[kind] || kind })}
                        title="Click to enlarge"
                      >
                        <img src={src} alt={SHOT_LABELS[kind] || kind} loading="lazy" decoding="async" />
                      </button>
                      <figcaption>{SHOT_LABELS[kind] || kind}</figcaption>
                    </figure>
                  );
                })}
              </div>
            )}
          </div>
        </>
      )}

      {zoomed && <Lightbox image={zoomed} onClose={() => setZoomed(null)} />}
    </div>
  );
}
