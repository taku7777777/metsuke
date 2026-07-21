# 01 — アーキテクチャ

## 全体構成

```
[Claude Code 本体（改造不可）]
 ├─ transcripts ~/.claude/projects/**/*.jsonl          ← 一次ソース（実測: usage/agentId/tool_use_id/中断痕跡）
 │    └ <sid>/subagents/agent-*.jsonl + *.meta.json    ← サブエージェント個体（別ファイル方式）
 ├─ hooks（SessionStart/UserPromptSubmit/Stop/PreCompact/
 │        PostCompact/PostToolUse/Notification の7種）
 │        → spool/ へ1行NDJSON append ＋ state.json 参照のみ（<10ms・fail-open）
 │        契約は docs/hooks/contract.md（SubagentStop/SessionEnd は登録しない）
 ├─ statusline スクリプト
 │        表示: state.json を cat（<50ms）／同時にセンサー: stdin JSON の要点を spool へ append
 └─ OTelタップ: otelcol-contrib 1バイナリ（OTLP受→file exporter→spool/otel/）
          目的は3つだけ: query_source（背景機能）・effort・リモート/コンテナagentの受け口

[リモート/コンテナ agent] ─ OTLP ─→ 上記タップ（source=remote・低忠実度フラグ）

      ↓ hook駆動で起動（Stop/PostToolUse契機）＋ launchd 5分タイマーはフォールバック
      ↓ （常駐デーモンなし。多重起動はロックファイルで排他）

[ingester]  唯一のDB書き手（single-writer）。冪等・増分（ファイル別カーソル）
 ├─ 1. archive: 生ログを**無加工で**月別 zstd へ追記（不変・sha256台帳）
 ├─ 2. parse: デデュープ／分岐処理／サブエージェント連結／quarantine（詳細は 02）
 ├─ 3. load: ledger.db（SQLite WAL）へ。spool 経由の人間/AI入力（marker・label・invoice）もここで取込
 ├─ 4. rollup: day/hour 集計を materialize → state.json を原子的に書出（ホットパス用キャッシュ）
 └─ 5. health: 鮮度・取込ゼロ検知・第2推定器突合 → 異常は state.json の ⚠ フラグへ

[保存]  ledger.db（SQLite・事実のみ・金額列なし） + archive/（生ログ・永久） + 単価表/設定（git）

[導出]  SQLビュー（git管理・読み出し時導出）: コスト・恒等式・ペーシング・タスク効率・ヘルス
        重い分析は DuckDB が ledger.db を read-only ATTACH

[提示3層]                                  [AIアナリスト]
 常時   : statusline 1行（高コスト色/詳細）  launchd 週次 → claude -p + cost-analyst skill
 瞬間   : hooks 警告/任意OS通知                 read-only SQL → 診断・帰属・施策1本提案・前週判定
 深掘り : ローカルdashboard（Stage 8）,       → 週次md ＋ 提案は spool 経由（人間承認ゲート）
          metsuke CLI＋自己完結trace HTML
```

## データフローの原則

1. **書き込みは ingester の一本道**。hooks・statusline・CLI（`metsuke mark/done/invoice`）・AIアナリスト
   を含む全ての生産者は spool への追記だけを行い、DBに触るのは ingester のみ。
   SQLiteはWAL＋busy_timeoutで「単一writer＋複数reader」が安全に成立する（DuckDBを正典にしない
   理由 = クロスプロセスロック。[ADR 0004](adr/0004-sqlite-single-writer-spool.md)）。
2. **ホットパスの分離**。statusline と hooks の判定は ingester が焼いた `state.json`（原子的
   rename書き換え）だけを読む。ホットパスからのSQL実行・LLM呼び出しは**禁止**（テストで強制）。
3. **hook駆動＋タイマーフォールバック**。取込はStop等のhookで即時起動（利用と計測が運命共同体）、
   launchd 5分がフォールバック。ingesterは常駐せず「落ちている」状態を作らない。Stage 8の
   dashboardプロセスは提示だけを担う`query_only`＋authorizer付きreaderで、停止しても
   取込・statusline・hooksへ影響しない。
4. **遡及再計算の保証**。`metsuke rebuild` = 派生物（DBの派生テーブル・rollup）を全DROPし
   archive/ から全量再取込。人間/AI由来の判断データ（marker・label・invoice・verdict）は
   spool 原本が archive に残るため rebuild を生き残る（この区別を実装で保証する）。

## 故障検知の設計（沈黙故障を殺す）

| 検知対象 | 仕組み |
|---|---|
| 取込の停止・空振り | 鮮度 = **最後に正常パースしたイベントの時刻**（プロセス生存ではない）。さらに「hooksは発火しているのに新規requestが0」を独立検知（取込ゼロ検知）。ソース別（local/remote/otel）に鮮度を持つ |
| パーサの静かな誤読 | ①未知レコード型は quarantine 表に生JSONごと退避し件数監視 ②`version` フィールドの新値初観測でレコード形状アサーション＋通知 ③golden fixture（凍結サンプル＋期待値）による回帰テスト ④**第2推定器**: statusline stdin のクライアント推計コストと自前計算の日次自動突合（意味的パース破壊を検知できる唯一の自動カナリア） |
| 週次アナリストの欠報 | 月曜朝に reports/ の当週ファイル存在を検査し、無ければ**プッシュ型で通知**（欠報アラート。バッジ点灯のようなプル型にしない） |
| 単価の陳腐化 | 未知モデルはコストNULL＋statusline赤ドット（暗黙のデフォルト単価で誤魔化さない）。月次請求突合（二段分離: 02参照） |
| launchd/TCC の半死 | `metsuke doctor` が権限・plist・パス・鮮度を一括自己診断。OSアップデート後の実行をロードマップの運用規約に含める |

表示場所は常に statusline（毎日必ず見る場所に故障が出る）。⚠stale の点灯条件と
第2推定器の詳細は [statusline/spec.md §6](statusline/spec.md) /
[statusline/sensor.md §5](statusline/sensor.md)。

## セキュリティ / プライバシー

- archive/ と ledger.db は**プロンプト全文・コード・ツール結果を含むPII資産**。
  パーミッション700/600、外部SaaSへの平文送信なし。バックアップは restic 等の暗号化リポジトリのみ。
- **リダクションは二層方式**（アーカイブは無加工原本のまま）:
  - archive/ は**無リダクション原本**。誤検出が原本を不可逆破壊しないため（原本は約30日で
    上流から消えるので、破壊は回復不能）。保護は権限＋暗号化バックアップで担う。
  - リダクションは**読み出し境界**で適用: parse層（`ledger.prompt.text` 等）と、AIアナリストの
    アーカイブ参照（リダクション済みアクセサ経由）。パターンカタログは versioned manifest
    （`redaction_version`）として git 管理し、誤検出・パターン追加は**再parseだけで全履歴に反映**できる。
    検出ログは「パターン名＋sha256＋位置」のみ（平文を残さない）。
- **AIアナリストは最小権限（宣言でなく強制）**: `--allowedTools` 許可リスト・sqlite `mode=ro`・
  Write先の限定・Bash/Web系禁止（egress遮断）。書き込みは spool 経由の「提案」のみで人間承認
  ゲートを通る。アーカイブ由来のテキストは untrusted data として扱う
  （プロンプトインジェクション対策。[ADR 0005](adr/0005-ai-analyst-least-privilege.md)）。

## 技術スタック

| 部品 | 選定 | 理由 |
|---|---|---|
| 言語 | Python 3.12（依存最小、可能な限りstdlib） | 10年後も動く・保守者=本人+Claude |
| 正典ストア | SQLite（WAL） | クロスプロセスの単一writer+複数readerが枯れている |
| 分析エンジン | DuckDB（read-only ATTACH） | 窓関数・重い集計。正典にはしない |
| アーカイブ | zstd 圧縮 JSONL（月別） | 利用量に応じて増えるが圧縮後は月あたり数十〜数百MB程度。永久保持可能 |
| スケジューラ | launchd（StartInterval/カレンダー） | macOSでスリープ復帰後も発火 |
| TUI | Textual/rich（`metsuke explain/trace` の Tree+Waterfall） | 業界収斂の2ビューを端末で。AIも同じ出力を読める |
| アドホック探索 | Datasette（オンデマンド起動） | 常駐させない |
| 人間向け探索 | loopback限定ローカルWeb UI（Stage 8） | 期間filter・履歴・遅延trace生成をGUIで提供。DBはread-only（ADR 0011） |
| 通知 | terminal-notifier ＋ ntfy等のオフラップトップ経路（要調査06） | リモートagentの暴走はラップトップ外へ届ける必要 |
| OTel受け | otelcol-contrib 単一バイナリ（file exporter・~20行設定） | 自前OTLP実装はしない（保守負債） |
| AIアナリスト | claude -p ヘッドレス + skill | 週次launchd起動 |
| 版管理 | git（本リポジトリ: コード・単価表・ビュー定義・skill・レポート・設定） | 全構成as-code |
