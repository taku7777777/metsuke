# ADR 0011: 人間向けの主入口を動的ローカルWeb UIへ移す

日付: 2026-07-21 / 状態: 採択（ユーザー合意済み・実装前）

## 文脈

ADR 0003 は、行動変容から遠い監視画面と単一保守者に重い運用基盤を避けるため、
常設ダッシュボードを採用しなかった。その後、ADR 0006/0007 によりtraceと4つの判断支援ビューを
自己完結HTMLとして実装した。分析内容は提供できるようになったが、実運用で次の摩擦が確認された。

- 昨日1日のサマリを見るだけでも、`metsuke view period --from ... --to ... --open`を知る必要がある。
- 期間、project、ビュー種別を変えるたびにコマンドと引数を組み直す必要がある。
- ビュー内の次の行動がコマンド文字列であり、未生成の詳細へクリックだけでは降りられない。
- `~/.metsuke/views/<name>.html`は上書きされるため、URLに期間や絞り込み状態が残らず、
  ブラウザの戻る・進む・ブックマークが探索履歴として機能しない。
- 操作方法をAIへ尋ねると、コスト削減のための道具が追加のAI利用を要求する逆転が起きる。

計測時点の実台帳は数万request規模である。**コスト発生prompt**は
`request.prompt_id`のdistinct件数であり、requestなしの制御/UI prompt等を含む`prompt`表とは
約2割食い違ったため、別のKPIとする。
全requestを自己完結HTMLへ埋め込んでブラウザ側だけで任意期間・多軸集計を行う方式は、
長期ではファイル肥大、PII複製、鮮度固定、SQL/JavaScript間の集計定義重複を招く。

これは「分析画面が必要か」という仮説上の議論ではなく、CLIとオンデマンドHTMLだけでは
参照の発見性を満たせないという実運用上の観測である。

## 決定

人間がpull型で調べる際の主入口を、**動的なローカルWeb UI（以下、dashboard）**へ移す。
dashboardはHTML/CSS/JavaScriptで表示するが、全データを埋め込んだ自己完結ファイルではなく、
loopbackだけで待ち受ける小さなPythonプロセスがSQLiteをquery-onlyで問い合わせ、必要な範囲だけを返す。
人間UIはserver-side renderingのMPAを基本とし、JavaScriptはtrace生成進捗等の漸進強化に限定する。

```text
Metsuke.app / statusline link
          ↓
127.0.0.1 のローカルUI
   ├─ read-only SQLite query → overview / period / trend / cache / dist
   ├─ prompt / session detail
   └─ trace job → 既存trace HTMLを生成・キャッシュして表示
```

### 1. 役割分担

| 面 | 主な利用者 | 役割 |
|---|---|---|
| dashboard | 人間 | 日付・project・観点を変えながら探索し、クリックで詳細へ降りる主入口 |
| 自己完結trace HTML | 人間 | transcript本文を含む重いセッション詳細。必要時だけ生成・キャッシュ |
| `metsuke` CLI / `--json` | AI・自動化・障害調査 | 再現可能な非GUI契約。削除せずdashboardと同じquery modelを使う |
| statusline / hooks | 人間 | ambient / just-in-timeの気づき。dashboardを置き換えず入口として接続 |

dashboardの参照・絞り込み・画面遷移はAI/APIを一切呼ばない。費用はローカルCPU/メモリだけである。

### 2. 動的queryを正とする

- 期間はローカル日付の包含区間`from <= day <= to`で表す。
- `今日 / 昨日 / 直近7日 / 今月 / 先月 / 任意期間`をGUIから選べる。
- project、ビュー種別、並び順、選択中prompt/sessionをURLへ保持する。
- 集計はリクエストごとにSQLiteの既存ビューをread-onlyで問い合わせる。
- 一覧はpagination/上限を持ち、全requestをブラウザへ送らない。
- Python/SQLのquery modelをCLI、静的export、dashboardで共有し、金額定義を複製しない。

### 3. 画面遷移をリソースとして扱う

URLは表示状態を再現できる契約とする。

```text
/dashboard?view=period&from=2026-07-20&to=2026-07-20
/dashboard?view=cache&range=7d&project=example
/prompts/<prompt_id>
/sessions/<session_id>
/traces/<session_id>?prompt=<prompt_id>&focus=<request_id>
```

- filterはGET form、tab/detailは実在する`a`リンクとし、URL・戻る・進む・reloadはブラウザの
  ネイティブ動作へ委ねる。SPAとJS無効fallbackの二重実装をしない。
- 表の行は実在する`a`要素とし、通常クリック、別タブ、キーボード操作を維持する。
- prompt詳細はledgerだけで即時表示し、重いtrace生成とは分離する。
- traceが未生成または古い場合、UIは生成中を表示し、完了後に対象アンカー付きで遷移する。
- ファイルの有無、生成コマンド、session/prompt IDを利用者に要求しない。
- `range=7d`等は常に相対期間を表す生きた入口として維持し、表示時に解決した`from/to`を
  canonical URLとして履歴へ残す。

### 4. traceは静的HTMLのまま残す

ADR 0006の自己完結trace、read-boundary redaction、CSP、0600/0700、原子的置換を維持する。
dashboardはtraceを全面再実装せず、生成と表示を仲介する。

- cache keyはsession IDに加え、session最終request時刻、parser/redaction/template versionを含む。
- template version定数、fingerprint、cache manifestは現行に無いためStage 8で新設する。
- cache miss/stale時だけ生成し、同一sessionの並行生成を排他する。
- 生成はin-memory jobとして開始し、UIへ`ready / running / failed`を返す。
- 成功時はローカルHTTP経由でHTMLを配信し、`#prompt=`等の既存アンカーを使う。
- trace生成物は引き続き導出物であり、archive/ledgerの正典にはしない。
- HTTP配信時はmeta CSPだけに依存せず、応答ヘッダへ`connect-src 'none'`を含むtrace専用CSP、
  `frame-ancestors 'none'`、`X-Frame-Options: DENY`を必ず付ける。traceからdashboardの認証済み
  endpointへ到達できないことを攻撃fixtureで固定する。headerだけではpopup/navigationを含む同一origin XSSを
  完全隔離できないため、`sandbox allow-scripts`によるopaque origin化または認証cookieを共有しない
  別originの成立をPhase 2の出荷gateとする。成立しなければHTTP配信せず既存`file://`を維持する。
- **出荷gate判定（2026-07-21・成立）**: `sandbox allow-scripts`によるopaque origin化を採用し、
  `X-Frame-Options: DENY`と併せて`/traces/<session_id>.html`のHTTP配信を出荷した。判定根拠は
  (a) 現行`trace_template.html`が`localStorage`/cookie/`postMessage`/`pushState`/`fetch`/form等の
  opaque originで制限されるAPIを一切使わず、`location.hash`を起動時に1度読むだけであること、
  (b) 実ブラウザ（Chrome 150）で縮小・拡大・レーン展開・表・グループ・帯クリックの主要操作が
  opaque origin下で動作することを利用者が確認したこと。trace専用CSPが他レスポンスの既定CSPへ
  波及しないことは10レスポンスの実測で固定した。
- **サーバからの`open`起動は廃止した。** 生成後にサーバプロセスが`open`を実行する方式は、
  どのブラウザ・どのタブに表示されるかをサーバ側から制御できず、実際に既定ブラウザや
  cmuxワークスペースへ飛ぶ事故が出た。dashboard経路はopenerを無効化し、同一タブ遷移に統一する。
  CLI（`metsuke trace`）は従来どおり`file://`を`open`する。
- cacheは0600のmanifestでfingerprint・size・last_accessed_atを管理し、既定30日かつ256MiBの
  LRU上限を持つ。redaction version不一致は即時削除し、件数・使用量・最古accessをdoctorへ出す。
- statuslineの高額promptリンクはserver停止時も動く既存`file://` traceを維持する。

### 5. プロセスは必要な間だけ起動する

初期実装ではOS起動時に`KeepAlive`するdaemonにせず、installerが作る`Metsuke.app`または内部CLIが起動する。
起動後はlogout/rebootまたは明示的なstopまで待受し、v1ではheartbeat依存のidle shutdownを行わない。
標準ライブラリ`ThreadingHTTPServer`を第一選択とし、固定の`127.0.0.1:48127`を既定にする。
48127はprivileged領域、常用開発port、49152以降の動的port帯を避けるための初期値である。
portは`METSUKE_DASHBOARD_PORT`で設定変更できるがブックマークが壊れることを明示し、
競合時は別processへ誤接続せず失敗する。

起動時は通常の`metsuke sync`を先に試す。sync失敗でもdashboardは開き、最終正常取込時刻とstale警告を
表示する。HTTPのGET処理からingesterを暗黙実行せず、DB single-writer原則を維持する。

dashboard readerはURI `mode=ro`で既存ファイルだけを開く。当初は`SQLITE_CANTOPEN`を避けるため
`mode=rw`＋`PRAGMA query_only=ON`を採る案だったが、2026-07-21のspike実測により棄却した。
`query_only`はSQL層の書込みしか止めず、**最終接続のclose時に走るauto-checkpointを止めない**。
実測では接続をcloseしただけでmain DBが書き換わりsidecarが削除され、
ADR 0004の「dashboardはDB writerにならない」に反した。根拠だった`CANTOPEN`は
Python同梱SQLiteでは再現せず、観測元はApple同梱CLIという別runtimeだった。
再現しない失敗を避けるためにmain DBを書き得る方式を採る取引は成立しない。

`immutable=1`はWAL内のコミット済み行を落として古い値を返すため使わない。
`query_only`とSQLite authorizerは多層防御として重ねる。authorizerは**allowlist方式**とし、
PRAGMA設定がすべて済んだ後に設置する。denylistは`PRAGMA query_only=OFF`を取りこぼしてguard自体を
無効化でき、実測で穴が開いた。busy timeoutはUI専用に250msとし、超過時は503 `ledger_busy`を返す。
WAL readerに不可避なsidecar生成は許容し、不変条件はmain DBのbyte列とschemaが変わらないこととする。
`mode=ro`が開けない環境が見つかった場合は暗黙にfallbackせず、明示エラーとdoctor導線を出す。

### 6. セキュリティ境界

- bind先とURLはIPv4リテラル`127.0.0.1`のみ。`localhost`、`0.0.0.0`、LAN、外部公開を許可しない。
- per-install secretと一回性・60秒のbootstrap nonceによる認証後、12時間有効の
  `HttpOnly; SameSite=Strict; Path=/` cookieを発行する。cookieは署名と期限を検証しserver再起動を跨げる。
- bootstrap nonceをcookieへ交換したら、queryを除いた`/dashboard`へ303 redirectし、URL/history/logへ残さない。
- nonceはserver instance IDも署名対象にし、使用済みdigestを期限までmemoryで保持する。server再起動で
  instance IDを更新し、再起動前のnonceを受理しない。
- cookieなし/期限切れは401画面で`Metsuke.appから開き直してください`と案内する。
- Host/Originをallowlist検査し、DNS rebindingと他originからの呼出しを拒否する。
- DB接続は`mode=ro`＋`query_only`＋allowlist authorizer。HTTP処理からDBへ書けないことを
  write/DDL/ATTACH/`query_only=OFF`試行テストと、接続開閉前後のmain DB hash不変テストで固定する。
- APIはledgerの数値とredact済みprompt冒頭だけを返す。生transcriptを読めるのはtrace生成境界だけ。
- API/HTMLは`Cache-Control: no-store`、`Referrer-Policy: no-referrer`とし、外部接続を許さないCSPを使う。
- 外部CDN、analytics、Web font、AI/API呼出しを禁止する。
- CORSヘッダを返さず、`/healthz`は無認証で`ok`だけを返す。
- v1では設定変更、marker、approve等の状態変更UIを持たない。trace cache生成だけを許す。
- session/prompt/request IDは既存の文字種・長さ制約で検証し、パスへ直接連結しない。

## 既存ADRとの関係

- **ADR 0003を部分置換する。** Grafana等の外部/汎用監視基盤、リアルタイム監視画面、
  運用の重いサーバ群を採用しない判断は維持する。一方、「ローカルWebプロセスを一律禁止」する制約は
  本ADRで撤回する。判断基準はプロセスの有無ではなく、利用摩擦、保守費、外部公開、AI費用である。
- **ADR 0007を提供方法について部分置換する。** V1〜V4の問い、指標所有、query定義、撤退基準は維持する。
  自己完結HTMLはexport/fallbackへ下げ、dashboardを通常の提供面にする。コマンド併記だけの降下動線は廃止する。
- **ADR 0006は維持する。** traceの内容・安全境界・生成形式は変更せず、dashboardから遅延生成する入口を足す。
- **ADR 0004は維持する。** dashboardはread-only readerであり、DB writerにはならない。

## 根拠

- 問題は可視化不足ではなく、発見性・状態保持・遷移・遅延生成の欠落である。
- SQLiteと既存queryがあるため、汎用BIや新しい保存基盤を持つ必要はない。
- server-side queryなら、期間を変えるたびに必要な集計だけを行い、履歴全量をPII入りHTMLへ複製しない。
- URLを状態契約にすることで、GUIの探索とCLI/AIの再現性を両立できる。
- 静的traceを残すことで、最も複雑で検証済みの画面を作り直さずに済む。
- SSR-MPAにより、URL状態、戻る/進む、reload、別タブ、JS無効時動作をブラウザへ委ね、
  SPAとfallbackの二重実装を避けられる。

## 受け入れる不利益

- ローカルHTTPプロセス、認証、port/lifecycle、ブラウザ互換性という新しい故障面が増える。
- UI用query modelへのリファクタリングが必要で、実装初期の変更量は小さくない。
- dashboardが停止するとGUIは使えないため、CLIと静的exportをfallbackとして維持する必要がある。
- traceの遅延生成中は待ち時間が生じる。loading/error/retryを製品機能として扱う必要がある。

## 棄却した代替案

| 案 | 棄却理由 |
|---|---|
| 全履歴を単一HTMLへ埋め込む | 長期肥大、PII複製、鮮度固定、query定義のJS重複 |
| 日別JSONを大量生成してfile://から読む | ブラウザ制約、整合性管理、未生成trace問題が残り、実質的に独自DBを増やす |
| SQLite WASMをブラウザへ配る | 実運用規模のDB複製、更新・権限・CSP・メモリ負荷に対して利点が小さい |
| ネイティブSwiftアプリ | 配布・署名・Web版との二重UIが単一保守者には重い |
| Grafana/Langfuse等へ戻る | データモデル不一致、外部依存、運用面積が目的に対して過剰 |
| CLIをラップしたコマンドパレットだけ作る | 引数暗記は減るが、多角的比較、状態保持、戻る/進む、クリック降下を満たさない |

## 検証と撤退

- dashboardとCLIの同一条件の集計値をfixture/実台帳で一致させる。
- GUI起動、view表示、prompt詳細、意図的なtrace表示を本文・ID・filter値なしのローカルspoolへ記録する。
- 利用分母は`dashboard_view_opened + view_html_generated`、trace利用は
  `dashboard_trace_opened + 手動CLIのtrace_html_generated`とし、
  statusline向け自動trace生成は従来どおり除外する。
- 4週間後、dashboard利用日数、CLI view直接実行数、trace到達率、エラー率、保守時間を確認する。
- dashboardが使われない、または四半期ROIを悪化させる場合は、ランチャー＋静的昨日summaryまで縮退する。
- ローカルHTTP面の安全性を維持できない場合は公開せず、実装を停止して静的HTMLへ戻す。

詳細設計と段階的な受け入れ基準は[08-dashboard.md](../08-dashboard.md)を正とする。
