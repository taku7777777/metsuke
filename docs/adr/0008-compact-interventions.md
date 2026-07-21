# ADR 0008: compact 介入 — 判断構造の保全

日付: 2026-07-18 / 状態: 採択

## 決定

auto-compact の手前と直後へ、次の3点を既存 nudge 枠組みでネイティブ実装する。

- statusline が context 60% を検知すると marker を置き、次の UserPromptSubmit で `/handoff` を
  one-shot 提案する。60% は statusline の既存色分け開始点と一致させる。
- PostCompact marker を次の UserPromptSubmit で消費し、圧縮サマリーを行動指示として扱わない
  additionalContext を one-shot 注入する。
- `/handoff` ブリーフ様式 v2 に、採用案の根拠・却下案と理由・Recovery Notes を必須化する。

hook は marker と state.json のみを読み書きし、SQL・LLM・ネットワークを使わない。参考実装は
u-ichi/compact-plus、背景理解は同著者の解説記事による。

## 根拠

Claude Code の auto-compact は要約時に却下案と理由・フェーズ前提などの「判断構造」を落とす。
圧縮後に却下案の再提案、検証スキップ、フェーズ混同が起きると迷走と再作業のコストになる。
圧縮前は新セッションへの意図的な区切りを安価な既定動作とし、圧縮された場合も再開規律を
直接注入することで、暗黙の要約を行動計画へ誤変換させない。

## 棄却した代替案

- **compact-plus プラグイン導入**: PreCompact の外部 LLM 呼出がコスト・レイテンシと hook 規律
  （<10ms・LLM禁止）に反する。UserPromptSubmit 注入が二重化し、TMPDIR marker と
  `~/.metsuke` の管理も二元化する。
- **PreCompact で LLM による state file 自動生成**: 同じ規律違反に加え、metsuke は
  `/handoff`（新セッション）を第一の代替行動とし、compact 前提のワークフローを増やさない。
- **compact-prep 型の手動 skill 追加**: 状態保存は handoff ブリーフ様式 v2 が担うため冗長。
