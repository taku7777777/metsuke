# hooks — 要件と受入基準

[spec.md](spec.md)（何を出すか）・[contract.md](contract.md)（どう繋がるか）に対し、
本ファイルは**満たすべき性質と確認手段**の正。介入の追加・変更はこの表を基準にレビューする。

## 1. 要件

| # | 要件 | 根拠 |
|---|---|---|
| R1 | **<10ms**・bash + jq のみ | プロンプト送信のたびに同期で走る |
| R2 | **fail-open**: 例外・欠損・環境不備で送信を妨げない（exit 0） | 計測ツールが仕事を止めてはならない |
| R3 | **DB に触らない**（SQL・LLM 禁止） | ホットパスの分離（[ADR 0004](../adr/0004-sqlite-single-writer-spool.md)） |
| R4 | 全ての警告に**代替行動を1つ**含める | 行動のコストを下げない警告は無視される |
| R5 | API換算額を根拠に**自動停止しない** | 定額購読の実上限と一致しない代理値で仕事を止めない |
| R6 | 発火は**一回性 marker ＋ 日次上限（既定3）**で必ず抑える | ナッジ疲れは介入の価値をゼロにする |
| R7 | 効果を測れない介入は**測らない**（`followed` を NULL に保つ） | それらしい代理指標は判断を誤らせる |
| R8 | state.json が**15分以上古ければ数値判定をスキップ**する | 古い数字での警告は誤報 |
| R9 | 判定は**論理時刻**（`fired_ts + 600`）で行う | `metsuke rebuild` の決定性 |
| R10 | アイドル中に効かせたい介入は **ingester 側**に置く | hook はアイドル中に発火しない（[spec.md §1](spec.md)） |

## 2. 受入基準

| # | 基準 | 確認手段 |
|---|---|---|
| A1 | spool へ `{metsuke_event, metsuke_ts, payload}` 形式で1行落ちる | `tests/test_hotpath.py::test_hook_sensor` |
| A2 | 予算 50 / 80% が**各1回だけ**発火し、段が正しい | `test_budget_warn_once_and_tier`（パラメタライズ） |
| A3 | 100%は一回だけ警告し、送信をblockしない。stale stateでは判定しない | `test_budget_100_warns_without_block_and_stale_is_silent` |
| A4 | coldcache が同一再開で二重発火せず、日次上限で止まり、nudge が spool へ落ちる | `test_coldcache_marker_cap_and_nudge_spool` |
| A5 | ctx警告 marker が UserPromptSubmit で消費される | `test_context_warn_hook_consumes_marker` |
| A6 | PostCompact が ctx警告を re-arm する | `test_postcompact_marker_rearms_context_warning` |
| A7 | compact_recovery が state.json 不在でも注入される（marker のみで成立） | `test_compact_recovery_without_fresh_state`（パラメタライズ） |
| A8 | 100%警告とcompact_recoveryが同じ応答で共存し、復旧markerを消費する | `test_compact_recovery_is_delivered_with_budget_100_warning` |
| A9 | PostToolUse は sync 起動のみ・30秒スロットルが効く | `test_posttooluse_trigger_only_and_throttle` |
| A10 | conversion が論理時刻で決まり、計測対象外ルールは NULL のまま | `tests/test_nudge.py` |
| A11 | スクリプト内に SQL・DB アクセスが存在しない | `test_hotpath_discipline`（静的検査） |

**新しい介入を足すときは A系に1行足せることを先に確認する。** 測れない介入は spec.md §5 の
「計測対象外」に明記して足す — 黙って測定対象に混ぜない。

## 3. エッジケース

| 状況 | 期待動作 | 担保 |
|---|---|---|
| `jq` 未導入 | 何もせず exit 0 | 冒頭の `command -v jq` |
| stdin が空・非JSON | 何もせず exit 0 | `[ -n "$input" ] \|\| exit 0` |
| state.json 不在・破損 | `fresh=false` として**数値判定を全スキップ**。marker 由来の介入は通常どおり動く | `[ -r "$state" ]` ＋ `generated_at` 判定 |
| state.json が15分以上古い | 同上（stale は「無い」と同じ扱い） | A3 で回帰 |
| 対象セッションが state.json に未登録 | coldcache・予算判定をスキップ | `.sessions[$sid] // empty` |
| 複数ルールが同時成立 | `systemMessage` を改行で連結し、`additionalContext` と共存 | §A8 |
| `session_id` に異常文字 | パス組み立てに使わせない | `tr -cd 'A-Za-z0-9._-'` |
| 日付をまたいだ | 日次カウンタは `-<date>` サフィックスで自然にリセット | marker 命名 |
| `metsuke sync` が重い・失敗する | hook の応答時間に影響しない | `nohup … &` で非同期・ロックで多重起動排他 |
| 旧 `unlock-until` marker が残る | 無視して通常どおり警告のみ | A3 |
| nudge_fired の再取込 | 二重計上しない | `nudge` PK ＋ `INSERT OR IGNORE` |

## 4. 非目標

| 対象 | 判断 |
|---|---|
| hookによる自動停止 | しない。継続判断は人間が持つ（R5） |
| 効かないルールの自動ミュート | しない。四半期の棚卸しで**人間が**文面改訂か廃止を決める |
| hook からの分析・SQL・LLM 呼び出し | R3。必要になった時点で ingester 側へ寄せる |
| アイドル中の hook 発火 | 不可能。ingester の launchd tick が担当（R10） |
| 介入文面の設定可能化 | 文面は設計そのもの（代替行動の名指し・R4）。可変にすると規律が崩れる |

## 5. 変更時の手順

1. [spec.md](spec.md) に介入行を足す（発火主体・条件・文面・上限を必ず埋める）。
2. 測れるかを判断し、[spec.md §5](spec.md) の計測対象／対象外へ振り分ける。
   測れるなら `ingest.py::_derive_nudges` の `measured` 集合と述語を追加。
3. 本ファイルの受入基準に行を足し、`tests/test_hotpath.py` / `tests/test_nudge.py` へテストを追加。
4. 実装（`scripts/hook-sensor.sh` または `src/metsuke/state.py`）→ `pytest tests/test_hotpath.py tests/test_nudge.py`。
5. 新しいイベント種別を使うなら [contract.md §1](contract.md) と
   `scripts/install-claude-hooks.sh` の登録リストを揃える。
