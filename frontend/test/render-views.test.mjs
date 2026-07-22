// Browser-free render gate for the node-tree views (period + dist), the analogue of the
// overview gate in render.test.mjs. It loads the COMMITTED bundle into jsdom, stubs fetch to
// return a FIXTURE payload generated from the REAL serializer (scripts/gen_v2_view_fixtures.py),
// mounts the app on #app for view=period / view=dist, and asserts the generic NodeView actually
// renders the real data:
//   - period: 3 interactive axis tabs, each a table with rows + sortable headers; clicking a
//     tab reveals a different panel's table (aria-selected + hidden move client-side).
//   - dist: the insight panel + the quantile / band / profile tables with rows.
//   - CSP: nothing NodeView emits carries a style= or on*= attribute (SVG attrs only).
//   - non-vacuity: an EMPTY-window period model renders its tables with ZERO rows.
//
// This proves correct data renders CSP-safely and tab-switching is wired; it does NOT prove the
// layout looks good or the tabs FEEL right — that needs a human with a browser.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { JSDOM } from "jsdom";

const here = dirname(fileURLToPath(import.meta.url));
const BUNDLE = readFileSync(
  resolve(here, "..", "..", "src", "metsuke", "dashboard2", "assets", "app.js"),
  "utf8",
);
const load = (name) => JSON.parse(readFileSync(resolve(here, `${name}.json`), "utf8"));

const SHELL = '<!doctype html><html><head></head><body><div id="app"></div></body></html>';

function mount(payload) {
  const url = "http://localhost/v2/dashboard?" + payload.request.canonical_query;
  const dom = new JSDOM(SHELL, { url, runScripts: "outside-only", pretendToBeVisual: true });
  const calls = [];
  dom.window.fetch = (input) => {
    calls.push(String(input));
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve(payload),
    });
  };
  dom.window.eval(BUNDLE);
  return { dom, app: dom.window.document.getElementById("app"), calls };
}

async function settle(check, tries = 160) {
  for (let i = 0; i < tries; i += 1) {
    if (check()) return true;
    await new Promise((r) => setTimeout(r, 5));
  }
  return check();
}

function assertCspSafe(app) {
  for (const el of app.querySelectorAll("*")) {
    assert.equal(el.getAttribute("style"), null, `<${el.tagName}> carries an inline style=`);
    for (const name of el.getAttributeNames()) {
      assert.ok(!/^on/i.test(name), `<${el.tagName}> carries an inline ${name}= handler`);
    }
  }
}

test("period: renders 3 axis tabs with tables and switches panels client-side", async () => {
  const fixture = load("fixture-period");
  const { app, calls } = mount(fixture);

  const tables = () => app.querySelectorAll("table.node-table");
  const ok = await settle(() => tables().length === 3);
  assert.ok(ok, "period never rendered its 3 axis tables");

  // It fetched the period endpoint specifically (same-origin).
  assert.ok(calls.some((u) => u.includes("/v2/api/period")), "did not fetch /v2/api/period");

  // Three interactive tabs (session / prompt / project), exactly one selected.
  const tabs = app.querySelectorAll('[role="tab"]');
  assert.equal(tabs.length, 3);
  const selectedInitially = Array.from(tabs).filter((t) => t.getAttribute("aria-selected") === "true");
  assert.equal(selectedInitially.length, 1);

  // Panels: all mounted, exactly one visible (the rest carry `hidden`).
  const panels = app.querySelectorAll('[role="tabpanel"]');
  assert.equal(panels.length, 3);
  const visible = () => Array.from(panels).filter((p) => !p.hasAttribute("hidden"));
  assert.equal(visible().length, 1, "more than one panel visible");

  // The visible panel holds a real table with data rows and sortable headers on data-sort cells.
  const activePanel = visible()[0];
  assert.ok(activePanel.querySelector("table.node-table"), "active panel has no table");
  assert.ok(activePanel.querySelectorAll("tbody tr").length > 0, "active panel table has no rows");
  assert.ok(activePanel.querySelectorAll("th[data-sortable]").length > 0, "no sortable headers");
  assert.ok(
    activePanel.querySelectorAll("td[data-sort]").length > 0,
    "cells expose no data-sort key",
  );

  const firstSelectedId = selectedInitially[0].id;

  // Switch to a DIFFERENT tab: a currently-hidden one becomes visible, selection follows.
  const other = Array.from(tabs).find((t) => t.getAttribute("aria-selected") !== "true");
  other.click();
  const switched = await settle(
    () => other.getAttribute("aria-selected") === "true" && visible().length === 1,
  );
  assert.ok(switched, "tab switch did not update aria-selected / visibility");
  assert.notEqual(visible()[0].id, activePanel.id, "same panel visible after switching tabs");
  assert.notEqual(
    app.querySelector(`#${firstSelectedId}`).getAttribute("aria-selected"),
    "true",
    "previous tab still selected after switch",
  );
  // The newly visible panel also has a table with rows (another axis of real data).
  assert.ok(visible()[0].querySelectorAll("tbody tr").length > 0, "switched panel has no rows");

  // No refetch happened for the client-side tab switch.
  assert.equal(calls.length, 1, "tab switch caused a refetch");

  // The dynamic Cell visuals positively render, each via a CSP-safe SVG presentation attribute
  // (not merely "no style= slipped in"): a magnitude bar, a category dot whose colour is an SVG
  // fill=, and a clip cell whose full text lives in title=. (querySelector sees hidden panels.)
  assert.ok(app.querySelector("svg.mag rect.mag-fill"), "no magnitude bar rendered");
  assert.ok(app.querySelector("svg.dot circle[fill]"), "no category dot with SVG fill=");
  assert.ok(app.querySelector(".clip-text[title]"), "no clip cell carrying full text in title=");

  assertCspSafe(app);
});

test("dist: renders the insight panel and the quantile/band/profile tables", async () => {
  const fixture = load("fixture-dist");
  const { app, calls } = mount(fixture);

  const tables = () => app.querySelectorAll("table.node-table");
  const ok = await settle(() => tables().length === 3);
  assert.ok(ok, "dist never rendered its 3 tables");
  assert.ok(calls.some((u) => u.includes("/v2/api/dist")), "did not fetch /v2/api/dist");

  // The front-loaded insight panel is present with at least one fact line.
  const insight = app.querySelector(".node-insight");
  assert.ok(insight, "dist has no insight panel");
  assert.ok(insight.querySelectorAll(".insight-fact").length >= 1, "insight has no facts");

  // Each of the 3 tables carries data rows.
  for (const table of tables()) {
    assert.ok(table.querySelectorAll("tbody tr").length > 0, "a dist table has no rows");
  }
  // The section headings (コスト分位点 × プロジェクト etc.) reached the DOM.
  assert.ok(app.querySelectorAll("h3.node-heading").length >= 3, "missing dist section headings");

  assertCspSafe(app);
});

test("non-vacuity: an empty-window period model renders tables with ZERO rows", async () => {
  const fixture = load("fixture-period-empty");
  const { app } = mount(fixture);

  // Give the app time to render the (empty) tables before asserting absence of rows.
  const ok = await settle(() => app.querySelectorAll("table.node-table").length === 3);
  assert.ok(ok, "empty period never rendered its table shells");
  assert.equal(app.querySelectorAll("tbody tr").length, 0, "empty model produced table rows");
});

// Count stacked/line bars that actually carry visible magnitude (parsed height > 0.5). Faithful
// geometry emits zero-height rects too, so this is how we distinguish "real data" from "shells".
function positiveBars(app) {
  return Array.from(app.querySelectorAll("svg.chart rect.ch-series")).filter(
    (rect) => Number.parseFloat(rect.getAttribute("height") ?? "0") > 0.5,
  ).length;
}

test("trend: renders SVG charts, grain + series tabs switch panels client-side, summary table", async () => {
  const fixture = load("fixture-trend");
  const { app, calls } = mount(fixture);

  const charts = () => app.querySelectorAll("svg.chart");
  const ok = await settle(() => charts().length > 0 && app.querySelector("table.node-table"));
  assert.ok(ok, "trend never rendered its charts");
  assert.ok(calls.some((u) => u.includes("/v2/api/trend")), "did not fetch /v2/api/trend");

  // Chart marks positively render (not just <svg> shells): stacked bars + line polylines/points.
  assert.ok(positiveBars(app) > 0, "no positive-height bars in trend charts");
  assert.ok(app.querySelector("svg.chart polyline.ch-line"), "no line-chart polyline rendered");
  assert.ok(app.querySelector("svg.chart circle.ch-series"), "no line-chart data points rendered");

  // The seeded marker band + regime line reached the DOM — proves the to_jsonable sqlite3.Row
  // extension survives end-to-end (trend hands raw Rows to volume_chart).
  assert.ok(app.querySelector("svg.chart rect.ch-marker"), "no marker band rendered");
  assert.ok(app.querySelector("svg.chart line.ch-regime"), "no regime line rendered");

  // Native <title> tooltips survive the transparent hover overlay: each hit column carries one
  // (so a browser tooltip fires even with the hover JS inert), plus the per-mark titles exist.
  assert.ok(app.querySelector("svg.chart rect.ch-hit title"), "hit columns carry no <title>");
  assert.ok(app.querySelector("svg.chart rect.ch-series title"), "bars carry no native <title>");

  // The ④ summary table renders with rows.
  assert.ok(
    app.querySelector("table.node-table tbody tr"),
    "summary table has no rows",
  );

  // --- grain tabs (日次/週次/月次): one global selector, all grain_panels toggle together. ---
  const grainTabs = app.querySelectorAll(".grain-strip [role=tab]");
  assert.equal(grainTabs.length, 3, "expected 3 grain tabs");
  const dailyVisible = () =>
    app.querySelectorAll('.grain-panel[data-grain="daily"]:not([hidden])').length;
  const weeklyVisible = () =>
    app.querySelectorAll('.grain-panel[data-grain="weekly"]:not([hidden])').length;
  assert.ok(dailyVisible() > 0, "no daily grain panel visible initially");
  assert.equal(weeklyVisible(), 0, "weekly grain panel visible before switch");

  const weeklyTab = Array.from(grainTabs).find((t) => t.textContent.includes("週次"));
  weeklyTab.click();
  const grainSwitched = await settle(() => weeklyVisible() > 0 && dailyVisible() === 0);
  assert.ok(grainSwitched, "grain switch did not toggle panels");
  assert.equal(weeklyTab.getAttribute("aria-selected"), "true", "weekly grain tab not selected");
  assert.equal(calls.length, 1, "grain switch caused a refetch");

  // --- series tabs (費目/モデル/プロジェクト): the existing v2-axis tabs/panel mechanism. ---
  const feePanel = app.querySelector("#panel-v2-axis-fee");
  const modelPanel = app.querySelector("#panel-v2-axis-model");
  assert.ok(feePanel && modelPanel, "v2-axis panels missing");
  assert.equal(feePanel.hasAttribute("hidden"), false, "fee axis not visible initially");
  assert.equal(modelPanel.hasAttribute("hidden"), true, "model axis visible before switch");

  app.querySelector("#tab-v2-axis-model").click();
  const axisSwitched = await settle(
    () => !modelPanel.hasAttribute("hidden") && feePanel.hasAttribute("hidden"),
  );
  assert.ok(axisSwitched, "series (v2-axis) tab switch did not toggle panels");
  assert.equal(calls.length, 1, "series tab switch caused a refetch");

  assertCspSafe(app);
});

test("cache: renders ⚡ stacked bars, the cache-balance dual-axis chart, and the session table", async () => {
  const fixture = load("fixture-cache");
  const { app, calls } = mount(fixture);

  const charts = () => app.querySelectorAll("svg.chart");
  const ok = await settle(() => charts().length > 0 && app.querySelector("table.node-table tbody tr"));
  assert.ok(ok, "cache never rendered its charts + table");
  assert.ok(calls.some((u) => u.includes("/v2/api/cache")), "did not fetch /v2/api/cache");

  // ⚡ stacked-bar charts are present (structurally, even where the seed has no reset events).
  assert.ok(app.querySelectorAll("svg.chart rect.ch-series").length > 0, "no stacked-bar rects");
  // The cache-balance dual-axis chart: grouped $ bars (positive height) + the w1h% ratio line.
  assert.ok(positiveBars(app) > 0, "cache-balance produced no positive-height bars");
  assert.ok(app.querySelector("svg.chart polyline.ch-line"), "no cache-balance ratio line");

  // metric tab (件数/再構築費) switches client-side, no refetch.
  const countPanel = app.querySelector("#panel-metric-metric-count");
  const costPanel = app.querySelector("#panel-metric-metric-cost");
  assert.ok(countPanel && costPanel, "metric panels missing");
  assert.equal(countPanel.hasAttribute("hidden"), false, "count metric not visible initially");
  app.querySelector("#tab-metric-metric-cost").click();
  const switched = await settle(
    () => !costPanel.hasAttribute("hidden") && countPanel.hasAttribute("hidden"),
  );
  assert.ok(switched, "metric tab switch did not toggle panels");
  assert.equal(calls.length, 1, "metric tab switch caused a refetch");

  // The write-heavy session table renders with rows.
  assert.ok(app.querySelector("table.node-table tbody tr"), "session table has no rows");

  assertCspSafe(app);
});

test("non-vacuity: empty-window trend/cache render chart shells with ZERO positive bars / rows", async () => {
  const trend = mount(load("fixture-trend-empty"));
  const trendOk = await settle(() => trend.app.querySelectorAll("svg.chart").length > 0);
  assert.ok(trendOk, "empty trend never rendered chart shells");
  assert.equal(positiveBars(trend.app), 0, "empty trend produced positive-height bars");
  // Weekly/monthly summary tables have no rows (the daily table is one-row-per-day by design).
  const weeklyPanel = trend.app.querySelector('.grain-panel[data-grain="weekly"]');
  assert.ok(weeklyPanel, "no weekly grain panel");

  const cache = mount(load("fixture-cache-empty"));
  const cacheOk = await settle(() => cache.app.querySelectorAll("svg.chart").length > 0);
  assert.ok(cacheOk, "empty cache never rendered chart shells");
  assert.equal(positiveBars(cache.app), 0, "empty cache produced positive-height bars");
  assert.equal(cache.app.querySelectorAll("tbody tr").length, 0, "empty cache produced table rows");
});
