import { useEffect, useState } from "react";
import { Plus, Search, RefreshCw, ChevronsLeft, ChevronLeft, ChevronRight, ChevronsRight } from "lucide-react";
import { STATE_ORDER, TABS, STATES } from "../constants.js";
import ReportTable from "../components/ReportTable.jsx";

// which tab each stat card jumps to (in-process rows live inside Review)
const statTab = { draft: "draft", inprocess: "review", review: "review", done: "done" };

const PAGE_SIZES = [5, 10, 15, 20];

export default function Reports({
  items, byState, filtered, ghosts,
  tab, setTab, query, setQuery,
  lastSync, refreshing, onRefresh, onOpen, onNew,
}) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(5);

  // jump back to the first page whenever the visible set changes
  useEffect(() => { setPage(1); }, [tab, query, pageSize]);

  const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
  const cur = Math.min(page, totalPages);
  const pageItems = items.slice((cur - 1) * pageSize, cur * pageSize);

  const q = query.trim();
  const emptyText = q ? "No reports match this search." : STATES[tab].empty;

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
            className={"ad-stat s-" + k + (tab === statTab[k] && (k !== "inprocess" || tab === "review") ? " on" : "")}
            onClick={() => setTab(statTab[k])}
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
          <div className="ad-tabs" role="tablist" aria-label="Report states">
            {TABS.map((k) => (
              <button
                key={k}
                role="tab"
                aria-selected={tab === k}
                className={"ad-tab" + (tab === k ? " on" : "")}
                onClick={() => setTab(k)}
              >
                {STATES[k].label}
                <span className="ad-tabcount">
                  {k === "all" ? filtered.length : (byState[k] || []).length}
                  {k === "review" && ghosts.length > 0 ? ` +${ghosts.length}` : ""}
                </span>
              </button>
            ))}
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
