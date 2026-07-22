// Cost-part composition: one horizontal stacked bar + a legend of colour/name/amount chips.
// Segment colours are the shared six-token set as literal SVG `fill=` attributes (CSP-safe);
// the same tokens back the legend swatches. Hover a segment or chip to cross-highlight both.
import { useState } from "preact/hooks";
import type { CostPart } from "../types";
import { PART_COLORS, PART_LABELS } from "../tokens";

const WIDTH = 1000;
const HEIGHT = 30;

export function Composition({ parts }: { parts: CostPart[] }) {
  const [active, setActive] = useState<string | null>(null);
  const total = parts.reduce((sum, part) => sum + (part.amount.raw ?? 0), 0);

  let x = 0;
  const segments = parts.map((part) => {
    const value = part.amount.raw ?? 0;
    const width = total > 0 ? (value / total) * WIDTH : 0;
    const seg = (
      <rect
        key={part.name}
        class={active && active !== part.name ? "cmp-seg cmp-dim" : "cmp-seg"}
        x={x}
        y={0}
        width={width}
        height={HEIGHT}
        fill={PART_COLORS[part.name]}
        onMouseEnter={() => setActive(part.name)}
        onMouseLeave={() => setActive(null)}
      >
        <title>{`${PART_LABELS[part.name] ?? part.name} ${part.amount.display}`}</title>
      </rect>
    );
    x += width;
    return seg;
  });

  return (
    <div class="composition">
      <svg
        class="cmp-bar"
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        preserveAspectRatio="none"
        role="img"
        aria-label="費目別コスト構成"
      >
        {total > 0 ? null : <rect class="cmp-empty" x={0} y={0} width={WIDTH} height={HEIGHT} />}
        {segments}
      </svg>
      <ul class="legend">
        {parts.map((part) => (
          <li
            key={part.name}
            class={active && active !== part.name ? "chip chip-dim" : "chip"}
            onMouseEnter={() => setActive(part.name)}
            onMouseLeave={() => setActive(null)}
          >
            <svg class="swatch" viewBox="0 0 10 10" aria-hidden="true">
              <circle cx={5} cy={5} r={5} fill={PART_COLORS[part.name]} />
            </svg>
            <span class="chip-name">{PART_LABELS[part.name] ?? part.name}</span>
            <strong class="chip-value">{part.amount.display}</strong>
          </li>
        ))}
      </ul>
    </div>
  );
}
