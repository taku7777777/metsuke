# METRICS.md — 指標定義集（AIアナリスト契約書・1指標1節）

> 各節 = 定義 / 単位 / 健全域 / 罠。数値の出所はすべて [SCHEMA.md](SCHEMA.md) の表とビュー。
> 週次レポートの帰属分析は §L の行動レバー一覧に基づいて行う。

## 0. 全指標に共通の境界定義

- **日次境界はローカルタイム**（`date(ts,'unixepoch','localtime')`）。利用者のローカルタイムゾーンを使う。
  console.anthropic.com の請求は UTC 境界なので、**請求突合のときだけ** UTC 換算する
  （それ以外の場面で混ぜない）。
- 週 = ISO週（月曜開始）。`strftime('%Y-W%W', ...)` ではなく **月曜起点で `weekday 0` を使う**:
  `date(ts,'unixepoch','localtime','weekday 0','-6 days')` が週の月曜。
- 予算は初期状態では未設定。`METSUKE_BUDGET_DAY/WEEK/MONTH` に利用者自身の上限を設定し、
  `METSUKE_BUDGET_WARN_ENABLED=1` とした場合だけ予算警告を有効にする。

## 1. 日次コスト（一次指標）

- **定義**: `v_daily.cost_usd`。全requestの自前計算合計（SDK推計ではない）。
- **単位**: USD/日。
- **健全域**: 利用者が設定した日次予算以下。分布で語り、直近4週の p50/p90 を添える。
- **罠**: 中断分の output は未計上（下限値）。プロンプト帰属の欠けの影響は受けない（v_daily が正）。

## 2. ペース比（pace_ratio）

- **定義**: 本日のこれまでの累積 ÷ 同曜日直近3週の同時刻累積の**中央値**（state.json `today.pace_ratio`、
  ベースライン2日未満なら null=参考値なし）。statusline には表示せず、日次レポート（`metsuke today` 等）で参照する。
- **単位**: 倍率。**着地予測** landing_usd = 本日累積 + 3週の「残り時間帯支出」の中央値。
- **健全域**: 緑 ≤1.2 / 琥珀 ≤1.5 / 赤 >1.5。
- **罠**: 同曜日比較なので祝日・休暇はregime_eventで確認してから解釈する。平均でなく中央値
  （スパイク1日に引きずられない）。**基準累積が小さい時点は不安定** — 基準日の同時刻累積が
  まだ0に近いため、倍率が大きく振れるか、一部の評価対象では null に落ちる。
  分母が小さい時点の値は方向の目安にとどめ、絶対値で判断しない。なお基準日の採用は「その日に一度でも
  request があるか」で判定しており、同時刻までが0の日も**正当な標本として分母に含める**
  （同時刻まで利用がない日も測るべき信号であってノイズではない。
  [06-open-questions Q16](06-open-questions.md) で検証済み）。

## 3. 燃焼レート（burn_rate_usd_h）

- **定義**: wall-clock の直近30分の請求額を時給換算（`burn_cost * 3600 /
  METSUKE_BURN_WINDOW_S`）。直近30分に request が0件なら null。state.json `today.burn_rate_usd_h`。
- **単位**: USD/時。
- **健全域**: 閾値の唯一の正は [statusline/spec.md §3](statusline/spec.md)。
- **罠**: 窓は wall-clock であり「直近の活動30分」ではないため、手を止めればレートは下がる。
  これは「今燃えているか」を表すための意図した挙動。窓の途中で始まったバーストは30分で割るので
  過小に出る。実測では時間帯どうしの差より同一時間帯内のばらつきが桁違いに大きかったため、
  時間帯正規化はしていない。

## 4. プロンプト単価分布

- **定義**: `v_prompt_cost.cost_usd` の分布（サブエージェント込み）。
- **支配項の定義（2026-07-21確定・単一所有）**: `cache_read` / `cache_creation`（5m書込と1h書込の**合算**） /
  `output` / `input` / `server_tool` の5分類で、いずれも `price_factor`（200k超の単価倍率）を掛けた
  金額の最大項。所有は `viewmodel/prompt.py` の1箇所であり、V1 period・explain・statuslineは
  すべてこれを使う。**再実装を禁じる。**
  - 以前はV1 periodだけが独自計算を持ち、5m/1hを分離し `price_factor` と `server_tool` を
    無視していた。実運用サンプルでは**約1.5%で支配項の結論が食い違い**、不一致は高額側に
    集中していた。調べる価値がある対象ほど矛盾するため統一した。
  - 5m/1hを合算するのは、TTLの内訳ではなく「キャッシュ作成に払ったか」が行動の分岐点だから。
    TTL別の内訳が要るときはV3 cacheが持つ。
- **単位**: USD/プロンプト。**平均は使わない — p50/p90/p99 で語る**。
- **健全域**: p50 ≤$1 / p90 ≤$8 目安。$5超の単発は暴走ガード（nudge表 runaway_guard）と突合。
- **罠**: prompt_id NULL の帰属不能分は含まれない。中断プロンプト（interrupted=1）は過小。
  agent走行費と task-notification ラリーの後続作業は起点プロンプトへ因果帰属済み
  （ADR 0009/0010）— 「1プロンプト」は「人間の1指示の因果総コスト」を意味する。

## 5. キャッシュ恒等式の破れ率

- **定義**: `v_cache_identity` の行数 ÷ 対象request数（系譜内で `cache_read(n+1) ≈
  cache_read(n)+cache_creation(n)` が prev_input+16tok を超えて崩れた回数）。原因分類つき。
- **単位**: 件/日・原因別構成比。
- **健全域**: ttl_expiry と unknown の**減少トレンド**が正義。初期の実運用サンプルでは
  unknown が最大群で、hook証拠の蓄積により compaction/config_change へ再分類された。
- **罠**: 破れ1件 = 「その時点の再構築コスト発生」。金額換算は `ゼロから再読みしたcache_creation
  トークン×書込単価` で見積もる。lineage単位で見ること（session単位は並列agentで偽陽性）。
- **`unknown` の解釈**（2026-07-19 分解・[06-open-questions Q14]）: unknown は gap符号で
  **read過大(+)＝キャッシュが増えた事象**と**read喪失(−)＝失った事象**がほぼ半々に混在する。
  **喪失側だけが V3の「回収されているか」に効く**（増加側は再構築費が発生しない）。
  なお **compaction の実体は喪失(−)側**（クリーン事例で read 224k→20k・要約を新規writeして再構築費が立つ）。
  集計上 compaction が「+型多数」に見えた主因は、**cause 判定の時間窓が実際には効いていなかった
  スコープバグ**（2026-07-19 発見・修正済み）。`v_cache_identity` の EXISTS 内で非修飾の `ts` が
  外側の `seq.ts` ではなく内側の `hook_event.ts` に束縛され、上限条件が自明に真になっていたため、
  「直前リクエスト以降にそのセッションで一度でも該当hookがあれば」ラベルが付いていた。
  修正により compaction / config_change の誤ラベルが大幅に減り、分類カバレッジも約4割から
  約3割へ下がった。
  **カバレッジ低下は劣化ではなく、従来値が誤ラベルで水増しされていたことの是正**である。
  その後 **5m-TTL層の追加**（2026-07-19・4条件限定）で分類カバレッジは数ポイント回復した。
  この層は「直前が5mのみ書込 ∧ 実待機>300秒（**終了基準**） ∧ 全消失 ∧ 生存中の1h書込なし」に
  限定されており、`unknown` からのみ再分類する（他のcauseは不変）。
- **⚡件数には「read二重計上」由来の偽ペアが混じる**（2026-07-19・原因未特定）: 1リクエストの
  `cache_read_tok` が直前の累積プレフィックスの約2倍で報告される事象があり、これが
  **増加(+)と喪失(−)を1件ずつ生む**。設計時サンプルでは**増加の約1割・喪失の約6%**を占めた。
  ingest の合算ではなく報告値そのもの（transcript原本で確認）。実課金かは請求書突合待ち。
  **⚡件数のトレンドを読むときは、増加側の1割前後がこの偽陽性であることを見込む**
  （[06-open-questions Q14]）。増加(+) unknown の正体は**会計上の偽陽性が主**で、
  **79.3% は直前ターンの output の再読**（gap ≈ 直前 request の output_tok）。恒等式を
  `read(n)+write(n)+output(n)` に補正すればこの分は破れとして湧かなくなる（[06-open-questions Q14]）。
  健全域トレンドは**符号分離後の「喪失系 unknown」**で読む（総unknownの減少ではなく）。

## 6. 起動固定費（context overhead）— 最安レバー

- **定義**: `v_context_overhead.startup_context_tok`（セッション初回リクエストの合計トークン =
  システムプロンプト＋ツール定義＋CLAUDE.md＋メモリ）。
- **単位**: トークン/セッション起動（週平均で追う）。
- **健全域**: 減少 or 横ばい。実運用では起動固定費が短期間に**約4割増え、数万tok規模**へ
  肥大したことがあり、MCP定義削減などの判断根拠になった。
- **罠**: モデルによりシステムプロンプト長が違う — 推移比較は同一モデル内で。削減施策
  （MCP定義削減・CLAUDE.mdダイエット）は marker を切ってから実施する。

## 7. サブエージェント構成比

- **定義**: `v_prompt_cost.n_agents / n_agent_requests`、金額側は
  `v_request_cost` を `agent_id IS NULL` で分解。
- **単位**: %（プロンプト費用に占める委任分）。
- **健全域**: 委任が悪ではない（探索の外部化は本体contextを守る）。**「委任した上に本体でも
  同じファイルを読み直す」二重払いの検出**が本義。
- **罠**: サブエージェントrequestのprompt_id は spawn元プロンプトへ**因果帰属**済み
  （[ADR 0009](adr/0009-causal-prompt-attribution.md)・走行時間帯ではなく起動した指示に付く）
  — v_prompt_cost は既に込み。二重加算しない。

## 8. nudge conversion（介入の追従率）

- **定義**: `nudge` 表。`followed` の意味はルール別（[hooks仕様 §5](hooks/spec.md) の述語表が正）:
  coldcache_warn = `/handoff` 等の明示的なセッション切替 / budget_warn = `/handoff`・モデル引き下げ・
  effort引き下げ等の観測 / runaway_guard = 中断または費用増加停止。判定は発火+600秒の論理時刻。
- **単位**: `followed / (followed + not_followed)`。unknown は別途観測率へ出し、分母に含めない。
- **健全域**: **2週間で conversion <10% のルールは文面・閾値を改訂**（roadmap撤退基準）。
  自動ミュートはしない — 週次レポートの棚卸し項目。
- **罠**: 「後続fan-outが無い」「10分間イベントが無い」だけでは成功ではなく unknown。
  `outcome_reason` と `observed_json` を確認する。unknown過多は介入効果ではなく観測設計の問題。

## 9. marker 勝敗（施策台帳）

- **定義**: `marker` 表。開始/終了時刻・仮説・期待効果・verdict（win/loss/inconclusive）。
- **判定方法**: 厳密統計はしない（単一ユーザーはA/B不能）。**前後のマクロ比較**（同曜日補正した
  日次・対象メトリクスの p50/p90 シフト）＋ regime_event の交絡確認で、人間が verdict を打つ
  （AIは提案まで — ADR 0005）。
- **健全域**: 常時 open marker ≤2（同時多施策は帰属不能になる）。
- **罠**: marker 期間中の regime_event（モデル追加・CC更新・休暇）は必ずレポートに併記。

## 10. ラベルカバレッジ（Stage 4-5 から）

- **定義**: `prompt.task_label` 非NULL率（税目: feature / incident / design / refactor / chore）と
  `outcome` 行の存在率。
- **健全域**: ラベル ≥80%（4-5 受け入れ基準）。
- **罠**: auto ラベルは AI 提案 + `metsuke approve` 承認済みのみ（無承認の自動書込は存在しない）。

## 11. 計測ヘルス

- **定義**: 台帳鮮度（`MAX(request.ts)` と hook活動の乖離 = state.json `stale`）・quarantine増分・
  未知モデル（price join 不成立）・第2推定器乖離（直近48時間でrequest_id対応した
  OTel `cost_usd_sdk` vs 自前計算のセッション別中央値、±10%目安）。statuslineのセッション累積値は
  サブエージェント集約範囲が異なるため、この整合判定には使わない。
- **罠**: 乖離±10%は**校正情報**でありゲートではない。stale ⚠ が出たら分析より先に計測を直す。

## 12. タスク効率

- **定義**: `v_task_efficiency`。実タスクに紐づく全プロンプトとagentの費用、成果、品質1〜5、
  手戻り時間を同じ行で扱う。主指標は同category内の completed率、費用分布、quality、rework。
- **単位**: USD/task、分/task、quality score。`cost_per_quality_point` は補助指標。
- **健全域**: 絶対閾値は4週間の本人ベースライン後に設定。まず終了タスクのoutcome付与率80%以上。
- **罠**: 高品質タスクは高価でよい。異なるcategoryや規模を単純な平均費用で順位付けしない。
  active taskを開始し忘れたプロンプトは自動帰属されないため `metsuke task attach` で補正する。

## 13. ツールROI

- **定義**: win markerの検証済み削減レンジ ÷（analyst費＋記録した直接費＋人時間×時間価値）。
  `metsuke roi` の point / low / high を併記し、四半期判断は `metsuke roi --days 90` を使う。
- **単位**: 倍率。分子・分母ともUSD。
- **健全域**: low推定でも1倍超を目標。2四半期連続赤字なら縮退判断。
- **罠**: 保守・レビュー・通知割込みを `metsuke roi --add-cost` へ記録しない限り過大評価になる。
  時間を記録して `METSUKE_HOURLY_VALUE_USD=0` のままなら cost_complete=false。

## 14. TTL施策レビュー

- **定義**: 直近28日の `v_cache_identity.cause='ttl_expiry'` に対応するcache write再構築費。
  `metsuke ttl-review` は証拠期間28日かつ10稼働日を要求し、1日$5未満または全cache writeの10%未満なら
  deprioritize、それ以外は continue_experiment とする。
- **単位**: 件、USD/暦日、cache write費に対する割合。
- **罠**: ttl_expiryは内在証拠による分類で、介入による因果削減額の保証ではない。
  休暇・モデル変更・設定変更はregime_eventと併記し、データ不足は結論にしない。

## L. 行動レバー一覧（帰属分析はこの語彙で書く）

| # | レバー | 動く指標（§） | 期待方向 |
|---|---|---|---|
| L1 | 使わないMCP定義・CLAUDE.md/メモリの削減 | 6（起動固定費）→1 | 全セッションに恒常減 |
| L2 | /handoff でセッション分割・肥大contextの捨て時 | 4 p90・5 ttl_expiry | 入力側単価減 |
| L3 | キャッシュTTL内の再開判断（🔥表示・事前通知に従う） | 5 ttl_expiry・再構築$ | 破れ減 |
| L4 | モデル引き下げ（タスク種に応じ sonnet/haiku へ） | 1・4・model mix | 単価減 |
| L5 | サブエージェント fan-out の抑制/委任の質 | 7・nudge runaway | 二重払い減 |
| L6 | 迷走の早期中断（Esc）と依頼の明確化 | 4 p99・interrupted率 | テール減 |
| L7 | compaction 後の再読み込み削減（要点を先に書き出す） | 5 compaction・4 | 再構築減 |
| L8 | 予算警告への応答（重い作業の翌日送り） | 1・2 pace | 日次平準化 |

**レポートの帰属節は「どの L が動いたか」を必ず1つ以上特定し、対応する§の指標変化で裏付ける。**
どの L でも説明できない変化は regime_event（外生ショック）を疑う。
