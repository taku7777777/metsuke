# hooks — 介入カタログ

提示3層の「瞬間（just-in-time）」層。**どの介入が・いつ・どんな条件で・どう届くか**の正。
実行契約（イベント登録・出力形式・marker・conversion）は [contract.md](contract.md)、
要件と受入基準は [requirements.md](requirements.md)。

実装: `scripts/hook-sensor.sh`（hook 側）＋ `src/metsuke/state.py`（ingester 側の通知）。

## 1. 発火主体の区別（設計上いちばん重要な区別）

介入は「hook が出すもの」と「ingester が出すもの」に分かれる。**hook はアイドル中に発火しない**
ため、人間が手を止めている間に効かせたい介入は必ず ingester（launchd tick）側に置く。

| 主体 | 到達手段 | 効くタイミング | 該当 |
|---|---|---|---|
| **hook**（UserPromptSubmit） | セッション内の `systemMessage` / `additionalContext` | 送信の瞬間だけ | coldcache_warn・ctx_warn・compact_recovery・budget_warn_50/80/100 |
| **ingester**（launchd tick / hook 起動の sync） | OS通知（`osascript` ＋ ntfy） | 手を止めていても届く | runaway_guard・ttl_prenotify・任意の高コスト通知 |

PostToolUse / Stop hook は**介入を出さない** — `metsuke sync` を起動するだけ（PostToolUse は
30秒スロットル）。暴走ガードと任意の高コスト通知は、その sync の中で ingester が判定・通知する。

## 2. 介入一覧

| 介入（rule） | 主体 | 発火条件 | 出力 |
|---|---|---|---|
| **coldcache_warn** | hook | state.json が新鮮 かつ `now - last_ts > 3600` かつ `rebuild_cost_usd ≥ $0.5` | `🧊 …再構築 ≈$X。続き作業でなければ /handoff で新セッションが安価です（この送信自体の費用は不可避）` |
| **ctx_warn** | hook | statusline が置いた `ctxwarn-<sid>` marker が存在（[statusline/sensor.md §3.2](../statusline/sensor.md)） | `📚 context X%消費 — auto-compact接近。区切りの良いところで /handoff が安価です（…判断文脈も失われます）` |
| **compact_recovery** | hook | `compacted-<sid>` marker が存在（PostCompact が置く） | `additionalContext` に4項目の復旧指示（§3） |
| **budget_warn_50** | hook | 本日累計 / 日次予算 ≥ 0.5 | `日次予算50%通過（$X/$Y・着地見込み$Z）` |
| **budget_warn_80** | hook | ≥ 0.8 | `⚠️ 日次予算80%超（$X/$Y）。重い依頼は明日へ、または /handoff で軽量続行を` |
| **budget_warn_100** | hook | ≥ 1.0 | `⛔ API換算の日次目安100%到達（$X/$Y）。送信は停止しません。継続価値を確認し、区切れるなら /handoff または明日に回してください` |
| **runaway_guard** | ingester | 進行中プロンプトの累積 `inflight_usd ≥ $5` | OS通知 `🚨 session xxxxxxxx… 進行中プロンプト $X` |
| **ttl_prenotify** | ingester | `3000s ≤ gap < 3600s` かつ `rebuild ≥ $0.5` かつ進行中プロンプトなし | OS通知 `⏳ …キャッシュ残N分（再構築≈$X）。続けるなら今、終わりなら放置か/handoff` |
| **高コスト通知**（receipt） | ingester | `METSUKE_RECEIPT_NOTIFY_ENABLED=1`、プロンプト完了後、`cost ≥ METSUKE_PROMPT_CRIT_USD`、かつ最終 request から600秒以内 | OS通知 `API換算 $X・モデル呼出N回・主因: …・詳細: metsuke explain xxxxxxxx --html --open` |

### 閾値の可変性 — §1 の主体の違いがそのまま効く

実行時設定の正は `~/.metsuke/config.env` で、明示的なプロセス環境変数が優先される。
hook側は `load-config.sh` を通して中央設定を読み、ingesterが計算する値はstate.json経由で受け取る。
予算段などのポリシー閾値は引き続き `hook-sensor.sh` に固定する。

| 閾値 | 可変性 |
|---|---|
| 日次予算の**金額** | `METSUKE_BUDGET_DAY`。hook へは state.json の `today.budget_usd` 経由で届く |
| 予算警告のON/OFF | `METSUKE_BUDGET_WARN_ENABLED=1/0`（既定0）。無効でも設定済み予算額と集計は維持する |
| プロンプトの黄/赤 | `METSUKE_PROMPT_WARN_USD` / `METSUKE_PROMPT_CRIT_USD`（既定 `$3` / `$7.5`） |
| 高コストOS通知 | `METSUKE_RECEIPT_NOTIFY_ENABLED=1/0`。既定OFF、ON時は赤閾値を共有 |
| **再構築費の下限 $0.5** | `METSUKE_COLDCACHE_MIN_USD`。hook へは state.json の `thresholds.coldcache_min_usd` 経由で届く（§2.1） |
| `runaway_guard` の $5 | `METSUKE_RUNAWAY_USD`（ingester 側） |
| `ttl_prenotify` の 3000s | `METSUKE_TTL_PRENOTIFY_GAP_S`（ingester 側） |
| ingester 側の日次上限 | `METSUKE_NUDGE_DAILY_CAP`（runaway・ttl に効く） |
| 予算の段（50/80/100%） | **ベタ書き**（`hook-sensor.sh`） |
| coldcache の gap 3600s | **ベタ書き**（キャッシュ TTL そのものなので可変にしない） |
| hook 側の日次上限（coldcache・ctx_warn の3発） | **ベタ書き** |

### 2.1 再構築費の下限が state.json を経由する理由

「このセッションはキャッシュを失うと痛いか」の判定は、**ttl_prenotify（予告）と
coldcache_warn（事後）が同じ閾値を共有する前提**でセット設計されている。しかし発火主体が
ingester と hook に分かれており、bash 側から `config.py` は読めない。

そこで **ingester が `state.json.thresholds.coldcache_min_usd` に閾値を焼き、hook がそれを読む**
（既存の「ingester が焼く → ホットパスが読む」チャネルに乗せる）。これにより env が
予告・事後の両方へ一度に効く。

かつては hook 側がベタ書きの `0.5` を持っており、env を設定すると**予告だけ止まって
事後警告だけが残る**（最も価値のある「失効前に判断させる」介入が消える）壊れ方をした。
2026-07-20 に解消。

`thresholds` キーが無い state.json（旧形式）では `0.5` にフォールバックする
（fail-open。`tests/test_hotpath.py::test_coldcache_respects_state_threshold_with_legacy_fallback`）。

### 文面の規律

- **警告には必ず代替行動を1つ名指す**。コストを下げない警告は無視される。
  代替行動はほぼ常に `/handoff`（[03-interfaces §5](../03-interfaces.md)）。
- API換算額は定額購読の実上限ではないため、警告から送信停止へ昇格させない。制御判断は人間に残す。

## 3. compact_recovery が注入する内容

圧縮直後の迷走（却下案の再提案・検証スキップ・フェーズ混同）を抑止する4項目。
根拠は [ADR 0008](../adr/0008-compact-interventions.md)。

1. 圧縮サマリーは「過去の作業ログ」であり「次の行動指示」ではない — サマリー由来の next step は
   仮説として扱い、ユーザー指示・plan・TaskList を正とする
2. サマリー中の案には却下済みのものが含まれうる — 再提案・再実行しない
3. フェーズ前提を再確認し、破壊的操作の前に前提を確かめる
4. TaskList・plan ファイル・編集中ファイルを読み直してから続行する

`systemMessage`（人間向け）ではなく `additionalContext`（モデル向け）で入れる。
予算100%警告と同時でも通常どおり注入し、markerを消費する。

## 4. 上限とクールダウン

| 機構 | 対象 | 実装 |
|---|---|---|
| **一回性 marker** | coldcache（セッション×`last_ts` 単位）・budget 各段（日付単位） | `state/nudge-*` ファイルの存在 |
| **日次上限**（既定3発） | coldcache・ctx_warn・runaway_guard・ttl_prenotify | `state/nudge-cap-*-<date>`（hook側）／ `meta.nudge_daily`（ingester側） |
| **重複抑止** | runaway（`sid:prompt_ts`）・ttl（`sid:last_ts`）・領収書（`prompt_id`） | `meta.nudges_notified` / `meta.receipts_notified`（各200件のリングバッファ） |

**自動ミュートはしない** — 効いていないルールは黙って消すのではなく、四半期の棚卸しで
文面を改訂するか廃止する（`metsuke nudges` の conversion を見る。[RUNBOOK §0](../RUNBOOK.md)）。

## 5. 効果測定（conversion）

発火から**10分**の観測窓で「行動が変わったか」を判定する。判定は壁時計ではなく
**`fired_ts + 600` の論理時刻**で行う（rebuild で結果が変わらないため）。

**計測対象**（`ingest.py` の `measured` 集合）:

| rule | followed の定義 |
|---|---|
| coldcache_warn / ctx_warn | `/handoff` の明示実行。後続送信なしだけなら unknown |
| budget_warn_50 / budget_warn_80 / budget_warn_100 | `/handoff`、より安いモデル、またはeffort引き下げ。後続送信があるのにいずれも無ければ not_followed |
| runaway_guard | 対象セッションのrequest中断。新規fan-outまたは観測費用+$0.25以上なら not_followed、いずれも観測できなければ unknown |

判定結果は `outcome=followed|not_followed|unknown`、理由は `outcome_reason`、根拠は
`observed_json` に保存する。conversionの分母はfollowed+not_followedだけで、unknownを成功にしない。

**計測対象外**（`outcome=unknown`, `followed=NULL`）: `compact_recovery`・旧形式の
`budget_stop` / `budget_over_unlocked`・`ttl_prenotify`・領収書。
理由は「行動転換の述語を定義できない」または「強制のため転換を語る意味がない」。
**定義できない介入は計測しない** — それらしい代理指標を置かない。

集計は `metsuke nudges`（[cli/commands.md](../cli/commands.md)）。

## 6. 未実装

| 介入 | 状態 |
|---|---|
| **着手前見積り**（過去の類似タスクから `$8〜25 見込み` を提示） | **Stage 4以降・未実装**。`task_label` 履歴と分位点テーブルが前提（[04-roadmap](../04-roadmap.md)） |
