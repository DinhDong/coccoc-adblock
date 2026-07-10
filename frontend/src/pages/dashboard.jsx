import { useState, useEffect, useMemo, useRef } from "react";
import "../styles/global.css";

/* ------------------------------------------------------------------ */
/*  Ad-block Rule Moderation Board — admin-only CMS frontend (draft)  */
/*  Ticket lifecycle: Draft → In-process → Review → Done              */
/* ------------------------------------------------------------------ */

const STATE_ORDER = ["draft", "inprocess", "review", "done"];
const TABS = ["draft", "review", "done"]; // in-process tickets surface inside Review as ghosts
const TAB_ROTS = ["-0.45deg", "0.3deg", "-0.35deg"];

const STATES = {
  draft: {
    label: "Draft",
    sub: "Created — not yet run",
    paper: "#F5FAF0",
    pill: { bg: "#BBC3BF", ink: "#1D3829" },
    empty: "Nothing pinned yet — add a report.",
  },
  inprocess: {
    label: "In-process",
    sub: "Running through the pipeline",
    paper: "#D6ECBE",
    pill: { bg: "#88C646", ink: "#1D3829" },
    empty: "Idle — send a draft to the pipeline.",
  },
  review: {
    label: "Review",
    sub: "Waiting on a moderator",
    paper: "#FFD2BE",
    pill: { bg: "#FF7439", ink: "#3D1A07" },
    empty: "No reports waiting on you.",
  },
  done: {
    label: "Done",
    sub: "Review complete",
    paper: "#C4E3A3",
    pill: { bg: "#1D3829", ink: "#C6E8A4" },
    empty: "Nothing finished yet.",
  },
};

const ENVS = [
  { k: "desktop", label: "Desktop" },
  { k: "android", label: "Android" },
  { k: "ios", label: "iOS" },
];

const USERS = [
  { k: "linh.nguyen", name: "Anh Dao", initials: "AD", bg: "#88C646", ink: "#1D3829" },
  { k: "minh.tran", name: "Duy Le", initials: "DL", bg: "#1D3829", ink: "#C6E8A4" },
  { k: "khoa.pham", name: "Hien Khuong", initials: "HK", bg: "#FF7439", ink: "#3D1A07" },
  { k: "thu.le", name: "Dong Tran", initials: "DT", bg: "#C4E3A3", ink: "#1D3829" },
];
const CURRENT_USER = USERS[0]; // stub for the signed-in admin

const userOf = (k) => USERS.find((u) => u.k === k);

const STAGES = [
  { k: "crawl", label: "Crawling page" },
  { k: "generate", label: "Generating rules" },
  { k: "validate", label: "Sandbox validation" },
];

/* ------------------------------ seed data ------------------------------ */

const SEED = [
  {
    id: "u1",
    name: "RPT-2026-0147",
    url: "https://genk.vn",
    env: "android",
    createdBy: "thu.le",
    state: "draft",
    created: "2026-07-07",
    focus: "Article footer",
    targets: [],
    notes: "Ads reappear about 10s after scrolling past them.",
  },
  {
    id: "u2",
    name: "RPT-2026-0146",
    url: "https://soha.vn",
    env: "desktop",
    createdBy: "linh.nguyen",
    state: "draft",
    created: "2026-07-06",
    focus: "",
    targets: ["Popup overlay"],
    notes: "Full-screen popup appears ~5 seconds after load.",
  },
  {
    id: "u3",
    name: "RPT-2026-0145",
    url: "https://dantri.com.vn",
    env: "ios",
    createdBy: "minh.tran",
    state: "inprocess",
    stage: "crawl",
    created: "2026-07-07",
    focus: "",
    targets: ["Video preroll"],
    notes: "",
  },
  {
    id: "u4",
    name: "RPT-2026-0141",
    url: "https://kenh14.vn",
    env: "desktop",
    createdBy: "linh.nguyen",
    state: "review",
    created: "2026-07-05",
    focus: "Right sidebar",
    targets: ["Floating video player", "Sidebar banners"],
    notes: "",
    rules: [
      { text: "kenh14.vn##.ads-sponsor-wrapper", status: "passed", conf: 0.93 },
      { text: 'kenh14.vn##div[id^="admzone"]', status: "passed", conf: 0.89 },
      { text: "||adx.admicro.vn^$third-party", status: "passed", conf: 0.95 },
      { text: "kenh14.vn##.video-float-ctn", status: "passed", conf: 0.84 },
      {
        text: 'kenh14.vn##div[class*="content"]',
        status: "failed",
        conf: 0.41,
        reason: "Too broad — hid the article body in the sandbox.",
      },
    ],
  },
  {
    id: "u5",
    name: "RPT-2026-0140",
    url: "https://znews.vn",
    env: "android",
    createdBy: "khoa.pham",
    state: "review",
    created: "2026-07-04",
    focus: "",
    targets: [],
    notes: "Reported twice this week.",
    rules: [
      { text: "znews.vn##.article-ads", status: "passed", conf: 0.9 },
      { text: "||static.adtima.vn^$third-party", status: "passed", conf: 0.94 },
      { text: "znews.vn##aside[data-ad]", status: "passed", conf: 0.82 },
      {
        text: "znews.vn##.overlay",
        status: "failed",
        conf: 0.43,
        reason: "Also matches the image lightbox overlay.",
      },
    ],
  },
  {
    id: "u6",
    name: "RPT-2026-0139",
    url: "https://vnexpress.net",
    env: "desktop",
    createdBy: "thu.le",
    reviewedBy: "minh.tran",
    state: "done",
    created: "2026-07-02",
    doneAt: "2026-07-03",
    focus: "",
    targets: ["Top banner"],
    notes: "",
    rules: [
      { text: "vnexpress.net##.banner-top-site", status: "passed", conf: 0.91, decision: "approve" },
      { text: "||ads.eclick.vn^$third-party", status: "passed", conf: 0.96, decision: "approve" },
      { text: 'vnexpress.net##div[id^="banner_"]', status: "passed", conf: 0.87, decision: "approve" },
      {
        text: "vnexpress.net##.sidebar",
        status: "failed",
        conf: 0.35,
        reason: "Hid the most-read column.",
      },
    ],
  },
  {
    id: "u7",
    name: "RPT-2026-0137",
    url: "https://tinhte.vn",
    env: "desktop",
    createdBy: "minh.tran",
    reviewedBy: "linh.nguyen",
    state: "done",
    created: "2026-06-29",
    doneAt: "2026-06-30",
    focus: "",
    targets: [],
    notes: "",
    rules: [
      { text: "tinhte.vn##.jsAdsPos", status: "passed", conf: 0.9, decision: "approve" },
      { text: "||googlesyndication.com^$domain=tinhte.vn", status: "passed", conf: 0.93, decision: "approve" },
      { text: "tinhte.vn##.p-sidebar", status: "passed", conf: 0.62, decision: "reject" },
    ],
  },
];

/* ------------------------------ helpers ------------------------------ */

const hash = (s, m) => {
  let h = 0;
  for (const c of s) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return h % m;
};
const noteRot = (id) => (hash(id, 9) - 4) * 0.55;
const tapeRot = (id) => (hash(id + "t", 7) - 3) * 1.1;

const hostname = (url) => {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url.replace(/^https?:\/\//, "").split("/")[0];
  }
};

const fmtDate = (iso) => {
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
};

const todayISO = () => new Date().toISOString().slice(0, 10);

function makeRules(t) {
  const host = hostname(t.url);
  const root = host.split(".").slice(-2).join(".");
  const rules = [
    { text: `${host}##div[id^="zone-ad"]`, status: "passed", conf: 0.88 },
    { text: `${host}##.banner-wrap`, status: "passed", conf: 0.81 },
    { text: `||ads.${root}^$third-party`, status: "passed", conf: 0.92 },
  ];
  if ((t.targets || []).some((x) => /popup|overlay/i.test(x)) || /popup/i.test(t.notes || "")) {
    rules.push({ text: `${host}##.popup-backdrop`, status: "passed", conf: 0.86 });
  } else {
    rules.push({ text: `${host}##.sticky-footer-ad`, status: "passed", conf: 0.79 });
  }
  rules.push({
    text: `${host}##[class*="ads"]`,
    status: "failed",
    conf: 0.38,
    reason: "Too broad — matched 3 non-ad elements in the sandbox.",
  });
  return rules;
}

const passedRules = (t) => (t.rules || []).filter((r) => r.status === "passed");
const approvedRules = (t) => passedRules(t).filter((r) => r.decision === "approve");

/* ------------------------------ small parts ------------------------------ */

function Tape({ rot, busy }) {
  return <span className={"mb-tape" + (busy ? " mb-busy" : "")} style={{ "--trot": rot + "deg" }} aria-hidden="true" />;
}

function Stamp({ approved }) {
  return (
    <span className={"mb-stamp" + (approved > 0 ? "" : " mb-gray")} aria-hidden="true">
      {approved > 0 ? "APPROVED" : "CLOSED"}
    </span>
  );
}

function StageList({ current }) {
  const idx = STAGES.findIndex((s) => s.k === current);
  return (
    <div>
      {STAGES.map((s, i) => {
        const done = idx > i || idx === -1;
        const cur = idx === i;
        return (
          <div className="mb-stage" key={s.k}>
            <span className={"mb-stagedot" + (done ? " mb-doneStage" : "") + (cur ? " mb-current" : "")} />
            <span className={"mb-stagelabel" + (!done && !cur ? " mb-pending" : "")}>
              {s.label}
              {cur ? "…" : done ? " — ok" : ""}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function MiniSandbox() {
  const Lines = () => (
    <>
      <div className="mb-line" style={{ width: "88%" }} />
      <div className="mb-line" style={{ width: "72%" }} />
      <div className="mb-line" style={{ width: "80%" }} />
    </>
  );
  return (
    <div className="mb-sandbox">
      <div className="mb-pane">
        <div className="mb-mini">
          <div className="mb-bar" />
          <div className="mb-ad">AD</div>
          <Lines />
          <div className="mb-ad">AD</div>
        </div>
        <div className="mb-panelabel">Before</div>
      </div>
      <div className="mb-pane">
        <div className="mb-mini">
          <div className="mb-bar" />
          <div className="mb-gone" />
          <Lines />
          <div className="mb-gone" />
        </div>
        <div className="mb-panelabel">After rules (sandbox)</div>
      </div>
    </div>
  );
}

function Avatar({ uk, size = 18, title }) {
  const u = userOf(uk);
  if (!u) return null;
  const label = title || u.name;
  return (
    <span
      className="mb-av"
      style={{ width: size, height: size, fontSize: Math.round(size * 0.42), background: u.bg, color: u.ink }}
      title={label}
      aria-label={label}
    >
      {u.initials}
    </span>
  );
}

/* ------------------------------ sticky note ------------------------------ */

function Note({ t, onOpen, justAdded, ghost }) {
  const st = STATES[t.state];
  const stage = STAGES.find((s) => s.k === t.stage);

  let foot = null;
  if (t.state === "draft") foot = <span>Tap to review &amp; run</span>;
  if (t.state === "inprocess")
    foot = (
      <span>
        {stage ? stage.label : "Queued"}
        <span className="mb-dots" aria-hidden="true"><span /><span /><span /></span>
      </span>
    );
  if (t.state === "review") {
    const p = passedRules(t).length;
    foot = (
      <span>
        <strong>{p}/{(t.rules || []).length}</strong>&nbsp;rules passed pre-test
      </span>
    );
  }
  if (t.state === "done") {
    const a = approvedRules(t).length;
    foot = <span>{a > 0 ? <><strong>{a}</strong>&nbsp;rule{a === 1 ? "" : "s"} live</> : "Closed — no rules deployed"}</span>;
  }

  return (
    <button
      className={"mb-note" + (justAdded ? " mb-justadded" : "") + (ghost ? " mb-ghost" : "")}
      style={{ "--rot": noteRot(t.id) + "deg", background: st.paper }}
      onClick={onOpen}
      aria-label={`Open report ${t.name}, status ${st.label}, created by ${userOf(t.createdBy)?.name || "unknown"}`}
    >
      <Tape rot={tapeRot(t.id)} busy={t.state === "inprocess"} />
      {t.state === "done" && <Stamp approved={approvedRules(t).length} />}
      <h4 className="mb-notetitle">{t.name}</h4>
      <div className="mb-notedomain">{hostname(t.url)}</div>
      <div className="mb-noterow">
        <span className="mb-env">{ENVS.find((e) => e.k === t.env)?.label || t.env}</span>
        <span className="mb-notedate">{fmtDate(t.created)}</span>
      </div>
      <div className="mb-notefoot">
        <span className="mb-footstatus">{foot}</span>
        <span className="mb-footpeople">
          <Avatar uk={t.createdBy} title={`Created by ${userOf(t.createdBy)?.name || "unknown"}`} />
          {t.reviewedBy && (
            <span className="mb-footrole" title={`Reviewed by ${userOf(t.reviewedBy)?.name || "unknown"}`}>
              ✓
              <Avatar uk={t.reviewedBy} title={`Reviewed by ${userOf(t.reviewedBy)?.name || "unknown"}`} />
            </span>
          )}
        </span>
      </div>
    </button>
  );
}

/* ------------------------------ ticket modal ------------------------------ */

function TicketModal({ t, onClose, onRun, onCancelRun, onDelete, onDecide, onFinish }) {
  const st = STATES[t.state];
  const passed = passedRules(t);
  const undecided = passed.filter((r) => !r.decision).length;
  const approved = approvedRules(t).length;
  const rejected = passed.filter((r) => r.decision === "reject").length;

  return (
    <div className="mb-card" style={{ background: st.paper }} onMouseDown={(e) => e.stopPropagation()}>
      <Tape rot={-1.6} busy={t.state === "inprocess"} />
      <button className="mb-close" onClick={onClose} aria-label="Close">×</button>

      <h2 className="mb-cardtitle">{t.name}</h2>
      <div className="mb-cardidrow">
        <span className="mb-pill" style={{ background: st.pill.bg, color: st.pill.ink }}>{st.label}</span>
        <span className="mb-cardid">created {fmtDate(t.created)}{t.doneAt ? ` · finished ${fmtDate(t.doneAt)}` : ""}</span>
      </div>

      <div className="mb-metagrid">
        <div className="mb-meta">
          <label>Reported page</label>
          <a href={t.url} target="_blank" rel="noreferrer">{t.url}</a>
        </div>
        <div className="mb-meta">
          <label>Environment</label>
          <div className="mb-val">{ENVS.find((e) => e.k === t.env)?.label || t.env}</div>
        </div>
        <div className="mb-meta">
          <label>Created by</label>
          <div className="mb-val mb-person">
            <Avatar uk={t.createdBy} size={20} />
            {userOf(t.createdBy)?.name || "—"}
          </div>
        </div>
        {t.reviewedBy && (
          <div className="mb-meta">
            <label>Reviewed by</label>
            <div className="mb-val mb-person">
              <Avatar uk={t.reviewedBy} size={20} />
              {userOf(t.reviewedBy)?.name}
            </div>
          </div>
        )}
        <div className="mb-meta">
          <label>Problem type</label>
          <div className="mb-val">{(t.targets || []).length ? "Specific ads reported" : "General ad clutter"}</div>
        </div>
      </div>

      {(t.focus || (t.targets || []).length > 0 || t.notes) && (
        <div className="mb-section">
          <h3>Ticket details</h3>
          {t.focus && (
            <div className="mb-meta" style={{ marginBottom: 10 }}>
              <label>Focus region (crawl scope)</label>
              <div className="mb-val">{t.focus}</div>
            </div>
          )}
          {(t.targets || []).length > 0 && (
            <div className="mb-meta" style={{ marginBottom: 10 }}>
              <label>Block these ads only</label>
              <div className="mb-chips">
                {t.targets.map((x) => <span className="mb-chip" key={x}>{x}</span>)}
              </div>
            </div>
          )}
          {t.notes && (
            <div className="mb-meta">
              <label>Notes</label>
              <p className="mb-notes-text">{t.notes}</p>
            </div>
          )}
        </div>
      )}

      {t.state !== "draft" && (
        <div className="mb-section">
          <h3>Pipeline</h3>
          <StageList current={t.state === "inprocess" ? t.stage : null} />
        </div>
      )}

      {(t.state === "review" || t.state === "done") && (
        <>
          <div className="mb-section">
            <h3>Sandbox render (mock)</h3>
            <MiniSandbox />
          </div>

          <div className="mb-section">
            <h3>{t.state === "review" ? "Candidate rules — your call" : "Rules"}</h3>
            {(t.rules || []).map((r, i) => (
              <div className={"mb-rule" + (r.status === "failed" ? " mb-failed" : "")} key={i}>
                <span className={"mb-badge " + (r.status === "passed" ? "mb-pass" : "mb-fail")}>
                  {r.status === "passed" ? "passed" : "failed"}
                </span>
                <span className="mb-ruletext">{r.text}</span>
                <span className="mb-conf">{Math.round(r.conf * 100)}%</span>

                {t.state === "review" && r.status === "passed" && (
                  <span className="mb-decide">
                    <button
                      className={"mb-tgl" + (r.decision === "approve" ? " mb-onA" : "")}
                      onClick={() => onDecide(i, "approve")}
                    >
                      Approve
                    </button>
                    <button
                      className={"mb-tgl" + (r.decision === "reject" ? " mb-onR" : "")}
                      onClick={() => onDecide(i, "reject")}
                    >
                      Reject
                    </button>
                  </span>
                )}
                {t.state === "done" && r.status === "passed" && (
                  <span className={"mb-decided-tag " + (r.decision === "approve" ? "mb-a" : "mb-r")}>
                    {r.decision === "approve" ? "✓ deployed" : "✕ rejected"}
                  </span>
                )}
                {r.status === "failed" && (
                  <span className="mb-reason">Auto-rejected by sandbox — {r.reason}</span>
                )}
              </div>
            ))}
          </div>
        </>
      )}

      <div className="mb-actions">
        {t.state === "draft" && (
          <>
            <button className="mb-btn mb-danger" onClick={onDelete}>Delete draft</button>
            <button className="mb-btn mb-primary" onClick={onRun}>Send to pipeline</button>
          </>
        )}
        {t.state === "inprocess" && (
          <>
            <span className="mb-tally">The system is working on this one.</span>
            <button className="mb-btn mb-ghost" onClick={onCancelRun}>Cancel run</button>
          </>
        )}
        {t.state === "review" && (
          <>
            <span className="mb-tally">
              {approved} approved · {rejected} rejected · {undecided} pending · reviewer: {CURRENT_USER.name}
            </span>
            <button className="mb-btn mb-primary" disabled={undecided > 0} onClick={onFinish}>
              {undecided > 0
                ? "Decide all rules to finish"
                : approved > 0
                ? `Finish review — deploy ${approved} rule${approved === 1 ? "" : "s"}`
                : "Finish review — deploy nothing"}
            </button>
          </>
        )}
        {t.state === "done" && (
          <span className="mb-tally">
            Locked — {approved > 0 ? `${approved} rule${approved === 1 ? "" : "s"} deployed.` : "closed with no rules deployed."}
            {t.reviewedBy ? ` Reviewed by ${userOf(t.reviewedBy)?.name}.` : ""}
          </span>
        )}
      </div>
    </div>
  );
}

/* ------------------------------ new report modal ------------------------------ */

function NewReportModal({ nextName, onCreate }) {
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
    <div className="mb-card" style={{ background: "#F5FAF0" }} onMouseDown={(e) => e.stopPropagation()}>
      <Tape rot={1.8} />
      <h2 className="mb-cardtitle">New report</h2>
      <div className="mb-cardid">A ticket starts as a draft, then runs crawl → rules → sandbox.</div>
      <div className="mb-cardid mb-person" style={{ marginTop: 8 }}>
        <Avatar uk={CURRENT_USER.k} size={18} />
        Created by {CURRENT_USER.name} (you)
      </div>

      <div className="mb-field">
        <label htmlFor="nr-name">Report name</label>
        <input id="nr-name" className="mb-input" value={name} onChange={(e) => setName(e.target.value)} />
      </div>

      <div className="mb-field">
        <label htmlFor="nr-url">Website link</label>
        <input
          id="nr-url"
          className="mb-input"
          placeholder="https://…"
          value={url}
          onChange={(e) => { setUrl(e.target.value); setErr(""); }}
        />
        {err && <div className="mb-err">{err}</div>}
      </div>

      <div className="mb-field">
        <label>Environment</label>
        <div className="mb-envtabs" role="group" aria-label="Crawl environment">
          {ENVS.map((e) => (
            <button
              key={e.k}
              type="button"
              className={"mb-envtab" + (env === e.k ? " mb-on" : "")}
              onClick={() => setEnv(e.k)}
            >
              {e.label}
            </button>
          ))}
        </div>
        <div className="mb-hint">The crawler and sandbox reuse this viewport &amp; user agent.</div>
      </div>

      <div className="mb-optdivider">Optional</div>

      <div className="mb-field">
        <label htmlFor="nr-focus">Focus on a specific part of the page</label>
        <input
          id="nr-focus"
          className="mb-input"
          placeholder="e.g. right sidebar, article footer"
          value={focus}
          onChange={(e) => setFocus(e.target.value)}
        />
      </div>

      <div className="mb-field">
        <label htmlFor="nr-targets">Block these ads only</label>
        <input
          id="nr-targets"
          className="mb-input"
          placeholder="e.g. popup overlay, video preroll (comma-separated)"
          value={targets}
          onChange={(e) => setTargets(e.target.value)}
        />
      </div>

      <div className="mb-field">
        <label htmlFor="nr-notes">Notes for the pipeline</label>
        <textarea
          id="nr-notes"
          className="mb-textarea"
          placeholder="Anything the reporter mentioned…"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
        />
      </div>

      <div className="mb-checkrow">
        <input id="nr-run" type="checkbox" checked={runNow} onChange={(e) => setRunNow(e.target.checked)} />
        <label htmlFor="nr-run" style={{ all: "unset", cursor: "pointer" }}>Send to pipeline right away</label>
      </div>

      <div className="mb-actions">
        <button className="mb-btn mb-primary" onClick={submit}>
          {runNow ? "Create & run" : "Create draft"}
        </button>
      </div>
    </div>
  );
}

/* ------------------------------ app ------------------------------ */

export default function ModerationBoard() {
  const [tickets, setTickets] = useState(SEED);
  const [modal, setModal] = useState(null); // {kind:'ticket',id} | {kind:'new'}
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState("review");
  const [userFilter, setUserFilter] = useState("all");
  const [refreshing, setRefreshing] = useState(false);
  const [lastSync, setLastSync] = useState(() => new Date());
  const [justAdded, setJustAdded] = useState(null);
  const timers = useRef({});
  const uid = useRef(100);
  const nextRpt = useRef(148);
  const syncedTeammate = useRef(false);

  const setT = (id, patch) =>
    setTickets((ts) =>
      ts.map((t) => (t.id === id ? { ...t, ...(typeof patch === "function" ? patch(t) : patch) } : t))
    );

  const pushTimer = (id, tid) => {
    (timers.current[id] = timers.current[id] || []).push(tid);
  };
  const clearFor = (id) => {
    (timers.current[id] || []).forEach(clearTimeout);
    timers.current[id] = [];
  };

  const advance = (id, stageIdx) => {
    if (stageIdx >= STAGES.length) {
      setT(id, (t) => ({ state: "review", stage: null, rules: makeRules(t) }));
      return;
    }
    setT(id, { state: "inprocess", stage: STAGES[stageIdx].k });
    pushTimer(id, setTimeout(() => advance(id, stageIdx + 1), 2700 + stageIdx * 500));
  };

  const runPipeline = (id) => { clearFor(id); advance(id, 0); setTab("review"); };
  const cancelRun = (id) => { clearFor(id); setT(id, { state: "draft", stage: null }); setTab("draft"); };

  // resume any seeded in-process tickets, clean everything on unmount
  useEffect(() => {
    tickets.forEach((t) => {
      if (t.state === "inprocess") {
        const idx = Math.max(0, STAGES.findIndex((s) => s.k === t.stage));
        pushTimer(t.id, setTimeout(() => advance(t.id, idx + 1), 3200));
      }
    });
    return () => Object.values(timers.current).flat().forEach(clearTimeout);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // esc closes modals
  useEffect(() => {
    if (!modal) return;
    const onKey = (e) => { if (e.key === "Escape") setModal(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [modal]);

  const decide = (id, ruleIdx, val) =>
    setT(id, (t) => ({
      rules: t.rules.map((r, i) =>
        i === ruleIdx ? { ...r, decision: r.decision === val ? undefined : val } : r
      ),
    }));

  const finishReview = (id) => {
    setT(id, { state: "done", doneAt: todayISO(), reviewedBy: CURRENT_USER.k });
    setTab("done");
  };

  const refreshBoard = () => {
    if (refreshing) return;
    setRefreshing(true);
    // stub: in the real CMS this re-fetches the ticket list from the API
    setTimeout(() => {
      if (!syncedTeammate.current) {
        // simulate a ticket another moderator created since our last sync
        syncedTeammate.current = true;
        const id = "u" + uid.current++;
        setTickets((ts) => [
          {
            id,
            name: "RPT-2026-0143",
            url: "https://baomoi.com",
            env: "android",
            state: "review",
            created: todayISO(),
            createdBy: "khoa.pham",
            focus: "",
            targets: [],
            notes: "Synced from another moderator's session.",
            rules: [
              { text: "baomoi.com##.bm-ads", status: "passed", conf: 0.9 },
              { text: "||media1.admicro.vn^$third-party", status: "passed", conf: 0.93 },
              { text: "baomoi.com##div[data-zone]", status: "passed", conf: 0.8 },
              { text: "baomoi.com##.story__meta", status: "failed", conf: 0.36, reason: "Hid article bylines." },
            ],
          },
          ...ts,
        ]);
        setJustAdded(id);
        setTimeout(() => setJustAdded(null), 600);
      }
      setLastSync(new Date());
      setRefreshing(false);
    }, 850);
  };

  const deleteTicket = (id) => {
    clearFor(id);
    setTickets((ts) => ts.filter((t) => t.id !== id));
    setModal(null);
  };

  const createTicket = (data, runNow) => {
    const id = "u" + uid.current++;
    nextRpt.current++;
    setTickets((ts) => [{ id, state: "draft", created: todayISO(), createdBy: CURRENT_USER.k, ...data }, ...ts]);
    setModal(null);
    setJustAdded(id);
    setTab("draft");
    setTimeout(() => setJustAdded(null), 600);
    if (runNow) setTimeout(() => runPipeline(id), 350);
  };

  const q = query.trim().toLowerCase();
  const filtered = useMemo(
    () =>
      tickets.filter(
        (t) =>
          (userFilter === "all" || t.createdBy === userFilter || t.reviewedBy === userFilter) &&
          (!q || t.name.toLowerCase().includes(q) || t.url.toLowerCase().includes(q))
      ),
    [tickets, q, userFilter]
  );
  const byState = useMemo(() => {
    const m = Object.fromEntries(STATE_ORDER.map((k) => [k, []]));
    filtered.forEach((t) => m[t.state] && m[t.state].push(t));
    return m;
  }, [filtered]);
  const ghosts = byState.inprocess || [];
  const items = tab === "review" ? [...(byState.review || []), ...ghosts] : byState[tab] || [];
  const realCount = tab === "review" ? (byState.review || []).length : items.length;

  const openTicket = modal?.kind === "ticket" ? tickets.find((t) => t.id === modal.id) : null;

  return (
    <div className="mb-app">

      <header className="mb-topbar">
        <div className="mb-brand">
          <h1>Moderation Board</h1>
          <span className="mb-sub">Cốc Cốc ad-report pipeline · internal CMS</span>
          <span className="mb-userchip">
            <Avatar uk={CURRENT_USER.k} size={22} />
            {CURRENT_USER.name}
          </span>
          <span className="mb-admin">ADMIN</span>
        </div>
        <div className="mb-topright">
          <input
            className="mb-search"
            placeholder="Search name or URL…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Search reports"
          />
          <select
            className="mb-select"
            value={userFilter}
            onChange={(e) => setUserFilter(e.target.value)}
            aria-label="Filter by user"
          >
            <option value="all">All users</option>
            {USERS.map((u) => (
              <option key={u.k} value={u.k}>{u.name}</option>
            ))}
          </select>
          <button className="mb-newbtn" onClick={() => setModal({ kind: "new" })}>+ New report</button>
        </div>
      </header>

      <div className="mb-tabrow">
        <nav className="mb-tabs" role="tablist" aria-label="Ticket states">
          {TABS.map((k, i) => (
            <button
              key={k}
              role="tab"
              aria-selected={tab === k}
              className={"mb-tab" + (tab === k ? " mb-tabon" : "")}
              style={{ "--paper": STATES[k].paper, "--tabrot": TAB_ROTS[i] }}
              onClick={() => setTab(k)}
            >
              {STATES[k].label}
              <span className="mb-tabcount">
                {(byState[k] || []).length}
                {k === "review" && ghosts.length > 0 ? ` +${ghosts.length}` : ""}
              </span>
            </button>
          ))}
        </nav>
        <div className="mb-refreshwrap">
          <button
            className={"mb-refresh" + (refreshing ? " mb-spinning" : "")}
            onClick={refreshBoard}
            disabled={refreshing}
            title={`Refresh board — last synced ${lastSync.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`}
            aria-label="Refresh board"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M21 12a9 9 0 1 1-2.64-6.36" />
              <path d="M21 3v6h-6" />
            </svg>
          </button>
        </div>
      </div>

      <div className="mb-subhead">
        <strong>{STATES[tab].sub}</strong>
        <span>
          · {realCount} ticket{realCount === 1 ? "" : "s"}
          {tab === "review" && ghosts.length > 0 ? ` · ${ghosts.length} incoming from the pipeline` : ""}
          {userFilter !== "all" ? ` · involving ${userOf(userFilter)?.name}` : ""}
          {q ? ` · matching “${query.trim()}”` : ""}
        </span>
      </div>

      <main className="mb-grid">
        {items.length === 0 && (
          <div className="mb-empty">
            {q || userFilter !== "all" ? "No tickets match these filters." : STATES[tab].empty}
          </div>
        )}
        {items.map((t) => (
          <Note
            key={t.id}
            t={t}
            ghost={t.state === "inprocess"}
            justAdded={justAdded === t.id}
            onOpen={() => setModal({ kind: "ticket", id: t.id })}
          />
        ))}
      </main>

      <div className="mb-flow">crawl → generate → sandbox → moderator review · rules deploy only after approval</div>

      {modal && (
        <div className="mb-overlay" onMouseDown={() => setModal(null)}>
          {modal.kind === "new" && (
            <NewReportModal
              nextName={`RPT-2026-0${nextRpt.current}`}
              onCreate={createTicket}
            />
          )}
          {openTicket && (
            <TicketModal
              t={openTicket}
              onClose={() => setModal(null)}
              onRun={() => runPipeline(openTicket.id)}
              onCancelRun={() => cancelRun(openTicket.id)}
              onDelete={() => deleteTicket(openTicket.id)}
              onDecide={(ri, val) => decide(openTicket.id, ri, val)}
              onFinish={() => finishReview(openTicket.id)}
            />
          )}
        </div>
      )}
    </div>
  );
}
