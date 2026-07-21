import { dAgo, dateStr, iso, minLater } from "../utils.js";

const D2 = dAgo(2, 10, 2), D3 = dAgo(3, 15, 20), D5 = dAgo(5, 9, 14), D9 = dAgo(9, 11, 0);

export const SEED = [
  {
    id: "u1", name: "RPT-2026-0147", url: "https://genk.vn", env: "android",
    state: "draft", created: dateStr(dAgo(0)), createdBy: "dong.tran",
    focus: "Article footer", targets: [], notes: "Ads reappear about 10s after scrolling past them.",
  },
  {
    id: "u2", name: "RPT-2026-0146", url: "https://soha.vn", env: "desktop",
    state: "draft", created: dateStr(dAgo(1)), createdBy: "anh.dao",
    focus: "", targets: ["Popup overlay"], notes: "Full-screen popup appears ~5 seconds after load.",
  },
  {
    id: "u3", name: "RPT-2026-0145", url: "https://dantri.com.vn", env: "ios",
    state: "inprocess", stage: "crawl", created: dateStr(dAgo(0)), createdBy: "duy.le",
    runStartedAt: new Date(Date.now() - 6 * 60000).toISOString(),
    focus: "", targets: ["Video preroll"], notes: "",
  },
  {
    id: "u4", name: "RPT-2026-0141", url: "https://kenh14.vn", env: "desktop",
    state: "review", created: dateStr(D2), createdBy: "anh.dao",
    runStartedAt: iso(D2), reviewReadyAt: iso(minLater(D2, 2.1)),
    focus: "Right sidebar", targets: ["Floating video player", "Sidebar banners"], notes: "",
    rules: [
      { text: "kenh14.vn##.ads-sponsor-wrapper", status: "passed", conf: 0.93 },
      { text: 'kenh14.vn##div[id^="admzone"]', status: "passed", conf: 0.89 },
      { text: "||adx.admicro.vn^$third-party", status: "passed", conf: 0.95 },
      { text: "kenh14.vn##.video-float-ctn", status: "passed", conf: 0.84 },
      { text: 'kenh14.vn##div[class*="content"]', status: "failed", conf: 0.41, reason: "Too broad — hid the article body in the sandbox." },
    ],
  },
  {
    id: "u5", name: "RPT-2026-0140", url: "https://znews.vn", env: "android",
    state: "review", created: dateStr(D3), createdBy: "hien.khuong",
    runStartedAt: iso(D3), reviewReadyAt: iso(minLater(D3, 1.6)),
    focus: "", targets: [], notes: "Reported twice this week.",
    rules: [
      { text: "znews.vn##.article-ads", status: "passed", conf: 0.9 },
      { text: "||static.adtima.vn^$third-party", status: "passed", conf: 0.94 },
      { text: "znews.vn##aside[data-ad]", status: "passed", conf: 0.82 },
      { text: "znews.vn##.overlay", status: "failed", conf: 0.43, reason: "Also matches the image lightbox overlay." },
    ],
  },
  {
    id: "u6", name: "RPT-2026-0139", url: "https://vnexpress.net", env: "desktop",
    state: "done", created: dateStr(D5), doneAt: dateStr(dAgo(4)),
    runStartedAt: iso(D5), reviewReadyAt: iso(minLater(D5, 2.5)), reviewedAt: iso(dAgo(4, 10, 5)),
    createdBy: "dong.tran", reviewedBy: "duy.le",
    focus: "", targets: ["Top banner"], notes: "",
    rules: [
      { text: "vnexpress.net##.banner-top-site", status: "passed", conf: 0.91, decision: "approve" },
      { text: "||ads.eclick.vn^$third-party", status: "passed", conf: 0.96, decision: "approve" },
      { text: 'vnexpress.net##div[id^="banner_"]', status: "passed", conf: 0.87, decision: "approve" },
      { text: "vnexpress.net##.sidebar", status: "failed", conf: 0.35, reason: "Hid the most-read column." },
    ],
  },
  {
    id: "u7", name: "RPT-2026-0137", url: "https://tinhte.vn", env: "desktop",
    state: "done", created: dateStr(D9), doneAt: dateStr(D9),
    runStartedAt: iso(D9), reviewReadyAt: iso(minLater(D9, 1.8)), reviewedAt: iso(minLater(D9, 252)),
    createdBy: "duy.le", reviewedBy: "anh.dao",
    focus: "", targets: [], notes: "",
    rules: [
      { text: "tinhte.vn##.jsAdsPos", status: "passed", conf: 0.9, decision: "approve" },
      { text: "||googlesyndication.com^$domain=tinhte.vn", status: "passed", conf: 0.93, decision: "approve" },
      { text: "tinhte.vn##.p-sidebar", status: "passed", conf: 0.62, decision: "reject" },
    ],
  },
  {
    id: "u8", name: "RPT-2026-0138", url: "https://gamek.vn", env: "desktop",
    state: "review", created: dateStr(dAgo(4, 14, 0)), createdBy: "duy.le",
    runStartedAt: iso(dAgo(4, 14, 0)), reviewReadyAt: iso(minLater(dAgo(4, 14, 0), 2.0)),
    focus: "", targets: [], notes: "",
    rules: [
      { text: "gamek.vn##.ads-holder", status: "passed", conf: 0.88 },
      { text: "||adx.admicro.vn^$domain=gamek.vn", status: "passed", conf: 0.92 },
      { text: "gamek.vn##.thumb-wrap", status: "failed", conf: 0.39, reason: "Hid article thumbnails." },
    ],
  },
  {
    id: "u9", name: "RPT-2026-0136", url: "https://24h.com.vn", env: "desktop",
    state: "done", created: dateStr(dAgo(6)), doneAt: dateStr(dAgo(6)),
    createdBy: "anh.dao", reviewedBy: "hien.khuong",
    runStartedAt: iso(dAgo(6, 8, 30)), reviewReadyAt: iso(minLater(dAgo(6, 8, 30), 2.2)), reviewedAt: iso(dAgo(6, 17, 45)),
    focus: "", targets: [], notes: "",
    rules: [
      { text: "24h.com.vn##.box-ads-top", status: "passed", conf: 0.91, decision: "approve" },
      { text: "||ads.24h.com.vn^$third-party", status: "passed", conf: 0.9, decision: "approve" },
      { text: "24h.com.vn##.sport-banner", status: "passed", conf: 0.58, decision: "reject" },
    ],
  },
  {
    id: "u10", name: "RPT-2026-0135", url: "https://cafef.vn", env: "desktop",
    state: "done", created: dateStr(dAgo(7)), doneAt: dateStr(dAgo(7)),
    createdBy: "dong.tran", reviewedBy: "duy.le",
    runStartedAt: iso(dAgo(7, 9, 0)), reviewReadyAt: iso(minLater(dAgo(7, 9, 0), 1.9)), reviewedAt: iso(dAgo(7, 12, 10)),
    focus: "", targets: [], notes: "",
    rules: [
      { text: "cafef.vn##.vnads-box", status: "passed", conf: 0.93, decision: "approve" },
      { text: "||media.eclick.vn^$third-party", status: "passed", conf: 0.95, decision: "approve" },
      { text: 'cafef.vn##div[id^="admbackground"]', status: "passed", conf: 0.87, decision: "approve" },
    ],
  },
  {
    id: "u11", name: "RPT-2026-0133", url: "https://afamily.vn", env: "android",
    state: "done", created: dateStr(dAgo(8)), doneAt: dateStr(dAgo(7)),
    createdBy: "dong.tran", reviewedBy: "hien.khuong",
    runStartedAt: iso(dAgo(8, 9, 20)), reviewReadyAt: iso(minLater(dAgo(8, 9, 20), 2.4)), reviewedAt: iso(dAgo(7, 11, 30)),
    focus: "", targets: [], notes: "",
    rules: [
      { text: "afamily.vn##.ads-inline", status: "passed", conf: 0.89, decision: "approve" },
      { text: "afamily.vn##.widget-right", status: "passed", conf: 0.55, decision: "reject" },
      { text: 'afamily.vn##[class*="box"]', status: "failed", conf: 0.4, reason: "Matched recipe cards." },
    ],
  },
  {
    id: "u12", name: "RPT-2026-0131", url: "https://vov.vn", env: "desktop",
    state: "done", created: dateStr(dAgo(10)), doneAt: dateStr(dAgo(10)),
    createdBy: "hien.khuong", reviewedBy: "anh.dao",
    runStartedAt: iso(dAgo(10, 9, 0)), reviewReadyAt: iso(minLater(dAgo(10, 9, 0), 1.6)), reviewedAt: iso(dAgo(10, 15, 0)),
    focus: "", targets: [], notes: "",
    rules: [
      { text: "vov.vn##.banner-zone", status: "passed", conf: 0.9, decision: "approve" },
      { text: "||ads.vov.vn^$third-party", status: "passed", conf: 0.88, decision: "approve" },
      { text: "vov.vn##.audio-player-wrap", status: "failed", conf: 0.33, reason: "Broke the radio player." },
    ],
  },
  {
    id: "u13", name: "RPT-2026-0129", url: "https://eva.vn", env: "android",
    state: "done", created: dateStr(dAgo(11)), doneAt: dateStr(dAgo(10)),
    createdBy: "anh.dao", reviewedBy: "duy.le",
    runStartedAt: iso(dAgo(11, 9, 0)), reviewReadyAt: iso(minLater(dAgo(11, 9, 0), 3.1)), reviewedAt: iso(dAgo(10, 15, 0)),
    focus: "", targets: ["Sticky footer"], notes: "",
    rules: [
      { text: "eva.vn##.sticky-footer-ad", status: "passed", conf: 0.9, decision: "approve" },
      { text: "eva.vn##.margin-box", status: "passed", conf: 0.52, decision: "reject" },
    ],
  },
  {
    id: "u14", name: "RPT-2026-0127", url: "https://plo.vn", env: "desktop",
    state: "done", created: dateStr(dAgo(12)), doneAt: dateStr(dAgo(12)),
    createdBy: "dong.tran", reviewedBy: "hien.khuong",
    runStartedAt: iso(dAgo(12, 10, 0)), reviewReadyAt: iso(minLater(dAgo(12, 10, 0), 2.0)), reviewedAt: iso(dAgo(12, 15, 0)),
    focus: "", targets: [], notes: "",
    rules: [
      { text: "plo.vn##.ads-container", status: "passed", conf: 0.92, decision: "approve" },
      { text: "||adx.admicro.vn^$domain=plo.vn", status: "passed", conf: 0.94, decision: "approve" },
      { text: "plo.vn##.inline-sponsor", status: "passed", conf: 0.83, decision: "approve" },
    ],
  },
  {
    id: "u15", name: "RPT-2026-0125", url: "https://thanhnien.vn", env: "ios",
    state: "done", created: dateStr(dAgo(13)), doneAt: dateStr(dAgo(13)),
    createdBy: "duy.le", reviewedBy: "dong.tran",
    runStartedAt: iso(dAgo(13, 9, 0)), reviewReadyAt: iso(minLater(dAgo(13, 9, 0), 2.6)), reviewedAt: iso(dAgo(13, 17, 0)),
    focus: "", targets: [], notes: "",
    rules: [
      { text: "thanhnien.vn##.pos-banner", status: "passed", conf: 0.9, decision: "approve" },
      { text: "||ads.eclick.vn^$domain=thanhnien.vn", status: "passed", conf: 0.93, decision: "approve" },
      { text: 'thanhnien.vn##[data-widget="related"]', status: "failed", conf: 0.36, reason: "Hid related-articles widget." },
    ],
  },
];
