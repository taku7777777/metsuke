// The only network calls in the app: same-origin GETs to /v2/api/<view>. CSP is
// `connect-src 'self'`, so these fetches are allowed and nothing else can leave the origin.
// Every view shares one envelope ({request, freshness, model}); only the model shape differs
// (overview's typed DTOs vs. period/dist's node tree), so one fetch helper serves all.
import type { OverviewResponse, ViewResponse } from "./types";

export class ApiError extends Error {
  readonly kind: string;
  readonly status: number;

  constructor(kind: string, status: number) {
    super(`view api ${status} (${kind})`);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
  }
}

async function fetchJson<T>(url: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, { headers: { Accept: "application/json" } });
  } catch {
    throw new ApiError("network", 0);
  }
  if (!response.ok) {
    let kind = response.status === 401 ? "unauthorized" : "error";
    try {
      const body = (await response.json()) as { error?: string };
      if (body && typeof body.error === "string") {
        kind = response.status === 401 ? "unauthorized" : body.error;
      }
    } catch {
      // A body-less error (e.g. 401 shell text) keeps the status-derived kind.
    }
    throw new ApiError(kind, response.status);
  }
  return (await response.json()) as T;
}

function apiUrl(view: string, search: string): string {
  const bare = search.replace(/^\?/, "");
  return bare ? `/v2/api/${view}?${bare}` : `/v2/api/${view}`;
}

export function fetchOverview(search: string): Promise<OverviewResponse> {
  return fetchJson<OverviewResponse>(apiUrl("overview", search));
}

/** Fetch a node-tree view (period/dist) — same envelope, rendered by the generic NodeView. */
export function fetchView(view: string, search: string): Promise<ViewResponse> {
  return fetchJson<ViewResponse>(apiUrl(view, search));
}
