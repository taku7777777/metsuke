# 02 — データモデル: 捕捉・永続化・導出

## 1. 捕捉ソースと実地検証済みの事実（2026-07-17 実機調査）

| ソース | 検証済みの事実 | 役割 |
|---|---|---|
| **トランスクリプト** `~/.claude/projects/<proj>/<sid>.jsonl` | assistantレコードに `requestId`・`message.usage`（4種トークン＋ `cache_creation` の**5m/1h TTL別内訳**＋ `server_tool_use`・`service_tier`・`inference_geo`・`speed`）・`message.model` が存在。`parentUuid` ツリーは複数分岐を持つ。`version`・`gitBranch`・`cwd`・`promptId`・`slug` | **一次ソース** |
| 同 `<sid>/subagents/agent-<17hex>.jsonl` + `agent-*.meta.json` | 全レコード `isSidechain:true`＋`agentId`。meta.json の `toolUseId` が親の Task tool_use ブロック `id` と一致（**個体識別＋親子リンクが確定的**）。親側 user レコードの `toolUseResult` に子の合計 usage | サブエージェント系譜 |
| 同 tool_use / tool_result | `tool_use.id` ↔ `tool_result.tool_use_id` の一致率100%＋`sourceToolAssistantUUID` で二重リンク | ツール往復の正確な連結 |
| 同 中断・エラー | user レコード `interruptedMessageId`。中断時はusageを保持する群とassistant行自体が無い群があり、`stop_reason=null`のoutput_tokensはプレースホルダ → 入力側は部分捕捉・出力側は欠損。APIエラーは `<synthetic>` model＋`isApiErrorMessage`（usage全ゼロ） | 未計上の部分回収 |
| 同 保持 | cleanup期間に上限があり、ローカル原本は恒久保存されない | **アーカイブ必須の根拠** |
| **hooks stdin JSON** | session_id / prompt_id / transcript_path / cwd 等。UserPromptSubmit は block・systemMessage・additionalContext 可（公式） | 時刻計測（待機・compaction）＋介入 |
| **statusline stdin JSON** | `cost.total_cost_usd`（クライアント推計）・`context_window.used_percentage` / `total_input_tokens` 等（公式）・`version` | context水位の時系列＋**第2コスト推定器**。version併記で context_window フィールドのバージョン間安定性を追跡 |
| **OTelタップ**（api_request イベント） | 公式の `session.id` / `event.sequence` / `prompt.id` とClaude属性から、`query_source`（背景機能: away_summary等）・`effort`・SDK `cost_usd` を取得。collector入口でprompt/tool詳細属性を削除 | 背景機能コスト・effort・リモートagent |
| **git** | post-commit hook → spool（コミット・変更行数を時刻とcwdで記録）。revert検出は「revertコミット＋同一ファイルの再修正」ヒューリスティック | 成果（outcome）と counter-metrics の分子 |
| **手入力** | console.anthropic.com の月次実請求（`metsuke invoice`） | 校正の最終アンカー（**請求額を確定できない契約形態では利用不可** — [Q1](06-open-questions.md)） |
| **単価表** | 公式pricing文書を出典URL・確認日つきSCD2で同梱。モデル/期間別のFast係数と `server_tool_use` 単価を別行管理し、UTCの日付で解決。`metsuke prices` で適用値を表示（更新手順は[PRICES.md](PRICES.md)） | API単価換算の利用量導出（定額購読の請求/上限ではない） |

## 2. 取込規則（正確さの生命線 — golden fixtureでテスト固定）

1. **デデュープ**: 1つのAPI応答が content block ごとに複数 assistant レコードへ分割され、
   **同一 `message.id`/`requestId` が同一 usage を複製保持**する（実運用サンプルでは大半が複数レコード）。
   `requestId`（無ければ `message.id`）単位で1行に正規化する。
2. **分岐（リトライ/rewind）**: parentUuid ツリーは分岐する。課金実績としては**全枝を保持**し、
   `ai-title` / `last-prompt` / `mode` / `permission-mode` / `attachment` / file-history系は
   非課金メタデータとして台帳化せず、archive原本だけに保持する。未知の新形式は隔離する。
3. **サブエージェント連結**: `subagents/agent-*.jsonl` を `agentId` で個体登録し、
   `meta.json.toolUseId` → 親 Task tool_use で親子リンク。親側 `toolUseResult.usage` は照合用に保持。
4. **除外・隔離**: `<synthetic>`/usage全ゼロ行は除外フラグ付き保持。未知レコード型は quarantine 表へ
   生JSONごと退避（落とさない）。パーサは未知フィールドを素通し、`parser_version` を全行に刻む。
5. **系譜（lineage）**: `lineage_id = session_id × coalesce(agent_id,'main')`。
   キャッシュ恒等式・トレース木の単位。
6. **真のストリーム途中切断のみ output を信用しない**: `stop_reason` が null の中断行は
   output_tokens がストリーム開始時のプレースホルダ値のため **NULL化**する（原値は raw_ptr で
   遡れる）。`stop_reason` が立つ中断（ツール実行前後に着地）は課金確定済みの実数として温存する。
   `is_interrupted=1` はどちらも中断イベントの事実として記録する。`v_unaccounted` は参照先 request
   が丸ごと不在の集合のみを対象とするため、温存した output との二重計上は起きない。
7. **OTel行との重複計上禁止**: OTelタップ由来の行は、ローカル既知の session_id に一致すれば
   **enrichment のみ**（query_source/effort/cost_usd_sdk の付与）でコスト計上しない。
   計上するのは remote フラグ付き・トランスクリプト不在のもののみ。
8. **agentリクエストは因果帰属**（[ADR 0009](adr/0009-causal-prompt-attribution.md)）:
   `agent_id` 付き request の `prompt_id` は、agentレーン観測値（＝記録時点のmain
   アクティブプロンプト。走行中agentでは後続プロンプトへスライスされる）ではなく、
   **spawn 連鎖**（`agent.parent_tool_use_id → tool_call → 親request.prompt_id`）で確定する。
   プロンプト集計の意味は「この指示が引き起こした総コスト」。ネストagentは親から順に
   固定点まで解決。連鎖が辿れない場合のみ観測値を温存。`tool_call.prompt_id` は
   所属 request と常時同期。
9. **task-notification 擬似プロンプトは起点へ畳み込む**（[ADR 0010](adr/0010-notification-prompt-folding.md)）:
   本文が `<task-notification>` で始まるプロンプト（ハーネス注入・人間の入力ではない）に
   帰属した request は、通知本文の `<tool-use-id>` → tool_call 直接参照（第一経路）または
   `<task-id>` → agent → spawn 連鎖（フォールバック）でタスクの起点プロンプトへ再帰属する。
   両経路とも不能なら温存。prompt 行自体（text・ts）は残る（request 0件になるため
   集計・trace 表示からは消える）。

## 3. 永続化スキーマ（ledger.db — 事実のみ・全て永久保持）

**導出金額は保存しない**。ただし「観測された金額の事実」（SDK推計 `cost_usd_sdk`・実請求
`billed_usd`）は事実として保存する — 保存しないのは自前計算の解釈だけ。

```sql
request(request_id PK, message_id, session_id, agent_id, lineage_id, prompt_id, ts,
        model, input_tok, output_tok, cache_read_tok,
        cache_w5m_tok, cache_w1h_tok,            -- TTL別内訳（実在を確認済み）
        server_tool_use, service_tier, speed, geo,
        stop_reason, on_main_path, is_synthetic, is_interrupted,
        source,           -- transcript | otel | remote
        parser_version, raw_path, query_source, effort, cost_usd_sdk, end_ts, api_duration_ms)
prompt(prompt_id PK, session_id, ts, text /*全文*/, interrupted_message_id, task_label)
tool_call(tool_use_id PK, request_id, name, ts, is_error, result_bytes)   -- 本文は保存しない
agent(agent_id PK, session_id, agent_type, parent_tool_use_id, spawn_depth, resolved_model)
session(session_id PK, project, slug, git_branch, version, started, ended)
hook_event(ts, kind, session_id, prompt_id, payload_json)  -- 待機時間・compaction時刻・context水位
otel_event(ts, request_id?, query_source, effort, cost_usd_sdk, raw_json)  -- タップ由来
-- 人間とAIの判断（spool経由・rebuildを生き残る）
marker(marker_id PK, ts_start, ts_end, category, hypothesis, expected_effect,
       verdict, verdict_ts, decided_by, saving_usd, saving_low_usd, saving_high_usd,
       saving_basis, verdict_note)
outcome(prompt_id, ts, label, lines_added, lines_removed, commits, source /*auto|manual*/,
        UNIQUE(prompt_id, ts, source))
nudge(rule, fired_ts, session_id, detail_json, followed, decided_ts,
      outcome /*followed|not_followed|unknown*/, outcome_reason, observed_json, experiment_group)
work_task(task_id PK, title, goal, category, project, ts_start, ts_end, status,
          outcome, quality_score, rework_minutes, note, created_by)
task_prompt(task_id, prompt_id UNIQUE, attached_ts, source, confidence)
roi_cost(cost_id PK, ts, kind, minutes, usd, note, source)
regime_event(ts, kind /*cc_version|model_new|price_change|config_change*/, detail)  -- 外生ショック台帳
-- 参照
price(model, valid_from, valid_to, in_usd, out_usd, cache_read_x, cache_w5m_x, cache_w1h_x,
      batch_x, fast_x, geo_us_x, source_url)     -- SCD2。gitのprices.jsonが原本
price_server_tool(tool, valid_from, valid_to, usd_per_unit, source_url)
invoice(month PK, billed_usd, note)
-- 運用
quarantine(ts, src, reason, raw)
ingest_log(ts, manifest_pos, segments, records, quarantined, parser_version)
```

## 4. 導出（SQLビュー — git管理・保存しない）

| ビュー | 定義の要点 |
|---|---|
| `v_request_cost` | トークン×price(SCD2をts解決)。5m/1h別書込単価・batch/fast/geo係数・server_tool_use を反映。未知モデルは**NULL**（ヘルスへ浮上） |
| `v_rollup_*` | prompt→request列→サブエージェント合算の全階層ロールアップ（trace/セッション/日次/週次） |
| `v_cache_identity` | 系譜内 lag() で `cache_read(n+1) − (cache_read(n)+cache_creation(n))` を検査。**許容誤差つき**（input端数1〜2tok・実測は≒）。破れの原因分類: `ttl_expiry / compaction / model_switch / config_change / clear_intentional / interruption / unknown`（compaction hook時刻・SessionStart設定ハッシュ・待機gap・中断ファクトと突合） |
| `v_pace` | 本日累積 vs 同曜日直近3週の同時刻中央値・着地予測。**ベースライン欠損時は「参考値なし」表示**（ゼロ除算やゴミ値を出さない）。日次境界は**ローカルタイム**（METRICS.md で確定・請求突合時のみUTC換算） |
| `v_task_efficiency` | work_task単位のprompt/request/agent費、経過、outcome、quality、rework。異なるcategoryを単純比較しない |
| `v_context_overhead` | **固定オーバーヘッド組成**: セッション初回リクエストの cache_creation からシステムプロンプト＋ツール定義＋CLAUDE.md/メモリ規模を推定し推移を追う（「使わないMCPを外す」=全セッションに効く最安レバーの計測） |
| `v_unaccounted` | 未計上推計。中断は「入力側 = 直近cache_read×0.1x＋新規分×書込単価、出力側 = 不明（下限0）」の**下限推計**として計上（過大推計の混入禁止） |
| `v_health` | 鮮度（ソース別）・取込ゼロ・quarantine件数・第2推定器乖離・未知モデル・**ラベルカバレッジ**（task_label未付与率・outcome欠落率）・orphan agent |
| `v_counter` | 過剰最適化の検知: 同一タスクの再試行率・revert率・上位モデル差し戻し率 |

## 5. 校正の二段分離プロトコル（月次）

実請求とのギャップ = 単価誤差 + 未計上（中断・背景・リモート欠落）+ TTL混在誤差 + server_tool_use。
これを混同して単価表を「校正」すると全履歴を誤った単価で汚染するため:

1. ギャップからまず `v_unaccounted` の推計を差し引く
2. 残差が閾値内（±5%目安。この閾値は**請求残差**用 — SDK推計との日常乖離監視±10%とは別物）の
   場合のみ price の SCD2 行として校正を追加（遡及再計算が自動で走る）
3. 超える場合は校正せず「説明不能残差」として v_health に計上し、原因調査タスクを起こす
