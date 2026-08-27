import { useState, useEffect, useRef } from "react";
import { Play, AlertTriangle, Eraser } from "lucide-react";
import { ENVS } from "../constants.js";
import { fmtDur, normalizeUrl, isWebUrl } from "../utils.js";
import { Lightbox } from "../components/ReportDetail.jsx";
import { usePersistentState, usePersistentJson } from "../usePersistentState.js";

const BACKEND = "http://127.0.0.1:5000";

const SHOT_LABELS = {
  crawl: "Page as loaded",
  before_boxed: "Ad candidates detected",
  after_rules: "With your rules applied",
};

// A stored environment that no longer exists would leave every tab unhighlighted.
const parseEnv = (raw) => (ENVS.some((e) => e.k === raw) ? raw : undefined);

export default function Playground({ seed, onSeedConsumed }) {
  // Persisted, because switching views unmounts this page: a URL, a pasted
  // rule list and a sandbox run that took a minute and a half all used to be
  // thrown away by a glance at the report list. `running` is deliberately not
  // stored — a run does not survive the unmount, and restoring it as true
  // would show a spinner for a request that is no longer coming back.
  const [url, setUrl] = usePersistentState("playground.url", "");
  const [env, setEnv] = usePersistentState("playground.env", "desktop", parseEnv);
  const [rulesText, setRulesText] = usePersistentState("playground.rules", "");
  const [result, setResult] = usePersistentJson("playground.result", null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [zoomed, setZoomed] = useState(null);
  const [startedAt, setStartedAt] = useState(null);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [pendingRun, setPendingRun] = useState(false);
  // Which seed object has already been applied. App passes onSeedConsumed as
  // an inline arrow, so it is a new function on every App render — and App
  // re-renders every 3s from the ticket poller. That made the effect below
  // re-run while `seed` was still set, re-arming the auto-run and firing the
  // same sandbox pass several times over. Comparing identity here makes
  // applying a seed idempotent however often the effect is invoked.
  const appliedSeed = useRef(null);

  // "Test in playground" in the rule library hands over the selected rules and
  // the page they came from. The props were already being passed; this
  // component ignored them, so arriving from the library dropped the selection
  // and left both boxes empty to be retyped by hand.
  //
  // Whether it then starts the run is the sender's call. The rule library
  // fills only -- its selection is usually about to be edited down. Live rules
  // sets autoRun, because "run these live rules" is already the whole intent
  // and stopping at a filled form would just need a second click.
  useEffect(() => {
    if (!seed || appliedSeed.current === seed) return;
    appliedSeed.current = seed;
    if (seed.url) setUrl(seed.url);
    if (Array.isArray(seed.rules)) setRulesText(seed.rules.join("\n"));
    if (seed.environment) setEnv(seed.environment);
    setResult(null);
    setError("");
    setPendingRun(Boolean(seed.autoRun));
    onSeedConsumed?.();
  }, [seed, onSeedConsumed]);

  // Wall-clock time for the run in flight. The result panel reports the
  // server's own validation time afterwards, but during the 30-90 seconds it
  // takes there was nothing moving on screen to say the request was alive.
  useEffect(() => {
    if (!running || !startedAt) return;
    setElapsedMs(Date.now() - startedAt);
    const tick = setInterval(() => setElapsedMs(Date.now() - startedAt), 1000);
    return () => clearInterval(tick);
  }, [running, startedAt]);

  // One rule per line; blank lines and # comments are ignored so a rule list
  // can be pasted straight from a filter file.
  const parsedRules = rulesText
    .split("\n")
    .map((r) => r.trim())
    .filter((r) => r && !r.startsWith("!") && !r.startsWith("#"));

  // Was any non-empty string; now the same check the report form uses, so a
  // bare host is accepted and a typo is caught here instead of surfacing as a
  // backend error after the request round-trips.
  const canRun = isWebUrl(url) && parsedRules.length > 0 && !running;

  // Height follows the content. A fixed 7 rows meant a selection sent over
  // from the rule library — which is routinely a dozen rules for one domain —
  // arrived scrolled, showing a fraction of what was about to be tested.
  // Clamped so it starts roomy and still cannot swallow the page.
  const rulesRows = Math.min(24, Math.max(10, rulesText.split("\n").length + 1));

  // Everything on this page is persisted, so a stale URL and rule list follow
  // you back into it days later. This is the way out — and it clears the
  // stored copies too, because the setters write through to localStorage.
  const hasAnything = Boolean(url || rulesText || result || error);

  const clearAll = () => {
    setUrl("");
    setRulesText("");
    setEnv("desktop");
    setResult(null);
    setError("");
    setStartedAt(null);
    setElapsedMs(0);
    // A seed already applied must not be re-applied after this: the ref is
    // what stops the effect refilling the form on the next App re-render.
    setPendingRun(false);
  };

  // Whether any outcome carries a failure reason, deciding if that column is
  // worth a place in the results table at all.
  const anyReason = (result?.outcomes || []).some((o) => o.reason);

  const run = async () => {
    if (!canRun) return;
    setRunning(true);
    setError("");
    setResult(null);
    const started = Date.now();
    setStartedAt(started);
    setElapsedMs(0);
    try {
      const response = await fetch(`${BACKEND}/api/playground/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: normalizeUrl(url), rules: parsedRules, environment: env }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      // Stored on the result rather than kept in its own state, so returning
      // to this page still shows how long the cached run actually took.
      setResult({ ...body.result, totalMs: Date.now() - started });
    } catch (e) {
      setError(e.message);
    } finally {
      setRunning(false);
    }
  };

  // Fires the auto-run a tick after the seed landed, not inside that effect:
  // setUrl/setRulesText have not been applied yet at that point, so calling
  // run() there would post the previous page's values — or nothing at all on
  // the first hand-off. Waiting for canRun to turn true means the request goes
  // out with the state the form is actually showing.
  useEffect(() => {
    if (!pendingRun) return;
    if (running) return;
    if (!canRun) {
      // Seed could not produce a runnable form (no rules, or a URL the sandbox
      // cannot load). Drop the request rather than leaving it armed to fire on
      // whatever gets typed next.
      setPendingRun(false);
      return;
    }
    setPendingRun(false);
    run();
  }, [pendingRun, canRun, running]);   // eslint-disable-line react-hooks/exhaustive-deps

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

      <div className="ad-card ad-pgform" style={{ padding: 16, marginTop: 18 }}>
        <div className="ad-field">
          <label htmlFor="pg-url">Page to load</label>
          <input
            id="pg-url"
            className="ad-input"
            placeholder="example.com/"
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
            rows={rulesRows}
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
          {/* Disabled mid-run on purpose: the in-flight request would still
              land and repopulate the result a moment after clearing. */}
          <button
            className="ad-btn ad-btn-ghost"
            onClick={clearAll}
            disabled={!hasAnything || running}
          >
            <Eraser /> Clear
          </button>
          <button className="ad-btn ad-btn-primary" onClick={run} disabled={!canRun}>
            <Play /> {running ? `Running sandbox… ${fmtDur(elapsedMs)}` : "Run"}
          </button>
        </div>

        {running && (
          <div className="ad-panelabel">
            Running for <b className="ad-steptime">{fmtDur(elapsedMs)}</b> — loading the
            page once per rule plus a combined pass, which usually takes 30–90 seconds.
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
                <span className="ad-steptime"> · {fmtDur(result.validationMs)} in the sandbox</span>
              )}
              {typeof result.totalMs === "number" && (
                <span className="ad-steptime"> · {fmtDur(result.totalMs)} total</span>
              )}
            </h3>
            {/* Reason is only ever filled in for a failure. On an all-passed
                run the column was a full height of "—", so it is dropped
                unless something actually has something to say. */}
            {result.outcomes.length === 0 ? (
              <div className="ad-panelabel">
                This run reported no per-rule verdicts.
              </div>
            ) : (
              <table className="ad-minitable" style={{ marginTop: 10 }}>
                <thead>
                  <tr>
                    <th>Rule</th>
                    <th>Result</th>
                    {anyReason && <th>Reason</th>}
                  </tr>
                </thead>
                <tbody>
                  {result.outcomes.map((o, i) => (
                    <tr key={i}>
                      <td><span className="ad-ruletext">{o.rule}</span></td>
                      <td>
                        <span className={"ad-pill " + (o.passed ? "pass" : "fail")}>
                          {o.passed ? "passed" : "failed"}
                        </span>
                      </td>
                      {anyReason && <td>{o.reason || "—"}</td>}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
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
