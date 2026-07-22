// Presentation helpers. IMPORTANT: cost/count *displays* always come verbatim from the
// server (`Money.display`, `Kpi.display`, `Comparison.display`); nothing here re-formats a
// raw number into a dollar figure or a count. These helpers only derive geometry (bar
// ratios, chart coordinates) and non-numeric chrome (day labels, durations, arrows).

import type { Comparison } from "./types";

export interface DeltaGlyph {
  symbol: string;
  cls: string;
  text: string;
}

/** Arrow + sign-colour class from the comparison; text is the server display verbatim. */
export function deltaGlyph(cmp: Comparison): DeltaGlyph {
  const change = cmp.percent_change;
  if (change === null) {
    return { symbol: "•", cls: "delta-na", text: "比較不能" };
  }
  if (change > 0) {
    return { symbol: "▲", cls: "delta-up", text: `前期比 ${cmp.display}` };
  }
  if (change < 0) {
    return { symbol: "▼", cls: "delta-down", text: `前期比 ${cmp.display}` };
  }
  return { symbol: "→", cls: "delta-flat", text: `前期比 ${cmp.display}` };
}

/** "MM-DD" from an ISO date, without pulling in a date library. */
export function shortDay(iso: string): string {
  return iso.length >= 10 ? iso.slice(5, 10) : iso;
}

/** A rounded axis maximum a hair above `value` (1 / 2 / 5 x 10^n ladder). */
export function niceTop(value: number): number {
  if (value <= 0) {
    return 1;
  }
  const raw = value / 4;
  const power = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 5, 10].map((m) => m * power).find((candidate) => candidate >= raw);
  return (step ?? 10 * power) * 4;
}

/** Human duration from a second span; non-numeric chrome, never a cost. */
export function duration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  if (total >= 3600) {
    return `${Math.floor(total / 3600)}時間${Math.floor((total % 3600) / 60)}分`;
  }
  if (total >= 60) {
    return `${Math.floor(total / 60)}分`;
  }
  return `${total}秒`;
}

/** Clamp a ratio into [0, 1] for magnitude bars; safe when maximum is 0. */
export function ratio(value: number | null, maximum: number): number {
  if (value === null || maximum <= 0) {
    return 0;
  }
  return Math.min(1, Math.max(0, value / maximum));
}

export function truncate(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`;
}

export function staleAge(seconds: number | null): string {
  if (seconds === null) {
    return "不明";
  }
  const total = Math.max(0, Math.floor(seconds));
  if (total >= 86400) {
    return `${Math.floor(total / 86400)}日${Math.floor((total % 86400) / 3600)}時間`;
  }
  if (total >= 3600) {
    return `${Math.floor(total / 3600)}時間${Math.floor((total % 3600) / 60)}分`;
  }
  return `${Math.floor(total / 60)}分`;
}
