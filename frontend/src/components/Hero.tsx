// The hero: the headline cost metric with its previous-period delta and an integrated
// sparkline of the 31-day daily series, plus a compact strip of the secondary KPIs (each
// with its own delta). This is the deliberate departure from v1's uniform KPI card row —
// one dominant number with trend context, then everything else subordinate.
import type { DailyCost, Kpi } from "../types";
import { deltaGlyph } from "../format";

const SW = 340;
const SH = 64;
const PAD = 3;

function Sparkline({ daily }: { daily: DailyCost[] }) {
  const count = Math.max(1, daily.length);
  const values = daily.map((d) => d.amount.raw ?? 0);
  const max = values.reduce((m, v) => Math.max(m, v), 0) || 1;
  const stepX = (SW - PAD * 2) / Math.max(1, count - 1);
  const points = values.map((v, i) => {
    const x = PAD + i * stepX;
    const y = SH - PAD - (v / max) * (SH - PAD * 2);
    return { x, y, selected: daily[i]?.selected ?? false };
  });
  const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
  const area = `${line} L${points[points.length - 1]?.x.toFixed(1) ?? PAD} ${SH - PAD} L${PAD} ${SH - PAD} Z`;
  const marker = points.find((p) => p.selected) ?? points[points.length - 1];

  return (
    <svg
      class="sparkline"
      viewBox={`0 0 ${SW} ${SH}`}
      preserveAspectRatio="none"
      role="img"
      aria-label="日次コストの推移"
    >
      <path class="spark-area" d={area} />
      <path class="spark-line" d={line} />
      {marker ? <circle class="spark-dot" cx={marker.x} cy={marker.y} r={2.6} /> : null}
    </svg>
  );
}

function Delta({ kpi }: { kpi: Kpi }) {
  const glyph = deltaGlyph(kpi.comparison);
  return (
    <p class={`delta ${glyph.cls}`}>
      <span class="delta-mark" aria-hidden="true">
        {glyph.symbol}
      </span>
      <span class="delta-text">{glyph.text}</span>
    </p>
  );
}

export function Hero({ kpis, daily }: { kpis: Kpi[]; daily: DailyCost[] }) {
  const primary = kpis[0];
  const rest = kpis.slice(1);
  return (
    <section class="hero" aria-label="概要サマリ">
      <div class="hero-main">
        {primary ? (
          <div class="hero-metric">
            <p class="hero-name">{primary.name}</p>
            <p class="hero-value">{primary.display}</p>
            <Delta kpi={primary} />
          </div>
        ) : null}
        <div class="hero-spark">
          <Sparkline daily={daily} />
        </div>
      </div>
      <ul class="stat-strip">
        {rest.map((kpi) => (
          <li class="stat" key={kpi.name}>
            <p class="stat-name">{kpi.name}</p>
            <p class="stat-value">{kpi.display}</p>
            <Delta kpi={kpi} />
          </li>
        ))}
      </ul>
    </section>
  );
}
