# metsuke — コマンドリファレンス

提示3層の「深掘り（deliberate）」層。**`metsuke` の全コマンドの正**。
出力契約（`--json`・exit code・TTY・書き込み規律）は [contract.md](contract.md)、
要件は [requirements.md](requirements.md)。

実装: `src/metsuke/cli.py`（`main()` の `add_parser` 群が唯一の定義）。
**この表は `add_parser` に対して完全であること** — 未記載のコマンドがあると、
古いドキュメントの記述が実装とずれても誰も気づかない。

## 1. 調べる（read）

| コマンド | 内容 |
|---|---|
| `metsuke today [--json]` | 本日の支出サマリ |
| `metsuke week` | 直近7日サマリ（`--json` **非対応**） |
| `metsuke explain [<prompt_id>\|last] [--json] [--html] [--open]` | このプロンプトがなぜこの値段か（請求明細）。`--html` はセッション単位の自己完結HTMLを生成し該当プロンプトを初期選択 |
| `metsuke trace <session> [--focus <request_id>] [--json] [--html] [--open]` | セッション系譜。`--html` は trace/span ウォーターフォールを生成し**ストーリービューをランディング**（左=プロンプト・中央=レーン・右=本文・下=context。実時間全体ビューはサイドバー切替） |
| `metsuke view <period\|trend\|cache\|dist> [期間] [--project <slug>] [--as-of YYYY-MM-DD] [--open]` | 判断支援ビュー。`~/.metsuke/views/<name>.html` を上書き生成（台帳は [07-views](../07-views.md)・ADR 0007） |
| `metsuke nudges [--json]` | 介入の発火数、観測数、unknown数、観測済みだけを分母にしたconversion |
| `metsuke roi [--days N] [--json]` | 全期間または直近N日のツール収支（検証済み削減レンジ vs analyst・直接費・人時間） |
| `metsuke task status [--json]` | active taskと直近50タスクの費用・成果状況 |
| `metsuke ttl-review [--days N] [--json]` | TTL施策の証拠期間・稼働日・再構築費を検査し、継続候補か優先度低下かを返す |
| `metsuke prices [--json]` | 外部更新は行わず、UTC当日に適用される同梱単価・最終確認日・SCD2健全性を表示（[更新手順](../PRICES.md)） |
| `metsuke config [--json]` | `~/.metsuke/config.env` と環境変数を解決した実効設定 |
| `metsuke doctor [--json]` | 環境の自己診断（`launchd:<label>`・`state_freshness`・`v_health`・`hook_spool`・`restic_backup`・`claude_hooks`・`archive_manifest`・`disk_free`） |
| `metsuke dashboard <serve [--open]\|status\|stop>` | loopback限定dashboardの起動・状態確認・明示停止 |
| `metsuke notify-test` | macOS通知を1回送信し、osascript/ntfyの受理結果を表示 |

`metsuke view` の期間指定（排他）: `--days N` / `--today` / `--week [last]` /
`--month [last|YYYY-MM]` / `--from A --to B`。期間無指定時は直近14日。

HTML系コマンドの`--open`は、cmux内（`CMUX_WORKSPACE_ID`あり）では
`metsuke viewer` workspaceを新規作成し、そのworkspaceを明示してHTMLを開く。
cmux外ではmacOSの`open`を使う。cmuxでHTMLを開けなかった場合は空の新規workspaceを
閉じ、CLIはwarningを表示する。

## 2. 記録する（人間の判断を台帳へ）

| コマンド | 内容 |
|---|---|
| `metsuke mark start --category <c> --hypothesis "…" [--expected "…"]` | 施策マーカー開始（仮説・期待効果つき） |
| `metsuke mark end [<marker_id>]` | 終了（省略時は最新の open marker） |
| `metsuke mark verdict <marker_id> <win\|loss\|inconclusive> [--note] [--saving-usd N] [--saving-low-usd N] [--saving-high-usd N] [--saving-basis TEXT]` | 勝敗と削減額レンジを確定（**人間が打つ**） |
| `metsuke done <completed\|reverted\|abandoned\|partial> [--prompt <id\|last>]` | 成果ラベルの手動訂正 |
| `metsuke task start <title> --category <feature\|incident\|design\|refactor\|chore> [--goal] [--project]` | 実タスクを開始し、以降のpromptを自動帰属 |
| `metsuke task attach <task_id> [--prompt <id\|last>]` | 既存promptをタスクへ手動帰属 |
| `metsuke task finish [<task_id>] --outcome <completed\|partial\|abandoned> [--quality 1..5] [--rework-minutes N] [--note]` | 実タスクの成果・品質・手戻りを確定 |
| `metsuke roi --add-cost <maintenance\|review\|interruption\|storage\|other> [--minutes N] [--usd N] [--note]` | ツール自身の運用コストを追加 |
| `metsuke regime add <kind> <detail>` | 外生ショックの記録（CLAUDE.md改変・MCP追加・休暇週等） |
| `metsuke invoice [<YYYY-MM> <usd>] [--note] [--check YYYY-MM] [--json]` | 月次請求の手入力と突合 |
| `metsuke approve <proposal>` | AIアナリストの提案を承認。**提案全文を表示してから確認**（TTY必須・[contract.md §3](contract.md)） |

これらは全て **spool 経由**で記録され、次の sync で ingester が台帳へ反映する
（[ADR 0004](../adr/0004-sqlite-single-writer-spool.md)）。`metsuke rebuild` を生き延びる。

## 3. 運転する（取込・保全）

| コマンド | 内容 |
|---|---|
| `metsuke sync [--quiet]` | archive ＋ ingest ＋ state.json 書出。**hook と launchd tick が呼ぶ主経路**。ロック取得失敗時は黙って exit 0 |
| `metsuke archive` | アーカイバ1回分だけ（増分・冪等） |
| `metsuke rebuild` | ledger を捨ててアーカイブ全量から再構築（判断データは spool 原本から復元） |
| `metsuke verify [--sample N] [--path <rel>]` | アーカイブとソースの整合検証（既定10件をランダム抽出） |
| `metsuke backup` | `METSUKE_RESTIC_REPO`で明示したresticへ archive/reports/proposals/handoffs/configを暗号化バックアップ |
| `metsuke backup-verify` | manifestとsegmentを復元し、展開後SHAを照合 |
| `metsuke unlock [<minutes>] [--off]` | 後方互換用no-op。日次予算のハードストップ撤去後も旧スクリプトを壊さないため残置 |
| `metsuke deadman [--now <epoch>]` | 前週レポートの存在検査（欠報通知の実体） |
| `metsuke install [--with-git-hooks] [--git-root PATH] [--skip-claude-hooks] [--skip-statusline] [--skip-otel] [--skip-launchd]` | checkout型統合を冪等に設定・更新。Git hookは明示opt-in、既存hookは変更しない |
| `metsuke uninstall [--git-root PATH] [--apply] [--purge-data]` | dry-run既定。統合だけを除去し、purge時もデータはTrashへ移動 |

## 4. ドキュメント上の既知のズレ

過去のドキュメントが実装と食い違っていた点。**実装が正**。

| 記述 | 実際 |
|---|---|
| 「全コマンド `--json`」 | **一部のみ**。対応表は [contract.md §1](contract.md) |

`metsuke health` は**構想として取り下げ**（2026-07-20）。`metsuke doctor` が `v_health` を取り込むため統合済み。
旧構想の `metsuke tasks` は、成果を明示する `metsuke task status` と `v_task_efficiency` として再設計した。
