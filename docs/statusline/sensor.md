# statusline — センサー契約

statusline は表示装置であると同時に、**Claude Code 本体の内部状態を採取する唯一の口**でもある。
本ファイルは stdin 契約・記録形式・スロットル・下流利用の正。表示は [spec.md](spec.md)。

実装: `scripts/statusline.sh` 前半（10〜33行目）。

## 1. なぜ statusline がセンサーを兼ねるか

`cost.total_cost_usd`（クライアント推計）と `context_window.*` は、**transcript にも hook にも
現れない**。statusline stdin だけが供給する。描画のたびに呼ばれるため、追加のプロセスや
常駐なしに高頻度サンプリングが成立する — 表示とセンサーを分離しない理由はここにある。

## 2. stdin 契約

Claude Code が statusline スクリプトに渡す JSON のうち、**記録するのは以下4フィールドのみ**
（ホワイトリスト方式 = 取込時リダクションの前段。プロンプト本文・cwd・transcript パス等は
そもそも spool に落とさない）。

| フィールド | 用途 | 欠損時 |
|---|---|---|
| `.session_id` | 全記録のキー・スロットルの粒度 | `null`（サンプルは書くが下流で紐づかない） |
| `.version` | CC バージョン併記（§4） | `null` |
| `.cost` | 第2推定器（`.cost.total_cost_usd` を利用） | `null` |
| `.context_window` | context 水位（`used_percentage` / `total_input_tokens`） | `null` → 表示は `%` へ縮退 |

`.cost` と `.context_window` は**オブジェクトごと丸ごと**保存する。個別フィールドを抜き出すと
CC 側のスキーマ変更（フィールド追加・改名）を後から追跡できなくなるため。

## 3. 書き出すもの

### 3.1 statusline_sample（spool）

```
~/.metsuke/spool/hooks/<epoch_ns>-<pid>-statusline_sample.ndjson
```
```json
{"metsuke_event":"statusline_sample","metsuke_ts":<epoch_s>,
 "payload":{"session_id":…,"version":…,"cost":…,"context_window":…}}
```

- **スロットル: セッション別 15秒**。マーカー `~/.metsuke/state/sl-<session_id>.last` に
  最終書出 epoch を保持し、`now - last >= 15` のときだけ書く。描画は毎ターン走るため、
  スロットルなしでは spool が洪水になる（15秒は「進行中プロンプトの検知SLO 60秒」を
  満たす最粗の粒度）。
- ファイル名の `<epoch_ns>-<pid>` により多重起動でも衝突しない。
- 書出失敗は**全て握り潰す**（`|| true`）。センサーの失敗で表示を止めない。

下流: ingester が `hook_event(kind='statusline_sample')` へ取り込む（[SCHEMA.md](../SCHEMA.md)）。
`UNIQUE(payload_json)` により再取込は冪等。

### 3.2 ctxwarn マーカー（hook への受け渡し）

```
~/.metsuke/state/ctxwarn-<session_id>   ← 中身は used_percentage の整数値
```

`used_percentage ≥ 60` かつ `ctxwarned-<session_id>` が**存在しない**ときに書く。

statusline は「水位が上がった」という事実を置くだけで、**文言を出さない**。実際の警告は
次の `UserPromptSubmit` で hook が one-shot 発火する（statusline に警告文を出しても
判断の瞬間に読まれないため — [ADR 0008](../adr/0008-compact-interventions.md)）。

マーカーのライフサイクル（消費側は `scripts/hook-sensor.sh`）:

| 契機 | 動作 |
|---|---|
| statusline（≥60%・未警告） | `ctxwarn-<sid>` を書く |
| UserPromptSubmit hook | `ctxwarn-<sid>` を削除し `ctxwarned-<sid>` を立てて発火（日次3発上限） |
| PostCompact hook | `ctxwarn-<sid>` と `ctxwarned-<sid>` を**両方削除**＝ re-arm |

`ctxwarned-<sid>` が「同一 context 世代では1度だけ」を保証し、PostCompact が世代を切る。

## 4. `.context_window` のバージョン安定性（Q3・決着済み）

statusline stdin は**非公開サーフェス**であり、CC 更新でフィールドが消える/改名される
リスクがある（[05-risks](../05-risks.md)）。

**Q3 の決着（2026-07-19）**: 監視するのではなく、`version` を毎サンプルに併記して
**後追い可能にする**。`hook_event.payload_json` に CC バージョンと `context_window` が
同一レコードで残るため、破損が起きた時点で「どのバージョンから形が変わったか」を
台帳から遡及特定できる。ingester 側の改修は不要。
（[06-open-questions Q3](../06-open-questions.md)）

破損時の縮退は表示側で吸収済み — `total_input_tokens` 欠損なら `%` 表示、
`context_window` ごと欠損なら `0%` かつ色なし。**警告は出るが描画は止まらない**。

## 5. 第2推定器としての役割

`.cost.total_cost_usd` は Claude Code のクライアント推計であり、**台帳の自前計算とは
完全に別系統**（別のコードが、別の入力から算出する）。この独立性が価値の全て。

| 用途 | 仕組み |
|---|---|
| **意味的パース破壊の自動カナリア** | ingester health 段が日次で自前計算と突合。乖離は「数字は出ているが意味が壊れている」唯一の自動検知手段（[01-architecture](../01-architecture.md)） |
| **進行中プロンプトの累積$**（暴走ガード・状況表示） | `state.py` が「現在の最新サンプル − UserPromptSubmit 時点のサンプル」の**差分**を取り `sessions[sid].inflight_usd` へ。PostToolUse hook は介入判断に、statusline は表示にこの値を読むだけ |
| **常時視界**（`sess $`） | 突合結果を待たず、人間が毎ターン目視で照合できる（[spec.md §2](spec.md)） |

差分方式の帰結: サンプルが15秒スロットルなので `inflight_usd` の粒度も15秒。
プロンプト開始前後にサンプルが1つも無い区間では `null` になる（過小に見せず、欠測を欠測として出す）。

## 6. 規律

- **DB に触らない**（SQL 実行禁止・`tests/test_hotpath.py::test_hotpath_discipline` で強制）。
- 書き込み先は spool と `state/` のマーカーのみ。台帳への反映は ingester の一本道
  （[ADR 0004](../adr/0004-sqlite-single-writer-spool.md)）。
- 失敗は全て握り潰して exit 0（fail-open）。
