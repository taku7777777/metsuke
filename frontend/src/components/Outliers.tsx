// Outlier tables (high-cost prompts / sessions). Each row carries a cost magnitude bar
// (SVG geometry, no inline width style), a cumulative-share %, project, req/context/duration,
// a truncated prompt/session head with the full text on hover, and a REAL drill link to the
// v1 detail page (/prompts/<id>, /sessions/<id>). Clicking a sortable header re-sorts the
// rows instantly, client-side, by each cell's raw orderable key — the displayed text is
// never parsed. Rank and cumulative % are fixed attributes computed in the server's cost
// order, so they stay meaningful after a client re-sort.
import type { ComponentChildren } from "preact";
import { useState } from "preact/hooks";
import type { RankedPrompt, RankedSession } from "../types";
import { duration, ratio, truncate } from "../format";

type SortDir = "asc" | "desc";

interface Column<T> {
  label: string;
  cls: string;
  sortable: boolean;
  defaultDir?: SortDir;
  sortKey?: (row: T) => number | string;
  render: (row: T) => ComponentChildren;
}

interface Ranked<T> {
  row: T;
  rank: number;
  cumulative: number;
}

function MagBar({ value, maximum }: { value: number | null; maximum: number }) {
  const filled = ratio(value, maximum) * 100;
  return (
    <span class="bar-cell">
      <svg class="mag" viewBox="0 0 100 8" preserveAspectRatio="none" role="img" aria-hidden="true">
        <rect class="mag-track" x={0} y={0} width={100} height={8} />
        <rect class="mag-fill" x={0} y={0} width={filled} height={8} />
      </svg>
    </span>
  );
}

function augment<T>(rows: T[], cost: (row: T) => number): Array<Ranked<T>> {
  const listed = rows.reduce((sum, row) => sum + cost(row), 0);
  let running = 0;
  return rows.map((row, index) => {
    running += cost(row);
    return {
      row,
      rank: index + 1,
      cumulative: listed > 0 ? (running / listed) * 100 : 0,
    };
  });
}

function SortTable<T>({
  caption,
  columns,
  rows,
  cost,
  empty,
}: {
  caption: string;
  columns: Array<Column<T>>;
  rows: T[];
  cost: (row: T) => number;
  empty: string;
}) {
  const initial = columns.findIndex((c) => c.sortable && c.defaultDir);
  const [sort, setSort] = useState<{ index: number; dir: SortDir }>({
    index: initial < 0 ? -1 : initial,
    dir: initial < 0 ? "desc" : columns[initial]!.defaultDir!,
  });

  const ranked = augment(rows, cost);
  const active = sort.index >= 0 ? columns[sort.index] : undefined;
  if (active && active.sortKey) {
    const key = active.sortKey;
    const dir = sort.dir;
    ranked.sort((a, b) => {
      const x = key(a.row);
      const y = key(b.row);
      let cmp: number;
      if (typeof x === "number" && typeof y === "number") {
        cmp = x - y;
      } else {
        cmp = String(x).localeCompare(String(y), "ja");
      }
      return dir === "desc" ? -cmp : cmp;
    });
  }

  function toggle(index: number): void {
    setSort((prev) =>
      prev.index === index
        ? { index, dir: prev.dir === "desc" ? "asc" : "desc" }
        : { index, dir: columns[index]!.defaultDir ?? "desc" },
    );
  }

  if (rows.length === 0) {
    return <p class="empty">{empty}</p>;
  }

  return (
    <div class="table-wrap">
      <table class="rank-table">
        <caption class="sr-only">{caption}</caption>
        <thead>
          <tr>
            <th scope="col" class="rank">
              #
            </th>
            {columns.map((col, index) => {
              const isActive = sort.index === index;
              const ariaSort = !col.sortable
                ? "none"
                : isActive
                  ? sort.dir === "desc"
                    ? "descending"
                    : "ascending"
                  : "none";
              const caret = isActive ? (sort.dir === "desc" ? " ▼" : " ▲") : "";
              return col.sortable ? (
                <th
                  key={col.label}
                  scope="col"
                  class={col.cls}
                  data-sortable=""
                  tabindex={0}
                  aria-sort={ariaSort}
                  onClick={() => toggle(index)}
                  onKeyDown={(event: KeyboardEvent) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      toggle(index);
                    }
                  }}
                >
                  {col.label}
                  {caret}
                </th>
              ) : (
                <th key={col.label} scope="col" class={col.cls}>
                  {col.label}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {ranked.map((item) => (
            <tr key={item.rank}>
              <td class="rank">{item.rank}</td>
              {columns.map((col) => (
                <td key={col.label} class={col.cls}>
                  {col.render(item.row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function PromptsTable({ rows }: { rows: RankedPrompt[] }) {
  const maximum = rows.reduce((m, r) => Math.max(m, r.amount.raw ?? 0), 0);
  const cost = (r: RankedPrompt): number => r.amount.raw ?? 0;
  const cumulativeOf = new Map(augment(rows, cost).map((x) => [x.row, x.cumulative]));
  const columns: Array<Column<RankedPrompt>> = [
    {
      label: "コスト",
      cls: "num",
      sortable: true,
      defaultDir: "desc",
      sortKey: cost,
      render: (r) => (
        <span class="cost-cell">
          <MagBar value={r.amount.raw} maximum={maximum} />
          <span class="bar-figure">{r.amount.display}</span>
        </span>
      ),
    },
    {
      label: "累積",
      cls: "num",
      sortable: false,
      render: (r) => `${Math.round(cumulativeOf.get(r) ?? 0)}%`,
    },
    { label: "project", cls: "proj", sortable: false, render: (r) => r.project ?? "—" },
    {
      label: "req",
      cls: "num",
      sortable: true,
      sortKey: (r) => r.request_count,
      render: (r) => r.request_count.toLocaleString(),
    },
    {
      label: "文脈ピーク",
      cls: "num",
      sortable: true,
      sortKey: (r) => r.context_peak,
      render: (r) => r.context_peak.toLocaleString(),
    },
    {
      label: "prompt",
      cls: "head",
      sortable: false,
      render: (r) => {
        const text = r.text ?? "—";
        return (
          <a class="head-link" href={`/prompts/${encodeURIComponent(r.prompt_id)}`} title={text}>
            {truncate(text, 64)}
          </a>
        );
      },
    },
  ];
  return (
    <SortTable
      caption="高額prompt"
      columns={columns}
      rows={rows}
      cost={cost}
      empty="この期間に該当するpromptはありません。"
    />
  );
}

export function SessionsTable({ rows }: { rows: RankedSession[] }) {
  const maximum = rows.reduce((m, r) => Math.max(m, r.amount.raw ?? 0), 0);
  const cost = (r: RankedSession): number => r.amount.raw ?? 0;
  const cumulativeOf = new Map(augment(rows, cost).map((x) => [x.row, x.cumulative]));
  const columns: Array<Column<RankedSession>> = [
    {
      label: "コスト",
      cls: "num",
      sortable: true,
      defaultDir: "desc",
      sortKey: cost,
      render: (r) => (
        <span class="cost-cell">
          <MagBar value={r.amount.raw} maximum={maximum} />
          <span class="bar-figure">{r.amount.display}</span>
        </span>
      ),
    },
    {
      label: "累積",
      cls: "num",
      sortable: false,
      render: (r) => `${Math.round(cumulativeOf.get(r) ?? 0)}%`,
    },
    { label: "project", cls: "proj", sortable: false, render: (r) => r.project ?? "—" },
    {
      label: "session",
      cls: "head",
      sortable: false,
      render: (r) => (
        <a class="head-link" href={`/sessions/${encodeURIComponent(r.session_id)}`} title={r.session_id}>
          {r.session_id.slice(0, 8)}
        </a>
      ),
    },
    {
      label: "prompt",
      cls: "num",
      sortable: true,
      sortKey: (r) => r.prompt_count,
      render: (r) => r.prompt_count.toLocaleString(),
    },
    {
      label: "req",
      cls: "num",
      sortable: true,
      sortKey: (r) => r.request_count,
      render: (r) => r.request_count.toLocaleString(),
    },
    {
      label: "継続",
      cls: "num",
      sortable: true,
      sortKey: (r) => Math.max(0, r.last_ts - r.first_ts),
      render: (r) => duration(Math.max(0, r.last_ts - r.first_ts)),
    },
  ];
  return (
    <SortTable
      caption="高額session"
      columns={columns}
      rows={rows}
      cost={cost}
      empty="この期間に該当するsessionはありません。"
    />
  );
}
