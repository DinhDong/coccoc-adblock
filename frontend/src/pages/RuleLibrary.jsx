import { useEffect, useMemo, useState } from "react";
import {
  Search, RefreshCw, ChevronsLeft, ChevronLeft, ChevronRight, ChevronsRight, Trash2, Combine, FlaskConical,
} from "lucide-react";
import { fmtDate } from "../utils.js";
import { PreTestCell, RuleActions, RuleEditor } from "../components/ReportDetail.jsx";
import { usePersistentState, parsePageSize } from "../usePersistentState.js";

const PAGE_SIZES = [10, 20, 50, 100];

const TYPES = [
  { k: "all", label: "All types" },
  { k: "cosmetic", label: "Cosmetic" },
  { k: "network", label: "Network" },
];

const STATUSES = [
  { k: "all", label: "All results" },
  { k: "passed", label: "Passed pre-test" },
  { k: "failed", label: "Failed pre-test" },
  { k: "pending", label: "Not validated" },
];

const DECISIONS = [
  { k: "all", label: "All decisions" },
  { k: "approve", label: "Approved" },
  { k: "reject", label: "Rejected" },
  { k: "undecided", label: "Undecided" },
];

function DecisionCell({ r }) {
  // A failed rule carries decision "reject" from the backend already; label it
  // so it does not read as a moderator's call.
  if (r.autoRejected) return <span className="ad-pill fail">Auto-rejected</span>;
  if (r.decision === "approve") {
    return <span className={"ad-pill " + (r.deployed ? "pass" : "")}>{r.deployed ? "Deployed" : "Approved"}</span>;
  }
  if (r.decision === "reject") return <span className="ad-pill fail">Rejected</span>;
  return <span className="ad-mute">Undecided</span>;
}

function LibraryRow({ r, selected, onToggle, onOpenReport, onEditRule, onDeleteRule }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(r.text);

  const save = () => { onEditRule(r.reportId, r.text, text.trim()); setEditing(false); };

  return (
    <tr className={selected ? "ad-rowsel" : ""}>
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
      <td><span className="ad-chip">{r.rule_type}</span></td>
      <td>{r.domain}</td>
      <td><PreTestCell r={r} /></td>
      <td><DecisionCell r={r} /></td>
      <td>
        <button className="ad-linkbtn" onClick={() => onOpenReport(r.reportId)}>
          {r.reportId}
        </button>
      </td>
      <td>{fmtDate(r.updatedAt)}</td>
      <td>
        <RuleActions
          editing={editing}
          setEditing={(next) => { setText(r.text); setEditing(next); }}
          label={r.text}
          onDelete={() => {
            if (window.confirm(`Delete this rule from ${r.reportId}?\n\n${r.text}`)) {
              onDeleteRule(r.reportId, r.text);
            }
          }}
        />
      </td>
    </tr>
  );
}

// A rule is identified by which report it belongs to plus its text.
const keyOf = (r) => `${r.reportId}::${r.text}`;

export default function RuleLibrary({
  rules, loading, onRefresh, onOpenReport, onEditRule, onDeleteRule, onBulkDelete,
  onMergeRules, onMergePreview, onSendToPlayground,
}) {
  const [selected, setSelected] = useState(() => new Set());
  const [query, setQuery] = useState("");
  const [type, setType] = useState("all");
  const [status, setStatus] = useState("all");
  const [decision, setDecision] = useState("all");
  const [domain, setDomain] = useState("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = usePersistentState("library.pageSize", 20, parsePageSize);

  useEffect(() => { setPage(1); }, [query, type, status, decision, domain, pageSize]);

  const domains = useMemo(
    () => [...new Set(rules.map((r) => r.domain).filter(Boolean))].sort(),
    [rules]
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return rules.filter((r) => {
      if (type !== "all" && r.rule_type !== type) return false;
      if (status !== "all" && r.status !== status) return false;
      if (domain !== "all" && r.domain !== domain) return false;
      if (decision === "undecided" ? r.decision : decision !== "all" && r.decision !== decision) return false;
      if (!q) return true;
      return (
        (r.text || "").toLowerCase().includes(q) ||
        (r.domain || "").toLowerCase().includes(q) ||
        (r.reportId || "").toLowerCase().includes(q)
      );
    });
  }, [rules, query, type, status, decision, domain]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const cur = Math.min(page, totalPages);
  const pageItems = filtered.slice((cur - 1) * pageSize, cur * pageSize);

  const passed = rules.filter((r) => r.status === "passed").length;
  const deployed = rules.filter((r) => r.deployed).length;
  const cosmetic = rules.filter((r) => r.rule_type === "cosmetic").length;

  const filtersOn = query.trim() || type !== "all" || status !== "all" || decision !== "all" || domain !== "all";

  // Selection is keyed by rule, not row index, so it survives filtering and
  // paging. Rules that scroll out of view stay selected on purpose.
  const toggle = (r) =>
    setSelected((prev) => {
      const next = new Set(prev);
      const k = keyOf(r);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });

  const pageKeys = pageItems.map(keyOf);
  const allOnPageSelected = pageKeys.length > 0 && pageKeys.every((k) => selected.has(k));

  const togglePage = () =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (allOnPageSelected) pageKeys.forEach((k) => next.delete(k));
      else pageKeys.forEach((k) => next.add(k));
      return next;
    });

  const selectedRules = rules.filter((r) => selected.has(keyOf(r)));

  // Merging folds two rules into one equivalent rule. The backend refuses
  // pairs whose union would change what gets blocked, so this previews the
  // result and lets the moderator confirm the exact text first.
  const mergeSelected = async () => {
    if (selectedRules.length !== 2) return;
    const [a, b] = selectedRules.map((r) => ({ reportId: r.reportId, rule: r.text }));
    const preview = await onMergePreview([a, b]);
    if (!preview) return;
    if (
      !window.confirm(
        `Merge these two rules into one?\n\n  ${a.rule}\n  ${b.rule}\n\nResult (kept on ${preview.keptOn}):\n  ${preview.merged}`
      )
    ) {
      return;
    }
    await onMergeRules([a, b]);
    setSelected(new Set());
  };

  // The sandbox loads one page, so a mixed-domain selection cannot be tested
  // together. Checked here for an instant answer; the backend enforces it too.
  const selectedDomains = new Set(selectedRules.map((r) => r.domain));
  const canTest = selectedRules.length > 0 && selectedDomains.size === 1;

  // Hands the selection to the playground, which runs it on arrival. One
  // sandbox screen instead of two slightly different ones.
  const testSelected = () => {
    if (!canTest) return;
    onSendToPlayground({
      url: selectedRules[0].url,
      rules: selectedRules.map((r) => r.text),
      environment: "desktop",
    });
  };

  const deleteSelected = async () => {
    if (selectedRules.length === 0) return;
    const preview = selectedRules.slice(0, 8).map((r) => `  ${r.text}`).join("\n");
    const more = selectedRules.length > 8 ? `\n  …and ${selectedRules.length - 8} more` : "";
    if (!window.confirm(`Delete ${selectedRules.length} rule(s)?\n\n${preview}${more}`)) return;
    await onBulkDelete(selectedRules.map((r) => ({ reportId: r.reportId, rule: r.text })));
    setSelected(new Set());
  };

  return (
    <div className="ad-content">
      <div className="ad-pagehead">
        <div>
          <h1>Rule library</h1>
          <p>Every rule the pipeline has generated, across all reports in the database.</p>
        </div>
      </div>

      <div className="ad-stats" style={{ marginTop: 18 }}>
        <div className="ad-kpi k-deep">
          <div className="ad-statnum">{rules.length}</div>
          <div className="ad-statlabel">Rules on record</div>
          <div className="ad-kpisub">across {domains.length} domain{domains.length === 1 ? "" : "s"}</div>
        </div>
        <div className="ad-kpi k-orange">
          <div className="ad-statnum">{passed}</div>
          <div className="ad-statlabel">Passed sandbox pre-test</div>
          <div className="ad-kpisub">{rules.length - passed} did not pass</div>
        </div>
        <div className="ad-kpi k-green">
          <div className="ad-statnum">{deployed}</div>
          <div className="ad-statlabel">Deployed</div>
          <div className="ad-kpisub">approved on a closed report</div>
        </div>
        <div className="ad-kpi k-deep">
          <div className="ad-statnum">{cosmetic}/{rules.length - cosmetic}</div>
          <div className="ad-statlabel">Cosmetic / network</div>
          <div className="ad-kpisub">hide elements vs block requests</div>
        </div>
      </div>

      <div className="ad-card">
        <div className="ad-toolbar">
          <div className="ad-tabs">
            {selected.size > 0 ? (
              <span className="ad-bulkbar">
                <b>{selected.size} selected</b>
                <button
                  className="ad-btn ad-btn-ghost"
                  onClick={mergeSelected}
                  disabled={selected.size !== 2}
                  title={selected.size === 2 ? "Combine into one rule" : "Select exactly two rules to merge"}
                >
                  <Combine /> Merge
                </button>
                <button
                  className="ad-btn ad-btn-ghost"
                  onClick={testSelected}
                  disabled={!canTest}
                  title={
                    selectedDomains.size > 1
                      ? `Selected rules span ${selectedDomains.size} sites — pick rules from one site`
                      : "Run these rules through the sandbox"
                  }
                >
                  <FlaskConical /> Test in playground
                </button>
                <button className="ad-btn ad-btn-danger" onClick={deleteSelected}>
                  <Trash2 /> Delete selected
                </button>
                <button className="ad-btn ad-btn-ghost" onClick={() => setSelected(new Set())}>
                  Clear
                </button>
              </span>
            ) : (
              <span className="ad-pageinfo">
                {filtered.length} rule{filtered.length === 1 ? "" : "s"}{filtersOn ? " matching filters" : ""}
              </span>
            )}
          </div>
          <div className="ad-tools">
            <span className="ad-search">
              <Search aria-hidden="true" />
              <input
                placeholder="Search rule, domain or report…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                aria-label="Search rules"
              />
            </span>
            <select className="ad-select" value={domain} onChange={(e) => setDomain(e.target.value)} aria-label="Filter by domain">
              <option value="all">All domains</option>
              {domains.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
            <select className="ad-select" value={type} onChange={(e) => setType(e.target.value)} aria-label="Filter by rule type">
              {TYPES.map((o) => <option key={o.k} value={o.k}>{o.label}</option>)}
            </select>
            <select className="ad-select" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Filter by pre-test result">
              {STATUSES.map((o) => <option key={o.k} value={o.k}>{o.label}</option>)}
            </select>
            <select className="ad-select" value={decision} onChange={(e) => setDecision(e.target.value)} aria-label="Filter by decision">
              {DECISIONS.map((o) => <option key={o.k} value={o.k}>{o.label}</option>)}
            </select>
            <button
              className="ad-refresh"
              onClick={onRefresh}
              disabled={loading}
              title="Reload rules from the database"
              aria-label="Refresh rule library"
            >
              <RefreshCw className={loading ? "ad-spin" : ""} />
            </button>
          </div>
        </div>

        <div className="ad-tablewrap">
          <table className="ad-table">
            <thead>
              <tr>
                <th style={{ width: 28 }}>
                  <input
                    type="checkbox"
                    className="ad-check"
                    checked={allOnPageSelected}
                    onChange={togglePage}
                    aria-label="Select every rule on this page"
                  />
                </th>
                <th>Rule</th>
                <th>Type</th>
                <th>Domain</th>
                <th>Pre-test</th>
                <th>Decision</th>
                <th>Report</th>
                <th>Updated</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {pageItems.map((r, i) => (
                <LibraryRow
                  key={`${r.reportId}-${r.text}-${i}`}
                  r={r}
                  selected={selected.has(keyOf(r))}
                  onToggle={() => toggle(r)}
                  onOpenReport={onOpenReport}
                  onEditRule={onEditRule}
                  onDeleteRule={onDeleteRule}
                />
              ))}
            </tbody>
          </table>
          {pageItems.length === 0 && (
            <div className="ad-empty">
              {rules.length === 0
                ? "No rules in the database yet. Run a report to generate some."
                : "No rules match these filters."}
            </div>
          )}
        </div>

        <div className="ad-pagebar">
          <span className="ad-pageinfo">
            Showing {pageItems.length} of {filtered.length} rule{filtered.length === 1 ? "" : "s"}
          </span>
          <div className="ad-pagectl">
            <label className="ad-pagelabel" htmlFor="rules-per-page">Rows per page</label>
            <select
              id="rules-per-page"
              className="ad-select"
              value={pageSize}
              onChange={(e) => setPageSize(Number(e.target.value))}
            >
              {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
            <span className="ad-pagelabel">Page {cur} of {totalPages}</span>
            <div className="ad-pagebtns">
              <button className="ad-pagebtn" disabled={cur === 1} onClick={() => setPage(1)} aria-label="First page"><ChevronsLeft /></button>
              <button className="ad-pagebtn" disabled={cur === 1} onClick={() => setPage(cur - 1)} aria-label="Previous page"><ChevronLeft /></button>
              <button className="ad-pagebtn" disabled={cur === totalPages} onClick={() => setPage(cur + 1)} aria-label="Next page"><ChevronRight /></button>
              <button className="ad-pagebtn" disabled={cur === totalPages} onClick={() => setPage(totalPages)} aria-label="Last page"><ChevronsRight /></button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
