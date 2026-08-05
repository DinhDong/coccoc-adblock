import { useState, useEffect, useMemo, useRef } from "react";
import { STATE_ORDER, STAGES, CURRENT_USER } from "./constants.js";
import { nowISO, todayISO, makeRules } from "./utils.js";
import { SEED } from "./data/seed.js";
import Layout from "./components/Layout.jsx";
import ReportDetail, { clearReportImageCache } from "./components/ReportDetail.jsx";
import NewReportModal from "./components/NewReportModal.jsx";
import DuplicateTargetModal from "./components/DuplicateTargetModal.jsx";
import Reports from "./pages/Reports.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import Trend from "./pages/Trend.jsx";
import Performance from "./pages/Performance.jsx";
import RuleLibrary from "./pages/RuleLibrary.jsx";
import TokenUsage from "./pages/TokenUsage.jsx";

export default function App() {
  const [tickets, setTickets] = useState(SEED);
  const [modal, setModal] = useState(null); // {kind:'ticket',id} | {kind:'new'}
  const [query, setQuery] = useState("");
  const [view, setView] = useState("reports"); // "reports" | "dashboard" | "trend" | "performance"
  const [tab, setTab] = useState("review");
  const [userFilter, setUserFilter] = useState("all");
  const [refreshing, setRefreshing] = useState(false);
  const [rules, setRules] = useState([]);
  const [rulesLoading, setRulesLoading] = useState(false);
  const [usage, setUsage] = useState(null);
  const [usageLoading, setUsageLoading] = useState(false);
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

  const loadRules = async () => {
    setRulesLoading(true);
    try {
      const response = await fetch(`${backendUrl}/api/rules`);
      if (!response.ok) throw new Error(`Rule load failed: ${response.status}`);
      const data = await response.json();
      setRules(data.rules || []);
    } catch (error) {
      console.error("Failed to load rules from backend", error);
    } finally {
      setRulesLoading(false);
    }
  };

  const loadUsage = async () => {
    setUsageLoading(true);
    try {
      const response = await fetch(`${backendUrl}/api/usage`);
      if (!response.ok) throw new Error(`Usage load failed: ${response.status}`);
      setUsage(await response.json());
    } catch (error) {
      console.error("Failed to load token usage from backend", error);
    } finally {
      setUsageLoading(false);
    }
  };

  const loadTickets = async () => {
    try {
      const response = await fetch(`${backendUrl}/api/tickets`);
      if (!response.ok) throw new Error(`Ticket load failed: ${response.status}`);
      const data = await response.json();
      setTickets(data.tickets || []);
      setLastSync(new Date());
    } catch (error) {
      console.error("Failed to load tickets from backend", error);
    }
  };

  const updateTicketStatusInBackend = async (id, status) => {
    try {
      await fetch(`${backendUrl}/api/tickets/${encodeURIComponent(id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      });
    } catch (error) {
      console.error(`Failed to update ticket ${id} status`, error);
    }
  };

  const advance = (id, stageIdx) => {
    if (stageIdx >= STAGES.length) {
      setT(id, (t) => ({ state: "review", stage: null, rules: makeRules(t), reviewReadyAt: nowISO() }));
      return;
    }
    setT(id, { state: "inprocess", stage: STAGES[stageIdx].k });
    pushTimer(id, setTimeout(() => advance(id, stageIdx + 1), 2700 + stageIdx * 500));
  };

  // Ask the backend whether this link has been run before, so the choice can
  // be put to the user before anything is crawled or any tokens are spent.
  const findDuplicates = async (id, url) => {
    try {
      const query = new URLSearchParams({ url, exclude: id });
      const response = await fetch(`${backendUrl}/api/tickets/duplicates?${query}`);
      if (!response.ok) return [];
      const data = await response.json();
      return data.duplicates || [];
    } catch (error) {
      console.error("Duplicate check failed", error);
      return [];
    }
  };

  const startRun = async (id, ticketOverride) => {
    const ticket = ticketOverride || tickets.find((t) => t.id === id);
    if (!ticket) {
      console.error(`Cannot run pipeline: ticket ${id} not found`);
      return;
    }

    const duplicates = await findDuplicates(id, ticket.url);
    if (duplicates.length > 0) {
      setModal({ kind: "duplicate", id, ticket, duplicates });
      return;
    }
    await runPipeline(id, ticket);
  };

  // ticketOverride lets a caller run a ticket that is not in `tickets` yet.
  // A just-created ticket never is: setTickets/loadTickets only schedule a
  // re-render, so the `tickets` captured by this closure is still the
  // pre-create list and the lookup below would miss.
  const runPipeline = async (id, ticketOverride, duplicateChoice) => {
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
          duplicate_choice: duplicateChoice,
        }),
      });
    } catch (error) {
      console.error(`Failed to run pipeline for ticket ${id}`, error);
    }

    // A re-run replaces the screenshots, so drop any cached presigned URLs
    // for this report before the modal is opened again.
    clearReportImageCache(id);
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

  // Rules are only needed by the library, and they go stale whenever a run
  // finishes or a decision changes — so refetch on entry rather than once.
  useEffect(() => {
    if (view === "library") loadRules();
    if (view === "tokens") loadUsage();
  }, [view]);

  // Toggling the active choice clears the decision, so `next` may be null.
  // Decisions live in the DB keyed by rule text — without persisting, the next
  // loadTickets() would rebuild `rules` from the server and drop them.
  const decide = async (id, ruleIdx, val) => {
    const ticket = tickets.find((t) => t.id === id);
    const rule = ticket?.rules?.[ruleIdx];
    if (!rule) return;

    const next = rule.decision === val ? null : val;

    setT(id, (t) => ({
      rules: t.rules.map((r, i) => (i === ruleIdx ? { ...r, decision: next ?? undefined } : r)),
    }));

    try {
      const response = await fetch(`${backendUrl}/api/tickets/${encodeURIComponent(id)}/decisions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rule: rule.text, decision: next, decided_by: CURRENT_USER.k }),
      });
      if (!response.ok) throw new Error(`decision save failed ${response.status}`);
    } catch (error) {
      console.error(`Failed to save decision for ${id}`, error);
      // Put the row back the way it was so the UI does not claim a decision
      // the backend never stored.
      setT(id, (t) => ({
        rules: t.rules.map((r, i) => (i === ruleIdx ? { ...r, decision: rule.decision } : r)),
      }));
    }
  };

  const finishReview = async (id) => {
    // The backend refuses to close a report with undecided rules; surface that
    // rather than leaving the board claiming a state the database rejected.
    try {
      const response = await fetch(`${backendUrl}/api/tickets/${encodeURIComponent(id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "done" }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.error || `finish failed ${response.status}`);
      }
    } catch (error) {
      console.error(`Failed to finish review for ${id}`, error);
      window.alert(`Could not finish this review: ${error.message}`);
      await loadTickets();
      return;
    }

    setT(id, { state: "done", doneAt: todayISO(), reviewedAt: nowISO(), reviewedBy: CURRENT_USER.k });
    setModal(null);
    setTab("done");
  };

  const saveTicketEdits = async (id, data) => {
    try {
      const response = await fetch(`${backendUrl}/api/tickets/${encodeURIComponent(id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.error || `edit failed ${response.status}`);
      }
      setModal(null);
      await loadTickets();
    } catch (error) {
      console.error(`Failed to edit ticket ${id}`, error);
      window.alert(`Could not save changes: ${error.message}`);
    }
  };

  // Rule add/edit/delete all rewrite the stored rule set, so both the board
  // and the library have to be pulled again to stay truthful.
  const mutateRule = async (id, method, body, failLabel) => {
    try {
      const response = await fetch(`${backendUrl}/api/tickets/${encodeURIComponent(id)}/rules`, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error || `${failLabel} failed ${response.status}`);
      }
      await Promise.all([loadTickets(), loadRules()]);
      return true;
    } catch (error) {
      console.error(`${failLabel} failed for ${id}`, error);
      window.alert(`${failLabel} failed: ${error.message}`);
      return false;
    }
  };

  const addRule = (id, rule) => mutateRule(id, "POST", { rule }, "Add rule");
  const editRule = (id, rule, newRule) =>
    mutateRule(id, "PATCH", { rule, new_rule: newRule }, "Edit rule");
  const removeRule = (id, rule) => mutateRule(id, "DELETE", { rule }, "Delete rule");

  // preview:true asks the backend what the merged rule would be without
  // changing anything, so the moderator confirms real text rather than a guess.
  const mergeRulePair = async (items, preview) => {
    try {
      const response = await fetch(`${backendUrl}/api/rules/merge`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rules: items, preview: !!preview }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || `merge failed ${response.status}`);
      if (!preview) await Promise.all([loadTickets(), loadRules()]);
      return body;
    } catch (error) {
      console.error("Merge failed", error);
      window.alert(`These rules cannot be merged:\n\n${error.message}`);
      return null;
    }
  };

  // Sandbox runs take tens of seconds (real page load per rule), so this has
  // no timeout of its own — the library shows a "Testing…" state meanwhile.
  const testRules = async (items) => {
    try {
      const response = await fetch(`${backendUrl}/api/rules/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rules: items }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || `test failed ${response.status}`);
      return body.result;
    } catch (error) {
      console.error("Rule test failed", error);
      window.alert(`Could not test these rules:

${error.message}`);
      return null;
    }
  };

  const bulkRemoveRules = async (items) => {
    try {
      const response = await fetch(`${backendUrl}/api/rules/bulk-delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rules: items }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || `bulk delete failed ${response.status}`);
      if (body.failed?.length) {
        window.alert(
          `Deleted ${body.deleted}. ${body.failed.length} could not be removed:\n` +
            body.failed.map((f) => `  ${f.rule} — ${f.error}`).join("\n")
        );
      }
      await Promise.all([loadTickets(), loadRules()]);
    } catch (error) {
      console.error("Bulk delete failed", error);
      window.alert(`Bulk delete failed: ${error.message}`);
    }
  };

  const refreshBoard = async () => {
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
    const ticket = tickets.find((t) => t.id === id);
    const ruleCount = (ticket?.rules || []).length;

    // Reviewed and completed reports carry rules and decisions, so deleting
    // one is not the same low-stakes action as discarding a draft.
    const warning =
      ruleCount > 0
        ? `\n\nThis also removes ${ruleCount} rule${ruleCount === 1 ? "" : "s"} and any decisions on them.`
        : "";
    if (!window.confirm(`Delete report ${id}?${warning}\n\nThis cannot be undone.`)) return;

    clearFor(id);
    try {
      const response = await fetch(`${backendUrl}/api/tickets/${encodeURIComponent(id)}`, {
        method: "DELETE",
      });
      if (!response.ok) {
        throw new Error(`delete failed ${response.status}`);
      }
    } catch (error) {
      // Keep the row on screen — dropping it here would imply a delete that
      // never happened.
      console.error(`Failed to delete ticket ${id}`, error);
      window.alert(`Could not delete ${id}: ${error.message}`);
      return;
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
      await startRun(id, ticketPayload);
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
  const editTicket = modal?.kind === "edit" ? tickets.find((t) => t.id === modal.id) : null;

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
      {view === "library" && (
        <RuleLibrary
          rules={rules}
          loading={rulesLoading}
          onRefresh={loadRules}
          onOpenReport={(id) => setModal({ kind: "ticket", id })}
          onEditRule={editRule}
          onDeleteRule={removeRule}
          onBulkDelete={bulkRemoveRules}
          onMergePreview={(items) => mergeRulePair(items, true)}
          onMergeRules={(items) => mergeRulePair(items, false)}
          onTestRules={testRules}
        />
      )}
      {view === "tokens" && (
        <TokenUsage usage={usage} loading={usageLoading} onRefresh={loadUsage} />
      )}

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
          {modal.kind === "duplicate" && (
            <DuplicateTargetModal
              url={modal.ticket.url}
              duplicates={modal.duplicates}
              onChoose={(choice) => {
                setModal(null);
                runPipeline(modal.id, modal.ticket, choice);
              }}
              onClose={() => setModal(null)}
            />
          )}
          {modal.kind === "edit" && editTicket && (
            <NewReportModal
              ticket={editTicket}
              onSave={(data) => saveTicketEdits(editTicket.id, data)}
              onClose={() => setModal({ kind: "ticket", id: editTicket.id })}
            />
          )}
          {openTicket && (
            <ReportDetail
              t={openTicket}
              onClose={() => setModal(null)}
              onRun={() => startRun(openTicket.id)}
              onCancelRun={() => cancelRun(openTicket.id)}
              onDelete={() => deleteTicket(openTicket.id)}
              onDecide={(ri, val) => decide(openTicket.id, ri, val)}
              onFinish={() => finishReview(openTicket.id)}
              onEdit={() => setModal({ kind: "edit", id: openTicket.id })}
              onAddRule={(rule) => addRule(openTicket.id, rule)}
              onEditRule={(rule, newRule) => editRule(openTicket.id, rule, newRule)}
              onDeleteRule={(rule) => removeRule(openTicket.id, rule)}
              onMergePreview={(items) => mergeRulePair(items, true)}
              onMergeRules={(items) => mergeRulePair(items, false)}
            />
          )}
        </div>
      )}
    </Layout>
  );
}
