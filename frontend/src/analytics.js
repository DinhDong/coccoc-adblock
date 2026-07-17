import { useMemo } from "react";
import { USERS } from "./constants.js";
import { hostname, dAgo, dateStr } from "./utils.js";

export function useMetrics(tickets) {
  return useMemo(() => {
    const done = tickets.filter((t) => t.state === "done");

    let passed = 0, failed = 0, approved = 0, rejected = 0, live = 0;
    tickets.forEach((t) => {
      (t.rules || []).forEach((r) => {
        if (r.status === "passed") passed++; else failed++;
        if (r.decision === "approve") { approved++; if (t.state === "done") live++; }
        if (r.decision === "reject") rejected++;
      });
    });

    const runPairs = tickets.filter((t) => t.runStartedAt && t.reviewReadyAt);
    const runs = runPairs.map((t) => new Date(t.reviewReadyAt) - new Date(t.runStartedAt));
    const reviewPairs = done.filter((t) => t.reviewReadyAt && t.reviewedAt);
    const reviews = reviewPairs.map((t) => new Date(t.reviewedAt) - new Date(t.reviewReadyAt));
    const avg = (arr) => (arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : null);
    const slaOk = reviews.filter((ms) => ms <= 24 * 3600000).length;

    const perUser = USERS.map((u) => {
      const reviewedTix = reviewPairs.filter((t) => t.reviewedBy === u.k);
      let uApproved = 0, uRejected = 0;
      reviewedTix.forEach((t) =>
        (t.rules || []).forEach((r) => {
          if (r.decision === "approve") uApproved++;
          if (r.decision === "reject") uRejected++;
        })
      );
      return {
        u,
        reviewed: done.filter((t) => t.reviewedBy === u.k).length,
        created: tickets.filter((t) => t.createdBy === u.k).length,
        uApproved, uRejected,
        uAvgReview: avg(reviewedTix.map((t) => new Date(t.reviewedAt) - new Date(t.reviewReadyAt))),
      };
    });

    const byDomain = {};
    tickets.forEach((t) => {
      const d = hostname(t.url).split(".").slice(-2).join(".");
      const row = (byDomain[d] = byDomain[d] || { domain: d, reports: 0, runs: [], approved: 0, rejected: 0, live: 0 });
      row.reports++;
      if (t.runStartedAt && t.reviewReadyAt) row.runs.push(new Date(t.reviewReadyAt) - new Date(t.runStartedAt));
      (t.rules || []).forEach((r) => {
        if (r.decision === "approve") { row.approved++; if (t.state === "done") row.live++; }
        if (r.decision === "reject") row.rejected++;
      });
    });
    const domains = Object.values(byDomain).map((r) => ({ ...r, avgRun: avg(r.runs) }));

    return {
      total: tickets.length, doneN: done.length,
      passed, failed, approved, rejected, live,
      avgRun: avg(runs), runsN: runs.length,
      avgReview: avg(reviews), reviewsN: reviews.length,
      sla: reviews.length ? Math.round((slaOk / reviews.length) * 100) : null,
      perUser, domains, reviewPairs,
    };
  }, [tickets]);
}

export const CHART = {
  green: "#88C646", deep: "#1D3829", orange: "#FF7439", grid: "#E7ECE8",
  tick: { fontSize: 11, fill: "#5C6B63", fontFamily: "Arial, Helvetica, sans-serif" },
};
const dayLabel = (d) => d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });

export function weekDelta(tickets, field) {
  const now = Date.now(), wk = 7 * 86400000;
  const ts = (v) => new Date(v.length === 10 ? v + "T12:00:00" : v).getTime();
  const count = (from, to) => tickets.filter((t) => t[field] && ts(t[field]) >= from && ts(t[field]) < to).length;
  return { cur: count(now - wk, now + 1), prev: count(now - 2 * wk, now - wk) };
}

export const latencySeries = (m) =>
  [...m.reviewPairs]
    .sort((a, b) => new Date(a.reviewedAt) - new Date(b.reviewedAt))
    .map((t) => ({
      name: t.name.replace("RPT-2026-", "#"),
      hours: +(((new Date(t.reviewedAt) - new Date(t.reviewReadyAt)) / 3600000).toFixed(1)),
    }));

export function trendSeries(tickets) {
  const days = [];
  for (let i = 13; i >= 0; i--) {
    const d = dAgo(i, 0, 0);
    const key = dateStr(d);
    days.push({
      label: dayLabel(d),
      created: tickets.filter((t) => t.created === key).length,
      completed: tickets.filter((t) => t.doneAt === key).length,
    });
  }
  return days;
}

export const runSeries = (tickets) =>
  tickets
    .filter((t) => t.runStartedAt && t.reviewReadyAt)
    .sort((a, b) => new Date(a.runStartedAt) - new Date(b.runStartedAt))
    .map((t) => ({
      name: t.name.replace("RPT-2026-", "#"),
      seconds: Math.round((new Date(t.reviewReadyAt) - new Date(t.runStartedAt)) / 1000),
    }));

export const EV_TEXT = { created: "created by", run: "sent to pipeline by", ready: "ready for review", done: "reviewed by" };

export function buildEvents(tickets) {
  const ev = [];
  tickets.forEach((t) => {
    const passedN = (t.rules || []).filter((r) => r.status === "passed").length;
    const liveN = (t.rules || []).filter((r) => r.decision === "approve").length;
    ev.push({ ts: +new Date(t.created + "T08:55:00"), name: t.name, kind: "created", by: t.createdBy });
    if (t.runStartedAt) ev.push({ ts: +new Date(t.runStartedAt), name: t.name, kind: "run", by: t.createdBy });
    if (t.reviewReadyAt) ev.push({ ts: +new Date(t.reviewReadyAt), name: t.name, kind: "ready", extra: `${passedN}/${(t.rules || []).length} rules passed` });
    if (t.reviewedAt) ev.push({ ts: +new Date(t.reviewedAt), name: t.name, kind: "done", by: t.reviewedBy, extra: `${liveN} deployed` });
  });
  return ev.filter((e) => e.ts <= Date.now()).sort((a, b) => b.ts - a.ts).slice(0, 7);
}
