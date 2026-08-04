import { useState, useEffect, useMemo, useRef } from "react";
import { STATE_ORDER, STAGES, CURRENT_USER } from "./constants.js";
import { nowISO, todayISO, makeRules } from "./utils.js";
import { SEED } from "./data/seed.js";
import Layout from "./components/Layout.jsx";
import ReportDetail from "./components/ReportDetail.jsx";
import NewReportModal from "./components/NewReportModal.jsx";
import Reports from "./pages/Reports.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import Trend from "./pages/Trend.jsx";
import Performance from "./pages/Performance.jsx";

export default function App() {
  const [tickets, setTickets] = useState(SEED);
  const [modal, setModal] = useState(null); // {kind:'ticket',id} | {kind:'new'}
  const [query, setQuery] = useState("");
  const [view, setView] = useState("reports"); // "reports" | "dashboard" | "trend" | "performance"
  const [tab, setTab] = useState("review");
  const [userFilter, setUserFilter] = useState("all");
  const [refreshing, setRefreshing] = useState(false);
  const [lastSync, setLastSync] = useState(() => new Date());
  const [, setNowTick] = useState(0);
  const timers = useRef({});
  const nextRpt = useRef(148);
  const syncedTeammate = useRef(false);

  const makeTicketId = () =>
    window.crypto?.randomUUID?.() ?? `u${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;

  const setT = (id, patch) =>
    setTickets((ts) =>
      ts.map((t) => (t.id === id ? { ...t, ...(typeof patch === "function" ? patch(t) : patch) } : t))
    );

  const pushTimer = (id, tid) => { (timers.current[id] = timers.current[id] || []).push(tid); };
  const clearFor = (id) => { (timers.current[id] || []).forEach(clearTimeout); timers.current[id] = []; };

  const advance = (id, stageIdx) => {
    if (stageIdx >= STAGES.length) {
      setT(id, (t) => ({ state: "review", stage: null, rules: makeRules(t), reviewReadyAt: nowISO() }));
      return;
    }
    setT(id, { state: "inprocess", stage: STAGES[stageIdx].k });
    pushTimer(id, setTimeout(() => advance(id, stageIdx + 1), 2700 + stageIdx * 500));
  };

  // ticketOverride lets a caller run a ticket that is not in `tickets` yet.
  // A just-created ticket never is: setTickets/loadTickets only schedule a
  // re-render, so the `tickets` captured by this closure is still the
  // pre-create list and the lookup below would miss.
  const runPipeline = async (id, ticketOverride) => {
    clearFor(id);
    setT(id, { runStartedAt: nowISO(), state: "inprocess", stage: "crawl" });

    const ticket = ticketOverride || tickets.find((t) => t.id === id);
    if (!ticket) {
      console.error(`Cannot run pipeline: ticket ${id} not found`);
      setT(id, { state: "draft", stage: null, runStartedAt: null });
      return;
    }

    try {
      await fetch(`${backendUrl}/api/tickets/${encodeURIComponent(id)}/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: ticket.url,
          environment: ticket.env,
          ticket_context: {
            focus: ticket.focus,
            targets: ticket.targets,
            notes: ticket.notes,
            createdBy: ticket.createdBy,
            created: ticket.created,
          },
          focus_region: ticket.focus,
        }),
      });
    } catch (error) {
      console.error(`Failed to run pipeline for ticket ${id}`, error);
    }

    await refreshBoard();
    setTab("review");
  };
  const cancelRun = async (id) => { clearFor(id); setT(id, { state: "draft", stage: null, runStartedAt: null }); await updateTicketStatusInBackend(id, "draft"); setTab("draft"); };

  // resume seeded in-process reports; clear all timers on unmount
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

  useEffect(() => {
    if (!modal) return;
    const onKey = (e) => { if (e.key === "Escape") setModal(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [modal]);

  // re-render every 10s so the "last updated" text stays honest
  useEffect(() => {
    const t = setInterval(() => setNowTick((x) => x + 1), 10000);
    return () => clearInterval(t);
  }, []);

  const decide = (id, ruleIdx, val) =>
    setT(id, (t) => ({
      rules: t.rules.map((r, i) => (i === ruleIdx ? { ...r, decision: r.decision === val ? undefined : val } : r)),
    }));

  const finishReview = (id) => {
    setT(id, { state: "done", doneAt: todayISO(), reviewedAt: nowISO(), reviewedBy: CURRENT_USER.k });
    setTab("done");
  };

  const refreshBoard = () => {
    if (refreshing) return;
    setRefreshing(true);
    // stub: in the real CMS this re-fetches the report list from the API
    setTimeout(() => {
      if (!syncedTeammate.current) {
        // simulate a report another moderator created since our last sync
        syncedTeammate.current = true;
        const id = "u" + uid.current++;
        setTickets((ts) => [
          {
            id, name: "RPT-2026-0143", url: "https://baomoi.com", env: "android",
            state: "review", created: todayISO(), createdBy: "hien.khuong",
            runStartedAt: new Date(Date.now() - 22 * 60000).toISOString(),
            reviewReadyAt: new Date(Date.now() - 20 * 60000).toISOString(),
            focus: "", targets: [], notes: "Synced from another moderator's session.",
            rules: [
              { text: "baomoi.com##.bm-ads", status: "passed", conf: 0.9 },
              { text: "||media1.admicro.vn^$third-party", status: "passed", conf: 0.93 },
              { text: "baomoi.com##div[data-zone]", status: "passed", conf: 0.8 },
              { text: "baomoi.com##.story__meta", status: "failed", conf: 0.36, reason: "Hid article bylines." },
            ],
          },
          ...ts,
        ]);
      }
      setLastSync(new Date());
      setRefreshing(false);
    }, 850);
  };

  const deleteTicket = async (id) => {
    clearFor(id);
    try {
      const response = await fetch(`${backendUrl}/api/tickets/${encodeURIComponent(id)}`, {
        method: "DELETE",
      });
      if (!response.ok) {
        throw new Error(`delete failed ${response.status}`);
      }
    } catch (error) {
      console.error(`Failed to delete ticket ${id}`, error);
    }
    setTickets((ts) => ts.filter((t) => t.id !== id));
    setModal(null);
  };

  const createTicket = async (data, runNow) => {
    // Use the user-provided name as the stable report id when available,
    // otherwise fall back to a generated id. This prevents the report from
    // being later replaced by a UUID identifier.
    const id = (data && data.name && data.name.trim()) ? data.name.trim() : makeTicketId();
    nextRpt.current++;
    setTickets((ts) => [{ id, state: "draft", created: todayISO(), createdBy: CURRENT_USER.k, ...data }, ...ts]);
    setModal(null);
    setTab("draft");

    try {
      await fetch("http://127.0.0.1:5000/api/tickets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(ticketPayload),
      });
    } catch (error) {
      console.error("Failed to save ticket to backend", error);
    }

    if (runNow) {
      // Hand the payload straight to runPipeline — it cannot look the ticket
      // up by id yet, and awaiting keeps the run tied to this call so a
      // failure surfaces instead of leaving the ticket sitting as a draft.
      await runPipeline(id, ticketPayload);
    }
  };

  const q = query.trim().toLowerCase();
  const filtered = useMemo(
    () =>
      tickets
        .filter(
          (t) =>
            (userFilter === "all" || t.createdBy === userFilter || t.reviewedBy === userFilter) &&
            (!q || t.name.toLowerCase().includes(q) || t.url.toLowerCase().includes(q))
        )
        .sort(
          (a, b) =>
            b.created.localeCompare(a.created) ||
            parseInt(b.id.slice(1), 10) - parseInt(a.id.slice(1), 10)
        ),
    [tickets, q, userFilter]
  );
  const byState = useMemo(() => {
    const m = Object.fromEntries(STATE_ORDER.map((k) => [k, []]));
    filtered.forEach((t) => m[t.state] && m[t.state].push(t));
    return m;
  }, [filtered]);

  const ghosts = byState.inprocess || [];
  // Failed runs need a moderator to look at them, so they surface in Review
  // alongside in-process rows rather than getting a tab of their own.
  const failures = byState.failed || [];
  const items =
    tab === "all" ? filtered
    : tab === "review" ? [...failures, ...(byState.review || []), ...ghosts]
    : byState[tab] || [];

  const openTicket = modal?.kind === "ticket" ? tickets.find((t) => t.id === modal.id) : null;

  return (
    <Layout view={view} setView={setView} lastSync={lastSync}>
      {view === "reports" && (
        <Reports
          items={items}
          byState={byState}
          filtered={filtered}
          ghosts={ghosts}
          tab={tab}
          setTab={setTab}
          query={query}
          setQuery={setQuery}
          userFilter={userFilter}
          setUserFilter={setUserFilter}
          lastSync={lastSync}
          refreshing={refreshing}
          onRefresh={refreshBoard}
          onOpen={(id) => setModal({ kind: "ticket", id })}
          onNew={() => setModal({ kind: "new" })}
        />
      )}
      {view === "dashboard" && (
        <Dashboard tickets={tickets} goReview={() => { setView("reports"); setTab("review"); }} />
      )}
      {view === "trend" && <Trend tickets={tickets} />}
      {view === "performance" && <Performance tickets={tickets} />}

      {/* ---------- modals ---------- */}
      {modal && (
        <div className="ad-overlay" onMouseDown={() => setModal(null)}>
          {modal.kind === "new" && (
            <NewReportModal
              nextName={`RPT-2026-0${nextRpt.current}`}
              onCreate={createTicket}
              onClose={() => setModal(null)}
            />
          )}
          {openTicket && (
            <ReportDetail
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
    </Layout>
  );
}
