# statusline — 要件と受入基準

[spec.md](spec.md)（何を出すか）・[sensor.md](sensor.md)（何を採るか）に対し、本ファイルは
**満たすべき性質と、それを確認する手段**の正。仕様変更のレビューはこの表を基準に行う。

## 1. 要件

| # | 要件 | 根拠 |
|---|---|---|
| R1 | **50ms 以内**に描画を返す | 毎ターン走る。遅い statusline は使用体験を直接劣化させ、やがて外される |
| R2 | **DB に触らない**（SQL・LLM 呼び出し禁止） | ホットパスの分離。ingester が焼いた state.json だけを読む（[ADR 0004](../adr/0004-sqlite-single-writer-spool.md)） |
| R3 | **fail-open**: いかなる入力・環境でも exit 0 かつ非空1行 | 計測ツールがエディタを壊してはならない。exit 非0 は CC 側の表示崩れを招く |
| R4 | **欠損はプレースホルダでなく非表示**（不明な値をゼロや既定値で埋めない） | 「静かに間違うより煩く正しい」（[00-vision](../00-vision.md)）。埋めた値は必ず信じられる |
| R5 | **鮮度異常を自分で検知して表示**する（上流の申告を鵜呑みにしない） | 見る場所と壊れたら気づく場所の同一化（[spec.md §6](spec.md)） |
| R6 | spool 書出は**セッション別15秒**にスロットル | spool 洪水の防止と検知SLO 60秒の両立（[sensor.md §3](sensor.md)） |
| R7 | 記録するフィールドは**ホワイトリスト4つのみ** | PII 面の最小化。プロンプト本文・パスを spool に落とさない |
| R8 | 表示の**情報量は1行を超えない** | ちら見（0秒）のための面。増やしたくなったら V1〜V4 か通知の担当 |

## 2. 受入基準

| # | 基準 | 確認手段 |
|---|---|---|
| A1 | 全フィールドが揃う入力で書式どおり1行を出す | `tests/test_hotpath.py::test_statusline_display_and_throttle` |
| A2 | 15秒以内の再実行では2つめの statusline_sample を書かない | 同上 |
| A3 | サンプルに CC `version` が併記される（Q3） | 同上（`payload.version` を検証） |
| A4 | TTL が `last_ts + 3600` の**絶対時刻**で出る | `tests/test_hotpath.py::test_statusline_ttl_expiry_time` |
| A5 | `used_percentage ≥ 60` で ctxwarn マーカーを書き、`ctxwarned` 存在時は書かない | `tests/test_hotpath.py::test_statusline_context_warn_marker_and_cooldown` |
| A6 | マーカーが UserPromptSubmit で消費され、PostCompact で re-arm される | `test_context_warn_hook_consumes_marker` / `test_postcompact_marker_rearms_context_warning` |
| A7 | スクリプト内に SQL・DB アクセスが存在しない | `tests/test_hotpath.py::test_hotpath_discipline`（静的検査） |
| A8 | state.json 欠損時に `metsuke: no data yet` で exit 0 | 手動: `echo '{}' \| bash scripts/statusline.sh`（METSUKE_HOME を空ディレクトリに） |
| A9 | 実行中1件と完了済み直近3件を順序・欠損・中断マーカー込みで表示する | `tests/test_hotpath.py::test_statusline_prompt_cost_group` |
| A10 | R1 の支配項である `jq` 起動を通常3回以下・センサー書込時4回以下に保つ | `test_statusline_jq_call_budget_normal_render` / `test_statusline_jq_call_budget_with_sensor_write` |
| A11 | 完了/実行中コストが <`$3`で既定色、≥`$3`で黄、≥`$7.5`で赤となり、`detail_url`がある値にOSC 8リンクが付く | `tests/test_hotpath.py::test_statusline_prompt_cost_group` |
| A12 | context絶対量が <200Kで既定色、≥200Kで黄、≥500Kで赤。絶対量欠損時だけ60% / 80%へ縮退する | `tests/test_hotpath.py::test_statusline_display_and_throttle` |

新しい表示フィールドを足すときは、**A系に1行足せない変更は入れない**。

## 3. エッジケース

| 状況 | 期待動作 | 実装上の担保 |
|---|---|---|
| `jq` 未導入 | `metsuke: no data yet`・センサーも黙って無効化 | 冒頭と描画前の2箇所で `command -v jq` |
| state.json が書き換え中 | 常に一貫した内容を読む | ingester が**原子的 rename** で差し替える |
| state.json が古い（>15分） | `⚠stale`（中身は表示するが信用の印を落とす） | statusline 側で `generated_at` を独立判定 |
| stdin が空 / 非JSON | state.json 由来のみ描画・spool 書出なし | `input` 空判定でセンサー節を丸ごとスキップ |
| `session_id` が異常な文字を含む | パス組み立てに使わせない | `tr -cd 'A-Za-z0-9._-'` でサニタイズ |
| セッションが state.json に未登録（初回） | プロンプトコスト群・`🔥` を非表示、他は描画 | `// empty` で欠損を空文字に落とす |
| プロンプト実行中 | `⏵$…` の後ろは**完了済み**の直近3件のみ（実行中を混ぜない） | `state.py` が inflight 中は `p.ts < 開始-2s` の最大3件を選ぶ |
| 中断されたプロンプト | 該当値を `⚡$…` で表示 | `recent_prompts[].interrupted` |
| 複数 CC ウィンドウ同時起動 | 互いに干渉しない | マーカー・spool ファイル名がセッション別／`pid` 付き |
| 描画が凍結したまま放置 | TTL 表示は真であり続ける（遅延分は安全側） | 絶対時刻表示（[spec.md §5](spec.md)） |
| CC が `context_window` を返さなくなった | `0%`・色なしで描画継続、後追い調査は台帳で | ホワイトリスト保存＋`version` 併記 |

## 4. 非目標

| 対象 | 判断 |
|---|---|
| リアルタイム性（秒単位の追従） | 不要。state.json は5分 tick＋hook 駆動で十分。ちら見の面に秒精度の意味がない |
| 見ていない時の防御 | 通知（暴走ガード・TTL事前通知）と hook（予算100%警告）の担当。statusline は**見ている時だけ**効く前提で設計する |
| 設定による表示項目・並び順のカスタマイズ | 1行の構成は設計判断そのもの。黄/赤など意味が固定された閾値のみ中央設定で変更可 |
| 履歴・グラフ | statuslineの責務外。現行は`metsuke view trend`、Stage 8ではローカルdashboardへ遷移する |

## 5. 変更時の手順

1. [spec.md](spec.md) / [sensor.md](sensor.md) を先に直す（doc が正・実装が従）。
2. 色の閾値を変えるなら **spec.md §3 のみ**を編集する（他所は参照しているだけ）。
3. 本ファイルの受入基準に行を足し、`tests/test_hotpath.py` に対応するテストを足す。
4. `scripts/statusline.sh` を変更 → `pytest tests/test_hotpath.py`。
5. 表示例を変えたら [03-interfaces.md §1](../03-interfaces.md) の要約と
   [RUNBOOK §0](../RUNBOOK.md) のちら見項目も揃える。
