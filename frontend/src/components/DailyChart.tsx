// The interactive context chart: a fixed 31-day window of daily cost with the current
// selection marked THREE independent ways (translucent band + dashed edges + brighter bars)
// so it never leans on colour alone. Client-app affordances SSR cannot offer:
//   - hovering a day cross-highlights it (crosshair + readout, other bars dimmed);
//   - clicking a day drills the whole dashboard to that single day (URL + refetch).
// Every dynamic visual is an SVG presentation attribute or a toggled class — never an
// inline style — so the strict CSP (style-src 'self') holds.
import { useState } from "preact/hooks";
import type { DailyCost } from "../types";
import { niceTop, shortDay } from "../format";

const WIDTH = 1200;
const HEIGHT = 240;
const ML = 66;
const MR = 14;
const MT = 16;
const MB = 34;
const PW = WIDTH - ML - MR;
const PH = HEIGHT - MT - MB;

function centerX(index: number, count: number): number {
  return ML + ((index + 0.5) * PW) / count;
}

export function DailyChart({
  daily,
  onSelectDay,
}: {
  daily: DailyCost[];
  onSelectDay: (day: string) => void;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const count = Math.max(1, daily.length);

  const values = daily.map((d) => d.amount.raw).filter((v): v is number => v !== null);
  const top = niceTop(values.reduce((m, v) => Math.max(m, v), 0));
  const barWidth = Math.max(3, (PW / count) * 0.62);

  const selected = daily.map((d, i) => (d.selected ? i : -1)).filter((i) => i >= 0);
  const selLo = selected.length > 0 ? selected[0]! : -1;
  const selHi = selected.length > 0 ? selected[selected.length - 1]! : -1;
  const bandX1 = selLo >= 0 ? ML + (selLo * PW) / count : 0;
  const bandX2 = selHi >= 0 ? ML + ((selHi + 1) * PW) / count : 0;

  const gridlines = [0, 1, 2, 3, 4].map((step) => {
    const value = (top * step) / 4;
    const y = MT + PH - (value / top) * PH;
    return { y, label: `$${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}` };
  });

  const hovered = hover !== null ? daily[hover] : null;
  const hoverX = hover !== null ? centerX(hover, count) : 0;

  return (
    <svg
      class="daily-chart"
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      preserveAspectRatio="xMidYMid meet"
      role="img"
      aria-label="日次コスト（31日コンテキスト）"
      onMouseLeave={() => setHover(null)}
    >
      {selLo >= 0 ? (
        <rect class="ch-selband" x={bandX1} y={MT} width={Math.max(0, bandX2 - bandX1)} height={PH} />
      ) : null}
      {selLo >= 0 ? <line class="ch-seledge" x1={bandX1} y1={MT} x2={bandX1} y2={MT + PH} /> : null}
      {selLo >= 0 ? <line class="ch-seledge" x1={bandX2} y1={MT} x2={bandX2} y2={MT + PH} /> : null}

      {gridlines.map((g, i) => (
        <g key={`g${i}`}>
          <line class="ch-grid" x1={ML} y1={g.y} x2={WIDTH - MR} y2={g.y} />
          <text class="ch-axis" x={ML - 7} y={g.y + 4} text-anchor="end">
            {g.label}
          </text>
        </g>
      ))}

      {daily.map((d, i) => {
        const raw = d.amount.raw;
        const h = ((raw ?? 0) / top) * PH;
        const x = centerX(i, count);
        const dim = hover !== null && hover !== i;
        const base = d.selected ? "ch-bar-sel" : "ch-bar";
        const cls = `${base}${dim ? " ch-dim" : ""}${hover === i ? " ch-focus" : ""}`;
        return (
          <g key={d.day}>
            <rect
              class={cls}
              x={x - barWidth / 2}
              y={MT + PH - h}
              width={barWidth}
              height={Math.max(0, h)}
              tabindex={0}
              role="button"
              aria-label={`${d.day} ${d.amount.display}`}
              onMouseEnter={() => setHover(i)}
              onFocus={() => setHover(i)}
              onClick={() => onSelectDay(d.day)}
              onKeyDown={(event: KeyboardEvent) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  onSelectDay(d.day);
                }
              }}
            >
              <title>{`${shortDay(d.day)} ${d.amount.display}`}</title>
            </rect>
            {raw === null ? (
              <line
                class="ch-unknown"
                x1={x - barWidth / 2}
                y1={MT + PH}
                x2={x + barWidth / 2}
                y2={MT + PH}
              />
            ) : null}
            {i % 5 === 0 || i === count - 1 ? (
              <text class="ch-axis" x={x} y={HEIGHT - 12} text-anchor="middle">
                {shortDay(d.day)}
              </text>
            ) : null}
          </g>
        );
      })}

      {hovered ? (
        <line class="ch-crosshair" x1={hoverX} y1={MT} x2={hoverX} y2={MT + PH} />
      ) : null}
      {hovered ? (
        <text
          class="ch-readout"
          x={Math.min(WIDTH - MR, Math.max(ML, hoverX))}
          y={MT - 4}
          text-anchor="middle"
        >
          {`${shortDay(hovered.day)} ${hovered.amount.display}`}
        </text>
      ) : null}
    </svg>
  );
}
