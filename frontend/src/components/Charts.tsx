// The chart node kinds that trend.query / cache.query emit, reimplemented as theme-aware,
// CSP-safe SVG. Each mirrors the geometry of the canonical server renderer
// (metsuke/viewgen/render.py: line_chart / stacked_bars / volume_chart / cache_balance) —
// same margins, same nice-top axis, same index/time x-mapping — but restyled with the v2
// design system: structural colours (grid, axis, moving-average, regime, marker) come from
// CSS classes bound to theme variables, while per-series channel colours arrive in the node
// args and are emitted as SVG `fill=` / `stroke=` presentation attributes. Nothing here sets
// an inline `style=` or an `on*=` attribute (Preact listeners attach via addEventListener),
// so the strict CSP (style-src 'self', no unsafe-inline) holds. Every mark carries a native
// <title> so tooltips survive with JS disabled; the crosshair + readout + column de-emphasis
// are the JS-on hover affordance, matching the overview DailyChart.
import type { VNode } from "preact";
import { useState } from "preact/hooks";
import { niceTop } from "../format";

// ---- formatting (display only; never changes a served number) --------------------------
function money(value: number): string {
  return `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

// Python's %g: up to 6 significant digits, trailing zeros trimmed. Used for count axes.
function fmtG(value: number): string {
  return Number(value.toPrecision(6)).toString();
}

function lineFmt(value: number, moneyAxis: boolean, precision: number, unit: string): string {
  if (moneyAxis) {
    return money(value);
  }
  return (
    value.toLocaleString("en-US", {
      minimumFractionDigits: precision,
      maximumFractionDigits: precision,
    }) + unit
  );
}

// render.py._axis_label: monthly labels are YYYY-MM, everything else MM-DD. The labels arrive
// as ISO date strings (to_jsonable turns dt.date into isoformat), so slice — no Date parsing.
function axisLabel(iso: string, grain: string): string {
  return grain === "monthly" ? iso.slice(0, 7) : iso.slice(5, 10);
}

// Python date.weekday(): Monday=0 … Sunday=6. Compute in UTC to dodge the local-tz footgun.
function pyWeekday(iso: string): number {
  const [y, m, d] = iso.split("-").map(Number);
  return (new Date(Date.UTC(y ?? 1970, (m ?? 1) - 1, d ?? 1)).getUTCDay() + 6) % 7;
}

function centerX(index: number, count: number, ml: number, pw: number): number {
  return ml + ((index + 0.5) * pw) / count;
}

type Grid = { y: number; label: string };

function gridlines(top: number, ph: number, mt: number, fmt: (v: number) => string): Grid[] {
  return [0, 1, 2, 3, 4].map((step) => {
    const value = (top * step) / 4;
    return { y: mt + ph - (value / top) * ph, label: fmt(value) };
  });
}

// Shared crosshair + readout overlay for the bar/point charts.
function Hover({
  hover,
  count,
  ml,
  mr,
  mt,
  ph,
  pw,
  width,
  readout,
}: {
  hover: number | null;
  count: number;
  ml: number;
  mr: number;
  mt: number;
  ph: number;
  pw: number;
  width: number;
  readout: (index: number) => string;
}) {
  if (hover === null) {
    return null;
  }
  const x = centerX(hover, count, ml, pw);
  return (
    <>
      <line class="ch-crosshair" x1={x} y1={mt} x2={x} y2={mt + ph} />
      <text
        class="ch-readout"
        x={Math.min(width - mr, Math.max(ml, x))}
        y={mt - 4}
        text-anchor="middle"
      >
        {readout(hover)}
      </text>
    </>
  );
}

// Transparent per-column hit targets that drive the hover index (kept off the visible marks
// so the whole column stays hoverable, like DailyChart). Because this overlay sits ON TOP of
// the marks it would otherwise shadow their native <title> tooltips; each hit rect carries the
// column's readout as its own <title>, so a native browser tooltip still fires with the hover
// JS inert (per-segment titles stay under the overlay, but the column summary survives).
function HitCols({
  count,
  ml,
  mt,
  pw,
  ph,
  onHover,
  title,
}: {
  count: number;
  ml: number;
  mt: number;
  pw: number;
  ph: number;
  onHover: (index: number | null) => void;
  title: (index: number) => string;
}) {
  return (
    <>
      {Array.from({ length: count }, (_, i) => (
        <rect
          key={i}
          class="ch-hit"
          x={ml + (i * pw) / count}
          y={mt}
          width={pw / count}
          height={ph}
          onMouseEnter={() => onHover(i)}
          onFocus={() => onHover(i)}
          tabindex={0}
        >
          <title>{title(i)}</title>
        </rect>
      ))}
    </>
  );
}

// ---- stacked_bars ----------------------------------------------------------------------
export function StackedBars({
  days,
  series,
  colors,
  height = 280,
  width = 1150,
  moneyValues = true,
}: {
  days: string[];
  series: Record<string, number[]>;
  colors: Record<string, string>;
  height?: number;
  width?: number;
  moneyValues?: boolean;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const ML = 58;
  const MR = 16;
  const MT = 18;
  const MB = 38;
  const pw = width - ML - MR;
  const ph = height - MT - MB;
  const count = Math.max(1, days.length);
  const keys = Object.keys(series);
  const totals = days.map((_, i) => keys.reduce((sum, key) => sum + (series[key]?.[i] ?? 0), 0));
  const maxTotal = Math.max(0, ...totals);
  const top = niceTop(maxTotal);
  const barWidth = Math.max(3, (pw / count) * 0.68);
  const grid = gridlines(top, ph, MT, (v) => (moneyValues ? money(v) : fmtG(v)));

  return (
    <svg
      class="chart"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={moneyValues ? "期間別コストの積み上げ棒グラフ" : "期間別件数の積み上げ棒グラフ"}
      onMouseLeave={() => setHover(null)}
    >
      {grid.map((g, i) => (
        <g key={`g${i}`}>
          <line class="ch-grid" x1={ML} y1={g.y} x2={width - MR} y2={g.y} />
          <text class="ch-axis" x={ML - 7} y={g.y + 4} text-anchor="end">
            {g.label}
          </text>
        </g>
      ))}
      {days.map((day, index) => {
        const x = centerX(index, count, ML, pw);
        const dim = hover !== null && hover !== index;
        let base = MT + ph;
        const rects = keys.map((key) => {
          const value = series[key]?.[index] ?? 0;
          const segment = (value / top) * ph;
          base -= segment;
          const label = moneyValues ? money(value) : `${fmtG(value)}件`;
          return (
            <rect
              key={key}
              class={dim ? "ch-series ch-dim" : "ch-series"}
              x={x - barWidth / 2}
              y={base}
              width={barWidth}
              height={Math.max(0, segment)}
              fill={colors[key]}
            >
              <title>{`${axisLabel(day, "daily")} ${key} ${label}`}</title>
            </rect>
          );
        });
        const showTotal = totals[index]! > 0.8 * Math.max(1, maxTotal);
        return (
          <g key={day}>
            {pyWeekday(day) === 0 ? (
              <line class="ch-divider" x1={x} y1={MT} x2={x} y2={MT + ph} />
            ) : null}
            {rects}
            {showTotal ? (
              <text class="ch-value" x={x} y={base - 4} text-anchor="middle">
                {moneyValues ? money(totals[index]!) : fmtG(totals[index]!)}
              </text>
            ) : null}
            {index % 2 === 0 ? (
              <text
                class={pyWeekday(day) > 4 ? "ch-axis" : "ch-axis ch-weekday"}
                x={x}
                y={height - 12}
                text-anchor="middle"
              >
                {axisLabel(day, "daily")}
              </text>
            ) : null}
          </g>
        );
      })}
      <HitCols
        count={days.length}
        ml={ML}
        mt={MT}
        pw={pw}
        ph={ph}
        onHover={setHover}
        title={(i) =>
          `${axisLabel(days[i]!, "daily")} ${moneyValues ? money(totals[i]!) : `${fmtG(totals[i]!)}件`}`
        }
      />
      <Hover
        hover={hover}
        count={count}
        ml={ML}
        mr={MR}
        mt={MT}
        ph={ph}
        pw={pw}
        width={width}
        readout={(i) =>
          `${axisLabel(days[i]!, "daily")} ${moneyValues ? money(totals[i]!) : `${fmtG(totals[i]!)}件`}`
        }
      />
    </svg>
  );
}

// ---- cache_balance (dual-axis: $ bars left, w1h% ratio line right) ----------------------
const CB_COLORS = { read: "#2dd4bf", write: "#facc15", ratio: "#fb923c" };

export function CacheBalance({
  days,
  read,
  write5m,
  write1h,
}: {
  days: string[];
  read: number[];
  write5m: number[];
  write1h: number[];
}) {
  const [hover, setHover] = useState<number | null>(null);
  const width = 1150;
  const height = 240;
  const ML = 58;
  const MR = 50;
  const MT = 18;
  const MB = 35;
  const pw = width - ML - MR;
  const ph = height - MT - MB;
  const count = Math.max(1, days.length);
  const writes = days.map((_, i) => (write5m[i] ?? 0) + (write1h[i] ?? 0));
  const top = niceTop(Math.max(0, ...read, ...writes));
  const barWidth = (pw / count) * 0.25;
  const grid = gridlines(top, ph, MT, (v) => `$${v.toFixed(2)}`);
  const points = days
    .map((_, i) => {
      const write = writes[i]!;
      const ratio = write ? ((write1h[i] ?? 0) / write) * 100 : 0;
      return `${centerX(i, count, ML, pw)},${MT + ph - (ratio / 100) * ph}`;
    })
    .join(" ");

  const bar = (index: number, offset: number, value: number, color: string, name: string) => {
    const barHeight = (value / top) * ph;
    const x = centerX(index, count, ML, pw);
    return (
      <rect
        key={`${name}-${index}`}
        class={hover !== null && hover !== index ? "ch-series ch-dim" : "ch-series"}
        x={x + offset - barWidth / 2}
        y={MT + ph - barHeight}
        width={barWidth}
        height={Math.max(0, barHeight)}
        fill={color}
      >
        <title>{`${axisLabel(days[index]!, "daily")} ${name} ${money(value)}`}</title>
      </rect>
    );
  };

  return (
    <svg
      class="chart"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label="キャッシュ読み書き費用と1時間キャッシュ比率"
      onMouseLeave={() => setHover(null)}
    >
      {grid.map((g, i) => (
        <g key={`g${i}`}>
          <line class="ch-grid" x1={ML} y1={g.y} x2={width - MR} y2={g.y} />
          <text class="ch-axis" x={ML - 6} y={g.y + 4} text-anchor="end">
            {g.label}
          </text>
        </g>
      ))}
      {days.map((day, index) => (
        <g key={day}>
          {bar(index, -barWidth / 2, read[index] ?? 0, CB_COLORS.read, "read")}
          {bar(index, barWidth / 2, writes[index]!, CB_COLORS.write, "write")}
          <text class="ch-axis" x={centerX(index, count, ML, pw)} y={height - 10} text-anchor="middle">
            {axisLabel(day, "daily")}
          </text>
        </g>
      ))}
      <polyline class="ch-line" points={points} fill="none" stroke={CB_COLORS.ratio} stroke-width={2} />
      <HitCols
        count={days.length}
        ml={ML}
        mt={MT}
        pw={pw}
        ph={ph}
        onHover={setHover}
        title={(i) => `${axisLabel(days[i]!, "daily")} read ${money(read[i] ?? 0)} / write ${money(writes[i]!)}`}
      />
      <Hover
        hover={hover}
        count={count}
        ml={ML}
        mr={MR}
        mt={MT}
        ph={ph}
        pw={pw}
        width={width}
        readout={(i) => `${axisLabel(days[i]!, "daily")} read ${money(read[i] ?? 0)} / write ${money(writes[i]!)}`}
      />
    </svg>
  );
}

// ---- volume_chart (stacked bars + 7d moving average + marker bands + regime lines) ------
export interface Marker {
  ts_start: number;
  ts_end: number | null;
  category: string | null;
  verdict: string | null;
}
export interface Regime {
  ts: number;
  kind: string | null;
}

export function VolumeChart({
  labels,
  data,
  colors,
  moving,
  grain,
  loTs,
  hiTs,
  markers,
  regimes,
}: {
  labels: string[];
  data: Record<string, number[]>;
  colors: Record<string, string>;
  moving: number[] | null;
  grain: string;
  loTs: number;
  hiTs: number;
  markers: Marker[];
  regimes: Regime[];
}) {
  const [hover, setHover] = useState<number | null>(null);
  const width = 1150;
  const height = 280;
  const MR = 16;
  const MT = 18;
  const MB = 38;
  const count = Math.max(1, labels.length);
  const keys = Object.keys(data);
  const totals = labels.map((_, i) => keys.reduce((sum, key) => sum + (data[key]?.[i] ?? 0), 0));
  const top = niceTop(Math.max(0, ...totals, ...(moving ?? [])));
  const ML = Math.max(58, money(top).length * 7 + 14);
  const pw = width - ML - MR;
  const ph = height - MT - MB;
  const barWidth = Math.max(8, (pw / count) * 0.68);
  const grid = gridlines(top, ph, MT, money);
  const xpos = (ts: number): number => ML + ((ts - loTs) / (hiTs - loTs)) * pw;
  const movingPoints =
    moving && moving.length > 0
      ? moving.map((value, i) => `${centerX(i, count, ML, pw)},${MT + ph - (value / top) * ph}`).join(" ")
      : "";

  return (
    <svg
      class="chart"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label="期間別コストと施策・外生イベント"
      onMouseLeave={() => setHover(null)}
    >
      {markers.map((marker, i) => {
        const x1 = Math.max(ML, xpos(marker.ts_start));
        const x2 = Math.min(width - MR, xpos(marker.ts_end ?? hiTs));
        return (
          <rect key={`m${i}`} class="ch-marker" x={x1} y={MT} width={Math.max(1, x2 - x1)} height={ph}>
            <title>{`${marker.category ?? "marker"} · ${marker.verdict ?? "pending"}`}</title>
          </rect>
        );
      })}
      {grid.map((g, i) => (
        <g key={`g${i}`}>
          <line class="ch-grid" x1={ML} y1={g.y} x2={width - MR} y2={g.y} />
          <text class="ch-axis" x={ML - 7} y={g.y + 4} text-anchor="end">
            {g.label}
          </text>
        </g>
      ))}
      {labels.map((label, index) => {
        const x = centerX(index, count, ML, pw);
        const dim = hover !== null && hover !== index;
        let base = MT + ph;
        const rects = keys.map((key) => {
          const value = data[key]?.[index] ?? 0;
          const segment = (value / top) * ph;
          base -= segment;
          return (
            <rect
              key={key}
              class={dim ? "ch-series ch-dim" : "ch-series"}
              x={x - barWidth / 2}
              y={base}
              width={barWidth}
              height={Math.max(0, segment)}
              fill={colors[key]}
            >
              <title>{`${axisLabel(label, grain)} ${key} ${money(value)}`}</title>
            </rect>
          );
        });
        return (
          <g key={label}>
            {rects}
            {grain !== "daily" || index % 2 === 0 ? (
              <text class="ch-axis" x={x} y={height - 12} text-anchor="middle">
                {axisLabel(label, grain)}
              </text>
            ) : null}
          </g>
        );
      })}
      {movingPoints ? (
        <polyline class="ch-avg" points={movingPoints} fill="none" stroke-width={2}>
          <title>7日移動平均</title>
        </polyline>
      ) : null}
      {regimes.map((regime, i) => {
        const x = xpos(regime.ts);
        return (
          <line key={`r${i}`} class="ch-regime" x1={x} y1={MT} x2={x} y2={MT + ph} stroke-dasharray="4 3">
            <title>{regime.kind ?? ""}</title>
          </line>
        );
      })}
      <HitCols
        count={labels.length}
        ml={ML}
        mt={MT}
        pw={pw}
        ph={ph}
        onHover={setHover}
        title={(i) => `${axisLabel(labels[i]!, grain)} ${money(totals[i]!)}`}
      />
      <Hover
        hover={hover}
        count={count}
        ml={ML}
        mr={MR}
        mt={MT}
        ph={ph}
        pw={pw}
        width={width}
        readout={(i) => `${axisLabel(labels[i]!, grain)} ${money(totals[i]!)}`}
      />
    </svg>
  );
}

// ---- line_chart (multi-series line/scatter; broken across null gaps) --------------------
export function LineChart({
  labels,
  series,
  colors,
  unit,
  moneyAxis = false,
  grain = "weekly",
  fixedTop = null,
  precision = 0,
}: {
  labels: string[];
  series: Record<string, (number | null)[]>;
  colors: Record<string, string>;
  unit: string;
  moneyAxis?: boolean;
  grain?: string;
  fixedTop?: number | null;
  precision?: number;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const width = 1150;
  const height = 230;
  const MR = 16;
  const MT = 18;
  const MB = 38;
  const count = Math.max(1, labels.length);
  const names = Object.keys(series);
  const valid = names.flatMap((name) => series[name]!.filter((v): v is number => v !== null));
  const top = fixedTop || niceTop(Math.max(0, ...valid));
  const fmt = (v: number): string => lineFmt(v, moneyAxis, precision, unit);
  const ML = Math.max(58, fmt(top).length * 7 + 14);
  const pw = width - ML - MR;
  const ph = height - MT - MB;
  const grid = gridlines(top, ph, MT, fmt);

  return (
    <svg
      class="chart"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`${names.join("、")} の推移`}
      onMouseLeave={() => setHover(null)}
    >
      {grid.map((g, i) => (
        <g key={`g${i}`}>
          <line class="ch-grid" x1={ML} y1={g.y} x2={width - MR} y2={g.y} />
          <text class="ch-axis" x={ML - 7} y={g.y + 4} text-anchor="end">
            {g.label}
          </text>
        </g>
      ))}
      {names.map((name) => {
        const values = series[name]!;
        const segments: string[][] = [];
        let run: string[] = [];
        const dots: VNode[] = [];
        values.forEach((value, index) => {
          if (value === null) {
            if (run.length > 0) {
              segments.push(run);
              run = [];
            }
            return;
          }
          const x = centerX(index, count, ML, pw);
          const y = MT + ph - (value / top) * ph;
          run.push(`${x},${y}`);
          const dim = hover !== null && hover !== index;
          dots.push(
            <circle
              key={`${name}-${index}`}
              class={dim ? "ch-series ch-dim" : "ch-series"}
              cx={x}
              cy={y}
              r={3}
              fill={colors[name]}
            >
              <title>{`${axisLabel(labels[index]!, grain)} ${name} ${fmt(value)}`}</title>
            </circle>,
          );
        });
        if (run.length > 0) {
          segments.push(run);
        }
        return (
          <g key={name}>
            {segments.map((pts, si) =>
              pts.length > 1 ? (
                <polyline
                  key={si}
                  class="ch-line"
                  points={pts.join(" ")}
                  fill="none"
                  stroke={colors[name]}
                  stroke-width={2}
                />
              ) : null,
            )}
            {dots}
          </g>
        );
      })}
      {labels.map((label, index) => (
        <text
          key={index}
          class="ch-axis"
          x={centerX(index, count, ML, pw)}
          y={height - 12}
          text-anchor="middle"
        >
          {axisLabel(label, grain)}
        </text>
      ))}
      <HitCols
        count={labels.length}
        ml={ML}
        mt={MT}
        pw={pw}
        ph={ph}
        onHover={setHover}
        title={(i) => {
          const parts = names
            .filter((name) => series[name]![i] != null)
            .map((name) => `${name} ${fmt(series[name]![i] as number)}`);
          return `${axisLabel(labels[i]!, grain)}${parts.length ? ` ${parts.join(" / ")}` : ""}`;
        }}
      />
      {hover !== null ? (
        <line class="ch-crosshair" x1={centerX(hover, count, ML, pw)} y1={MT} x2={centerX(hover, count, ML, pw)} y2={MT + ph} />
      ) : null}
    </svg>
  );
}
