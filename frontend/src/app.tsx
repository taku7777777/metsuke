// Root of the client app. Owns: the async overview state, the theme choice, and URL-driven
// navigation. The URL query is the source of truth — the app fetches it verbatim, renders
// the resolved `request` the API returns, and canonicalizes the URL in place (replaceState)
// so bare/preset queries settle to explicit windows without polluting history. Preset/date
// edits pushState + refetch; popstate refetches. All of this is behaviour SSR cannot do
// without a full round-trip per interaction.
import { useCallback, useEffect, useState } from "preact/hooks";
import type { OverviewResponse, RequestMeta, ViewResponse } from "./types";
import { ApiError, fetchOverview, fetchView } from "./api";
import { currentSearch, dayQuery, viewFromSearch, viewQuery } from "./url";
import { staleAge } from "./format";
import { Rail, type ThemeChoice } from "./components/Rail";
import { ViewTabs } from "./components/ViewTabs";
import { NodeScreen } from "./components/NodeView";
import { Hero } from "./components/Hero";
import { DailyChart } from "./components/DailyChart";
import { Composition } from "./components/Composition";
import { PromptsTable, SessionsTable } from "./components/Outliers";

const THEME_KEY = "metsuke.v2.theme";

function readTheme(): ThemeChoice {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    if (stored === "light" || stored === "dark" || stored === "auto") {
      return stored;
    }
  } catch {
    // localStorage may be unavailable; fall back to auto.
  }
  return "auto";
}

function applyTheme(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === "auto") {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", choice);
  }
}

type Status =
  | { kind: "loading" }
  | { kind: "overview"; data: OverviewResponse }
  | { kind: "nodeview"; data: ViewResponse }
  | { kind: "error"; error: ApiError };

// Metadata the rail needs, kept independent of the content status so it survives view switches.
type RailMeta = { request: RequestMeta; timezone: string };

export function App() {
  const [status, setStatus] = useState<Status>({ kind: "loading" });
  const [theme, setTheme] = useState<ThemeChoice>(readTheme);
  const [rail, setRail] = useState<RailMeta | null>(null);
  const [view, setView] = useState<string>(() => viewFromSearch(currentSearch()));

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const load = useCallback((search: string) => {
    const target = viewFromSearch(search);
    setView(target);
    const onError = (error: unknown): void =>
      setStatus({ kind: "error", error: error instanceof ApiError ? error : new ApiError("error", 0) });
    const settle = (meta: RailMeta): void => {
      setRail(meta);
      if (currentSearch() !== meta.request.canonical_query) {
        history.replaceState(null, "", `?${meta.request.canonical_query}`);
      }
    };

    setStatus((prev) => (prev.kind === "loading" ? prev : { kind: "loading" }));
    if (target === "overview") {
      fetchOverview(search)
        .then((data) => {
          settle({ request: data.request, timezone: data.model.timezone });
          setStatus({ kind: "overview", data });
        })
        .catch(onError);
    } else {
      fetchView(target, search)
        .then((data) => {
          settle({ request: data.request, timezone: data.model.timezone });
          setStatus({ kind: "nodeview", data });
        })
        .catch(onError);
    }
  }, []);

  useEffect(() => {
    load(currentSearch());
    const onPop = (): void => load(currentSearch());
    addEventListener("popstate", onPop);
    return () => removeEventListener("popstate", onPop);
  }, [load]);

  const navigate = useCallback(
    (query: string) => {
      history.pushState(null, "", `?${query}`);
      load(query);
    },
    [load],
  );

  const selectView = useCallback(
    (target: string) => {
      navigate(rail ? viewQuery(target, rail.request) : `view=${target}`);
    },
    [navigate, rail],
  );

  const changeTheme = useCallback((choice: ThemeChoice) => {
    setTheme(choice);
    try {
      localStorage.setItem(THEME_KEY, choice);
    } catch {
      // persistence is best-effort
    }
  }, []);

  if (status.kind === "error") {
    return <ErrorScreen error={status.error} onRetry={() => load(currentSearch())} />;
  }

  return (
    <div class="layout">
      {rail ? (
        <Rail
          view={view}
          request={rail.request}
          timezone={rail.timezone}
          theme={theme}
          onTheme={changeTheme}
          onNavigate={navigate}
        />
      ) : (
        <aside class="rail rail-skeleton" aria-hidden="true" />
      )}
      <main class={status.kind === "loading" ? "cockpit is-loading" : "cockpit"}>
        <ViewTabs current={view} onSelect={selectView} />
        <Content key={view} status={status} onSelectDay={(day) =>
          status.kind === "overview" ? navigate(dayQuery("overview", status.data.request, day)) : undefined
        } />
      </main>
    </div>
  );
}

function Content({
  status,
  onSelectDay,
}: {
  status: Status;
  onSelectDay: (day: string) => void;
}) {
  if (status.kind === "overview") {
    return <Cockpit data={status.data} onSelectDay={onSelectDay} />;
  }
  if (status.kind === "nodeview") {
    return <NodeScreen model={status.data.model} />;
  }
  return <p class="loading-note">読み込み中…</p>;
}

function Cockpit({
  data,
  onSelectDay,
}: {
  data: OverviewResponse;
  onSelectDay: (day: string) => void;
}) {
  const { model, freshness } = data;
  return (
    <>
      {model.unknown_cost_request_count > 0 ? (
        <div class="warn-banner" role="status">
          <span class="warn-mark" aria-hidden="true">
            ⚠
          </span>
          未知価格の request が {model.unknown_cost_request_count.toLocaleString()} 件あります。表示コストは下限値です。
        </div>
      ) : null}
      {freshness.stale ? (
        <div class="stale-banner" role="status">
          <strong>台帳の取込が遅れています。</strong> 経過 {staleAge(freshness.age_seconds)}。過去データを表示しています。
        </div>
      ) : null}

      <Hero kpis={model.kpis} daily={model.daily_costs} />

      <section class="panel panel-wide">
        <div class="panel-head">
          <h2 class="panel-title">日次コスト</h2>
          <p class="panel-note">選択範囲を帯・境界線・強調バーで表示。日をクリックでその日に絞り込み。</p>
        </div>
        <div class="chart-frame">
          <DailyChart daily={model.daily_costs} onSelectDay={onSelectDay} />
        </div>
      </section>

      <section class="panel panel-wide">
        <div class="panel-head">
          <h2 class="panel-title">費目構成</h2>
        </div>
        <Composition parts={model.cost_parts} />
      </section>

      <div class="outlier-grid">
        <section class="panel">
          <div class="panel-head">
            <h2 class="panel-title">高額prompt</h2>
          </div>
          <PromptsTable rows={model.top_prompts} />
        </section>
        <section class="panel">
          <div class="panel-head">
            <h2 class="panel-title">高額session</h2>
          </div>
          <SessionsTable rows={model.top_sessions} />
        </section>
      </div>

      {model.cache_rebuilds.length > 0 ? (
        <section class="panel panel-wide">
          <div class="panel-head">
            <h2 class="panel-title">cache再作成</h2>
          </div>
          <div class="table-wrap">
            <table class="rank-table">
              <thead>
                <tr>
                  <th scope="col" class="head">
                    原因
                  </th>
                  <th scope="col" class="num">
                    req
                  </th>
                  <th scope="col" class="num">
                    金額
                  </th>
                </tr>
              </thead>
              <tbody>
                {model.cache_rebuilds.map((item) => (
                  <tr key={item.cause}>
                    <td class="head">{item.cause}</td>
                    <td class="num">{item.request_count.toLocaleString()}</td>
                    <td class="num">{item.amount.display}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}
    </>
  );
}

function ErrorScreen({ error, onRetry }: { error: ApiError; onRetry: () => void }) {
  const message =
    error.kind === "unauthorized"
      ? "セッションが失効しました。Metsuke.app から開き直してください。"
      : error.kind === "initial_sync"
        ? "初回同期が完了していません。取込後に再読み込みしてください。"
        : error.kind === "network"
          ? "サーバに接続できませんでした。"
          : "データを取得できませんでした。";
  return (
    <div class="error-screen" role="alert">
      <p class="error-title">{message}</p>
      {error.kind !== "unauthorized" ? (
        <button type="button" class="apply" onClick={onRetry}>
          再試行
        </button>
      ) : null}
    </div>
  );
}
