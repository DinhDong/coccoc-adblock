/* ------------------------------------------------------------------ */
/*  Ad-block Rule CMS — shared constants                              */
/*  Lifecycle: Draft → In-process → Review → Done (or Failed)          */
/* ------------------------------------------------------------------ */

export const STATE_ORDER = ["draft", "queued", "inprocess", "review", "failed", "done"];

// What the stat cards show, in order. Not the same list as STATE_ORDER: a card
// can stand for more than one state, and "all" is a card but not a state.
//   - "all" leads, as a running total of the whole queue.
//   - "active" replaces the old separate Queued and Running cards. Splitting
//     them made a moderator read two numbers to answer one question ("is
//     anything in flight?"), and a report crosses between the two on its own
//     within seconds of being picked up, so the split was never stable enough
//     to act on.
export const CARD_ORDER = ["all", "draft", "active", "review", "failed", "done"];

// Which ticket states each card counts and filters to.
export const CARD_STATES = {
  all: STATE_ORDER,
  draft: ["draft"],
  active: ["queued", "inprocess"],
  review: ["review"],
  failed: ["failed"],
  done: ["done"],
};

export const STATES = {
  all: {
    label: "All",
    card: "Total",
    sub: "Every report, whatever its state",
    empty: "No reports yet. Create one to get started.",
  },
  draft: {
    label: "Draft",
    card: "Drafts",
    sub: "Created — not yet run",
    empty: "No draft reports. Create one to get started.",
  },
  active: {
    label: "In progress",
    card: "In progress",
    sub: "Queued or being crawled right now",
    empty: "Nothing is queued or running.",
  },
  queued: {
    label: "Queued",
    card: "Queued",
    sub: "Waiting for the worker to pick it up",
    empty: "Nothing is waiting in the queue.",
  },
  inprocess: {
    label: "Running",
    card: "Running now",
    sub: "The worker is crawling this report",
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
    card: "Done",
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

export const VIEW_TITLES = { reports: "Reports", performance: "Performance", live: "Live rules", library: "Rule library", playground: "Rule playground", tokens: "Token usage" };
