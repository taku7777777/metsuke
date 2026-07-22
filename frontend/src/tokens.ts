// The six cost-part colour tokens. A colour maps to exactly one cost part and is reused
// identically as chart fills, stacked-bar segments, sparkline area, and legend swatches,
// so a hue always means one thing. These literal values are echoed as SVG `fill=`
// presentation attributes (CSP-safe) and are also declared as CSS variables in styles.css.
export const PART_ORDER = [
  "input",
  "output",
  "cache_read",
  "cache_w5m",
  "cache_w1h",
  "server_tool",
] as const;

export type PartName = (typeof PART_ORDER)[number];

export const PART_COLORS: Record<string, string> = {
  input: "#94a3b8",
  output: "#f472b6",
  cache_read: "#2dd4bf",
  cache_w5m: "#facc15",
  cache_w1h: "#fb923c",
  server_tool: "#818cf8",
};

export const PART_LABELS: Record<string, string> = {
  input: "入力",
  output: "出力",
  cache_read: "キャッシュ読取",
  cache_w5m: "キャッシュ書込5m",
  cache_w1h: "キャッシュ書込1h",
  server_tool: "サーバツール",
};

export const PRESETS: ReadonlyArray<{ label: string; value: string }> = [
  { label: "昨日", value: "yesterday" },
  { label: "今日", value: "today" },
  { label: "直近7日", value: "7d" },
  { label: "今月", value: "month" },
  { label: "先月", value: "last-month" },
];

export const PRESET_LABELS: Record<string, string> = {
  yesterday: "昨日",
  today: "今日",
  "7d": "直近7日",
  month: "今月",
  "last-month": "先月",
  custom: "カスタム",
};
