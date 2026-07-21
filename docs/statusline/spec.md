# statusline — 表示仕様

提示3層の「常時（ambient）」層。**表示に関する唯一の正**（書式・フィールド・色の閾値）。
センサー側面は [sensor.md](sensor.md)、要件と受入基準は [requirements.md](requirements.md)。
なぜ常設ダッシュボードでなく1行なのかは [ADR 0003](../adr/0003-no-dashboards.md)。

実装: `scripts/statusline.sh`（bash + jq）。

## 1. 出力

1行・ANSIカラー付き。全フィールドが揃った最大形:

```
⛽$X/$BUDGET $R/h 着地$FORECAST | ⏵$P1 $P2 $P3 $P4 | sess $S ctx Ntok 🔥HH:MM | ⚠stale
```

固定部は `⛽本日` と `sess $… ctx …` の2つだけ。予算を含む他の値は**データが無ければ丸ごと消える**
（プレースホルダを出さない — 空欄は「未取得」と「ゼロ」を混同させる）。

## 2. フィールド

| 表示 | 意味 | データ源 | 書式 |
|---|---|---|---|
| `⛽$X/$BUDGET` | 本日累計 / 日次予算 | state.json `today.cost_usd` / `today.budget_usd`（予算未設定なら分母を省略） | `$%.1f`＋設定時のみ`/$%.0f` |
| `$R/h` | wall-clock の直近30分の燃焼レート | state.json `today.burn_rate_usd_h` | `$%.0f/h`・**色つき**（§3）。null なら非表示 |
| `着地$FORECAST` | 本日の着地予測 | state.json `today.landing_usd` | `着地$%.0f`。null なら非表示 |
| `⏵$P1 $P2 $P3 $P4` | 左から実行中・前回・前々回・前々前回のコスト | state.json `sessions[sid].inflight_usd` / `sessions[sid].recent_prompts` | `⏵` は実行中、`⚡` は中断。各値 `$%.2f`、欠損分は非表示 |
| `sess $S` | セッション累計の**クライアント推計** | stdin `.cost.total_cost_usd` | `$%.1f`。第2推定器を常時視界に置く（§5） |
| `ctx 87K` | context の**絶対トークン** | stdin `.context_window.total_input_tokens` | §4・**色つき**（§3） |
| `🔥HH:MM` | キャッシュ失効の**絶対時刻** | state.json `sessions[sid].last_ts` + 3600s | `🔥%H:%M`（localtime）・残15分で赤・失効時は `❄` |
| `⚠stale` | 計測ヘルス異常 | state.json `stale` または `generated_at` | 赤。§6 |

`⏵` の値は CC クライアント推計のセッション差分（`sess $` と同一系統・15秒粒度・
[sensor.md §5](sensor.md)）である。一方、完了済み3件は台帳の因果帰属コスト
（サブエージェント込み・[ADR 0009](../adr/0009-causal-prompt-attribution.md) / [ADR 0010](../adr/0010-notification-prompt-folding.md)）。
**出所の異なる数字が同じ列に並ぶ**ため、サブエージェントを撒くプロンプトでは両者が乖離しうる。

従来 §8 で進行中額を出さなかった理由は、見ていない時の*介入*を通知に任せる判断だった。
見ている時の*状況把握*は別の需要なので表示へ加えたが、1行の情報量上限は依然有効であり、
この変更で最大行幅は約75文字から約90文字へ増えている。

完了済みの各金額はコスト閾値で色分けする。近時間の高コストプロンプトには
ingesterがローカル詳細HTMLを事前生成し、OSC 8ハイパーリンクを付ける。
リンク対応端末でクリック（端末の設定によってはCommand+クリック）すると、
`#prompt=<id>`で対象プロンプトを選択した自己完結HTMLが開く。非対応端末では色付き文字のまま縮退する。

### 燃焼レートに記号を付けない理由

燃焼レートは絶対量であり、ペース比のような方向を持たないため記号を付けない。
健全性は色だけで示す。窓は wall-clock の直近30分であり、「直近の活動30分」ではない。
手を止めればレートは下がるため、「今燃えているか」を表せる。

## 3. 色の閾値（唯一の定義）

**この節が全システムの閾値の正。** METRICS.md・ビュー・レポートはここを参照し、数値を再掲しない。

| 対象 | 通常（燃焼レートのみ緑 `32`） | 琥珀 `33` | 赤 `31` |
|---|---|---|---|
| 燃焼レート `burn_rate_usd_h` | < $45/h | ≤ $90/h | > $90/h |
| context 絶対量 `total_input_tokens` | < 200K | ≥ 200K | ≥ 500K |
| プロンプトコスト | < $3 | ≥ $3 | ≥ $7.5 |
| キャッシュ TTL 残 | > 15分 | — | ≤ 15分（失効後は `❄`・無色） |

context の色は**絶対トークン量**で判定する。閾値は
`METSUKE_CONTEXT_WARN_TOKENS` / `METSUKE_CONTEXT_CRIT_TOKENS`で変更できる。
`total_input_tokens`が取得できない場合だけ、`used_percentage`の60% / 80%へ縮退する。
色と[context水位警告 hook](../03-interfaces.md#2-瞬間just-in-time-hooks)は別の責務であり、
hookの発火条件は従来どおり60%（マーカーを書くのは statusline センサー側 —
[sensor.md §3](sensor.md)）。
プロンプト閾値は `METSUKE_PROMPT_WARN_USD` / `METSUKE_PROMPT_CRIT_USD` で変更でき、
無効値は表示側で既定値へ縮退する。

## 4. context を絶対トークンで出す理由

`%` はウィンドウサイズ次第で同じ数値でも費用が数倍違う。判断に使えるのは絶対量なので、
表示は絶対トークンに固定する。

| 値 | 書式 | 例 |
|---|---|---|
| < 1,000 | 整数 | `ctx 840` |
| < 10,000 | `%.1fK` | `ctx 8.4K` |
| ≥ 10,000 | `%.0fK` | `ctx 87K` |

`total_input_tokens` が stdin に無い場合のみ `used_percentage` の `%` 表示と60% / 80%の
色判定へ縮退する
（フィールドのバージョン安定性は [sensor.md §4 / Q3](sensor.md)）。

## 5. TTL を絶対時刻で出す理由

「残N分」の相対表示は、**描画凍結・取込遅延で腐る**。絶対時刻なら描画がいつ凍っていても
表示は真であり、遅延分は常に「早め＝安全側」に出る。

- 失効時刻 = セッション最終活動 `last_ts` + **3600秒**（固定）。
- 残 ≤ 15分で赤、残 ≤ 0 で `❄`、`last_ts` が無ければフィールドごと非表示。
- 完全放置（statusline を見ていない）ケースは OS 事前通知が拾う（発火主体は ingester の
  launchd tick — hook はアイドル中に発火しない）。

**既知の限界**: 3600秒は「セッション再開が温かいか」の単一ヒューリスティックであり、
実際の cache 書込は 5m 層と 1h 層が混在する（`v_cache_identity`）。5m 書込主体の
セッションでは `🔥` が実態より楽観的に出る。分析側の TTL 内訳は V3 が持ち、
statusline は**ちら見の一次近似**に留める（1行に2つの TTL を出さない — [§8](#8-意図的に出さないもの)）。

## 6. ⚠stale — 沈黙故障の一次防衛線

次のいずれかで点灯（赤）:

| 条件 | 意味 |
|---|---|
| state.json `stale == true` | ingester の health 段が異常を検知（鮮度・取込ゼロ） |
| `now - generated_at > 900`（15分） | state.json 自体が更新されていない＝ ingester か launchd tick が死んでいる |

後者は state.json の中身を信用せずに statusline 側で独立判定する。ingester が壊れたまま
`stale:false` を焼き続ける故障モードを、この二重化が拾う。

**見る場所と、壊れたら気づく場所を同一にする** — この設計原理により、計測ヘルス専用の画面を
作らない（[01-architecture](../01-architecture.md) 故障検知の設計）。

対処は [RUNBOOK §4「statusline に ⚠stale が出た」](../RUNBOOK.md)。

## 7. 縮退表示

| 状況 | 出力 |
|---|---|
| `jq` が無い / state.json が読めない | `metsuke: no data yet`（exit 0） |
| state.json のパース失敗 | 同上 |
| stdin が空・非JSON | state.json 由来のフィールドのみで描画（sess/ctx は既定値 0 / `0%`） |

いかなる入力でも **exit 0**・**非空1行**。詳細は [requirements.md](requirements.md)。

## 8. 意図的に出さないもの

| 対象 | 理由 |
|---|---|
| 週次・月次のヘッドルーム | 1行に載る情報量の上限。ペーシングは日次で足り、週/月は `metsuke view period` の担当 |
| プロジェクト名・モデル名 | ちら見の判断に効かない。帰属は V1/V2 の担当 |
| 相対表示の TTL 残 | §5 のとおり腐る |
