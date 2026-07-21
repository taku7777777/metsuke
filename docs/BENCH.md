# BENCH.md — 契約ドキュメントの受け入れベンチ（roadmap 4-1）

> **儀式**: 新規セッションの Claude（この会話履歴を知らない個体）に
> [SCHEMA.md](SCHEMA.md)・[METRICS.md](METRICS.md) と read-only 接続だけを与え、
> 下の質問を**期待SQLを見せずに**出題する。回答のSQL実行結果が「期待SQL」の実行結果と
> 一致（金額は±$0.01、件数は完全一致）し、かつ罠踏み（各問の検査観点）が無ければ合格。
> 6/8 問以上で契約ドキュメントは合格、落ちた問は**ドキュメント側を**改訂する（回答者を責めない）。

接続: SCHEMA.md §0 の read-only 手順（Python `mode=ro` が確実。macOS の sqlite3 CLI は
WAL の ro open に失敗することがある — その挙動自体が Q0 の暗黙の罠）。

## Q1. 今日はいくら使ったか

- 検査観点: v_daily を使う（v_prompt_cost 合計で答えたら不合格 — 帰属不能分が欠ける）。
```sql
SELECT cost_usd FROM v_daily WHERE day = date('now','localtime');
```

## Q2. 直近7日で最も高かったプロンプト3件と、その支配項

- 検査観点: サブエージェント込みで語る（n_agents に言及）。支配項は4種トークン×単価で判定。
```sql
SELECT prompt_id, cost_usd, n_requests, n_agents, interrupted
FROM v_prompt_cost WHERE ts >= CAST(strftime('%s','now','-7 days') AS REAL)
ORDER BY cost_usd DESC LIMIT 3;
```
- 追加の罠: `strftime('%s')` は TEXT を返す。`v_prompt_cost.ts` は式由来（affinity無し）
  なので CAST しないと**静かに空集合**になる（SCHEMA.md §4-6）。

## Q3. 昨日の本体 vs サブエージェントの金額分解

- 検査観点: `agent_id IS NULL` で分解する（agent表をJOINし始めたら遠回り）。
```sql
SELECT agent_id IS NULL AS is_main, ROUND(SUM(cost_usd),2)
FROM v_request_cost
WHERE date(ts,'unixepoch','localtime') = date('now','localtime','-1 day')
GROUP BY 1;
```

## Q4. 中断されたリクエストは何件あり、その出力コストはいくらか

- 検査観点: 「output_tok が NULL のため出力側は**計上不能（下限0）**」と答えること。
  推定で埋めたら不合格。
```sql
SELECT COUNT(*) FROM request WHERE is_interrupted = 1;
-- 出力側の答え: 不明（NULL化済み・下限0）。入力側のみ v_request_cost に計上されている。
```

## Q5. 直近7日のキャッシュ恒等式破れの原因別件数

- 検査観点: lineage 単位の概念を説明できる。unknown が多い場合「hook証拠の蓄積で再分類される」
  ことに言及。
```sql
SELECT cause, COUNT(*) FROM v_cache_identity
WHERE ts >= strftime('%s','now','-7 days') GROUP BY cause ORDER BY 2 DESC;
```

## Q6. セッション起動固定費の直近3日の日平均は肥大しているか

- 検査観点: METRICS §5 の「約4割増え、数万tok規模へ」という警戒実績と比較して解釈を述べる。
```sql
SELECT day, ROUND(AVG(startup_context_tok)) FROM v_context_overhead
WHERE day >= date('now','localtime','-3 days') GROUP BY day ORDER BY day;
```

## Q7. 今週のモデル別コスト構成比

- 検査観点: 週境界 = 月曜起点ローカルタイム（METRICS §0 のイディオムを使う）。
```sql
SELECT model, ROUND(SUM(cost_usd),2) usd
FROM v_request_cost
WHERE date(ts,'unixepoch','localtime') >= date('now','localtime','weekday 0','-6 days')
GROUP BY model ORDER BY usd DESC;
```

## Q8. 現在 open な施策マーカーと、その仮説

- 検査観点: 「常時 open ≤2 が健全」（METRICS §8）への言及。verdict の意味を説明できる。
```sql
SELECT marker_id, datetime(ts_start,'unixepoch','localtime') started, category, hypothesis
FROM marker WHERE ts_end IS NULL;
```

## 実施記録

| 日付 | 回答者 | 結果 | ドキュメント改訂 |
|---|---|---|---|
| 2026-07-17 | 新規コンテキストの Claude（general-purpose agent・履歴なし） | **8/8 合格**。全罠回避（v_daily採用・中断output推定拒否・週境界月曜起点・未知モデル0確認）。Q3でv_daily突合、Q6でモデル内比較を自発実施。ro接続失敗時はmd5検証付きスナップショット+mode=roで自己回復（ADR 0005の強制パターンと同型） | ①接続手順をPython `mode=ro` 推奨へ改訂（macOS CLIはWALのro openでerror 14） ②`strftime('%s')`のTEXT比較で式由来ts列が静かに空集合になる罠をSCHEMA §4-6とQ2に追記（出題側の期待SQLが踏んだ） |

## dashboard P0 実台帳snapshot

`tests/dashboard_values.py` は実台帳を SQLite URI `mode=ro` で開き、さらに
`PRAGMA query_only=ON` を設定した read transaction から識別子なしの集約値だけを出力する。
生成先は gitignore 済みの `.metsuke-local/dashboard-ledger-snapshot.json` とし、リポジトリには
実測値を保存しない。`tests/fixtures/dashboard/sanitized_ledger_summary.json` はallowlist契約だけを
検査する合成fixtureである。prompt本文、project名、session/prompt/request IDは出力しない。

再計測コマンド:

```sh
.venv/bin/python tests/dashboard_values.py \
  --ledger ~/.metsuke/ledger.db \
  --output .metsuke-local/dashboard-ledger-snapshot.json
```

実運用規模の台帳で、dashboard KPIと`prompt`表の件数が約2割ずれることを確認した。
差は台帳成長ではなく数え方に由来するため、KPI定義は`v_request_cost`上のdistinct
`prompt_id`に固定する。絶対額・絶対件数・観測期間はローカルsnapshotだけに保持する。

## dashboard query集約（P3性能最適化）

数万request規模の実台帳で計測した。計測中もwriterが稼働しており、dashboard側からの接続は
全てread-onlyである。macOS arm64 / Python 3.12+ / SQLite 3.4x以降を使用した。各ケースは
`connect_dashboard()`でrequestごとに接続し、`Page()`を渡したview model queryとcloseを
一体で計測した。warm-up 1回の後に10回計測し、p95はnearest-rank法（10試行では最大値）とした。
実台帳はSQLite URI `mode=ro`かつP2のquery-only/authorizer経由でのみ開いた。

最適化前（P3最適化前、同じ実台帳・同じ計測スクリプト）:

| ケース | 予算 | 中央値 | p95 / 最大 |
|---|---:|---:|---:|
| overview 昨日 | 300ms | 657.5ms | 1,033.9ms |
| overview 7日 | 300ms | 855.9ms | 1,484.3ms |
| period 7日 | 500ms | 768.0ms | 1,010.8ms |
| period 31日 | 500ms | 1,280.3ms | 1,683.6ms |

最適化後:

| ケース | 予算 | 中央値 | p95 / 最大 | 判定 |
|---|---:|---:|---:|---|
| overview 昨日 | 300ms | 115.2ms | 116.7ms | 予算内 |
| overview 7日 | 300ms | 303.2ms | 331.7ms | **予算超過** |
| period 7日 | 500ms | 148.8ms | 172.7ms | 予算内 |
| period 31日 | 500ms | 402.5ms | 430.2ms | 予算内 |

overview/periodとも、8本の独立SQLを1本のcompound queryへ統合した。先頭の
`scoped AS MATERIALIZED`が必要列だけを選択して窓内の`v_request_cost`を1回評価し、後続CTEが
totals、費目、prompt/session/project ranking、前期間、cache causeを導出する。SQLite一時テーブルや
in-memory cacheは使用していない。overview 7日は現在期間と同日数の前期間（合計14日）に加え、
`v_cache_identity`も同じ応答で評価するため、初回p95 300msには31.7ms届かなかった。残る対応は
§12の次段である短時間cacheを含め、初回表示を隠さない形で別途判断する。

出力比較には同じ実台帳を`mode=ro`接続からSQLite backupした一時snapshotを使用した（snapshotは
page-cache/WAL条件が変わるため性能値には不採用）。固定timestampで生成した直近7日のV1〜V4 HTMLは
最適化前とbyte-for-byte一致し、`metsuke explain last`のtext/JSON出力も一致した。

### 独立検証（レビュー側の再計測）

上記とは別に、HTTP層を除いたview model query層だけを同一手順で前後比較した（warm-up 1回＋15回、
p95はnearest-rank）。計測機は本作業のエージェントとingesterが同時稼働しており負荷変動が大きいため、
絶対値は上表と一致しないが、改善幅と超過箇所の傾向は再現した。

| ケース | 前 中央値 | 後 中央値 | 改善 | 後 p95 | 予算 |
|---|---:|---:|---:|---:|---:|
| overview 昨日 | 541ms | 116ms | 4.7x | 118ms | 300ms |
| overview 7日 | 772ms | 282ms | 2.7x | 397ms | 300ms |
| period 7日 | 643ms | 167ms | 3.8x | 186ms | 500ms |
| period 31日 | 797ms | 473ms | 1.7x | 717ms | 500ms |

**中央値は全ケース予算内**。p95はoverview 7日とperiod 31日で超過するが、負荷のある計測機での
値であり、静穏環境での再計測が必要である。短時間cacheの要否は、この再計測とPhase 4の実利用計測を
見てから判断する（初回表示を隠さないため、現時点では入れない）。

## dashboard P4 全ビュー31日SSR

数万request規模の実台帳を31日窓で計測した。macOS arm64 / Python 3.12+ / SQLite 3.4x以降。
各タブを`dashboard_response()`でrequestごとに`connect_dashboard()`から開き、
Window/project解決、view model query、dashboard SSR、closeまでを一体で計測した。HTTP socketと
利用実測spool書込みは含まない。warm-up 1回後に10回計測し、p95はnearest-rank法のため10試行の
最大値とした。接続はSQLite URI `mode=ro`＋query-only/authorizerで、DBへの書込みは行っていない。

| 31日タブ | 予算 | 中央値 | p95 / 最大 | response規模 | 判定 |
|---|---:|---:|---:|---:|---|
| overview | 500ms | 458.1ms | 614.5ms | 数十KB | **予算超過** |
| period | 500ms | 405.5ms | 531.9ms | 100KB未満 | **予算超過** |
| trend | 500ms | 553.8ms | 728.3ms | 数十KB | **予算超過** |
| cache | 500ms | 3,820.7ms | 4,908.5ms | 数十KB | **予算超過** |
| dist | 500ms | 217.9ms | 278.6ms | 10KB未満 | 予算内 |

全responseは通常上限1,000,000 bytesの9%未満だった。P4では既存viewmodelをそのまま共有し、
SQL・金額/cause/閾値定義・短時間cacheを追加していない。特にcacheは既存query modelの初回表示自体が
予算を大幅に超える。出力同一性を崩す見切り最適化は行わず、query planと`v_cache_identity`を含む各
statementの個別計測を次のquery最適化判断として残す。

### `idx_request_session_ts`追加後

同日、通常のsync移行経路で`idx_request_session_ts ON request(session_id,ts)`が適用された後の
実台帳を、dashboard側から`mode=ro`で再計測した。対象は同じ数万request規模である。
期間、Python/SQLite、request単位の接続〜SSR〜closeは上表と同じである。warm-up 1回＋20回、
p95はnearest-rank法。一時コピーは出力不変と索引前後の行一致検証にだけ使用した。

| 31日タブ | 追加前 median | 追加後 median | 追加後 p95 | 追加後 max | 500ms判定 |
|---|---:|---:|---:|---:|---|
| overview | 458.1ms | 445.0ms | 532.3ms | 655.9ms | **32.3ms超過** |
| period | 405.5ms | 377.8ms | 442.6ms | 519.3ms | 予算内 |
| trend | 553.8ms | 532.7ms | 579.5ms | 666.7ms | **79.5ms超過** |
| cache | 3,820.7ms | 375.2ms | 430.3ms | 448.2ms | 予算内 |
| dist | 217.9ms | 232.4ms | 260.2ms | 263.2ms | 予算内 |

cacheの中央値は約10.2倍改善し、p95 500ms予算内に入った。支配的だった
`v_context_overhead`の同じ数千行取得は、索引なし
4,465.5msから、索引あり20試行でmedian 124.1ms / p95 139.9msへ改善した。query planは
`SCAN request USING INDEX idx_request_ts`から
`SEARCH request USING INDEX idx_request_session_ts (session_id=?)`へ変化した。全行をcanonical JSON化した
SHA-256が追加前後で一致することを確認した。索引追加による出力値の変更はなく、
V1〜V4は生成時刻固定でbyte-for-byte一致、`metsuke explain last`の
text/JSON SHA-256も追加前後一致した。overviewとtrendは引き続きp95 500msを超えており、追加対応の
要否はこの実測を基に別途判断する。
