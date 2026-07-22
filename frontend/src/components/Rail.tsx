// The persistent context/filter rail — v2's structural signature, replacing v1's top
// chrome strip. It shows the resolved window, the preset chips, an explicit date/project/
// count form, and a theme control. Every change builds a query string and asks the app to
// navigate (pushState + refetch); the rail itself holds no data state beyond the form.
import type { RequestMeta } from "../types";
import { PRESETS, PRESET_LABELS } from "../tokens";
import { buildQuery, presetQuery } from "../url";

export type ThemeChoice = "auto" | "light" | "dark";

const THEME_CYCLE: Record<ThemeChoice, ThemeChoice> = { auto: "light", light: "dark", dark: "auto" };
const THEME_LABEL: Record<ThemeChoice, string> = { auto: "自動", light: "ライト", dark: "ダーク" };

export function Rail({
  view,
  request,
  timezone,
  theme,
  onTheme,
  onNavigate,
}: {
  view: string;
  request: RequestMeta;
  timezone: string;
  theme: ThemeChoice;
  onTheme: (choice: ThemeChoice) => void;
  onNavigate: (query: string) => void;
}) {
  function submit(event: Event): void {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    const value = (name: string): string =>
      (form.elements.namedItem(name) as HTMLInputElement | null)?.value ?? "";
    const project = value("project").trim();
    const limit = Number(value("limit"));
    onNavigate(
      buildQuery(view, request, {
        from: value("from"),
        to: value("to"),
        project: project === "" ? null : project,
        limit: Number.isFinite(limit) && limit >= 1 ? Math.floor(limit) : request.limit,
      }),
    );
  }

  return (
    <aside class="rail">
      <div class="brand">
        <span class="brand-name">metsuke</span>
        <span class="brand-tag">v2</span>
      </div>

      <div class="window-card">
        <p class="window-preset">{PRESET_LABELS[request.preset] ?? request.preset}</p>
        <p class="window-range">
          {request.from} <span class="range-sep">→</span> {request.to}
        </p>
        {request.project ? <p class="window-proj">project: {request.project}</p> : null}
        {timezone ? <p class="window-tz">TZ {timezone}</p> : null}
      </div>

      <nav class="rail-presets" aria-label="期間プリセット">
        {PRESETS.map((preset) => (
          <button
            key={preset.value}
            type="button"
            class={request.preset === preset.value ? "preset preset-on" : "preset"}
            aria-current={request.preset === preset.value ? "true" : undefined}
            onClick={() => onNavigate(presetQuery(view, request, preset.value))}
          >
            {preset.label}
          </button>
        ))}
      </nav>

      <form class="rail-form" onSubmit={submit}>
        <label class="field">
          <span class="field-label">開始</span>
          <input type="date" name="from" max={request.to} value={request.from} required />
        </label>
        <label class="field">
          <span class="field-label">終了</span>
          <input type="date" name="to" min={request.from} value={request.to} required />
        </label>
        <label class="field">
          <span class="field-label">project</span>
          <input name="project" maxLength={1024} value={request.project ?? ""} placeholder="全て" />
        </label>
        <label class="field">
          <span class="field-label">件数</span>
          <input type="number" name="limit" min={1} max={200} value={request.limit} />
        </label>
        <button type="submit" class="apply">
          適用
        </button>
      </form>

      <div class="rail-foot">
        <button
          type="button"
          class="theme-toggle"
          aria-label={`テーマ: ${THEME_LABEL[theme]}`}
          onClick={() => onTheme(THEME_CYCLE[theme])}
        >
          テーマ: {THEME_LABEL[theme]}
        </button>
      </div>
    </aside>
  );
}
