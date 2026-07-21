# 03 — インターフェース: 提示3層と AIアナリスト

**提示3層の動線ハブ。** 各層の仕様の正は専用ディレクトリが持ち、本ファイルは
**層の役割分担・層をまたぐ共有契約（state.json）・層の外側（`/handoff`・AIアナリスト）**を扱う。

| 層 | 面 | 仕様の正 | 一言 |
|---|---|---|---|
| 常時（ambient） | statusline 1行 | [statusline/](statusline/) — [spec](statusline/spec.md) / [sensor](statusline/sensor.md) / [requirements](statusline/requirements.md) | ちら見でペースと故障に気づく |
| 瞬間（just-in-time） | hooks の警告・緊急/任意OS通知 | [hooks/](hooks/) — [spec](hooks/spec.md) / [contract](hooks/contract.md) / [requirements](hooks/requirements.md) | 判断の瞬間に代替行動を差し出す |
| 深掘り（deliberate） | ローカルdashboard（Stage 8）＋ `metsuke` CLI ＋ HTML | dashboard: [08-dashboard](08-dashboard.md)、CLI: [cli/](cli/) | GUIで迷わず探索し、CLI/HTMLで再現・深掘りする |

判断支援ビュー（pull型の精査層）の台帳は [07-views.md](07-views.md)。
**以下の §1〜§3 は動線上の位置づけを示す要約であり、数値・条件の正ではない。**

## 1. 常時（ambient）: statusline 1行

```
⛽$X/$BUDGET $R/h 着地$FORECAST | ⏵$P1 $P2 $P3 $P4 | sess $S ctx Ntok 🔥HH:MM | ⚠stale(異常時のみ)
```

本日累計/予算・wall-clock の直近30分の燃焼レート（色のみ・記号なし）・着地予測・実行中1件＋完了済み直近3件のコスト（黄≥$3、赤≥$7.5、詳細HTMLへのリンク）・
セッション累計のクライアント推計（第2推定器を常時視界に）・**context絶対トークン**・
**キャッシュ失効の絶対時刻**・計測ヘルス⚠。実装は `state.json` を読むだけ（<50ms）で、
同時に stdin JSON を spool へ記録するセンサーを兼ねる。

**鮮度⚠が沈黙故障の一次防衛線** — 見る場所と壊れたら気づく場所を同一にする。

> **仕様の正は [docs/statusline/](statusline/) 配下**。書式・フィールド・**色の閾値**は
> [spec.md](statusline/spec.md)、stdin契約・スロットル・第2推定器は
> [sensor.md](statusline/sensor.md)、要件と受入基準は [requirements.md](statusline/requirements.md)。
> 本節は動線上の位置づけを示す要約であり、数値の正ではない。

## 2. 瞬間（just-in-time）: hooks

判断の瞬間に**代替行動を1つ名指して**差し出す層。介入は9種で、**発火主体が2つに分かれる**のが
この層の設計の要:

- **hook（UserPromptSubmit）** — 送信の瞬間だけ効く: cold-cache警告・context水位警告・
  compact復旧注入・API換算の内部予算警告(50/80/100%、送信は停止しない)
- **ingester（launchd tick / hook 起動の sync）** — 手を止めていても届く: 暴走ガード・
  TTL事前通知・任意の高コスト通知。**hook はアイドル中に発火しない**ため、放置中に効かせたい介入は必ずこちら

規律: 全hookは「spool追記 or state.json参照」のみ（<10ms・fail-open・SQL/LLM禁止 — 規律テストで強制）。
発火は一回性 marker ＋ 日次上限（既定3発）で抑える。**自動ミュートはしない**（四半期の棚卸し項目）。
効果は発火から10分の論理時刻で判定し、**述語を定義できない介入は測らない**。

> 介入ごとの条件・文面・上限は [hooks/spec.md](hooks/spec.md)、イベント登録・出力形式・
> marker の受け渡しは [hooks/contract.md](hooks/contract.md)、
> 要件と受入基準は [hooks/requirements.md](hooks/requirements.md)。

## 3. 深掘り（deliberate）: dashboard＋`metsuke` CLI

人間の通常入口は、期間・project・観点を画面で変え、未生成traceへクリックで降りられる
ローカルdashboardへ移す（Stage 8・[ADR 0011](adr/0011-local-dashboard.md)）。dashboardは
AI/APIを呼ばず、SQLiteを`query_only`＋authorizerで問い合わせる。実装完了までは現行の
CLI＋自己完結HTMLを使う。

`metsuke`はAI・自動化・障害時fallbackとして維持し、3系統に分かれる:

| 系統 | 代表コマンド | 性格 |
|---|---|---|
| **調べる** | `today` `week` `explain` `trace` `view` `nudges` `roi` `task status` `ttl-review` `prices` `config` `doctor` | read-only。trace/export HTMLはオンデマンド生成物。dashboardと同じquery modelを使う |
| **記録する** | `mark` `done` `task start/attach/finish` `roi --add-cost` `regime` `invoice` `approve` | 人間の判断を **spool 経由**で台帳へ（`rebuild` を生き延びる） |
| **運転する** | `sync` `archive` `rebuild` `verify` `backup` `install` `uninstall` `unlock` `deadman` | 取込・保全・ローカル統合。`sync` が hook / launchd の主経路 |

`metsuke` は**AI の口も兼ねる** — 対応コマンドの `--json` は端末出力と同一スキーマを返す
（`--json` は全コマンドではない）。アドホックSQLは `datasette ledger.db`（呼んだ時だけ起動）。
台帳を変える提案の適用は **TTY での全文表示＋明示承認**を要する（`metsuke approve`）。

> 全コマンドの引数と既定値は [cli/commands.md](cli/commands.md)、`--json` 対応表・exit code・
> 承認ゲート・書き込み規律は [cli/contract.md](cli/contract.md)、
> 要件と受入基準は [cli/requirements.md](cli/requirements.md)。

## 4. state.json 契約（層をまたぐ共有インターフェース）

**statusline と hooks の両方が読む**ため、どちらの層にも属さない共有契約として本ファイルが持つ。
ingester が rollup のたびに**原子的 rename** で書き出し、ホットパスはこのファイル
**だけ**を読む（DBアクセス禁止）。

| キー | 内容 | 主な読み手 |
|---|---|---|
| `generated_at` / `freshness_ts` / `stale` | 書出時刻・最後に正常パースしたイベントts・⚠フラグ | 両方（鮮度ゲート） |
| `thresholds.coldcache_min_usd` | 「重いセッション」判定の下限（env の値を hook へ届ける経路） | hooks（cold-cache。[hooks/spec.md §2.1](hooks/spec.md)） |
| `today.{date,cost_usd,n_requests,budget_usd,pace_ratio,burn_rate_usd_h,landing_usd}` | 本日累計・予算・同曜日ペース比・燃焼レート・着地予測 | statusline（燃焼レート等）・日次レポート（ペース比等） |
| `week.*` / `month.*` | 週/月の累計と予算 | （現状どちらも未使用） |
| `last_prompt` | 直前プロンプトの実費と支配項 | ingester（任意の高コスト通知） |
| `sessions[sid].last_ts` | セッション最終活動（TTL の起点） | 両方 |
| `sessions[sid].cost_today_usd` / `context_tok` | セッション別の累計と context 量 | — |
| `sessions[sid].ttl_remaining_s` / `rebuild_cost_usd` | TTL残・再構築費の推定 | hooks（cold-cache） |
| `sessions[sid].inflight_prompt_ts` / `inflight_usd` | 進行中プロンプトの累積 | ingester（暴走ガード）・statusline |
| `sessions[sid].recent_prompts` | 完了済み直近3プロンプトのID・実費・中断フラグ・完了時刻・任意の`detail_url`（新しい順） | statusline |

実装は `src/metsuke/state.py::build()`。**鮮度ゲート（15分）は読み手側が各自かける** —
[statusline/spec.md §6](statusline/spec.md) / [hooks/contract.md §5](hooks/contract.md)。

未実装: `着手前見積りの分位点テーブル`（Stage 4以降・[hooks/spec.md §6](hooks/spec.md)）。

## 5. `/handoff` スキル（最重要の行動変容装置）

セッション切替の最大障壁は「文脈喪失の恐怖」。`/handoff` は現セッションから
**引き継ぎブリーフを自動生成して新セッションを開く**ワンコマンド。cold-cache警告・
context水位警告の「代替行動」は常にこれを名指す（行動のコストを下げない警告は無視される）。
ブリーフには判断構造（却下案と理由・Recovery Notes）を必須で含め、引き継ぎ後の再提案事故を防ぐ。

## 6. AIアナリスト（J7）

- **契約**: `SCHEMA.md`（表定義・不変条件: デデュープ規則・分岐扱い・恒等式の意味・既知の罠）と
  `METRICS.md`（1指標1節: 定義・単位・健全域・罠。**行動レバー一覧**と**日次境界＝ローカルタイム**の
  定義を含む）。Claude はこれを読み read-only SQL で自走する。
- **週次ジョブ**（launchd → `claude -p` + cost-analyst skill、モデル/effort固定・自己コストも計測対象）:
  1. 診断（週次差分のマクロ）→ 2. 帰属（どの行動レバーが動いたか — METRICS.md の一覧に基づく）→
  3. **施策提案はちょうど1本**（marker テンプレ付き）→ 4. 前週施策の効果判定（前後マクロ比較）→
  5. `reports/YYYY-Www.md` 執筆 → OS通知。
- **権限（宣言でなく強制）**: `--allowedTools` 許可リスト＋sqlite `mode=ro`＋Write は `reports/` と
  `spool/proposals/` のみ＋Bash/WebFetch/WebSearch 禁止（**egress遮断 = PII持ち出し経路の封鎖**）。
  提案（task_label・verdict・ナッジ規則変更案）は `metsuke approve` の人間承認を経て ingester が反映。
  アーカイブ由来テキストは untrusted data として扱い、**レポートの指示・提案セクションには
  生データ文字列を展開しない**（引用ブロックで区別）— 詳細は [ADR 0005](adr/0005-ai-analyst-least-privilege.md)。
- **レポートの書出し**: `reports/` へ直接書く（read-only原則の明示的例外・承認不要の読み物）。
  spool には写しを残す（archive保全）。欠報検知は reports/ の存在チェックなので ingester 停止と独立。
- **欠報検知**: 月曜朝に当週レポートの存在を検査、無ければプッシュ通知（デッドマンスイッチ）。
- 対話利用: 通常セッションの Claude も同じ口（SQL＋`metsuke --json`）でアドホック調査ができる。
