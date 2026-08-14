import {
  ClipboardList, FileCode2, MessageSquareText, Settings,
  TrendingUp, Gauge, HelpCircle, Coins,
} from "lucide-react";
import { CURRENT_USER, VIEW_TITLES } from "../constants.js";
import { agoText } from "../utils.js";
import { Avatar } from "./Avatar.jsx";

export default function Layout({ view, setView, lastSync, children }) {
  return (
    <div className="ad-app">
      {/* ---------- sidebar ---------- */}
      <aside className="ad-side">
        <div className="ad-logo">
          <div className="ad-logomark">CC</div>
          <div className="ad-logotext">
            <b>Cốc Cốc CMS</b>
            <span>AdBlock Rule Engine · v0.1 demo</span>
          </div>
        </div>
        <nav className="ad-nav">
          <div className="ad-navlabel">Reports</div>
          <button className={"ad-navitem" + (view === "reports" ? " on" : "")} onClick={() => setView("reports")}><ClipboardList /> List</button>
          <div className="ad-navlabel">Rules</div>
          <button className={"ad-navitem" + (view === "library" ? " on" : "")} onClick={() => setView("library")}><FileCode2 /> Rule library</button>
          <button className="ad-navitem" disabled title="Not part of this demo"><MessageSquareText /> Prompt templates</button>
          <div className="ad-navlabel">Analytics</div>
          <button className={"ad-navitem" + (view === "trend" ? " on" : "")} onClick={() => setView("trend")}><TrendingUp /> Trend</button>
          <button className={"ad-navitem" + (view === "performance" ? " on" : "")} onClick={() => setView("performance")}><Gauge /> Performance</button>
          <button className={"ad-navitem" + (view === "tokens" ? " on" : "")} onClick={() => setView("tokens")}><Coins /> Token usage</button>
          <div style={{ flex: 1 }} />
          <div className="ad-navlabel">Help</div>
          <button className="ad-navitem" disabled title="Not part of this demo"><Settings /> Settings</button>
          <button className="ad-navitem" disabled title="Not part of this demo"><HelpCircle /> Help</button>
        </nav>
        <div className="ad-sidefoot">
          <Avatar uk={CURRENT_USER.k} size={30} />
          <div>
            <div className="ad-name">{CURRENT_USER.name}</div>
            <div className="ad-role">s3978728@rmit.edu.vn</div>
          </div>
        </div>
      </aside>

      {/* ---------- main ---------- */}
      <div className="ad-main">
        <header className="ad-topbar">
          <div className="ad-crumb">
            CMS / <b>{VIEW_TITLES[view]}</b>
            {view !== "reports" && <span className="ad-crumbsub"> · Last updated {agoText(lastSync)}</span>}
          </div>
          <div className="ad-userchip">
            <Avatar uk={CURRENT_USER.k} size={26} />
            {CURRENT_USER.name}
            <span className="ad-role">ADMIN</span>
          </div>
        </header>
        {children}
      </div>
    </div>
  );
}
