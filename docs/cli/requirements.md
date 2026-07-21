# metsuke — 要件と受入基準

[commands.md](commands.md)（何ができるか）・[contract.md](contract.md)（どう返すか）に対し、
本ファイルは**満たすべき性質と確認手段**の正。

## 1. 要件

| # | 要件 | 根拠 |
|---|---|---|
| R1 | **DB を書くのは `sync` / `rebuild` のみ**。他は read-only 接続 | 単一writer原則（[ADR 0004](../adr/0004-sqlite-single-writer-spool.md)） |
| R2 | 人間・AI の判断入力は**必ず spool 経由**で記録する | `metsuke rebuild` を生き延びさせる。DB直書きは復元不能な判断を生む |
| R3 | `sync` は**失敗しても exit 0**（fail-open・ロック競合は即座に諦める） | hook と launchd の主経路。取込の失敗で呼び出し側を壊さない |
| R4 | AI 向け出力は**端末出力と同一スキーマ**（`--json`） | 表示用に丸めた別系統を持つと、人間と AI が違う数字を見る |
| R5 | 台帳を変える提案の適用は **TTY での全文表示＋明示承認**を要する | 無人承認の経路を構造的に塞ぐ（[ADR 0005](../adr/0005-ai-analyst-least-privilege.md)） |
| R6 | trace/export HTMLはオンデマンド生成物。Stage 8のローカルdashboardだけを例外とし、DB readerは`query_only`＋authorizer・外部公開禁止 | [ADR 0006](../adr/0006-html-trace-view.md) / [ADR 0011](../adr/0011-local-dashboard.md) |
| R7 | `rebuild` は**決定性**を持つ（増分取込と同一結果） | 遡及再計算の保証。壁時計に依存する判定を作らない |
| R8 | [commands.md](commands.md) は `add_parser` に対して**完全**である | 未記載コマンドはドキュメントのズレを隠す（§4 のズレはこれで発生した） |

## 2. 受入基準

| # | 基準 | 確認手段 |
|---|---|---|
| A1 | 増分取込と `rebuild` の結果ハッシュが一致する | `test_ingest.py::test_incremental_ingest_and_rebuild_determinism` |
| A2 | 判断データ（marker・outcome・invoice・verdict）が `rebuild` を生き延びる | `test_judgment.py::test_judgment_cli_sequence_and_rebuild` |
| A3 | `approve` が TTY 不在で exit 1 し、台帳を変更しない | `test_judgment.py::test_approve_requires_tty_and_rejects_partial` |
| A4 | `view` が生成でき、期間指定が解決される（不正な期間は拒否） | `test_views.py::test_cli_view_path` / `test_window_resolution_boundaries_and_errors` |
| A5 | `doctor` が `v_health` の warn/fail を取り込み、fail で非0終了する | `test_stage5.py::test_doctor_json_and_fail_exit` |
| A6 | `invoice --check` が突合結果を返す | `test_stage5.py::test_invoice_check_and_rebuild` |
| A7 | `deadman` が前 ISO 週レポートの不在を検知する | `test_judgment.py::test_deadman_previous_iso_week` |
| A8 | アナリストの SQL 経路が書き込みを拒否する | `test_analyst.py::test_analyst_query_rejects_writes_and_multiple_statements` / `test_analyst_runner_has_no_write_escape` |
| A9 | `sync` がロック競合時に exit 0 を返し、**ingest を走らせない** | `test_state.py::test_sync_is_fail_open_on_lock_contention` |
| A10 | `sync` が ingest 例外時に exit 0 を返す | `test_state.py::test_sync_is_fail_open_on_ingest_error` |

コマンドを追加したら **commands.md の表・contract.md の `--json` 対応表・本表**の3箇所を同時に更新する。

## 3. エッジケース

| 状況 | 期待動作 |
|---|---|
| `ledger.db` 不在で読み取り系を実行 | `view` は「`metsuke sync` を先に実行してください」で exit 1。`doctor` は `v_health: ledger missing` を fail として報告 |
| `sync` が二重起動 | 後発は非ブロッキングで即 exit 0（`sync.lock`） |
| `rebuild` が `sync` と衝突 | ブロッキングで待って必ず実行 |
| `approve` の提案名にパス区切りを含む | `proposals_dir()` 外を指すため `proposal not found` で exit 1 |
| 同一提案の二重適用 | 適用済みは `applied-*.json` へ rename 済みのため見つからない |
| `explain`/`trace` の対象が存在しない | エラー終了（数字を捏造しない） |
| `verify` の照合不一致 | 該当を `❌` 表示し exit 1（黙って続けない） |
| 旧運用が `unlock` を呼ぶ | 成功するno-opとして扱い、送信可否へ影響しない |
| 未知モデルが混ざった | コスト NULL のまま。既定単価で埋めない（[01-architecture](../01-architecture.md)） |

## 4. 非目標

| 対象 | 判断 |
|---|---|
| 汎用・外部公開・リアルタイム監視dashboard | ADR 0003/0011。loopback限定のquery-onlyローカルUIだけを許可 |
| CLI からの DB 直書き | R1/R2。判断は spool 経由に統一 |
| 対話的な承認をスキップするフラグ（`--yes` 等） | R5。承認ゲートを迂回する経路を作らない |
| 出力の色・書式のカスタマイズ | 需要がない。AI 向けは `--json` が正 |
| リアルタイム追従・watch モード | 常時視界は statusline の担当 |

## 5. 変更時の手順

1. [commands.md](commands.md) にコマンド行を足す（引数・既定値まで）。
2. AI から使う可能性があるなら `--json` を実装し、[contract.md §1](contract.md) の表を更新。
3. exit code の意味を [contract.md §2](contract.md) の3値に収める（新しい意味を足さない）。
4. 本ファイルの受入基準に行を足し、対応するテストを追加。
5. 実装（`src/metsuke/cli.py`）→ `pytest`。
6. 運用手順に関わるなら [RUNBOOK.md](../RUNBOOK.md) も更新。
