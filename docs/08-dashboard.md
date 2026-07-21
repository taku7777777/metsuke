# 08 — ローカルdashboard設計

状態: 実装済み・運用検証中 / 決定の正: [ADR 0011](adr/0011-local-dashboard.md)

## 1. 目的

利用者がコマンド名・ID・期間引数を覚えたりAIへ質問したりせず、次の探索を完結できるようにする。

1. 昨日、今日、任意期間の全体像を開く。
2. 期間、project、費目、ビューを変えて同じデータを多角的に見る。
3. 高額prompt/sessionを特定し、その場で内訳へ降りる。
4. traceが未生成でも、クリック後に生成を待って対象箇所を表示する。
5. 戻る・進む・再読み込み・別タブで探索状態を失わない。

dashboard利用によるLLM/API呼出しは0件を不変条件とする。

## 2. 非目標

- 秒単位のリアルタイム監視。最大5分のingest遅延を許容し、鮮度を明示する。
- 外部公開、LAN共有、マルチユーザー、モバイル提供。
- Grafana等の汎用ダッシュボード、プラグイン基盤、自由SQLビルダー。
- dashboardからの設定変更、施策承認、marker/outcome入力。v1はread-only探索に限定する。
- transcript全文を検索・一覧化すること。全文を読む権限は既存trace境界に留める。
- CLIと静的HTMLの廃止。障害時と機械利用のfallbackとして維持する。

## 3. 情報設計

### 3.1 グローバルナビゲーション

| タブ | 最初に答える問い | 既存資産 |
|---|---|---|
| 概要 | 選択期間に何へいくら使い、最初に見るべき外れ値は何か | V1の要約＋V3のTTL再作成要点 |
| 期間 | prompt/session/project別の集中先はどこか | V1 period |
| 推移 | 費用構成と行動指標はどう変化したか | V2 trend |
| キャッシュ | cache writeは回収され、破れは何が原因か | V3 cache |
| 分布 | 高コスト・巨大contextは分布上どこにいるか | V4 dist |

タブはデータの別コピーではなく、同じ期間filterを受け取る異なる投影とする。

### 3.2 グローバルfilter

全タブの同じ位置に次を置く。

- preset: `昨日`（初回既定）、`今日`、`直近7日`、`今月`、`先月`
- `開始日` / `終了日`（ローカル日付・両端包含）
- project（複数ではなくv1は単一選択＋全体）
- filter解除
- 最終正常取込時刻、表示生成時刻、stale状態

preset選択後に日付を手動変更した場合は`カスタム`と表示する。未来日、開始>終了、不正形式は
送信前とserver側の両方で拒否する。

### 3.3 概要画面

上から順に次を表示する。

1. KPI: API換算コスト、コスト発生prompt、request、session、project数と直前同日数期間比。
2. 費目構成: input / output / cache read / cache write 5m / cache write 1h / server tool。
3. 高額prompt: コスト色、時刻、project、冒頭、request数、context peak、支配費目。
4. 高額session: コスト、期間、prompt/request数、平均peak context、project。
5. cache再作成: ttl_expiry等の原因別金額・件数と最大事例。
6. 次の確認: 上記の外れ値に対応するprompt/sessionへのリンク。

予算は設定が有効な場合だけ補助KPIとして表示し、画面の主目的にはしない。
前期比較は選択期間の直前にある同じ日数の期間を使い、前期0の場合は割合を捏造せず`比較不能`とする。
prompt数は`v_request_cost`に現れるnon-synthetic requestのdistinct `prompt_id`であり、
requestなしの制御/UI promptを含む`prompt`表の行数ではない。

### 3.4 詳細の段階

```text
概要/各ビュー
   ├─ prompt行 → prompt detail（即時、ledgerのみ）
   │                 └─ traceで見る → 遅延生成 → 対象prompt
   ├─ session行 → session detail（prompt一覧、費用構成）
   │                 └─ traceで見る → 遅延生成 → story
   └─ cache破れ → request focus → 遅延生成 → 対象⚡
```

prompt行の単一クリックで直ちに重いtraceを生成しない。まず数値内訳を高速表示し、本文・ツール・
時間軸が必要な場合だけ`traceで見る`を選ぶ。高額値から直接traceへ入る既存statusline導線は維持する。

## 4. URL・履歴契約

### 4.1 canonical URL

| 画面 | URL |
|---|---|
| dashboard | `/dashboard?view=<overview|period|trend|cache|dist>&from=YYYY-MM-DD&to=YYYY-MM-DD[&project=...]` |
| prompt detail | `/prompts/<prompt_id>` |
| session detail | `/sessions/<session_id>` |
| trace | `/traces/<session_id>?prompt=<prompt_id>[&focus=<request_id>]`＋表示時のfragment |

`range=yesterday`や`range=7d`は、常に相対期間を開く生きたbookmark用の入力契約として維持する。
表示時に解決した`from/to`をcanonical URLとして履歴へ残し、「昨日」の意味が翌日に変わっても
過去の閲覧履歴が変質しないようにする。

### 4.2 遷移規則

- v1はSSRのMPAとし、filterはGET form、tab/detailは実在する`a href`で遷移する。
- URL、戻る・進む、reload、別タブ、スクロール復元はブラウザのネイティブ機能へ委ねる。
- JavaScript無効時も、一覧・filter・detail・trace job状態ページまでを必須機能として動かす。
- prompt/session detail URLへ期間を結合しない。元のfilter復元はブラウザ履歴が担う。
- 未存在ID、不正prefix、削除済みデータは404画面で説明し、dashboardへ戻るリンクを出す。

## 5. システム構成

```text
~/Applications/Metsuke.app
        │ start/reuse + auth bootstrap + open
        ▼
metsuke dashboard serve (single instance, 127.0.0.1 only)
        ├─ SSR pages     GET form＋実在リンク（HTML）
        ├─ enhancement   trace進捗・復帰health確認だけの小さなJS
        ├─ routes        URL/query検証、HTML/最小job JSON応答
        ├─ query service ── SQLite mode=ro + query_only + authorizer
        │                    └─ shared view models
        ├─ trace jobs    ── trace_html.generate()
        └─ local events  ── spool（利用実測のみ）

shared view models
        ├─ dashboard SSR pages
        ├─ metsuke --json / text
        └─ self-contained HTML export
```

### 5.1 実装境界案

```text
src/metsuke/dashboard/
  server.py          # ThreadingHTTPServer、lifecycle、single instance
  routes.py          # HTTP route、validation、SSR response
  auth.py            # bootstrap token、cookie、Host/Origin検査
  jobs.py            # trace生成排他と状態
  pages.py           # viewmodelから安全なHTMLを生成する唯一のdashboard renderer
  static/
    app.css
    enhance.js       # progressive enhancementのみ

src/metsuke/viewmodel/
  common.py          # Window、pagination、共通DTO
  overview.py
  period.py
  trend.py
  cache.py
  dist.py
  prompt.py
  session.py

src/metsuke/viewgen/ # viewmodelを自己完結HTMLへ描画するfallback/export（既存render.pyを維持）
```

既存`viewgen/v1_period.py`等は生HTMLを直接生成しておらず、マークアップ出力は既に`render.py`へ
集約・テスト強制されている。残る結合は**SQL/集計とrender primitive呼出し**であるため、これを
JSON化可能な`viewmodel`と既存rendererへ分離する。`render.py`を作り直したり、dashboard用に
同じSQLを新規実装したりしてはならない。

## 6. query model契約

### 6.1 共通入力

```text
Window {
  start: local date
  end: local date          # inclusive
  project: string | null
}

Page {
  limit: 1..200
  page: integer >= 1
  sort: allowlisted key
  order: asc | desc
}
```

- SQL境界は既存どおり`start 00:00:00 <= localtime(ts) < end+1day 00:00:00`。
- timezoneは端末のローカルtimezoneとし、画面へ明示する。
- 端末のtimezoneを変更すると、同じ`from/to`でも日境界が変わり得る。単一端末向けの既知制約として
  受け入れ、応答と画面に評価時timezoneを含める。
- project、sort、view名はallowlist、値は必ずbind parameterで渡す。
- 一覧は既定40件、最大200件。v1は`LIMIT/OFFSET`で実装し、全件を返さない。
  cursor paginationは実測で必要になるまで導入しない。
- view modelの金額は丸め前の数値と表示文字列を両方持ち、表示とsortの意味を分離する。
- unknown costを0へ変換しない。件数と理由を別フィールドで返す。

### 6.2 同一性

同じWindowに対する次の値は、CLI、静的HTML、dashboardで完全一致させる。

- total cost / request / prompt / session / project数
- 費目別金額
- prompt/session/projectランキングとtie-break
- cache identityのcause、再作成費、gap
- context peakと黄/赤閾値
- 支配費目（定義と所有は[METRICS.md](METRICS.md) §4。`viewmodel/prompt.py`が単一所有）

tie-breakは現行実装がprompt ID昇順・session ID降順という非対称な規則を持つ。
実運用台帳では同額行が観測されず合成fixture上でしか現れなかったため、
現行挙動のまま凍結して正規化しない。

fixtureだけでなく、実台帳の複数期間を使ったsnapshot比較も行う。

### 6.3 dashboard reader接続

方式は2026-07-21のspikeで実測して確定した。実測結果は§6.4に残す。

**`mode=ro`を第一選択とする。** 当初案の`mode=rw`＋`query_only=ON`は採用しない。
`query_only`はSQL層の書込みだけを止め、**最終接続がcloseする際のauto-checkpointを止めない**。
実測では、crash後のWALを持つDBへ`mode=rw`＋`query_only=ON`で接続してcloseしただけで
main DBが書き換わりsidecarが削除された。これはADR 0004の「dashboardはDB writerにならない」に反する。

dashboard専用接続は次を不可分に行う。

1. DBが既存の通常ファイルであることを確認する。
2. SQLite URI `mode=ro`で開き、DBを新規作成しない。
3. `PRAGMA busy_timeout=250`を設定する。
4. `PRAGMA query_only=ON`を重ねる（`mode=ro`だけで書込みは拒否されるが、多層防御として置く）。
5. **上記PRAGMAをすべて設定した後に**SQLite authorizerを設置する。順序を逆にすると
   authorizer自身がPRAGMA設定を拒否して初期化に失敗する。
6. authorizerは**allowlist方式**とする。write/DDL/ATTACHを拒否し、PRAGMAは安全な名前だけを通す。
7. allowlist済みqueryだけを実行する。入力由来SQLと`executescript`を許可しない。

authorizerをdenylistで書いてはならない。実測では、危険PRAGMAを列挙するdenylistが
`PRAGMA query_only=OFF`を取りこぼし、接続を書込み可能へ戻せる穴が実際に開いた。
allowlistでは同じ試行が拒否され`query_only`は1のまま保たれた。現在値の読み取りだけは
引数なし呼出し（`arg2 is None`）として許可してよい。

`immutable=1`は使わない。実測でWAL内のコミット済みデータを黙って落とし、
古い値だけを返した。安全性ではなく正しさの問題である。

**sidecarの扱い**: WAL DBを読むあらゆるreaderは、`-wal`/`-shm`が無ければ生成する。
`mode=ro`でも生成は起きる（実測確認済み）。これはWAL readerに不可避であり、データ変更ではないため許容する。
不変条件は「**main DBのbyte列とschemaが変わらないこと**」であり、sidecarの存在の有無ではない。

テストでは次を固定する。

- INSERT/UPDATE/DELETE/DDLが接続レベルで失敗する。
- ATTACHと`PRAGMA writable_schema=ON`がauthorizerで拒否される。
- `PRAGMA query_only=OFF`が拒否され、値が1のまま保たれる（denylist退行の検出）。
- sidecar欠損状態・未checkpointのWAL・crash後のWALから最新commitを読める。
- 接続の開閉前後でmain DBのhashとmtimeが変わらない。
- writerがWAL transactionを保持していても読めるか、busy時は既定250msで`ledger_busy`へ収束する。

通常時のp95性能予算とbusy時の最大待ち時間は別指標として扱う。

### 6.4 reader方式の実測記録（2026-07-21）

実行環境はmacOS arm64 / Python 3.12+ / SQLite 3.4x以降。数万request規模のWAL台帳で検証した。

| 検証 | 結果 |
|---|---|
| `mode=ro`で実台帳を読む | 数万request取得・31ms・main DBのsize/mtime/hashが不変 |
| `mode=ro`のsidecar生成 | `-wal`/`-shm`が無い状態から生成される。main DBは書き換えない |
| `mode=rw`＋`query_only`のclose | **最終接続だとauto-checkpointでmain DBが書き換わりsidecarが消える** |
| 他の接続が同時に開いている場合 | 上記のclose時書込みは起きない。単独起動時だけ発火する |
| `immutable=1` | WAL内のコミット済み行を落とし、古い値を返す |
| authorizer denylist | `PRAGMA query_only=OFF`が通り、guardを無効化できた |
| authorizer allowlist | 同試行を拒否。`query_only`は1のまま |
| PRAGMA設定とauthorizerの順序 | authorizerを先に設置すると`query_only`/`busy_timeout`設定が拒否される |
| busy_timeout=250の実測 | 通常のWAL writerでは競合しない。writerが`locking_mode=EXCLUSIVE`の病的ケースで287msで失敗 |

`SQLITE_CANTOPEN`は、当初`mode=ro`を退けた根拠だったが、**実行環境のPythonでは再現しなかった**。
観測元はApple同梱の`sqlite3` CLIであり、dashboardが使うPython同梱SQLiteとは別runtimeである。
再現しない失敗を避けるためにmain DBを書き得る方式を採る取引は成立しない。
`mode=ro`が開けない環境が将来見つかった場合は、`mode=rw`へ暗黙にfallbackせず、
明示エラーとdoctor導線を出す。

## 7. HTTP契約（v1・SSR-MPA）

### 7.1 HTMLページ

| Method | Path | 内容 |
|---|---|---|
| GET | `/healthz` | 無認証。bodyは`ok`だけでversion・PII・DB状態を返さない |
| GET | `/bootstrap?nonce=...` | 60秒・一回性nonceをcookieへ交換し、queryを除いた`/dashboard`へ303 redirect |
| GET | `/dashboard` | view/window/project/sort/pageを受けてSSRする主画面 |
| GET | `/prompts/<id>` | explain相当の数値内訳をSSRする |
| GET | `/sessions/<id>` | session要約とprompt一覧をSSRする |
| GET | `/trace-jobs/<job_id>` | JSなしでも使えるjob状態ページ。完了時はtraceへ遷移 |
| GET | `/traces/<session_id>.html` | 認証後に自己完結traceを配信 |

人間UIの通常経路にoverview/view用JSON APIは設けない。CLI/`--json`はHTTPを経由せず、同じ
`viewmodel`を直接使う。これによりSSRとSPA用rendererの二重実装、frontend/API version skewを避ける。

### 7.2 導出物生成と漸進強化

| Method | Path | 内容 |
|---|---|---|
| POST | `/trace-jobs` | allowlist検証済みIDでtraceを生成し、303でjob状態ページへ遷移 |
| GET | `/trace-jobs/<job_id>?format=json` | 進捗dialog用の最小JSON。HTMLと同じjob modelを使う |

POSTはDBを変更しない。trace cacheと利用実測spoolだけを書ける。CSRF tokenを必須にし、
同一sessionのjobは共有する。JavaScript有効時だけdialog内でJSONをpollし、無効時は状態ページの
手動更新または短い`Refresh`応答ヘッダで完了を待てる。

### 7.3 error contract

HTML経路は同じ画面shell内に日本語の原因、再試行可否、dashboardへ戻るリンクを表示する。
progressive enhancementのJSON応答だけは次の共通schemaを使う。

```json
{
  "error": {
    "code": "invalid_window",
    "message": "開始日は終了日以前にしてください",
    "retryable": false,
    "request_id": "local-correlation-id"
  }
}
```

stack trace、SQL、ローカル絶対パス、tokenをHTTP応答へ出さない。詳細は0600のローカルログへ残す。

## 8. trace遅延生成

### 8.1 状態遷移

```text
click
  ├─ valid cache → ready(URL)
  └─ cache miss/stale → queued → running → ready(URL)
                                     └────→ failed(error)
```

- UIは元画面を保持したまま進捗dialogを出す。
- ready後にpromptなら`#prompt=<id>`、cache破れなら`#request=<id>`を付けて遷移する。
- failed時はdialog内で理由と再試行を提示し、元のfilter状態を失わない。
- job IDは推測困難なランダム値で、process終了時に破棄する。
- 生成中に同じリンクを複数回押してもjobを増やさない。
- 原則は非同期jobとするが、cache hitは即時にreadyを返す。
- job実行中はserver stopを受け付けても生成終了またはtimeoutまでgracefulに待つ。

### 8.2 cache freshness

最低限、次が一致した場合だけ再利用する。

- session ID
- sessionの最終request/end時刻
- parser version
- redaction version
- trace template/schema version

`trace template/schema version`定数、fingerprint、cache manifestは現行コードに存在しないため、
Phase 2で新設する。現行purgeはredaction version不一致だけであり、サイズ/日数管理済みとは扱わない。

`~/.metsuke/state/trace-cache.json`（0600・原子的置換）へfingerprint、generated_at、
last_accessed_at、size_bytesを持ち、次の順でpurgeする。

1. redaction/template version不一致を即時削除。
2. 最終accessから既定30日を超えたものを削除。
3. 合計が既定256MiB以下になるまでLRUで削除。

生成中・配信中のファイルはpurge対象外とし、設定は`METSUKE_TRACE_CACHE_MAX_MB`と
`METSUKE_TRACE_CACHE_MAX_AGE_DAYS`へ集約する。doctorは件数、合計bytes、最古access、purge失敗を表示する。

### 8.3 trace HTTP配信境界

traceはinline scriptを持つため、dashboardと同一originで配信すると、trace内XSSがdashboardの
認証済みendpointへ到達できる面が生じる。meta CSPだけを防御層とみなさず、server応答に次を必須とする。

- trace専用`Content-Security-Policy` header: 既存directive＋`connect-src 'none'`＋
  `frame-ancestors 'none'`。
- `X-Frame-Options: DENY`、`Cache-Control: no-store`、`Referrer-Policy: no-referrer`。
- dashboard用CSPを流用せず、`script-src 'unsafe-inline'`をtrace path以外へ広げない。
- CSP headerを削除/緩和するmutation testが、外部通信またはdashboardの認証済みpage/job endpointへ
  到達する検査を失敗させること。

Phase 2着手前に、(A) 同一origin＋上記header、(B) response CSPの
`sandbox allow-scripts`でopaque origin、(C) 認証cookieを共有しない別origin配信の3案を
実ブラウザで比較する。上記headerは全案の最低条件だが、Aだけでは同一origin XSS時のpopup/navigation
まで隔離できないため、出荷条件はBまたはCを成立させることとする。Bが既存traceのhash navigation、
DOM操作、閲覧を壊さなければBを採用する。Cは別portだけではcookieが分離されないため、host/path/tokenを
含む認証境界とlifecycleを独立させ、その複雑さに見合う追加防御が実測できる場合だけ採用する。
いずれも成立しなければdashboardからのHTTP trace配信を見送り、既存`file://`導線を維持する。

## 9. frontend方針

- v1はSSR-MPAとし、framework/build stepを導入しない。標準ライブラリのHTTP serverと
  Python renderer、HTML/CSS、小さなES moduleだけで実装する。
- 数値集計、価格計算、cause分類、一覧rendererをJavaScriptで行わない。JSはtrace進捗dialog、
  `visibilitychange/pageshow`時のhealth確認、使いやすさの漸進強化だけを担う。
- JSが挿入する文字列は`textContent`/安全な属性設定を使い、生データを`innerHTML`へ渡さない。
- 色だけに意味を持たせず、金額・閾値ラベル・icon/textを併記する。
- keyboard、focus-visible、ARIA live、reduced motion、狭幅レイアウトを既存HTMLと同じ受入対象にする。
- loading、empty、stale、partial/unknown、errorを正常系と同じ密度で設計する。
- viewmodelやjob応答をlocalStorage/IndexedDBへ永続保存しない。表示設定だけを保存できる。
- JavaScript無効の実ブラウザE2Eを必須にし、「fallback」ではなくMPAの基本動作として扱う。

## 10. 起動・終了・導線

### 10.1 人間の入口

installerは`~/Applications/Metsuke.app`を作り、Spotlightから`Metsuke`で起動できるようにする。
Dock追加は利用者の明示操作に任せ、自動変更しない。

起動処理:

1. `metsuke sync`をfail-openで試す。
2. dashboardのsingle-instance lock/stateを確認する。
3. 未起動ならloopback serverを開始し、healthz成功を待つ。
4. 60秒有効・一回性のbootstrap URLで12時間有効の認証cookieを設定する。
5. 既定の`昨日・概要`を開く。
6. cmux利用中は専用workspace、そうでなければ既定ブラウザを使う。

CLIには保守・テスト用として`metsuke dashboard [--open]`と`metsuke dashboard stop/status`を提供するが、
通常利用で覚える必要はない。

既定portは、privileged領域、3000/8000/8080等の常用開発port、49152以降の動的port帯を避けて
`48127`とする。設定名は`METSUKE_DASHBOARD_PORT`とする。bind先と表示URLは
`127.0.0.1`リテラルだけを使う。portは設定変更できるが、
既存bookmarkが壊れることを設定時に明示する。

### 10.2 lifecycle

- single instance。PIDだけでなくprocess start time/healthzでstale stateを判定する。
- v1はheartbeatとidle shutdownを持たない。起動後はlogout/rebootまたは明示的なstopまで待受する。
- 待受中はpollingをせず、idle CPUをほぼ0に保つ。OS起動時の自動起動はしない。
- `pageshow`と`visibilitychange`でhealthzを即時確認し、serverが消えていたらページ内に
  `Metsuke.appを開き直してください`と表示する。定期timerへ生存判定を依存させない。
- trace job実行中は明示stopでもgraceful completion/timeoutを待つ。
- trace HTMLは読込済みなら自己完結しているため、server停止後も現在の表示は維持できる。
- cookieはper-install secretで署名し、server再起動後も期限内なら有効とする。期限切れ/不正時は
  401画面からMetsuke.app再起動を案内する。
- `~/.metsuke/state/dashboard-secret`と`dashboard-state.json`は0600で原子的に更新する。stateには
  PID、process start time、port、server instance IDだけを置き、cookie、bootstrap nonce、閲覧URLを保存しない。
- bootstrap nonceはrandom値、期限、server instance IDをper-install secretで署名し、使用済みdigestを
  server memoryへ60秒だけ保持する。server再起動時はinstance IDが変わるため旧nonceを受理しない。
- HTTP serverの既定access logを無効化し、必要な監査logにもrequest path/query/cookieを残さない。

### 10.3 statusline導線

- 高額prompt金額 → 現行の自己完結`file://` traceを維持する。dashboard停止中も壊さない。
- dashboard入口を追加する場合は、総額側から`Metsuke.app`を起動できるcustom URL scheme等を
  Phase 3で実ブラウザ/terminal検証する。未検証の固定HTTP URLへ置換しない。
- statusline向けの自動trace生成は利用実測に数えない。dashboardではcache hit/missにかかわらず、
  利用者が`traceで見る`を選んだ時点の`dashboard_trace_opened`を数える。

## 11. セキュリティ詳細

| 脅威 | 対策 |
|---|---|
| LAN/外部からの到達 | IPv4 loopbackだけへbind。公開bindの設定を持たない |
| DNS rebinding / 悪意あるWebページ | Host/Origin検査、bootstrap認証、SameSite cookie、CSRF token |
| 同一origin traceからdashboardへの到達 | trace専用response CSP/XFOに加え、opaque-origin sandboxまたはcookie非共有originを出荷条件とする。失敗時はHTTP配信しない |
| PIIのブラウザcache/送信 | no-store、no-referrer、外部接続を禁止するCSP、analytics/CDN禁止 |
| SQL injection | route/query allowlist＋bind parameter。SQL文字列を入力から組み立てない |
| XSS | read-boundary redaction、JSON encoding、textContent、安全renderer、CSP |
| path traversal | ID形式検証、DB解決後の内部pathのみ使用。URL文字列をfilesystemへ連結しない |
| DB破損 / readerによるDB書換え | `mode=ro`で既存DBだけを開く。`query_only`とallowlist authorizerを重ね、close時auto-checkpointでmain DBを書き得る`mode=rw`は採らない。write試行testとhash不変testで固定。250ms busy timeout、503応答 |
| trace生成DoS | 認証、session単位排他、同時job上限、期間/list件数上限 |
| token漏洩 / 長寿命化 | secretは0600、bootstrap nonceは60秒・一回性、cookieは署名付き12時間。URLから即redirectしlogにquery/tokenを残さない |

dashboard shellには原則として
`default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`
相当のCSPを適用する。traceはADR 0006のより厳しい自己完結HTML用CSPを維持する。
同一macOSユーザー権限で動く悪意あるprocessは0600のledger/tokenを直接読めるため脅威モデル外とし、
Web origin、LAN、誤設定、偶発的な外部送信を主な防御対象とする。
この脅威モデルの要約は[docs/05-risks.md](05-risks.md)へ反映済みとし、実装時は攻撃fixtureと
ブラウザ検証結果を同文書へ追記する。

## 12. 性能予算

数万request規模の実台帳を基準に、次を初期予算とする。実装前後で実台帳ベンチを残す。

| 操作 | 目標 |
|---|---|
| app起動からoverview HTML表示 | 2秒以内 |
| 昨日/7日overview query | p95 300ms以内 |
| 期間tab切替（31日） | p95 500ms以内 |
| prompt detail | p95 200ms以内 |
| session detail | p95 300ms以内 |
| trace cache hit | 200ms以内に遷移開始 |
| trace cache miss | p95 2秒以内を目標。超過してもUIをblockせず進捗表示 |
| HTML/job JSON response | 通常1MB未満。表はpagination |
| DB busy判定 | busy_timeout 250msで中断し、503画面を返す。lock poll分の実測上振れ（約287ms）を含めて300ms以内 |
| idle時CPU | ほぼ0。timer pollingをせずrequest待ち |

性能のためにDBへ派生金額を永続化しない。必要ならindex、query見直し、短時間のin-memory cacheの順で対応する。

## 13. 故障時の表示

| 状態 | UI |
|---|---|
| ledger不在 | 初期同期が必要と表示。空の0円dashboardを出さない |
| stale | 最終正常取込時刻と経過時間をheaderへ固定表示。過去データは閲覧可能 |
| 一部cost unknown | 合計から黙って消さず、unknown件数と不完全表示を示す |
| DB busy | 短いretry後に再試行ボタン。ingesterを止めない |
| server停止 | 表示中ページにMetsuke.app再起動を案内。CLI/static exportは利用可能 |
| 認証cookie期限切れ | 401画面からMetsuke.appを開き直す。元の生URLへtokenを付与しない |
| trace生成失敗 | 元画面を保持し、理由・再試行・数値detailへの戻りを表示 |
| trace cache上限超過 | 非使用中LRUを削除し、件数・bytes・最古access・purge失敗をdoctorへ表示 |
| port競合 | 別サービスへ接続せず、競合を明示してdoctor導線を出す |

## 14. 実装フェーズ

### Phase 0 — query model分離

- 現在`viewgen` builderに同居するSQL/集計とrenderer primitive呼出しを、純粋なview modelへ分離する。
- V1〜V4の既存HTML/CLIとのgolden一致に加え、view model自体のgoldenを追加する。
- overview、prompt、session modelを追加し、前期間比較とcost-bearing prompt定義を固定する。
- dashboard readerを`mode=ro`＋`query_only`＋allowlist authorizerで実装し、WAL中の読取り、
  write拒否、main DB不変、busy timeoutをtestする。

完了条件: dashboardをまだ作らなくても既存テストがすべて通り、V1〜V4の出力値が変わらず、
WALが存在する実DB相当fixtureをquery-only readerで参照できる。

### Phase 1 — query-only dashboard MVP

- `ThreadingHTTPServer`、bootstrap/cookie認証、SSR overview/period page。
- 昨日/今日/7日/任意期間、project filter、GET URL、native history。
- prompt/session detail、stale/unknown/busy/auth/error page、JavaScript無効E2E。
- `127.0.0.1:48127`固定既定、Host/Origin/CSRF/CORSなし/response headerの安全性test。

完了条件: appを開いてから、昨日の最大promptの数値内訳までCLIなしで到達できる。

### Phase 2 — 全ビューとtrace遅延生成

- trend/cache/distを共通filterへ接続する。
- trace job、template version/fingerprint、manifest、LRU/age purge、loading/error/retry、focus付き遷移。
- trace専用CSP/XFOを実装し、同一origin、sandbox、別portを実ブラウザで比較する安全性gateを通す。
- dashboard→detail→trace→戻るで状態を保持する。

完了条件: 未生成traceをクリックし、生成後に対象prompt/requestが選択された画面へ到達できる。

### Phase 3 — macOS/cmux導線と運用

- Metsuke.app、installer/uninstaller、single instance、明示stop、再起動案内。
- statuslineのprompt金額は既存`file://` traceを維持し、総額側のdashboard導線を実ブラウザで検証する。
- `metsuke doctor`へserver/auth/port/cache freshness/LRU使用量を追加する。

完了条件: SpotlightまたはDockから起動でき、コマンドを一度も入力せず主要探索を完走できる。

### Phase 4 — 実運用評価

- 4週間、起動日数、filter変更、detail/trace到達、失敗、生成時間、保守時間をローカル計測する。
- 使われないタブや過剰なKPIを削る。
- dashboard有効化日を`regime_event`へ記録し、Stage 7の実験に対する観測条件の変更として扱う。
- dashboard導入前後で直接CLI view実行数を比較する。AIへ操作方法を質問したかはprompt本文を
  自動分類せず、4週間後に利用者が定性的に確認する。
- view利用分母は`dashboard_view_opened + view_html_generated`、trace利用は
  `dashboard_trace_opened + 手動CLIのtrace_html_generated`とし、statusline向け自動生成を除外する。

## 15. 受け入れ基準

### 機能

- [ ] 初回表示がローカル日付の昨日で、表示範囲が明記される。
- [ ] preset/任意日付/project/tabを変更するとURLと内容が一致する。
- [ ] GET formと実在するlinkだけで、戻る・進む・reload・別タブの状態を再現できる。
- [ ] JavaScript無効でもoverview、filter、detail、trace生成待ち画面を操作できる。
- [ ] dashboardと同条件のCLI/静的HTMLで集計値が一致する。
- [ ] prompt/session IDをコピーまたは入力しなくても詳細へ到達できる。
- [ ] 未生成/stale traceが生成され、対象prompt/requestへfocusして開く。
- [ ] 生成失敗時に元画面とfilter状態が残る。
- [ ] dashboard利用中にAI/API通信が0件である。

### 安全性

- [ ] loopback以外から接続できない。
- [ ] tokenなし、誤Host、cross-origin、CSRFなしPOSTを拒否する。
- [ ] bootstrap nonceの再利用、60秒超過、別server instanceでの利用を拒否し、URL queryをlogへ残さない。
- [ ] HTTP処理中のSQLite接続が`mode=ro`で、write/DDL/ATTACH/`query_only=OFF`を拒否する。
- [ ] dashboard接続の開閉前後でmain DBのhashとmtimeが変わらない。
- [ ] WALが存在する状態でもdashboard readerが最新commitを読め、busy時は250msで503になる。
- [ ] XSS/path traversal/SQL injectionのfixtureを拒否または安全に表示する。
- [ ] response/log/browser storageへsecret tokenや非redact本文を残さない。
- [ ] dashboard/trace双方のresponse CSP下で外部通信が発生せず、traceからdashboard endpointへ到達できない。
- [ ] CORS headerを返さず、bind先・表示URL・Host allowlistが`127.0.0.1`だけである。

### 運用

- [ ] installer/uninstallerがapp、state、cacheを冪等に扱う。
- [ ] stale process/port競合/DB不在をdoctorとGUIの両方で説明する。
- [ ] dashboard停止中もstatusline、hooks、CLI、ingesterが影響を受けない。
- [ ] statuslineの高額prompt `file://`リンクがdashboard停止中も開く。
- [ ] trace cacheのfingerprint不一致、30日超過、256MiB超過を検出・purgeし、doctorで説明する。
- [ ] 性能予算を実台帳で測定し、結果をBENCH.mdへ残す。

## 16. prototypeで固定する残件

SSR-MPA、標準`ThreadingHTTPServer`、query-only reader、既定port、認証寿命、非idle-shutdownは
本設計で確定した。実装中のprototypeで判断する残件は次に限定する。

1. trace隔離は、response headerを最低条件として、`sandbox allow-scripts`と別loopback portのどちらを
   上乗せするか。hash navigation、DOM操作、認証面、lifecycleの実ブラウザ結果で決める。
2. trace jobの同時実行上限とtimeout。初期候補は全体2、session単位1、timeout 30秒とする。
3. overviewのKPI密度と狭幅時の優先順位。総額、前期間差、cost-bearing prompt、最大promptを優先候補とする。
4. cmux/既定ブラウザの優先設定名と、statusline総額からMetsuke.appを開く導線。
5. local usage eventの共通fieldと重複抑制。event名は`dashboard_view_opened`と
   `dashboard_trace_opened`に固定し、prompt本文・ID・project名・filter値は記録しない。共通field候補は
   画面名、結果、所要時間、起動方法、trace cache hit/missに限定する。

## 17. `08-dashboard-fb.md`への対応

| FB | 判断・反映先 |
|---|---|
| S1 | 採用。trace response CSP/XFOを必須化し、opaque/cookie非共有originをPhase 2出荷gateに設定（§8.3、§11） |
| S2 | 採用。ただしspike実測で方式を再決定。`mode=rw`＋`query_only`はclose時checkpointでmain DBを書くため棄却し、`mode=ro`＋allowlist authorizerを採用（§6.3、§6.4、§11、Phase 0） |
| S3 | 採用。v1をSSR-MPAに固定し、JSは漸進強化だけ（§4、§7、§9） |
| S4 | 採用。heartbeat/idle shutdownをv1から削除し、明示stopとpage復帰時health確認へ変更（§10） |
| S5 | 採用。30日・256MiB LRU、manifest、doctor表示を追加（§8.2） |
| F1 | 指摘を補正して採用。markupの正は既に`render.py`だが、builderのSQL/集計と描画呼出しを純view modelへ分離（Phase 0） |
| F2 | 採用。template version/fingerprint/manifestは新規実装であることを明記（§8.2） |
| F3 | リスク台帳へ実装前提と検証項目を追記（§11、`05-risks.md`） |
| F4 | 採用。prompt表行数ではなくcost-bearing distinct promptをKPIに固定（§3、ADR 0011） |
| F5 | 採用。statuslineのprompt導線は既存`file://`を維持（§10.3） |
| P1 | 採用。概要KPIへ直前同日数期間との差を追加し、前期0は`比較不能`とする（§3.3） |
| P2 | 採用。prompt/session detail URLをIDだけにし、元filterはbrowser historyへ委ねる（§4） |
| P3 | 採用。v1はLIMIT/OFFSETとし、cursorは実測後まで入れない（§6.1） |
| P4 | 採用。60秒nonce、12時間cookie、再起動跨ぎ、401 UX、即時redirectを固定（§7、§10） |
| P5 | 採用。view/traceのdashboard明示利用eventとCLI生成eventを分母にし、自動生成を除外（§10、Phase 4） |
| P6 | 採用。dashboard有効化を`regime_event`へ記録する（Phase 4） |
| P7 | 採用。`range=`を生きたbookmark入力として維持し、解決後は`from/to`を履歴へ残す（§4.1） |
| P8 | 採用。新規依存なしの`ThreadingHTTPServer`をv1に固定（§5、§9） |
| 軽微 | `127.0.0.1`、port選定、最小health、CORSなし、viewmodel golden、TZ可変制約を各契約へ反映 |

これらを除き、主入口、query方式、URL状態、遅延trace、安全境界、段階実装は本書で確定とする。
