# 04 — ロードマップ: 最速で最強に到達する実装計画

## 原則

- **各Stageが単独で価値を出す**。どこで止めても、止めた時点までの投資が回収されている状態を保つ。
- **正確さの生命線（取込規則）を最初にテストで固定**し、以降の全機能をその上に積む。
  受け入れ基準は「その時点で存在するデータ・機構だけで機械的に検証できる」ことを条件とする。
- 開発フロー: Claude が設計・受け入れ基準を書く → Codex が実装 → **golden fixture＋実データで検証**
  （diffレビューは Claude）。1 Stage = 1〜2 PR 粒度。
- 撤退基準を各Stageに持つ。効かない部品は削る（道具自身のROI管理 — [00-vision](00-vision.md) 成功基準）。

## Stage 0 — 出血を止める（Day 0・即日）🩸

トランスクリプトはcleanup設定により有限期間で消える。**他の全てに先行して原本確保を開始する。**
リダクションは Stage 0 に置かない（原本を不可逆破壊するリスクと Day 0 の遅延要因になる —
リダクションは読み出し境界で行う。[01 セキュリティ](01-architecture.md)）。

| # | タスク | 受け入れ基準 |
|---|---|---|
| 0-1 | リポジトリ scaffold（uv・pytest・ruff・本docs）＋ `metsuke doctor` 最小版（launchd plist ロード確認・最終実行時刻） | `metsuke --help` と `metsuke doctor` が出る |
| 0-2 | archiver: `~/.claude/projects/**`（subagents含む）を**無加工で**月別zstdへ増分退避＋sha256台帳。**最後の改行までを退避**（書込み中ファイルの欠け行対策）。launchd毎時 | 既存全ファイルのバックフィル完了・1時間後の増分が取れている・doctor が最終実行時刻を表示 |
| 0-3 | パーミッション固定（archive/ 700・ファイル600） | `ls -l` で確認 |
| 0-4 | `cleanupPeriodDays` の設定確認・必要なら延長（保険の二重化） | settings で明示されている |
| 0-5 | 暗号化オフサイトコピー（restic最小構成 or 暗号化外付け、日次） | 初回コピー完了・リストアで1ファイル取り出せる |

**工数目安: 0.5〜1日。今日やる。**

## Stage 1 — 台帳と説明（Week 1）

J1/J4の土台。**正確さはここで決まる。**

| # | タスク | 受け入れ基準 |
|---|---|---|
| 1-1 | golden fixture 整備: 実トランスクリプトから匿名化サンプル凍結（複数レコード分割・分岐・サブエージェント・中断・synthetic を含む）＋期待値 | `pytest` が**取込規則5点**（デデュープ/分岐/連結/隔離/**中断output のNULL化**）を固定 |
| 1-2 | ingester v1: transcripts → ledger.db（02のスキーマ）。カーソル増分・quarantine・parser_version・多重起動ロック。**`version` 新値の初観測を regime_event に自動記録** | 全履歴取込後、**request数・トークン合計が fixture 期待値と完全一致（決定的ゲート）** |
| 1-3 | price SCD2（初期値投入）＋ `v_request_cost` / `v_rollup_*`。**statusline センサーの一時キャプチャを先行導入**し参考突合 | fixture のコスト期待値と一致（ゲート）。クライアント推計との乖離±10%は**校正情報として記録**（ゲートにしない — 推計側の癖が原因でも先へ進める） |
| 1-4 | `metsuke today / explain <prompt_id>`（テキスト版） | explain 出力に単価分解・ツール往復・サブエージェント合算・**支配項の1行説明**が含まれる（機械検証） |
| 1-5 | `v_cache_identity`（許容誤差・原因分類）— この段階では**トランスクリプト内在証拠のみ**で分類（compact記録・中断・モデル切替・経過時間）。hook突合による精緻化は 2-6 | 実データで破れが検出され、内在証拠で分類される |
| 1-6 | `v_context_overhead`（セッション初回 cache_creation の組成推定）— **最安レバー「使わないMCP/CLAUDE.md削減」を初週から見える化** | セッション別の固定費推定が出る |
| 1-7 | rebuild 決定性テスト | ledger 削除→`metsuke rebuild`→全テーブル内容ハッシュ一致 |
| 1-8 | リダクション層: versioned パターンmanifest（redaction_version）を parse層（prompt.text）と AI可視アクセサに適用。検出ログは「パターン名＋sha256＋位置」のみ | fixture で検出・置換・**誤検出時は再parseだけで回復**できる |

**工数目安: 4〜5実働日。撤退基準: 1-2/1-3 の fixture 一致が達成できない場合、原因を潰すまで先へ進まない。**

## Stage 2 — 常時視界とセンサー（Week 2 前半）

J2。「見に行く」を廃止する。

| # | タスク | 受け入れ基準 |
|---|---|---|
| 2-1 | rollup → state.json（原子的書出。契約は [03 §4](03-interfaces.md)）＋hook駆動ingest＋launchd 5分フォールバック | Stop後60秒以内に state.json が更新される |
| 2-2 | statusline（表示＋センサー化＋鮮度⚠）。センサー記録は**値変化時のみ・最小間隔15秒**（spool洪水防止）。現在 statusLine は未設定のため衝突なし | 常時表示が機能し、ingest停止15分で⚠が出る（クロック注入の単体テスト＋初回手動スモーク） |
| 2-3 | **センサーhooks一式の設置**: SessionStart（model/effort/MCP構成ハッシュのスナップショット）・Stop/UserPromptSubmit（時刻→待機gap）・PreCompact/PostCompact（Q2の実在確認込み）・Notification。＋**hookハーネス**（共通timeout・fail-open・「spool追記 or state.json参照のみ」を強制する規律テスト） | 各hookのspool行がfixtureで検証され、規律テストが通る |
| 2-4 | 応答後の利用量フィードバック。2026-07-20にOS領収書主体から**statuslineの黄/赤＋詳細HTMLリンク**へ移行。OS高コスト通知は既定OFFの任意機能 | `$3`以上黄・`$7.5`以上赤。高コスト値から対象プロンプトのHTMLへ降下できる |
| 2-5 | `v_pace`（同曜日ベースライン）。**バックフィル約2週＋稼働後の蓄積で3週に到達次第フル動作**。欠損時は「参考値なし」 | 2週分データで部分表示・欠損時にゴミ値を出さない |
| 2-6 | v_cache_identity の hook 突合分類拡張（SessionStart設定ハッシュ・compaction時刻） | config_change / compaction の分類精度が内在証拠のみより向上 |
| 2-7 | `metsuke trace <session>` / `week` / `tasks` ＋ explain/trace の Tree+Waterfall TUI化（Textual） | 系譜レーン（並列サブエージェント個体分離）と恒等式破れ⚡が1画面で辿れる |

**工数目安: 3実働日。撤退基準: 1ヶ月後もダッシュボード的な画面を見に行きたくなるなら、この層の設計を見直す。**

## Stage 3 — 行動介入（Week 2 後半）

J8。ここが本体。

| # | タスク | 受け入れ基準 |
|---|---|---|
| 3-1 | cold-cache 再開警告（UserPromptSubmit・state.json の系譜TTL残参照）＋ **gap 50分の事前通知（発火主体は ingester の launchd tick**。hookはアイドル中に発火しないため） | 故意に1h放置→再開で警告文（金額つき）。50分時点で事前通知 |
| 3-2 | API換算の内部予算警告50/80/100%。当初の100%ハードストップは、定額購読の実上限と一致しない代理値で仕事を止めるため撤去 | 各段が一回だけ発火し、100%でも送信を止めない |
| 3-3 | 実行中の暴走ガード。**機構**: statuslineセンサーのセッション別コストスナップショット × UserPromptSubmit時の開始値差分を ingester（PostToolUse契機）が state.json の「進行中プロンプト累積」へ反映。**SLO: ツール完了から60秒以内に検知**。通知は terminal-notifier＋**ntfy最小経路（curl 1本）**でオフラップトップにも届ける | fan-outテストで**サブエージェント実行中でも**通知が届く |
| 3-4 | `/handoff` スキル（引き継ぎブリーフ生成＋新セッション） | 実際に1回セッションを切り替えて文脈が引き継がれる |
| 3-5 | nudge conversion 計装（ルール別の観測述語は [hooks仕様 §5](hooks/spec.md) の定義に従う。計算は ingester の rollup 段） | nudge表に発火と判定結果が残る |

**工数目安: 3実働日。撤退基準: 2週間のconversion率が10%未満のルールは文面・閾値を改訂、
それでも効かなければ無効化（ただし自動ミュートはしない）。**

## Stage 4 — 閉ループ（Week 3）

J3/J7。分析の労力を人間からAIへ移す。

| # | タスク | 受け入れ基準 |
|---|---|---|
| 4-1 | SCHEMA.md / METRICS.md（AI契約書。**行動レバー一覧・日次境界の定義**を含む） | **ベンチ質問N問（期待SQL/期待値つき）**に新規セッションの Claude が正答 |
| 4-2 | `metsuke mark / done / approve / regime add`＋marker勝敗台帳 | 施策1本をマーカー付きで開始できる。**marker/label 投入後に rebuild → 判断データが生存**（テスト） |
| 4-3 | cost-analyst skill＋週次 launchd。**最小権限の強制**: `--allowedTools` 許可リスト・sqlite `mode=ro`・Write は reports/ と spool/proposals/ のみ・Bash/Web系禁止（[ADR 0005](adr/0005-ai-analyst-least-privilege.md)） | 初回レポートが生成され帰属が実データと整合。**「アナリスト設定から書込到達経路ゼロ」をテストで確認** |
| 4-4 | 欠報デッドマン（月曜朝チェック→プッシュ通知） | レポートを故意に消して通知が来る（クロック注入テスト） |
| 4-5 | **成果計装＋自動ラベル**: git post-commit hook→spool（コミット紐付け）・PostToolUse の変更行数・**revert検出**（revertコミット＋同一ファイル再修正）・task_label 自動付与（AI事後・spool経由提案・訂正可能）・ラベルカバレッジを v_health へ | 全履歴の80%以上にラベル・outcome の auto 行が貯まり始める |
| 4-6 | **着手前見積り**（task_label×規模の分位点を state.json へ事前焼込→UserPromptSubmit で提示。依存: 4-5） | ラベル済みタスク種で $レンジが提示され nudge 表に記録される |

**工数目安: 4実働日。撤退基準: 4週間で施策判定が1本も回らなければ、週次の形式を対話型に変更。**

## Stage 5 — 完全性（Week 4）

J6の完成と死角の解消。

| # | タスク | 受け入れ基準 |
|---|---|---|
| 5-1 | OTelタップ（otelcol-contrib→file→ingest）: query_source/effort/SDK cost_usd。**二重計上禁止規則**（ローカル既知 session に一致する行は enrichment のみ・計上は remote/transcript不在のみ — 02 §2 規則7） | 背景機能コストが日次で見え、総額が transcript 系と二重計上されない |
| 5-2 | 第2推定器の日次自動突合＋取込ゼロ検知＋v_health 全項目 | v_health が実データで動作・故意の停止/欠測を検知 |
| 5-3 | `v_unaccounted`（下限推計）＋`metsuke invoice`＋校正の二段分離 | 月次突合が儀式として1回完走する |
| 5-4 | リモート/コンテナagent受け（source=remote・忠実度フラグ）＋オフラップトップ通知の本格化（Q7の選定結果を反映） | リモートagentのコストが計上され、閾値超過が携帯に届く |
| 5-5 | `v_counter`（過剰最適化検知）＋ `metsuke roi`（分子=verdict付き施策の推定削減額、分母=アナリスト実費＋申告保守時間） | 週次レポートに counter/roi 節が出る |
| 5-6 | `metsuke doctor` 完全版・restic本格運用・リストア検証・運用手順書 | doctor 全green・リストア検証1回完走 |

**工数目安: 4実働日。撤退基準: 四半期ROIが2期連続で赤字（運用コスト>検証済み削減額）なら
週次アナリストを停止し Stage 2 構成（台帳＋statuslineの色/詳細導線＋緊急通知）へ縮退する。**

## Stage 6 — 深掘りHTML（trace/span）と時間軸の厳密化（2026-07-18〜）

J1/J5 の深掘り。ADR 0006（ADR 0003 の予約経路の発動 — 常設サーバは持たない）。
**時間軸の収束規則・セキュリティ規範・幾何のPython側焼き込みは [ADR 0006](adr/0006-html-trace-view.md) が正**（ここでは繰り返さない）。

| # | タスク | 受け入れ基準 |
|---|---|---|
| 6-1 | スキーマ＋ingest拡張（新列は各テーブル**末尾**・try-ALTERと同一順）: `request.end_ts` / `request.api_duration_ms` / `tool_call.result_ts` / `tool_call.workflow_run_id` / `agent.workflow_run_id`。tool_result 側は **upsert 化**（スタブ行許容・`result_ts IS NULL` の間だけ3値一括設定）。workflow derive は **prompt継承UPDATEより前**・`ORDER BY ts, tool_use_id LIMIT 1`。quarantine の redact→切詰め修正。v_health に end_ts/result_ts 充填率（直近7日）。PARSER_VERSION=4 | golden fixture 拡張（複数レコードrequest・tool_result先着/二重result・workflow subagent[meta=2キー採取形]・**otel先着→transcript昇格**・prompt_id NULL request）で新列の期待値一致。otel は archive の kind='otel' 経路で投入（DB直INSERT禁止）し rebuild 後に api_duration_ms 非NULL。migration冪等（PRAGMA table_info の**列順まで**新規DB/移行DBで一致）。`_table_hash` に tool_call/agent/**prompt** を追加し、増分==rebuild のハッシュ一致。quarantine.raw に fixture の秘密文字列が現れない |
| 6-2 | 生本文アクセサ: `source_dir()/raw_path` 優先・無ければ `archiver.reconstruct(rel)`（manifest は生成実行ごとに一括インデックス化・ValueError捕捉）。**JSONパース後の各テキスト値ごとに redact→（64KB超のみ）切詰め** を read-boundary 一関数で不可分に | live削除時に archive から同一内容が返る。redact→切詰めの順序・値単位適用がテストで固定 |
| 6-3 | redaction拡充: REDACTION_VERSION=2（ADR 0006 §7 のパターン群）＋ **ingest.py の prompt.text 逆順バグ修正**（redact後に切詰め） | 各新パターン1件ずつの検出テスト＋20000字境界跨ぎ秘密のテスト。rebuild で prompt.text に遡及 |
| 6-4 | HTML生成器: `src/metsuke/trace_html.py`＋`trace_template.html`（**1枚・差し込み点はJSON blobの1箇所・無ビルド**）。幾何（レーン/帯座標/費用バー）はPython側で静的SVG化、JSは操作系のみ（目安200行）。`metsuke trace/explain --html [--open]` → `~/.metsuke/traces/<session_id>.html`（explain は同一ファイル＋`#prompt=<id>` 初期選択）。CSP meta・application/json 埋め込み（`<`→`\u003c`・U+2028/29）・textContent描画・0600/0700・旧redaction版ファイルのpurge・生成記録をhook_eventへ取込。Stop領収書に `metsuke explain <prompt_id先頭8> --html` 導線（'last' のレース回避） | tests/test_report.py: (a) span幾何の契約テスト — fixture に対しレーン数・帯の開始/終端ts・金額分解が期待値一致（golden） (b) fixture の tool_result に `</script><script>alert(1)</script>`・`</ScRiPt foo>`・秘密文字列を仕込み、生成HTMLに**非エスケープ形が現れない**＋`[REDACTED` が現れる (c) CSP meta の存在と内容 (d) テンプレファイル数=1・差し込み点=1 (e) 旧版purgeの動作 (f) exit code: データ無し=1・`--open` 失敗は警告のみ0。test_analyst に「アナリスト起動面に traces が現れない」assert |
| 6-5 | 契約docs整合: SCHEMA.md（新列の行・NULL条件「rebuild遡及前はNULL」・result_ts人間待ち込み注記・otel行のtsセマンティクス）・README使い方表・03-interfaces **§3のTextual TUI記述を置換**＋**§2領収書行に導線追記**・00-vision原則2の1文精密化（deliberate層のオンデマンド生成物は除く）・RUNBOOK（traces/=導出物・restic除外作業は不要・ブラウザ履歴注意・四半期棚卸しに生成記録確認） | docs に現れるコマンド・列名・ファイル名が実装に実在する（grep で機械確認）＋人的diffレビュー（機械検証原則の明示的例外）。`metsuke rebuild` 後に実セッションで `--html` が完走し新列が充填されている |
| 6-6 | trace HTML 対話強化（FBラウンド1）: **時間軸独立ズーム**のため描画基板を再配分 — SVG は伸縮する形状のみ（`preserveAspectRatio="none"`・罫線/折れ線は `vector-effect="non-scaling-stroke"`）、テキスト・点マーカー（目盛/レーン名/ツール■/⚡/hooks）は geometry 座標から **DOMオーバーレイ**描画（非等方ズームでも文字が歪まない。JSの計算は `(x−plot_x)·zx / y·zy` の線形変換のみ）。zx/zy 独立ズーム＋`⌥+ホイール` アンカーズーム・`⌘B`/`⌘⌥B` の左右パネルトグル・レーンラベル列 sticky 化。詳細は [ADR 0006 追記](adr/0006-html-trace-view.md) | SVG に `<text` が無い・`preserveAspectRatio="none"`・geometry 新フィールド（ticks/lane_labels/sparks/hook_marks/context_label/色）の golden。テンプレ契約に `KeyB`・`no-nav`/`no-aside`。オーバーレイのピクセルオフセットは CSS transform で当てる（座標×zx に混ぜない — 高ズームドリフト回帰の防止） |
| 6-7 | trace HTML 対話強化（FBラウンド2）: **レーン別ツール展開**と描画基板のDOM移行 — SVG は context 折れ線のみ（高さ48ストリップ）、帯・ツール・罫線は geometry データから DOM 描画（hotspot と一本化）。geometry は**レーン相対座標**（lanes はリーン形 — 生dict同梱による JSON 肥大も解消）。展開レイアウトは **greedy packing**（行数=最大同時実行数・Python側・決定的）、展開時ツールは `ts→result_ts` 帯。折りたたみ時は **×N クラスタバッジ**（x差11px未満を集約・クリックで展開）。詳細は [ADR 0006 追記](adr/0006-html-trace-view.md) | packing golden（時間重複2本→行分離・直列→同行・近接2本→cluster count=2）。lanes に生 dict が無い assert。SVG は polyline のみ（`<rect` 不在）。テンプレ契約: `.band` が button・「展開」ボタン存在・既存契約維持 |

| 6-8 | trace HTML 対話強化（FBラウンド3）: 詳細パネルを**概要→リクエスト→レスポンス**の構造へ再構成 — stop_reason 日本語注記・このrequestが発行したツールのチップ列（request⇄tool 相互リンク）・プロンプト行・raw_path 末尾化。**thinking は presence のみ**（Claude Code は thinking 本文を transcript に残さず、非空はごく少数。本文4種契約と `must not be embedded` 番兵assertは維持） | `req_thinking` presence の fixture 期待値＋番兵assert復元。ヘッドレスChrome実描画で tool_use / end_turn / tool→request 逆リンクの3状態を確認 |
| 6-9 | request比較表とキーボード体系（FBラウンド4）: タイムライン下に比較表（# はts順固定番号・cost相対バー・context=read+write+input・合計行・全列ソート）。**エージェント別グループが既定**（集計行=合計値とcontext最大・クリック折りたたみ・ヘッダクリックはグループ順とグループ内行順へ同一キー適用）、「グループ」トグルでフラット全体ソート。キーは **←→=レーン内時系列・↑↓=レーン間（ts直近選択）**（単一レーン時は表の表示順）・⇧↓/⇧↑=request⇄tool の紐づき移動（レーン自動展開）・T=全レーン展開・Esc=解除。副次修正: main/#viewport の min-height 連鎖欠落と lanerow の inline-block ベースライン起因の縦ドリフト | キーイベント注入＋画面内アサーションで レーン内移動・レーン間ts直近・紐づき降下/復帰・2階層ソート・折りたたみ中選択の自動展開 を実描画検証。ラベル行とレーンの getBoundingClientRect 全レーン一致 |
| 6-10 | サブエージェント文脈（FBラウンド5）: agent request に**依頼の表示** — agent行（型＋id8）・依頼チップ（Agentツール入力の description＋prompt冒頭・クリックで親呼び出しへ）・Agentツール詳細に spawned agent 逆リンク・レーンsubへ依頼description併記（同型agent×Nの識別）。**task-notification プロンプトの構造化表示**（⚑ラベル・対象agent/status/note・`<result>`=最終報告のpre表示）。非同期agentの時間帯ベース帰属によるレーンスライス断片は **Q13 として docs/06 へ起票**（因果帰属への変更はADR級のため保留） | 複数agent並列の実データで依頼⇄Agentツール⇄レーンの往復動線と通知ラリーの構造化を実描画検証。tool_io 欠落・parse失敗時は dim フォールバック |

| 6-11 | **Q13決着: agentリクエストの因果プロンプト帰属**（[ADR 0009](adr/0009-causal-prompt-attribution.md)）— derive段の固定点ループで spawn連鎖（parent_tool_use_id→tool_call→親request）へ上書き・連鎖不能のみ観測値温存・tool_call.prompt_id 常時同期・取込規則8・PARSER_VERSION=5 | fixture 3ケース（プロンプト境界またぎ非同期agent・二段ネスト・連鎖不能）で期待値一致・増分==rebuildハッシュ一致維持。実台帳 rebuild 後に解決可能な時間帯/因果の残差0行・通知プロンプト誤帰属0行・traceレーン断片解消 |
| 6-12 | **通知擬似プロンプトの畳み込み**（[ADR 0010](adr/0010-notification-prompt-folding.md)）— task-notification に帰属した総額の約2割を `<tool-use-id>`直接参照→`<task-id>`agent連鎖の優先順で起点プロンプトへ再帰属。derive固定点ループへ統合（通知ラリー中spawnの次波agentも推移的に到達）・取込規則9・PARSER_VERSION=6 | fixture（agent型/MCP型/task-id不明温存/引用非畳込/次波agent推移・単一パスでは誤答になる配置）＋増分==rebuildハッシュ一致。実台帳で v_daily 過去日不変・通知残存が約8割縮小（連鎖情報なし旧形式のみ・解決可能なのに残存ゼロ）・頭アンカー判定 偽陰性ゼロ |
| 6-13 | **帯始端近似の精緻化**（FBラウンド6・ADR 0006 追記）— 無実測時の始端を「直前requestが `tool_use`（非synthetic）のときのみ直前終端・end_turn等の外部待ち後とレーン先頭は自身のts」へ変更。6-12の畳み込みでmainレーンに混ざった外部待ちギャップによる2桁規模の帯膨張を解消 | test_report の幾何golden更新＋契約3ケース（end_turn後=ts・tool_use後=直前終端・レーン先頭=ts）。実データで短いrequestの帯が本来の長さへ戻り、agentレーンのツール往復帯は不変・フッタ凡例が新規則を明示 |
| 6-14 | **セッション実時間ビュー**（ADR 0006 追記）— 同一HTML内に `__session__` 擬似グループ（全request・レーンはmain先頭→コスト降順）＋プロンプト境界ストリップ（request>0のプロンプトをts順・クリックで降下）。並列・compaction・アイドルの実測用（6-15のストーリーと相補） | session geometry golden（span数=全request・レーン順・strip ts順/x単調）・テンプレ契約（`__session__`・strip）・既存プロンプトビューgolden不変・番兵維持。大規模stress caseで生成1.3秒・複数MBのHTMLでも可読 |
| 6-15 | **セッション・ストーリービュー（プロンプト横連結）**（ADR 0006 追記）— 章=プロンプトをts順に横連結（既存geometry再利用・幅=px/秒比例・最小60px・アイドルは「⋯N分」固定幅マーカー）。章ヘッダ（ローカルHH:MM・$・冒頭）クリックで降下。ランディング=ストーリー（hashなし）・`#prompt=`=従来・実時間はサイドバー切替。根拠: アイドル時間で可読幅が圧迫される構造を避ける | storyレイアウトgolden（章順=ts・幅比例/最小幅・ギャップ秒・重なり0扱い）・テンプレ契約（ストーリー/実時間エントリ・ランディング分岐）・既存golden/番兵不変。多章の大規模セッションも描画可 |

**工数目安: 2〜3実働日（＋FBラウンド3〜5で1実働日）。撤退基準: 四半期棚卸しで spool の生成記録が0なら 6-4 を削除して
テキスト版に戻す（6-1/6-3 の台帳品質・リダクション修正は残す）。**

## Stage 7 — 精度・実験・運用性の是正（2026-07-20〜）

初期運用レビューで判明した「数字は出るが判断を誤らせうる箇所」と、長期運用の摩擦を是正する。
定額購読の請求・利用上限との統合は低優先度とし、まずAPI単価換算による利用量把握の正確さを上げる。

| # | タスク | 状態・受け入れ基準 |
|---|---|---|
| 7-1 | 公式単価SCD2、モデル/期間別Fast係数、server tool費、UTC価格日、期間重複検査 | 実装済み。`metsuke prices` とprice fixtureで確認。購読固有上限は未実装 |
| 7-2 | OTel公式属性・必須env・collector validation・収集境界でのPII削除 | 実装済み。公式dot属性と旧aliasのfixture、設定検証を追加 |
| 7-3 | unknown record隔離、request/hook/ingest別鮮度、同期失敗の永続表示、中央設定 | 実装済み。`metsuke doctor`/`metsuke config` とfailure fixtureで確認 |
| 7-4 | nudgeをfollowed/not_followed/unknownへ再設計し、沈黙を成功扱いしない | 実装済み。conversionと観測率を分離 |
| 7-5 | work_task/task_prompt/outcome/quality/rework とROI費用・削減レンジ | 実装済み。4週間の実運用データ蓄積は2026-07-20開始後に判定 |
| 7-6 | TTL施策の継続/縮小判断 | `metsuke ttl-review` 実装済み。28日幅かつ10稼働日未満は insufficient_data とし、先回りして結論を出さない |
| 7-7 | archive spool batching・byte cursor、offsite backup復元SHA検証、統合install/uninstall | 実装済み。uninstallはdry-run既定、purgeはTrash移動 |
| 7-8 | HTMLのキーボード/ARIA/レスポンシブ対応、CI、契約文書更新 | 実装済み。pytest/Ruff/shell構文/config parseをCIゲート化 |

### 4週間の実運用判定プロトコル

1. 作業開始時に `metsuke task start`、終了時に outcome / quality / rework を記録する。
2. TTL施策はmarkerを1本だけ開き、休暇・Claude Code更新・モデル/設定変更をregime_eventへ記録する。
3. 毎週 `metsuke nudges` でunknown率を確認する。conversionより先に観測率の低さを直す。
4. 28日後、task outcome付与率80%以上を確認して同category内の費用/品質/手戻りを比較する。
5. `metsuke ttl-review --days 28` が deprioritize ならTTL通知を縮小候補、continue_experimentなら継続候補とする。
   分類は因果保証ではないため、marker前後とregime_eventを併読して人間が最終決定する。

**未完了条件**: カレンダー28日と10稼働日は実時間を必要とするため、このStageの「実測による最終判断」だけは
2026-08-17以降かつ必要標本充足後に行う。コード実装完了を実験成功と混同しない。

## Stage 8 — 動的ローカルdashboard（2026-07-21〜・設計確定、実装前）

実運用で確認された「参照したいがコマンドと期間引数を覚えていない」という摩擦を解消する。
人間のpull型探索をdashboardへ移し、CLIはAI・自動化・fallbackとして維持する。決定は
[ADR 0011](adr/0011-local-dashboard.md)、画面・URL・HTTP・安全性・段階実装の正は
[08-dashboard](08-dashboard.md)。

| # | タスク | 状態・受け入れ基準 |
|---|---|---|
| 8-1 | V1〜V4のSQL/集計を純粋な共有view modelへ分離し、dashboard readerを追加 | **P0〜P2実装済み**。数万request規模の実台帳でV1〜V4のHTMLと`explain`がバイト単位一致。readerは`mode=ro`＋`query_only`＋allowlist authorizerで、`mode=rw`退行とdenylist退行がそれぞれテストを赤にすることを注入で確認済み |
| 8-2 | loopback限定SSR-MPA dashboard MVP | **P3実装済み**。127.0.0.1のみbind、bootstrap nonce＋署名cookie＋Host検査＋CSRF、overview/period/prompt/session detail、状態画面。退行注入で各防御の有効性を確認。query集約で複数倍高速化し中央値は予算内（p95の一部は要静穏環境再計測・BENCH.md） |
| 8-3 | trend/cache/dist統合＋trace遅延生成 | 未生成traceをクリックすると対象へfocus。template fingerprint、30日/256MiB LRUを実装し、response CSP＋opaque/別originの安全性gateに合格 |
| 8-4 | Metsuke.app・single instance・12時間認証・明示stop・doctor/install/uninstall | Spotlight/Dockからコマンドなしで起動。loopback/auth/Host/Origin/CSRF/CORSなし/query-only readerの攻撃fixtureに合格。statuslineの`file://`導線は維持 |
| 8-5 | 4週間の実運用評価 | 導入日を`regime_event`へ記録。dashboard表示＋CLI view生成、明示trace到達、失敗、生成時間、保守時間を確認し、使われない画面を削る |

**撤退基準**: 4週間でdashboardの利用が定着しない、または保守費が便益を上回る場合は、
Metsuke.app＋静的な昨日summaryへ縮退する。ローカルHTTP面の安全性を担保できなければ公開しない。

## クリティカルパスと全体像

```
Day 0        Stage 0 🩸（即日・無条件）
Week 1       Stage 1（正確さの固定）━━ 品質のボトルネック
Week 2 前半  Stage 2（常時視界＋センサー）
Week 2 後半  Stage 3（行動介入）
Week 3       Stage 4（AIアナリスト・閉ループ）
Week 4       Stage 5（完全性）
─────────────────────────────
実働 約18〜20日 / カレンダー約4週間でフル稼働。価値は Day 0 から発生。
（Stage 2/3、4/5 は一部並行可。日程はレビュー指摘を織り込んだ現実値）

2026-07-18〜 Stage 6（深掘りHTML）
2026-07-20〜 Stage 7（精度・実験・運用性）
2026-07-21〜 Stage 8（人間向け動的dashboard。設計確定・実装前）
```

## 運用の定常状態（完成後）

| 頻度 | 人間の作業 |
|---|---|
| 常時 | statusline をちら見（作業のついで・ゼロコスト） |
| 週次 | AIレポートを読む（5分）＋施策 approve（1コマンド） |
| 月次 | `metsuke invoice`（30秒・唯一の手動データ入力儀式）＋校正結果確認 |
| 四半期 | ナッジ棚卸し・`metsuke roi` 確認・撤退判断 |

## 旧システム（claude-code-monitoring）の扱い

新系とは独立なので**移行作業は不要**（並走コストはDockerコンテナのみ）。停止は
**Stage 5-1（OTelタップ稼働）後** — それ以前に止めると query_source/effort/リモートagent の
観測が完全欠測する空白期間が生じるため。statusline は現在未設定のため Stage 2-2 での
導入に衝突はない。データ移行はしない（旧Lokiの保持分より、Stage 0 が確保する
トランスクリプト原本の方が高忠実度）。
