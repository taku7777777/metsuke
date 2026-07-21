# ADR 0006: 深掘り層に自己完結HTMLのトレースビューを追加する（サーバレス・オンデマンド生成）

日付: 2026-07-18 / 状態: 採択（3視点敵対的レビュー反映済み）

> 2026-07-21: traceの自己完結形式・安全境界・生成契約は維持する。通常の入口と未生成traceの
> 遅延生成は[ADR 0011](0011-local-dashboard.md)のローカルdashboardが担う。本ADR内の
> 「ローカルWebサーバを持たない」は当時の提供方式の記録であり、dashboardへの全面禁止には使わない。
> HTTP配信はresponse CSP/XFOに加えopaque-origin sandboxまたはcookie非共有originを成立させた場合だけ
> 許可し、不成立なら従来の`file://`を維持する。

## 決定

`metsuke trace <session|last> --html` / `metsuke explain <prompt_id|last> --html` が、trace/span
ウォーターフォールの**自己完結HTML**を生成する。常設サーバは持たず、外部送信もしない。

- 生成物は**セッション単位で1ファイル** `~/.metsuke/traces/<session_id>.html`（0600・上書き）。
  `explain --html` は同一ファイルを生成し、該当プロンプトを URLフラグメント
  `#prompt=<prompt_id>` で初期選択して開くだけ（**テンプレ1枚・データビルダー1本** — 分裂禁止）。
- ビュー構成: 左=プロンプト・ラリー（帰属不能 request は「unattributed」帯として可視化し、
  ヘッダ合計は全 request 基準） / 中央=main＋agentレーンのウォーターフォール（帯=APIリクエスト・
  色=モデル・■=ツール・⚡恒等式破れ・hooksマーカー） / 右=クリックで請求明細と本文ドリルダウン /
  下=main の context 水位。
- **幾何計算は Python 側**（`trace_html.py` が帯座標を静的SVGとして焼き込み、pytest でgolden固定）。
  JS は操作系のみ（クリック→詳細パネル・ズーム・凡例。目安200行以下）。テンプレは無ビルド・
  単一ファイル・**差し込み点は JSON blob の1箇所のみ**。
- 本文（プロンプト・assistantテキスト・tool_use入力・tool_result）は **ledger に保存しない方針を
  維持**し、生成時に live transcript（無ければ `archiver.reconstruct`）から読む。
- 生成のたびに spool へ1行記録し、ingest が `hook_event`（kind='trace_html_generated'）へ取込む
  （spool 自体は取込後に消費される）。四半期棚卸し（撤退判断）は、手動CLIの同eventと
  Stage 8の`dashboard_trace_opened`を数える。statusline向け自動生成は除外する。

### 2026-07-20追記: statuslineからの高コスト詳細導線

`METSUKE_PROMPT_WARN_USD`（既定`$3`）以上の完了プロンプトを最近10分の活動セッションで検知した場合、
ingesterはstatuslineのOSC 8リンク先として本HTMLを自動生成する。同一セッション1ファイル上書き・0600・
ローカルのみ・バックアップ対象外は不変。自動生成はクリックの証拠ではないため
`trace_html_generated`には記録しない。利用実測は手動CLI生成に加え、Stage 8実装後はdashboardからの
明示クリックをcache hit/missによらず`dashboard_trace_opened`として数える。

### 2026-07-20追記: 実データによる入口UXレビュー

statuslineから開くV0について、単純な高額プロンプト、13 agentを含むプロンプト、
拡張コンテキスト帯の合成fixtureを通常幅と760px幅で再現した。入口で答える問いは
「いくら・何が支配項か・contextはどこまで大きいか・次にどこを確認するか」とする。

- 選択プロンプトの上部に、コスト、支配項、**main** context peak、request数、agent数と
  確認ポイントを常設する。コストとcontextは中央設定の黄/赤閾値を共有する。
- 右ペインは帯・ツールの詳細を担い、上部要約は選択中request/toolにかかわらず
  プロンプト単位の結論を保持する。
- 701〜1050pxでは、左一覧＋中央viewerの下へ詳細を配置する。ページ全体を横スクロールさせず、
  タイムラインと比較表だけを各領域内でスクロールさせる。
- 常時表示する凡例は実測/近似・tool・cache再作成に絞り、キーボード操作は「操作」ボタンで
  展開する。自己完結HTML、外部依存なし、CSP、textContent描画という境界は変更しない。

あわせて時間軸を厳密化する（`metsuke rebuild` で全履歴に遡及 — ADR 0001/0002 の配当）。

## 時間軸のセマンティクス（規範）

| 値 | 由来 | 収束規則（増分＝rebuild を不変条件とする） |
|---|---|---|
| `request.ts` | 同一requestId**最初**のレコードts（ストリーム開始の近似） | conflict時保持（既存どおり） |
| `request.end_ts` | 同一requestId**最終**のレコードts（ストリーム終端の近似） | INSERT時 `end_ts=ts`。conflict時は **同一 raw_path のレコードに限り** NULL安全なMAX（resume/compact でのファイル横断複製から防御）。transcript→otel昇格時は他列と同じく excluded 優先。`_derive_otel` は触らない |
| `request.api_duration_ms` | OTel api_request の `duration_ms`（**API呼び出し全体の実測** — 実データで検証済み: otel.ts≒完了時刻） | `_derive_otel` で **COALESCE**（transcript昇格パスでも保持する専用CASE — 既存の「otel行はexcluded勝ち」パターンに引きずられないこと） |
| source='otel' 行の `ts` | OTelイベント時刻＝**完了時刻**（開始ではない） | INSERT時に `end_ts=ts` も設定。帯は `end_ts − api_duration_ms` から描画 |
| `tool_call.ts` | tool_use ブロックのレコードts（ツール発行時刻） | 初出優先（COALESCE） |
| `tool_call.result_ts` | tool_result を含む user レコードts（完了時刻。AskUserQuestion・許可待ち等は**人間待ちを含む**） | **初出優先** — result側は upsert（スタブ行許容）とし、`result_ts IS NULL` の間だけ is_error/result_bytes/result_ts を一括設定（到着順に依存しない） |

**帯の始端の優先順位**（HTML凡例に明示）: (1) `api_duration_ms` があれば `start = end_ts −
api_duration_ms/1000`（実測） → (2) 無ければ**直前requestの `stop_reason` が `tool_use`
（非synthetic）のときのみ**「同レーン直前の **request の end_ts**」の近似（ギャップ＝ツール
実行時間でこのrequestサイクルの一部。tool_result は基準にしない — background実行の遅延resultで
負幅になるため） → (3) それ以外（直前が end_turn＝外部待ちアイドル・NULL・synthetic・
レーン先頭）は**自身の `ts`**（2026-07-19 追記: ADR 0010 の通知畳み込みで main レーンに
外部待ちギャップが混ざるようになり、短いrequestに長い待ち時間が加算されるend_turn跨ぎの帯膨張を
解消するため精緻化）。常に `start = min(start, end)` でクランプし、近似帯は実測帯と
視覚的に区別する。`result_ts` NULL の
ツールは開放端で描画。分岐（リトライ/rewind）は on_main_path 未実装のため重なって表示される旨を
凡例に明記する。

workflow系サブエージェントの親リンク: 親 user レコードの `toolUseResult.runId`（実データ検証済み）
→ `tool_call.workflow_run_id`、meta.json パスの `wf_<runId>` → `agent.workflow_run_id`、
冪等derive（`ORDER BY ts, tool_use_id LIMIT 1` で決定的）で `agent.parent_tool_use_id` を充填。
**deriveは prompt 継承UPDATEより前に実行**（rebuild直後と定常状態の一致 — 決定性テストで固定）。

新列は schema.sql でも**各テーブル末尾**に、ledger.py の try-ALTER と**同一順**で追加する
（SELECT * の列順が新規DBと移行DBで一致すること — PRAGMA table_info で機械検証）。
充填率（直近7日の end_ts / result_ts）は v_health に載せ、transcript形式変化の沈黙劣化を検知する。

## セキュリティ / プライバシー（規範）

生成HTMLは**PII資産**。transcript/otel由来の**全文字列**（本文4種に加えツール名・モデル名・
agentType・パス・エラー文言等を含む）を untrusted data として扱う（ADR 0005 と同格）。

1. **読み出し境界**: JSONパース後の**各テキスト値ごと**に `redact()`（全文適用）→ 64KB超のみ
   切詰め（先頭48KB＋末尾8KB）、を read-boundary の一関数で不可分に行う。**切詰め→redact の
   逆順は禁止**。HTML に載る文字列は由来を問わず全て生成時に redact を通す
   （ledger 側が旧 REDACTION_VERSION でも新パターンが効く）。
2. **埋め込み**: サーバ側のテンプレ差し込みは**エスケープ済み JSON blob ただ1箇所**。
   `<script type="application/json">` に `json.dumps(...)` 後、**`<` を `\\u003c` へ全置換**
   （`</script>` 脱出と `<!--` による script-data double-escape の両方を封鎖 — JSON で `<` は
   文字列内にしか現れないため常に正当）＋ U+2028/2029 をエスケープして埋め、
   JS は `JSON.parse(textContent)` で読む。
3. **描画**: DOM挿入は textContent / setAttribute のみ（innerHTML・insertAdjacentHTML 禁止）。
4. **CSP**: テンプレ先頭（最初の script より前）に `<meta http-equiv="Content-Security-Policy"
   content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';
   img-src data:; form-action 'none'; base-uri 'none'">` — 仮に上記が破れても外部送信（exfil）を
   構造的に遮断する多層防御。リンク/ナビゲーション遷移による exfil は CSP では遮断できないため、
   テンプレは外部リンクを一切生成しない。
4b. **パス安全**: 出力ファイル名に使う session_id は untrusted のため
   `[A-Za-z0-9._-]{1,128}`（先頭 `.` を拒否）で検証し、不合格は明示エラー
   （traces/ 外への書き込みを構造的に排除）。
5. **ファイル**: `traces/` は mkdir(0700)、ファイルは `os.open(..., 0o600)`（または一時ファイル
   0600→`os.replace`）で作成。restic 対象は archive/ のみなので除外設定は**不要**（追加作業を
   誘発しないよう RUNBOOK に明記）。生成時に同一セッションを上書きし、フッタ刻印の
   redaction_version が現行未満の既存ファイルを purge する。
6. **既存バグの是正**（本ADRのスコープ）: quarantine の生本文保存を redact→切詰めに修正・
   prompt.text の「切詰め→redact」逆順を修正（いずれも rebuild で全履歴遡及）。
7. **リダクション拡充**: REDACTION_VERSION=2 — `sk-(proj|svcacct|admin|or-v1)-` 系・
   AWS Secret Access Key（文脈付き）・`xox[abcdeprs]-`・`AIza`・`glpat-`・`npm_`・
   `[sr]k_(live|test)_`・`hf_`・private_key_block 上限 15000 字。誤検出は archive 原本無傷・
   再parseで回復可能なため recall 側に振る。
8. **残余リスクの明示**: 「外部共有・claude.ai Artifact へのアップロード禁止」は**運用規律であり
   構造的強制ではない**（対話セッションの Claude は traces/ を読める）。週次アナリストの実行面には
   traces/ を露出しない（起動オプションに traces が現れないことを回帰テストで固定）。
   `--open` は file:// パスがブラウザ履歴（同期含む）に残ることを凡例に明記。

## 根拠

- ADR 0003 が予約した条件「深掘り需要が CLI/Datasette で満たせない事態」の発動。ただし予約時の
  スコープ（ledger 直読み）を**本文読み出しまで拡張**しており、その代償（traces/ という新たな
  PII 生成物の管理）を上記5・生成記録による利用計測・撤退基準で引き受ける。
  当時維持した常設dashboard禁止のうち、軽量loopback UIまで一律に禁じる部分はADR 0011が後に置換した。
- 実データモックの実証: 多数agentを含むfan-outプロンプトで、cache writeが支配項であること、
  委任費が本体費を大きく上回る構造、長い単独レーン内のモデル切替・許可待ちが1画面で判読できた。
- 保守予算: テンプレ1枚（差し込み点1箇所・無ビルド）＋幾何はPython側でテスト固定＋JSは操作系のみ。

## 条件と検証

- 近似が残る箇所（タップ導入前の帯始端・archiveフォールバック時の末尾欠け・分岐の重なり）は
  HTML凡例に明示。全文が必要な場合のために raw_path を常に併記。
- フッタに redaction_version（生成時適用）・parser_version・生成時刻を刻印し、ledger 側の
  リダクション版が古い場合は rebuild を促す注記を出す。
- 四半期棚卸し: 手動CLIの`trace_html_generated`とdashboardの`dashboard_trace_opened`で利用実態を確認し、
  statusline向け自動生成を除外する。使われなければ6-4を削除してテキスト版に戻す
  （時間軸厳密化6-1は台帳品質として残す）。

## 棄却した代替案

- **Textual TUI強化**（Stage 2-7 の原案）: 並列18レーン・数百KBの本文ドリルダウンで端末表現の
  限界。テキスト版 explain/trace は AI の口・軽い確認用として現状維持し、TUI化はしない
  （03-interfaces の TUI 記述は本ADRで置換）。
- **運用の重い常設Web基盤 / 既製UI（Langfuse等）**: ADR 0003 の棄却理由のまま変更なし。
  軽量loopback dashboardは、実運用の摩擦を根拠にADR 0011で後から採用した。
- **本文の ledger 保存**: 事実はarchive・台帳は数値の分離（ADR 0001/0004）に反する。
- **JS側での幾何計算**（モックの実装）: 700行級JSの保守と描画正しさの無検証化を招くため、
  Python側SVG焼き込み＋契約テストに置換。

## 追記（2026-07-18: 時間軸独立ズームと描画基板の再配分 — 6-6）

横軸（時間軸）のみのズーム要望に対応するにあたり、SVG にテキストを焼き込んだまま
非等方スケールすると文字が歪むため、描画の役割分担を再配分した。
**「幾何計算は Python 側・JS は操作系のみ」の規範は不変**:

- **SVG＝伸縮する形状のみ**: 帯 rect・cost内訳ストリップ・目盛縦線・レーン区切り線・
  context 折れ線。`preserveAspectRatio="none"` で X/Y 独立伸縮、罫線・折れ線は
  `vector-effect="non-scaling-stroke"`（ズームで線が太らない）。viewBox はプロット領域のみ
  （ラベル列は SVG から除外）。
- **テキスト・点マーカー＝DOMオーバーレイ**: 目盛ラベル・レーン名・ツール■・⚡・hooks・
  context ラベルは geometry の座標データ（ticks/lane_labels/tools/sparks/hook_marks/
  context_label — Python 側で算出・golden 固定）から textContent/setAttribute で描画。
  フォントはスケールせず常に読める。JS が行う計算は `left=(x−plot_x)·zx / top=y·zy` の
  線形変換のみ。**ピクセルオフセット（目盛+3px 等）は CSS transform で当て、座標×zx に
  混ぜない**（高ズーム時のドリフト防止 — テストで固定）。
- **操作系**: zx（時間軸）/ zy（縦）の独立ズーム（`横−`/`横＋` ボタン・`⌥+ホイール` は
  カーソル位置アンカー）。`⌘B`（左のプロンプト一覧）/ `⌘⌥B`（右の詳細）のパネルトグル
  （`e.code==="KeyB"` 判定 — ⌥+B は e.key が変わるため）。レーンラベル列は
  `position:sticky` で横スクロール中も表示。
- セキュリティ規範（§1〜8）は全て不変。DOM オーバーレイの新文字列（agent_type・⚡cause 等）
  も従来どおり JSON blob 経由＋textContent 描画。

## 追記（2026-07-18: レーン別ツール展開と描画基板のDOM移行 — 6-7）

同時刻に重なるツール■の個数が判読できない問題への対応（デフォルトは俯瞰のまま、
レーン別トグルで縦展開）。静的SVGはレーン展開時のリフローが構造的に不可能なため、
**描画基板をDOMへ移行**した。6-6 で SVG の中身は「色付き矩形と直線」まで痩せており、
帯は hotspot として同座標の DOM 要素を二重描画していた — これを一本化する。

- **SVG に残るのは context 折れ線のみ**（高さ48のストリップ・`preserveAspectRatio="none"`・
  non-scaling-stroke）。リクエスト帯・費用内訳ストリップ・ツール・目盛縦線は
  geometry データから DOM 描画（帯＝`<button class="band">`、破線=近似は CSS border、
  費用内訳は `cost_pcts` の%幅の入れ子div）。
- **geometry はレーン相対座標へ**: lanes はリーンな `{label, sub, base_h, expanded_h, rows}`
  （従来の生 dict 同梱をやめ JSON 肥大も解消）。レーンは文書フローで積むため、
  展開すると下のレーンが自然に押し下がる。
- **展開レイアウトは greedy packing**（Python側・決定的・golden固定）: ツールを px 区間
  `[x, x+max(11, bar_w)]` として重ならない最初の行へ詰める。行数=最大同時実行数。
  展開時のツールは `ts→result_ts` の帯（幅があればツール名表示・open=破線・error=赤枠）で、
  実行時間（人間待ち込み）が読める。
- **折りたたみ時はクラスタ描画**: x 差 11px 未満の連続ツールを `×N` バッジ付き■に集約
  （Python側で計算）。count>1 のクラスタをクリックするとそのレーンが展開される。
- 展開状態はプロンプト単位で保持。ツールバーに全展開/全折りたたみボタン。
- JS の役割は不変: レーンをフローで積む＋ `(x−plot_x)·zx / y·zy` の線形変換のみ。
  packing・クラスタ・レーン高さは Python 側で計算し golden で固定する。

## 追記（2026-07-19: セッション全体ビュー — 6-14）

ドリルダウンの梯子（V1期間 → セッション → プロンプト → raw）の欠けていた一段。
同一HTML内に `prompt_svgs["__session__"]` 擬似グループを追加し、プロンプト単位ビューは不変。

- **geometry**: 既存 `_span_geometry` に全 request・全 tool（prompt_id NULL 含む）を渡すだけ。
  レーン順序はセッションビューのみ main 先頭 → agent コスト降順（同額は agent_id 順）。
- **prompt_strip**: request を1件以上持つプロンプトの `{prompt_id, x, width, cost_usd,
  n_req, label(redact済み冒頭60字)}` を ts 順で Python 側焼き込み。タイムライン上部に
  DOMボタンとして交互色調で描画し、クリックで該当プロンプトビューへ降下（ズーム追随は
  帯と同じ線形変換）。
- **ランディング規則**: `#prompt=` あり → そのプロンプト（`metsuke explain --html` 用）。
  hash なし → `__session__`（`metsuke trace --html` 用）。`__session__` 不在の旧ファイルは
  従来どおり先頭プロンプト。
- セッション全体のコンテキスト水位線により compaction の谷が初めて可視化される。
- 規模実測: 大規模stress caseで生成1.3秒・複数MBのHTML・
  クラスタバッジとX独立ズームで可読。レーン数上限は設けない（全レーン縦スクロール）。

## 追記（2026-07-19: セッション・ストーリービュー（プロンプト横連結）— 6-15）

「セッションを1本のtraceとして連続して読む」ためのモード。6-14 の実時間全体ビューは
実時間レイアウトでは長いプロンプト間アイドルが幅を支配し「読む」用途に不適となるため、
ストーリービューではアイドルを畳み、隣接プロンプトを横連結する。

- **章=プロンプト**（request>0）を ts 順に横連結。各章は既存プロンプト単位 geometry を
  再利用し、幅はセッション共通 px/秒 で実働時間に比例（最小60px）。章間アイドルは
  固定幅48pxの「⋯ N分」ギャップマーカーに畳む（実時間で重なる章間は 0 扱い）。
- 章ヘッダ（ローカル時刻 HH:MM・$・redact済み冒頭）クリックで従来のプロンプト単位
  ビューへ降下。main レーンが各章の最上段に来るためセッションを通じた main の物語が
  視覚的に連続する。
- **章=指示の因果全量**（ADR 0009/0010 と同じ哲学）。実時間の交錯・並列・compaction の
  実測は 6-14 の実時間ビューで見る（相補・両方残す）。凡例に明記。
- **ランディング**: hash なし → ストーリー。`#prompt=` → プロンプト（explain 用）。
  実時間はサイドバー切替のみ。story 不在の旧ファイルは実時間→先頭プロンプトの順で
  フォールバック。
