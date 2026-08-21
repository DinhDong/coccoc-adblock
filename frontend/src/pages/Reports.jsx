import { useEffect, useState } from "react";
import { Plus, Search, RefreshCw, ChevronsLeft, ChevronLeft, ChevronRight, ChevronsRight } from "lucide-react";
import { STATE_ORDER, STATES } from "../constants.js";
import ReportTable from "../components/ReportTable.jsx";
import { usePersistentState, parsePageSize } from "../usePersistentState.js";

const PAGE_SIZES = [5, 10, 15, 20];

export default function Reports({
  items, byState, statusFilter, setStatusFilter, query, setQuery,
  lastSync, refreshing, onRefresh, onOpen, onNew,
}) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = usePersistentState("reports.pageSize", 5, parsePageSize);

  // jump back to the first page whenever the visible set changes
  useEffect(() => { setPage(1); }, [statusFilter, query, pageSize]);

  const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
  const cur = Math.min(page, totalPages);
  const pageItems = items.slice((cur - 1) * pageSize, cur * pageSize);

  const q = query.trim();
  const emptyText =
    q || statusFilter !== "all"
      ? "No reports match these filters."
      : "No reports yet. Create one to get started.";

  return (
    <div className="ad-content">
      <div className="ad-pagehead">
        <div>
          <h1>Reports</h1>
          <p>Ad-block rule moderation queue — school demo build.</p>
        </div>
        <button className="ad-btn ad-btn-primary" onClick={onNew}>
          <Plus /> New report
        </button>
      </div>

      {/* stat cards */}
      <div className="ad-stats">
        {STATE_ORDER.map((k) => (
          <button
            key={k}
            className={"ad-stat s-" + k + (statusFilter === k ? " on" : "")}
            onClick={() => setStatusFilter(statusFilter === k ? "all" : k)}
            title={STATES[k].sub}
          >
            <div className="ad-statnum">{(byState[k] || []).length}</div>
            <div className="ad-statlabel">{STATES[k].card}</div>
          </button>
        ))}
      </div>

      {/* table card */}
      <div className="ad-card">
        <div className="ad-toolbar">
          <div className="ad-tabs">
            <span className="ad-pageinfo">
              {items.length} report{items.length === 1 ? "" : "s"}
              {statusFilter !== "all" ? ` · ${STATES[statusFilter].label}` : ""}
            </span>
          </div>
          <div className="ad-tools">
            <span className="ad-search">
              <Search aria-hidden="true" />
              <input
                placeholder="Search name or URL…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                aria-label="Search reports"
              />
            </span>
            <select
              className="ad-select"
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              aria-label="Filter by status"
            >
              <option value="all">All</option>
              {STATE_ORDER.map((k) => (
                <option key={k} value={k}>{STATES[k].label}</option>
              ))}
            </select>
            <span className="ad-sync">Synced {lastSync.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
            <button
              className="ad-refresh"
              onClick={onRefresh}
              disabled={refreshing}
              title="Refresh — changes from other moderators arrive on refresh"
              aria-label="Refresh report list"
            >
              <RefreshCw className={refreshing ? "ad-spin" : ""} />
            </button>
          </div>
        </div>

        <ReportTable items={pageItems} emptyText={emptyText} onOpen={onOpen} />

        <div className="ad-pagebar">
          <span className="ad-pageinfo">
            Showing {pageItems.length} of {items.length} result{items.length === 1 ? "" : "s"}
          </span>
          <div className="ad-pagectl">
            <label className="ad-pagelabel" htmlFor="rows-per-page">Rows per page</label>
            <select
              id="rows-per-page"
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
