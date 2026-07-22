// URL is the source of truth. The app reads `location.search`, hands it verbatim to the
// API (which owns window/preset resolution, exactly like v1's resolve_query), and rewrites
// the URL from the resolved `request` the API returns. Preset/date edits build a new query
// and pushState; the API's canonical form then replaces it in place. Back/forward work
// because each history entry is a distinct, self-describing query string.
//
// NOTE ON PARAM NAMING: the count field travels as `limit` (not `count`) so the query is
// accepted by the shared server resolver and is byte-identical to a v1 URL. This is the one
// place the client mirrors the backend contract rather than inventing its own.

import type { RequestMeta } from "./types";

export interface FilterEdit {
  range?: string;
  from?: string;
  to?: string;
  project?: string | null;
  limit?: number;
}

function withView(pairs: Array<[string, string]>): string {
  return pairs.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&");
}

/** The set of views the client can navigate to. */
export const VIEWS = ["overview", "period", "trend", "cache", "dist"] as const;
export type ViewName = (typeof VIEWS)[number];

/** The current view from a query string (defaults to overview for a bare/unknown value). */
export function viewFromSearch(search: string): ViewName {
  const value = new URLSearchParams(search.replace(/^\?/, "")).get("view");
  return (VIEWS as readonly string[]).includes(value ?? "") ? (value as ViewName) : "overview";
}

/** Query that selects a single day (used by click-to-drill on the daily chart). */
export function dayQuery(view: string, base: RequestMeta, day: string): string {
  return buildQuery(view, base, { from: day, to: day });
}

/** Query for a preset button (delegates window maths to the server), keeping the view. */
export function presetQuery(view: string, base: RequestMeta, range: string): string {
  return buildQuery(view, base, { range });
}

/** Query that switches to another view while keeping the current resolved window/project. */
export function viewQuery(view: string, base: RequestMeta): string {
  return buildQuery(view, base, {});
}

/**
 * Build a query string for `view` from the current resolved request plus an edit. A `range`
 * edit drops explicit from/to (they are mutually exclusive server-side); otherwise explicit
 * dates are carried. Non-default limit/order/project are preserved.
 */
export function buildQuery(view: string, base: RequestMeta, edit: FilterEdit): string {
  const pairs: Array<[string, string]> = [["view", view]];
  if (edit.range !== undefined) {
    pairs.push(["range", edit.range]);
  } else {
    const from = edit.from ?? base.from;
    const to = edit.to ?? base.to;
    pairs.push(["from", from], ["to", to]);
  }
  const project = edit.project !== undefined ? edit.project : base.project;
  if (project) {
    pairs.push(["project", project]);
  }
  const limit = edit.limit !== undefined ? edit.limit : base.limit;
  if (limit !== 40) {
    pairs.push(["limit", String(limit)]);
  }
  if (base.order && base.order !== "desc") {
    pairs.push(["order", base.order]);
  }
  return withView(pairs);
}

/** The query portion of the current location, without the leading "?". */
export function currentSearch(): string {
  return location.search.replace(/^\?/, "");
}
