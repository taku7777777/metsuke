// The top view-nav bar (概要 / 期間 / 推移 / キャッシュ / 分布). Selecting a tab navigates to
// `view=<v>` (pushState + refetch of /v2/api/<v>), keeping the current window/project. Overview
// keeps its bespoke renderer; period/dist render through NodeView; 推移/キャッシュ are inert
// placeholders until a later task adds their endpoints. This is app chrome, not a node — so it
// deliberately uses distinct classes (.viewtab) and never the legend's .chip.
import { VIEWS } from "../url";

const LABELS: Record<string, string> = {
  overview: "概要",
  period: "期間",
  trend: "推移",
  cache: "キャッシュ",
  dist: "分布",
};

export function ViewTabs({
  current,
  onSelect,
}: {
  current: string;
  onSelect: (view: string) => void;
}) {
  return (
    <nav class="view-tabs" aria-label="ビュー切り替え">
      {VIEWS.map((view) => {
        const active = current === view;
        return (
          <button
            key={view}
            type="button"
            class={active ? "viewtab viewtab-on" : "viewtab"}
            aria-current={active ? "page" : undefined}
            onClick={() => onSelect(view)}
          >
            {LABELS[view] ?? view}
          </button>
        );
      })}
    </nav>
  );
}
