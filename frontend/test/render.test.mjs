// The browser-free render gate: the equivalent of an SSR curl-check for a client app.
//
// It loads the COMMITTED bundle (dashboard2/assets/app.js) into jsdom, stubs fetch to return
// a FIXTURE overview payload (generated from the real to_jsonable serializer — see
// scripts/gen_v2_fixture.py), mounts the app on #app, and asserts the app actually renders
// the real data: the KPI displays, exactly 31 daily bars with the selected ones marked, the
// 6 cost-part legend entries, and outlier rows whose links point at /prompts/<id> and
// /sessions/<id>. A second test proves the gate is NOT vacuous: an empty payload must NOT
// yield 31 bars or 6 legend entries.
//
// This proves correct data renders and is wired up; it does NOT prove the layout looks good
// or that hover/sort/drill FEEL right — that needs a human with a browser.
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
const FIXTURE = JSON.parse(readFileSync(resolve(here, "fixture.json"), "utf8"));

const SHELL = '<!doctype html><html><head></head><body><div id="app"></div></body></html>';

function mount(payload, { status = 200 } = {}) {
  const url =
    "http://localhost/v2/dashboard?view=overview&from=" +
    payload.request.from +
    "&to=" +
    payload.request.to;
  const dom = new JSDOM(SHELL, { url, runScripts: "outside-only", pretendToBeVisual: true });
  const calls = [];
  dom.window.fetch = (input) => {
    calls.push(String(input));
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      json: () => Promise.resolve(payload),
    });
  };
  dom.window.eval(BUNDLE);
  return { dom, app: dom.window.document.getElementById("app"), calls };
}

async function settle(check, tries = 120) {
  for (let i = 0; i < tries; i += 1) {
    if (check()) return true;
    await new Promise((r) => setTimeout(r, 5));
  }
  return check();
}

test("renders the overview fixture into the real DOM", async () => {
  const { app, calls } = mount(FIXTURE);
  const bars = () => app.querySelectorAll("rect.ch-bar, rect.ch-bar-sel");
  const ok = await settle(() => bars().length === 31);
  assert.ok(ok, "app never rendered 31 daily bars");

  // The app actually called the overview API (same-origin).
  assert.ok(calls.some((u) => u.includes("/v2/api/overview")), "did not fetch /v2/api/overview");

  // Exactly 31 daily bars, with the selected ones marked as a distinct class.
  assert.equal(bars().length, 31);
  const selected = FIXTURE.model.daily_costs.filter((d) => d.selected).length;
  assert.ok(selected >= 1);
  assert.equal(app.querySelectorAll("rect.ch-bar-sel").length, selected);
  assert.equal(app.querySelectorAll("rect.ch-bar").length, 31 - selected);
  // Selection is shown three ways, never colour alone: band + two dashed edges.
  assert.equal(app.querySelectorAll("rect.ch-selband").length, 1);
  assert.equal(app.querySelectorAll("line.ch-seledge").length, 2);

  // Every KPI display string reaches the DOM verbatim.
  const text = app.textContent ?? "";
  for (const kpi of FIXTURE.model.kpis) {
    assert.ok(text.includes(kpi.display), `missing KPI display ${kpi.display}`);
  }

  // Exactly the 6 cost-part legend entries.
  assert.equal(app.querySelectorAll(".legend .chip").length, 6);

  // Outlier rows carry REAL drill links to the v1 detail pages.
  const hrefs = new Set(
    Array.from(app.querySelectorAll("a[href]")).map((a) => a.getAttribute("href")),
  );
  for (const p of FIXTURE.model.top_prompts) {
    assert.ok(
      hrefs.has(`/prompts/${encodeURIComponent(p.prompt_id)}`),
      `missing prompt link for ${p.prompt_id}`,
    );
  }
  for (const s of FIXTURE.model.top_sessions) {
    assert.ok(
      hrefs.has(`/sessions/${encodeURIComponent(s.session_id)}`),
      `missing session link for ${s.session_id}`,
    );
  }
});

test("gate is not vacuous: an empty payload does NOT render 31 bars or 6 legend entries", async () => {
  const empty = {
    request: FIXTURE.request,
    freshness: { stale: false, last_ingest: null, age_seconds: null },
    model: {
      window: FIXTURE.model.window,
      previous_window: FIXTURE.model.previous_window,
      timezone: FIXTURE.model.timezone,
      kpis: [],
      daily_costs: [],
      cost_parts: [],
      top_prompts: [],
      top_sessions: [],
      cache_rebuilds: [],
      unknown_cost_request_count: 0,
    },
  };
  const { app } = mount(empty);
  // Give the app the same opportunity to render something before asserting absence.
  await settle(() => (app.textContent ?? "").length > 0, 40);
  assert.notEqual(app.querySelectorAll("rect.ch-bar, rect.ch-bar-sel").length, 31);
  assert.equal(app.querySelectorAll(".legend .chip").length, 0);
});
