import { useState, useEffect, useMemo, useRef } from "react";
import { STATE_ORDER, CURRENT_USER } from "./constants.js";
import { nowISO, todayISO } from "./utils.js";
import Layout from "./components/Layout.jsx";
import ReportDetail, { clearReportImageCache } from "./components/ReportDetail.jsx";
import NewReportModal from "./components/NewReportModal.jsx";
import Reports from "./pages/Reports.jsx";
import Performance from "./pages/Performance.jsx";
import RuleLibrary from "./pages/RuleLibrary.jsx";
import LiveRules from "./pages/LiveRules.jsx";
import TokenUsage from "./pages/TokenUsage.jsx";
import Playground from "./pages/Playground.jsx";

export default function App() {
  const [tickets, setTickets] = useState([]);
  const [modal, setModal] = useState(null); // {kind:'ticket',id} | {kind:'new'}
  const [query, setQuery] = useState("");
  const [view, setView] = useState("reports"); // "reports" | "library" | "trend" | "performance" | "tokens" | "playground"
  const [statusFilter, setStatusFilter] = useState("all");
  const [refreshing, setRefreshing] = useState(false);
  const [rules, setRules] = useState([]);
  const [rulesLoading, setRulesLoading] = useState(false);
  const [usage, setUsage] = useState(null);
  const [usageLoading, setUsageLoading] = useState(false);
  // Rules handed over from the library so the playground can run them on open.
  const [playgroundSeed, setPlaygroundSeed] = useState(null);
  const [lastSync, setLastSync] = useState(() => new Date());
  // "localhost" resolves to ::1 before 127.0.0.1 on Windows, but Flask binds
  // IPv4 only by default. Chrome tries the IPv6 address first and the request
  // dies with ERR_CONNECTION_RESET, which surfaces as a CORS error and makes
  // every button feel intermittently broken. Address IPv4 directly instead.
  const backendHost =
    window.location.hostname === "localhost" ? "127.0.0.1" : window.location.hostname;
  const backendUrl = (
    import.meta.env.VITE_BACKEND_URL ||
    `${window.location.protocol}//${backendHost}:5000`
  ).replace(/\/$/, "");
  const [, setNowTick] = useState(0);
  // The id a new report will get. Fetched from the backend when the form
  // opens rather than counted locally: a local counter restarted at 148 on
  // every page load and handed out ids that already existed.
  const [nextId, setNextId] = useState("");

  const makeTicketId = () =>
    window.crypto?.randomUUID?.() ?? `u${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;

  const setT = (id, patch) =>
    setTickets((ts) =>
      ts.map((t) => (t.id === id ? { ...t, ...(typeof patch === "function" ? patch(t) : patch) } : t))
    );

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
      return data.tickets || [];
    } catch (error) {
      console.error("Failed to load tickets from backend", error);
      return null;
    }
  };

  const updateTicketStatusInBackend = async (id, status) => {
    try {
      const response = await fetch(`${backendUrl}/api/tickets/${encodeURIComponent(id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.error || `status update failed ${response.status}`);
      }
      return true;
    } catch (error) {
      console.error(`Failed to update ticket ${id} status`, error);
      return false;
    }
  };

  // Runs start immediately. There used to be a prompt here when the same site
  // had been crawled before, asking whether to keep or discard its rules —
  // but duplicate rules are no longer dropped, they are generated, flagged
  // and put to the moderator on the review screen. Asking up front made
  // people decide before they could see what the duplicates actually were.
  const startRun = async (id, ticketOverride) => {
    const ticket = ticketOverride || tickets.find((t) => t.id === id);
    if (!ticket) {
      console.error(`Cannot run pipeline: ticket ${id} not found`);
      return;
    }
    await runPipeline(id, ticket);
  };

  // ticketOverride lets a caller run a ticket that is not in `tickets` yet.
  // A just-created ticket never is: setTickets/loadTickets only schedule a
  // re-render, so the `tickets` captured by this closure is still the
  // pre-create list and the lookup below would miss.
  const runPipeline = async (id, ticketOverride, duplicateChoice) => {
    // Optimistically queued, not running — the worker has not claimed it yet.
    setT(id, { runStartedAt: nowISO(), state: "queued", stage: null });

    const ticket = ticketOverride || tickets.find((t) => t.id === id);
    if (!ticket) {
      console.error(`Cannot run pipeline: ticket ${id} not found`);
      setT(id, { state: "draft", stage: null, runStartedAt: null });
      return;
    }

    try {
      const response = await fetch(`${backendUrl}/api/tickets/${encodeURIComponent(id)}/run`, {
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

      // 202 means the run was accepted onto the worker queue and is now
      // running in the background; the outcome arrives via polling.
      const body = await response.json().catch(() => ({}));
      if (!response.ok || body.ok === false) {
        // 409 means a run for this report is already queued or running. The
        // ticket really is in progress, so keep showing that and attach a
        // watcher rather than resetting it to draft.
        if (response.status === 409) {
          clearReportImageCache(id);
          watchRun(id);
          return;
        }
        throw new Error(body.error || `run rejected (${response.status})`);
      }
    } catch (error) {
      console.error(`Failed to queue pipeline for ticket ${id}`, error);
      // Prefer database truth over guessing: only fall back to draft when the
      // reload itself failed.
      const latest = await loadTickets();
      if (!latest) {
        setT(id, { state: "draft", stage: null, runStartedAt: null });
      }
      window.alert(`Could not start this run: ${error.message}`);
      return;
    }

    // A re-run replaces the screenshots, so drop any cached presigned URLs
    // for this report before the modal is opened again.
    clearReportImageCache(id);
    await loadTickets();
    watchRun(id);
  };

  // The run outlives this request now, so follow it by polling the ticket
  // until the backend moves it out of an in-progress state. Kept in a ref so
  // a second run on the same report replaces its watcher instead of stacking.
  const watchers = useRef({});

  const watchRun = (id) => {
    if (watchers.current[id]) clearInterval(watchers.current[id]);

    const started = Date.now();
    watchers.current[id] = setInterval(async () => {
      // Give up after 10 minutes rather than polling a dead backend forever.
      if (Date.now() - started > 10 * 60 * 1000) {
        clearInterval(watchers.current[id]);
        delete watchers.current[id];
        return;
      }

      try {
        const response = await fetch(`${backendUrl}/api/tickets`);
        if (!response.ok) return;
        const data = await response.json();
        const fresh = (data.tickets || []).find((t) => t.id === id);
        setTickets(data.tickets || []);
        setLastSync(new Date());

        // Keep polling while the report is queued as well as running.
        if (fresh && fresh.state !== "inprocess" && fresh.state !== "queued") {
          clearInterval(watchers.current[id]);
          delete watchers.current[id];
        }
      } catch (error) {
        // Transient backend hiccup — keep polling until the timeout.
        console.debug("run poll failed", error);
      }
    }, 4000);
  };
  const cancelRun = async (id) => {
    const updated = await updateTicketStatusInBackend(id, "draft");
    if (!updated) {
      window.alert(`Could not reset ${id} to draft. The pipeline status was not changed.`);
      await loadTickets();
      return;
    }
    setT(id, { state: "draft", stage: null, runStartedAt: null });
  };

  // A run outlives the page, so any ticket the backend still reports as
  // in-process gets a watcher rather than a simulated stage timer.
  useEffect(() => {
    tickets.forEach((t) => {
      if ((t.state === "inprocess" || t.state === "queued") && !watchers.current[t.id]) watchRun(t.id);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tickets]);

  useEffect(() => {
    const running = watchers.current;
    return () => Object.values(running).forEach(clearInterval);
  }, []);

  // Load database truth on startup and keep long-running pipeline stages fresh.
  useEffect(() => {
    loadTickets();
    // Skip a tick while a decision is in flight; otherwise the poll can
    // return a read that predates the write and undo it on screen.
    const poller = window.setInterval(() => {
      if (decidingCount.current === 0 && !reviewingRef.current) loadTickets();
    }, 3000);
    return () => window.clearInterval(poller);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const open = modal?.kind === "ticket";
    reviewingRef.current = open;
    if (!open) loadTickets();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modal]);

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
    if (view === "library" || view === "live") loadRules();
    if (view === "tokens") loadUsage();
  }, [view]);

  // Toggling the active choice clears the decision, so `next` may be null.
  // Decisions live in the DB keyed by rule text — without persisting, the next
  // loadTickets() would rebuild `rules` from the server and drop them.
  // Decisions are written one rule at a time while a 3s poller is replacing the
  // whole ticket array. Three things made that glitch, all fixed here:
  //   * the poller could land between the click and the write completing, so it
  //     replayed the pre-click value and the button appeared to flip back;
  //   * rules were addressed by array index, which points at a different rule
  //     if the poller ever returns them in another order;
  //   * the previous decision was read from a render-time closure that the
  //     poller had already replaced, so toggling off often computed the wrong
  //     next value.
  const decidingCount = useRef(0);
  // Each response carries the whole decisions map, so a slow earlier reply
  // would repaint values a later click has already superseded. Only the most
  // recent request per rule is allowed to write to the screen.
  const decisionSeq = useRef({});
  // The background refresh must not rewrite rows the moderator is working on.
  // While a report is open, every poll replaced the rules array, and a click
  // landing just after one computed its toggle from the refreshed value —
  // turning "approve" into "unset" because it looked like a repeat click.
  const reviewingRef = useRef(false);
  // One in-flight write per rule, in click order. A row lock on the server
  // serialises concurrent writes but does not preserve their order — the
  // second of two rapid clicks could acquire the lock first, leaving the
  // database holding the older intent while the screen showed the newer one.
  const decisionChain = useRef({});

  // Current decision per rule, updated synchronously on click and whenever the
  // server confirms. React state updaters run during render, so computing the
  // toggle inside one meant a fast second click could read it before it had
  // been assigned and drop the click entirely.
  const decisionNow = useRef({});

  const decide = async (id, ruleText, val) => {
    const key = `${id}::${ruleText}`;

    if (!(key in decisionNow.current)) {
      const rule = tickets.find((t) => t.id === id)?.rules?.find((r) => r.text === ruleText);
      if (!rule) return;
      decisionNow.current[key] = rule.decision ?? null;
    }

    const previous = decisionNow.current[key];
    const next = previous === val ? null : val;
    decisionNow.current[key] = next;

    const mySeq = (decisionSeq.current[key] || 0) + 1;
    decisionSeq.current[key] = mySeq;

    const paint = (value) =>
      setTickets((ts) =>
        ts.map((t) =>
          t.id === id
            ? {
                ...t,
                rules: (t.rules || []).map((r) =>
                  r.text === ruleText ? { ...r, decision: value ?? undefined } : r
                ),
              }
            : t
        )
      );

    paint(next);

    const send = async () => {
      decidingCount.current += 1;
      try {
        const response = await fetch(`${backendUrl}/api/tickets/${encodeURIComponent(id)}/decisions`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rule: ruleText, decision: next, decided_by: CURRENT_USER.k }),
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.error || `decision save failed ${response.status}`);

        // Only the newest click on this rule may repaint; an older reply
        // carries a value the moderator has already moved past.
        if (decisionSeq.current[key] === mySeq) {
          const entry = body.decisions?.[ruleText];
          const server = entry && typeof entry === "object" ? entry.decision : entry;
          decisionNow.current[key] = server ?? null;
          paint(server ?? null);
        }
      } catch (error) {
        console.error(`Failed to save decision for ${id}`, error);
        if (decisionSeq.current[key] !== mySeq) return;
        decisionNow.current[key] = previous;
        paint(previous);
      } finally {
        decidingCount.current -= 1;
      }
    };

    // Queue behind any write already running for this rule: a row lock
    // serialises writes but does not preserve the order they were clicked in.
    const chained = (decisionChain.current[key] || Promise.resolve()).then(send, send);
    decisionChain.current[key] = chained;
    return chained;
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
    try {
      await loadTickets();
    } finally {
      setRefreshing(false);
    }
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
    // The requested name is only a request. The backend owns report ids: it
    // allocates the next free one and tells us what the report is actually
    // called, so two people (or one person after a reload) can no longer be
    // handed the same id and silently overwrite each other's report.
    let id = (data && data.name && data.name.trim()) ? data.name.trim() : makeTicketId();
    let ticketPayload = {
      id,
      state: "draft",
      created: todayISO(),
      createdBy: CURRENT_USER.k,
      ...data,
    };

    try {
      const response = await fetch(`${backendUrl}/api/tickets`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(ticketPayload),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.error || `ticket save failed ${response.status}`);
      }
      if (body.id && body.id !== id) {
        // The requested name was taken. Adopt the id the backend assigned,
        // otherwise the local row would point at a report that is not there.
        if (body.renamed) {
          window.alert(`${id} already exists, so this report was filed as ${body.id}.`);
        }
        id = body.id;
        ticketPayload = { ...ticketPayload, id, name: body.id };
      }
    } catch (error) {
      console.error("Failed to save ticket to backend", error);
      window.alert(`Could not create ${id}: ${error.message}`);
      return;
    }

    setTickets((ts) => [ticketPayload, ...ts.filter((t) => t.id !== id)]);
    setModal(null);

    if (runNow) {
      // Hand the payload straight to runPipeline — it cannot look the ticket
      // up by id yet, and awaiting keeps the run tied to this call so a
      // failure surfaces instead of leaving the ticket sitting as a draft.
      await startRun(id, ticketPayload);
    }
  };

  // Ask for the next free id whenever the create form opens, so the name it
  // suggests is one the backend will actually accept.
  useEffect(() => {
    if (modal?.kind !== "new") return;
    let cancelled = false;
    (async () => {
      try {
        const response = await fetch(`${backendUrl}/api/tickets/next-id`);
        if (!response.ok) return;
        const data = await response.json();
        if (!cancelled && data.id) setNextId(data.id);
      } catch (error) {
        // Non-fatal: the field just opens blank and the backend still
        // assigns a free id on submit.
        console.debug("next-id lookup failed", error);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modal?.kind]);

  const q = query.trim().toLowerCase();

  // Ordering is purely newest-first by creation time. It used to fall back to
  // parsing digits out of the report id, which is not a timestamp and put
  // reports in an order nobody could predict.
  const filtered = useMemo(
    () =>
      tickets
        .filter((t) => !q || t.name.toLowerCase().includes(q) || t.url.toLowerCase().includes(q))
        .filter((t) => statusFilter === "all" || t.state === statusFilter)
        .sort((a, b) => new Date(b.created || 0) - new Date(a.created || 0)),
    [tickets, q, statusFilter]
  );

  // Kept for the stat cards, which count every state regardless of the filter.
  const byState = useMemo(() => {
    const m = Object.fromEntries(STATE_ORDER.map((k) => [k, []]));
    tickets.forEach((t) => m[t.state] && m[t.state].push(t));
    return m;
  }, [tickets]);

  // How long a run is likely to take, averaged over the runs already on
  // record. Shown against the stage in flight so a moderator watching a
  // pipeline knows whether to wait or come back. Purely local: every number
  // needed is already on the tickets the list fetched.
  const estimates = useMemo(() => {
    // Median, not mean. Run times are long-tailed — one site that took six
    // minutes to crawl drags a mean far above anything you would actually
    // wait, while the median stays on a run that really happened.
    const median = (key) => {
      const values = tickets
        .map((t) => t.metrics?.[key])
        .filter((v) => typeof v === "number" && v > 0)
        .sort((a, b) => a - b);
      if (!values.length) return null;
      const mid = Math.floor(values.length / 2);
      return values.length % 2 ? values[mid] : (values[mid - 1] + values[mid]) / 2;
    };
    const total = median("totalMs");
    if (total === null) return null;
    return {
      crawl: median("crawlMs"),
      generate: median("generationMs"),
      validate: median("validationMs"),
      total,
      sample: tickets.filter((t) => typeof t.metrics?.totalMs === "number").length,
    };
  }, [tickets]);

  const openTicket = modal?.kind === "ticket" ? tickets.find((t) => t.id === modal.id) : null;
  const editTicket = modal?.kind === "edit" ? tickets.find((t) => t.id === modal.id) : null;

  return (
    <Layout view={view} setView={setView} lastSync={lastSync}>
      {view === "reports" && (
        <Reports
          items={filtered}
          byState={byState}
          statusFilter={statusFilter}
          setStatusFilter={setStatusFilter}
          query={query}
          setQuery={setQuery}
          lastSync={lastSync}
          refreshing={refreshing}
          onRefresh={refreshBoard}
          onOpen={(id) => setModal({ kind: "ticket", id })}
          onNew={() => setModal({ kind: "new" })}
        />
      )}
      {view === "performance" && (
        <Performance tickets={tickets} onRefresh={refreshBoard} refreshing={refreshing} />
      )}
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
          onSendToPlayground={(seed) => { setPlaygroundSeed(seed); setView("playground"); }}
        />
      )}
      {view === "live" && (
        <LiveRules
          rules={rules}
          loading={rulesLoading}
          onRefresh={loadRules}
          onOpenReport={(id) => setModal({ kind: "ticket", id })}
        />
      )}
      {view === "playground" && (
        <Playground seed={playgroundSeed} onSeedConsumed={() => setPlaygroundSeed(null)} />
      )}
      {view === "tokens" && (
        <TokenUsage usage={usage} loading={usageLoading} onRefresh={loadUsage} />
      )}

      {/* ---------- modals ---------- */}
      {modal && (
        <div className="ad-overlay" onMouseDown={() => setModal(null)}>
          {modal.kind === "new" && (
            <NewReportModal
              nextName={nextId}
              onCreate={createTicket}
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
              backendUrl={backendUrl}
              estimates={estimates}
              onClose={() => setModal(null)}
              onRun={() => startRun(openTicket.id)}
              onCancelRun={() => cancelRun(openTicket.id)}
              onDelete={() => deleteTicket(openTicket.id)}
              onDecide={(ruleText, val) => decide(openTicket.id, ruleText, val)}
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
