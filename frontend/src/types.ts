// Mirror of the JSON emitted by /v2/api/overview. The shape is a pure transcription of
// metsuke.viewmodel.overview.OverviewModel via to_jsonable, wrapped with request/freshness
// metadata by metsuke.dashboard2.web.overview_payload. Keep this in lock-step with the
// committed test/fixture.json (which is generated from the real serializer).

export interface Money {
  raw: number | null;
  display: string;
}

export interface Comparison {
  current: number;
  previous: number;
  percent_change: number | null;
  display: string;
}

export interface Kpi {
  name: string;
  value: number;
  display: string;
  comparison: Comparison;
}

export interface CostPart {
  name: string;
  amount: Money;
}

export interface DailyCost {
  day: string;
  amount: Money;
  selected: boolean;
}

export interface RankedPrompt {
  prompt_id: string;
  session_id: string;
  project: string | null;
  ts: number;
  text: string | null;
  request_count: number;
  context_peak: number;
  amount: Money;
}

export interface RankedSession {
  session_id: string;
  project: string | null;
  first_ts: number;
  last_ts: number;
  request_count: number;
  prompt_count: number;
  amount: Money;
}

export interface CacheRebuild {
  cause: string;
  request_count: number;
  amount: Money;
}

export interface WindowModel {
  start: string;
  end: string;
  project: string | null;
  label: string;
}

export interface OverviewModel {
  window: WindowModel;
  previous_window: WindowModel;
  timezone: string;
  kpis: Kpi[];
  daily_costs: DailyCost[];
  cost_parts: CostPart[];
  top_prompts: RankedPrompt[];
  top_sessions: RankedSession[];
  cache_rebuilds: CacheRebuild[];
  unknown_cost_request_count: number;
}

export interface RequestMeta {
  view: string;
  preset: string;
  from: string;
  to: string;
  project: string | null;
  limit: number;
  order: string;
  canonical_query: string;
}

export interface Freshness {
  stale: boolean;
  last_ingest: number | null;
  age_seconds: number | null;
}

export interface OverviewResponse {
  request: RequestMeta;
  freshness: Freshness;
  model: OverviewModel;
}

// --- Serialized node tree (period / dist) ------------------------------------
// A pure transcription of metsuke.viewmodel.common's Node/Cell/Column/Row via to_jsonable.
// The Python view models (period.query / dist.query) return a LegacyViewModel whose `body`
// is a Node tree; the client renders it generically with the NodeView component. Keep this
// in lock-step with the committed test/fixture-{period,dist}.json (real-serializer output).

export interface SNode {
  kind: string;
  args: unknown[];
  kwargs: Record<string, unknown>;
}

export interface SColumn {
  label: string;
  cls: string;
  sortable: boolean;
  sort_dir: string;
}

export interface SCell {
  text: string | Money;
  cls: string;
  sort: string | number | null;
  title: string | null;
  bar: number | null;
  content: SNode | null;
  clip: string;
  dot: string | null;
  warn: boolean;
}

export interface SRow {
  cells: SCell[];
  highlight: boolean;
}

export interface NodeViewModel {
  title: string;
  period: string;
  total: SNode | string;
  body: SNode;
  timezone: string;
}

export interface ViewResponse {
  request: RequestMeta;
  freshness: Freshness;
  model: NodeViewModel;
}
