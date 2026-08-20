/* ------------------------------------------------------------------ */
/*  Ad-block Rule CMS — shared constants                              */
/*  Lifecycle: Draft → In-process (ghost rows in Review) → Review → Done */
/* ------------------------------------------------------------------ */

export const STATE_ORDER = ["draft", "inprocess", "review", "failed", "done"];
export const TABS = ["review", "draft", "done", "all"]; // in-process and failed rows surface inside Review

export const STATES = {
  all: {
    label: "All",
    card: "All reports",
    sub: "Every report, newest first",
    empty: "No reports yet. Create one to get started.",
  },
  draft: {
    label: "Draft",
    card: "Drafts",
    sub: "Created — not yet run",
    empty: "No draft reports. Create one to get started.",
  },
  inprocess: {
    label: "Processing",
    card: "Processing",
    sub: "Running through the pipeline",
    empty: "",
  },
  review: {
    label: "Review",
    card: "Awaiting review",
    sub: "Waiting on a moderator decision",
    empty: "No reports are waiting for review.",
  },
  failed: {
    label: "Failed",
    card: "Failed runs",
    sub: "The pipeline stopped before producing rules",
    empty: "No failed runs.",
  },
  done: {
    label: "Done",
    card: "Completed",
    sub: "Review complete",
    empty: "No completed reports yet.",
  },
};

export const ENVS = [
  { k: "desktop", label: "Desktop" },
  { k: "android", label: "Android" },
  { k: "ios", label: "iOS" },
];

export const STAGES = [
  { k: "crawl", label: "Crawling page" },
  { k: "generate", label: "Generating rules" },
  { k: "validate", label: "Sandbox validation" },
];

export const USERS = [
  { k: "anh.dao", name: "Anh Dao", initials: "AD", bg: "#88C646", ink: "#1D3829" },
  { k: "duy.le", name: "Duy Le", initials: "DL", bg: "#1D3829", ink: "#D9EFBD" },
  { k: "hien.khuong", name: "Hien Khuong", initials: "HK", bg: "#FF7439", ink: "#3D1A07" },
  { k: "dong.tran", name: "Dong Tran", initials: "DT", bg: "#C4E3A3", ink: "#1D3829" },
];
export const CURRENT_USER = USERS[0]; // stub for the signed-in admin
export const userOf = (k) => USERS.find((u) => u.k === k);

export const VIEW_TITLES = { reports: "Reports", performance: "Performance", live: "Live rules", library: "Rule library", playground: "Rule playground", tokens: "Token usage" };
