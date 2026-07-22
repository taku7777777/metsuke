// A generic, recursive renderer for the serialized Node tree that period.query / dist.query
// emit (via to_jsonable). Where the overview screen has bespoke components, period + dist are
// *data-driven*: the server ships a tree of {kind,args,kwargs} nodes and this component walks
// it. Every dynamic visual (magnitude bars, category dots, legend swatches) is an SVG
// presentation attribute (width=/fill=), never an inline style — the strict CSP forbids
// style=/on*=/runtime style injection, so all static styling lives in app.css and only class
// toggles + SVG attributes carry dynamic state. Preact event listeners (onClick/onKeyDown)
// attach via addEventListener and are NOT rendered as inline on* attributes, so they are
// CSP-safe.
//
// Tabs are the one stateful piece: period ships a `tabs` node plus sibling `panel` nodes that
// share a group id. A NodeList owns the active-tab map for its direct children, so clicking a
// tab flips which sibling panel is visible — client-side, no refetch, no URL change (a v2
// upgrade over v1's page-reload tabs). All panels stay mounted; inactive ones carry `hidden`.
import type { ComponentChildren } from "preact";
import { createContext } from "preact";
import { useContext, useState } from "preact/hooks";
import type { Money, NodeViewModel, SCell, SColumn, SNode, SRow } from "../types";
import type { Marker, Regime } from "./Charts";
import { CacheBalance, LineChart, StackedBars, VolumeChart } from "./Charts";

type SortDir = "asc" | "desc";
type ActiveMap = Record<string, string>;

interface Ctx {
  tabs?: ActiveMap;
  setTabs?: (updater: (prev: ActiveMap) => ActiveMap) => void;
}

// The grain axis (日次/週次/月次) is ONE global selector for a trend view, but its `grain_panel`
// siblings are scattered across the tree — some nested inside the v2-axis `panel` bodies, which
// NodeView renders WITHOUT threading the NodeList tab state. So grain can't ride the per-NodeList
// `tabs` mechanism; it needs a view-wide context. `grain_tabs` writes it, every `grain_panel`
// reads it to toggle `hidden`. NodeScreen provides one per screen; <Content key={view}> remounts
// on a view switch, so grain resets per view while surviving v2-axis (費目/モデル/…) switches.
interface GrainCtx {
  grain: string;
  setGrain: (grain: string) => void;
}
const GrainContext = createContext<GrainCtx>({ grain: "daily", setGrain: () => {} });

function isMoney(text: string | Money): text is Money {
  return typeof text === "object" && text !== null;
}

/** The display string of a cell's text (Money -> its server-formatted display, verbatim). */
function cellText(text: string | Money): string {
  return isMoney(text) ? text.display : (text ?? "");
}

/** Numeric when both sides parse as finite numbers; otherwise a Japanese-collated compare. */
function compareSort(a: string, b: string): number {
  const na = Number(a);
  const nb = Number(b);
  const aNum = a.trim() !== "" && Number.isFinite(na);
  const bNum = b.trim() !== "" && Number.isFinite(nb);
  if (aNum && bNum) {
    return na - nb;
  }
  return a.localeCompare(b, "ja");
}

function CellBar({ fraction }: { fraction: number }) {
  const filled = Math.min(100, Math.max(0, fraction * 100));
  return (
    <span class="bar-cell">
      <svg class="mag" viewBox="0 0 100 8" preserveAspectRatio="none" role="img" aria-hidden="true">
        <rect class="mag-track" x={0} y={0} width={100} height={8} />
        <rect class="mag-fill" x={0} y={0} width={filled} height={8} />
      </svg>
    </span>
  );
}

function CellDot({ color, title }: { color: string; title: string | null }) {
  return (
    <span class="cell-dot" title={title ?? undefined}>
      <svg class="dot" viewBox="0 0 10 10" role="img" aria-hidden="true">
        <circle cx={5} cy={5} r={4} fill={color} />
      </svg>
    </span>
  );
}

function CellBody({ cell }: { cell: SCell }) {
  const text = cellText(cell.text);
  const warnCls = cell.warn ? "cell-warn" : "";
  const pieces: ComponentChildren[] = [];
  if (cell.bar != null) {
    // magnitude bar + the (money) figure, aligned like the overview cost cell.
    pieces.push(
      <span class="cost-cell" key="bar">
        <CellBar fraction={cell.bar} />
        <span class={`bar-figure ${warnCls}`.trim()}>{text}</span>
      </span>,
    );
  } else if (cell.dot) {
    pieces.push(<CellDot key="dot" color={cell.dot} title={cell.title} />);
  } else if (text !== "") {
    const cls = `${cell.clip} ${warnCls}`.trim();
    const title = cell.title ?? (cell.clip ? text : undefined);
    pieces.push(
      cell.clip ? (
        <span key="clip" class={`clip-text ${cls}`.trim()} title={title}>
          {text}
        </span>
      ) : (
        <span key="text" class={cls || undefined} title={cell.title ?? undefined}>
          {text}
        </span>
      ),
    );
  }
  if (cell.content) {
    pieces.push(<NodeView key="content" node={cell.content} />);
  }
  return <>{pieces}</>;
}

function NodeTable({ columns, rows, foot }: { columns: SColumn[]; rows: SRow[]; foot: SCell[] | null }) {
  const initial = columns.findIndex(
    (col) => col.sortable && (col.sort_dir === "asc" || col.sort_dir === "desc"),
  );
  const [sort, setSort] = useState<{ index: number; dir: SortDir } | null>(
    initial >= 0 ? { index: initial, dir: columns[initial]!.sort_dir as SortDir } : null,
  );

  const ordered =
    sort && sort.index < columns.length
      ? [...rows].sort((ra, rb) => {
          const va = String(ra.cells[sort.index]?.sort ?? "");
          const vb = String(rb.cells[sort.index]?.sort ?? "");
          const cmp = compareSort(va, vb);
          return sort.dir === "desc" ? -cmp : cmp;
        })
      : rows;

  function toggle(index: number): void {
    setSort((prev) =>
      prev && prev.index === index
        ? { index, dir: prev.dir === "desc" ? "asc" : "desc" }
        : { index, dir: "desc" },
    );
  }

  return (
    <div class="table-wrap">
      <table class="rank-table node-table">
        <thead>
          <tr>
            {columns.map((col, index) => {
              const active = sort?.index === index;
              const ariaSort = !col.sortable
                ? undefined
                : active
                  ? sort!.dir === "desc"
                    ? "descending"
                    : "ascending"
                  : "none";
              const caret = active ? (sort!.dir === "desc" ? " ▼" : " ▲") : "";
              return col.sortable ? (
                <th
                  key={index}
                  scope="col"
                  class={col.cls || undefined}
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
                <th key={index} scope="col" class={col.cls || undefined}>
                  {col.label}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {ordered.map((row, ri) => (
            <tr key={ri} class={row.highlight ? "row-highlight" : undefined}>
              {row.cells.map((cell, ci) => (
                <td
                  key={ci}
                  class={cell.cls || undefined}
                  data-sort={cell.sort == null ? undefined : String(cell.sort)}
                >
                  <CellBody cell={cell} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
        {foot && foot.length > 0 ? (
          <tfoot>
            <tr>
              {foot.map((cell, ci) => (
                <td key={ci} class={cell.cls || undefined}>
                  <CellBody cell={cell} />
                </td>
              ))}
            </tr>
          </tfoot>
        ) : null}
      </table>
    </div>
  );
}

function InsightPanel({ children }: { children: ComponentChildren }) {
  return (
    <div class="node-insight" role="note">
      {children}
    </div>
  );
}

/** Collect the initial active tab per group from the direct children's `tabs` nodes. */
function collectTabs(nodes: SNode[]): ActiveMap {
  const map: ActiveMap = {};
  for (const node of nodes) {
    if (node && node.kind === "tabs") {
      const group = String(node.args[0] ?? "");
      const defs = (node.args[1] as [string, string, boolean][]) ?? [];
      const active = defs.find((def) => def[2]) ?? defs[0];
      if (active) {
        map[group] = active[0];
      }
    }
  }
  return map;
}

/** The 日次/週次/月次 selector: writes the view-wide grain context (client-side, no refetch). */
function GrainTabs({ defs }: { defs: [string, string, boolean, string | null][] }) {
  const { grain, setGrain } = useContext(GrainContext);
  return (
    <div class="tab-strip grain-strip" role="tablist">
      {defs.map(([id, label, , note]) => {
        const selected = id === grain;
        return (
          <button
            key={id}
            type="button"
            role="tab"
            class={selected ? "viewtab-btn is-active" : "viewtab-btn"}
            aria-selected={selected ? "true" : "false"}
            title={note ?? undefined}
            tabindex={selected ? 0 : -1}
            onClick={() => setGrain(id)}
            onKeyDown={(event: KeyboardEvent) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                setGrain(id);
              }
            }}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}

/** One grain's content; every grain_panel across the tree toggles off the same grain context. */
function GrainPanel({ grain, child }: { grain: string; child: SNode }) {
  const { grain: active } = useContext(GrainContext);
  const isActive = active === grain;
  return (
    <div
      class="tab-panel grain-panel"
      role="tabpanel"
      data-grain={grain}
      aria-current={isActive ? "true" : undefined}
      hidden={!isActive}
    >
      <NodeView node={child} />
    </div>
  );
}

/**
 * Renders an ordered list of sibling nodes, owning the active-tab state for any `tabs`/`panel`
 * groups among its direct children. Nested joins get their own NodeList (independent state).
 */
function NodeList({ nodes }: { nodes: SNode[] }) {
  const [tabs, setTabs] = useState<ActiveMap>(() => collectTabs(nodes));
  return (
    <>
      {nodes.map((node, index) => (
        <NodeView key={index} node={node} tabs={tabs} setTabs={setTabs} />
      ))}
    </>
  );
}

export function NodeView({ node, tabs, setTabs }: { node: SNode } & Ctx) {
  if (!node || typeof node !== "object" || typeof node.kind !== "string") {
    return null;
  }
  const args = node.args ?? [];
  const kwargs = node.kwargs ?? {};

  switch (node.kind) {
    case "join":
      return <NodeList nodes={args as SNode[]} />;

    case "card":
      return (
        <section class="node-card">
          <NodeList nodes={args as SNode[]} />
        </section>
      );

    case "block": {
      const cls = String((kwargs.cls as string) ?? "");
      return (
        <div class={`node-block ${cls}`.trim()}>
          <NodeList nodes={args as SNode[]} />
        </div>
      );
    }

    case "table": {
      const columns = (args[0] as SColumn[]) ?? [];
      const rows = (args[1] as SRow[]) ?? [];
      const foot = (kwargs.foot as SCell[] | null) ?? null;
      return <NodeTable columns={columns} rows={rows} foot={foot} />;
    }

    case "insight": {
      const text = String(args[0] ?? "");
      const facts = text.split("\n").filter((line) => line.trim() !== "");
      return (
        <InsightPanel>
          {facts.map((fact, index) => (
            <p key={index} class="insight-fact">
              {fact}
            </p>
          ))}
        </InsightPanel>
      );
    }

    case "insight_body":
      return (
        <InsightPanel>
          <NodeView node={args[0] as SNode} />
        </InsightPanel>
      );

    case "heading": {
      const level = Number(args[0] ?? 2);
      const text = String(args[1] ?? "");
      const Tag = (level <= 2 ? "h3" : level === 3 ? "h4" : "h5") as "h3" | "h4" | "h5";
      return <Tag class="node-heading">{text}</Tag>;
    }

    case "legend": {
      const items = (args[0] as [string, string][]) ?? [];
      return (
        <ul class="node-legend">
          {items.map(([name, color], index) => (
            <li key={index} class="node-chip">
              <svg class="swatch" viewBox="0 0 10 10" role="img" aria-hidden="true">
                <rect x={0} y={0} width={10} height={10} fill={color} />
              </svg>
              <span class="chip-name">{name}</span>
            </li>
          ))}
        </ul>
      );
    }

    case "tabs": {
      const group = String(args[0] ?? "");
      const defs = (args[1] as [string, string, boolean][]) ?? [];
      const activeId = tabs?.[group] ?? (defs.find((def) => def[2]) ?? defs[0])?.[0];
      return (
        <div class="tab-strip" role="tablist">
          {defs.map(([id, label]) => {
            const selected = id === activeId;
            const select = (): void => setTabs?.((prev) => ({ ...prev, [group]: id }));
            return (
              <button
                key={id}
                type="button"
                role="tab"
                id={`tab-${group}-${id}`}
                class={selected ? "viewtab-btn is-active" : "viewtab-btn"}
                aria-selected={selected ? "true" : "false"}
                aria-controls={`panel-${group}-${id}`}
                tabindex={selected ? 0 : -1}
                onClick={select}
                onKeyDown={(event: KeyboardEvent) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    select();
                  }
                }}
              >
                {label}
              </button>
            );
          })}
        </div>
      );
    }

    case "panel": {
      const group = String(args[0] ?? "");
      const tabId = String(args[1] ?? "");
      const child = args[2] as SNode;
      const activeId = tabs?.[group];
      const isActive = activeId != null ? activeId === tabId : Boolean(kwargs.active);
      return (
        <div
          class="tab-panel"
          role="tabpanel"
          id={`panel-${group}-${tabId}`}
          aria-labelledby={`tab-${group}-${tabId}`}
          aria-current={isActive ? "true" : undefined}
          hidden={!isActive}
        >
          <NodeView node={child} />
        </div>
      );
    }

    case "code":
      return <code class="node-code">{String(args[0] ?? "")}</code>;

    case "code_lines": {
      const lines = (args[0] as string[]) ?? [];
      return (
        <span class="node-code-lines">
          {lines.map((line, index) => (
            <code key={index} class="node-code">
              {line}
            </code>
          ))}
        </span>
      );
    }

    case "plain":
      return <span class="node-plain">{String(args[0] ?? "")}</span>;

    case "clip":
      return <span class="clip-text">{String(args[0] ?? "")}</span>;

    case "warning":
      return <span class="node-warn">{String(args[0] ?? "")}</span>;

    case "text_block": {
      const cls = String((kwargs.cls as string) ?? "");
      return <p class={`node-text-block ${cls}`.trim()}>{String(args[0] ?? "")}</p>;
    }

    case "grain_tabs": {
      const defs = (args[0] as [string, string, boolean, string | null][]) ?? [];
      return <GrainTabs defs={defs} />;
    }

    case "grain_panel":
      return <GrainPanel grain={String(args[0] ?? "")} child={args[1] as SNode} />;

    case "line_chart":
      return (
        <LineChart
          labels={(args[0] as string[]) ?? []}
          series={(args[1] as Record<string, (number | null)[]>) ?? {}}
          colors={(args[2] as Record<string, string>) ?? {}}
          unit={String(args[3] ?? "")}
          moneyAxis={Boolean(kwargs.money_axis)}
          grain={String(kwargs.grain ?? "weekly")}
          fixedTop={(kwargs.fixed_top as number | null) ?? null}
          precision={Number(kwargs.precision ?? 0)}
        />
      );

    case "stacked_bars":
      return (
        <StackedBars
          days={(args[0] as string[]) ?? []}
          series={(args[1] as Record<string, number[]>) ?? {}}
          colors={(args[2] as Record<string, string>) ?? {}}
          height={kwargs.height != null ? Number(kwargs.height) : undefined}
          width={kwargs.width != null ? Number(kwargs.width) : undefined}
          moneyValues={kwargs.money_values != null ? Boolean(kwargs.money_values) : undefined}
        />
      );

    case "volume_chart":
      return (
        <VolumeChart
          labels={(args[0] as string[]) ?? []}
          data={(args[1] as Record<string, number[]>) ?? {}}
          colors={(args[2] as Record<string, string>) ?? {}}
          moving={(args[3] as number[] | null) ?? null}
          grain={String(args[4] ?? "daily")}
          loTs={Number(args[5] ?? 0)}
          hiTs={Number(args[6] ?? 0)}
          markers={(args[7] as Marker[]) ?? []}
          regimes={(args[8] as Regime[]) ?? []}
        />
      );

    case "cache_balance":
      return (
        <CacheBalance
          days={(args[0] as string[]) ?? []}
          read={(args[1] as number[]) ?? []}
          write5m={(args[2] as number[]) ?? []}
          write1h={(args[3] as number[]) ?? []}
        />
      );

    default:
      // Unknown / not-yet-implemented kind: a labeled placeholder keeps the tree rendering
      // instead of crashing.
      return (
        <div class="node-unknown" data-kind={node.kind}>
          [未対応ブロック: {node.kind}]
        </div>
      );
  }
}

/** Top-level screen for a node-tree view (period/dist/trend/cache): header + body. Owns the
 * view-wide grain context so trend's one 日次/週次/月次 selector toggles every grain_panel. */
export function NodeScreen({ model }: { model: NodeViewModel }) {
  const [grain, setGrain] = useState("daily");
  return (
    <GrainContext.Provider value={{ grain, setGrain }}>
      <section class="view-screen">
        <header class="view-header">
          <h1 class="view-title">{model.title}</h1>
          <p class="view-period">{model.period}</p>
          <div class="view-total">
            {typeof model.total === "string" ? model.total : <NodeView node={model.total} />}
          </div>
        </header>
        <NodeView node={model.body} />
      </section>
    </GrainContext.Provider>
  );
}
