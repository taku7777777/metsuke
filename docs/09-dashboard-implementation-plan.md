# 09 — ローカルdashboard実装計画

日付: 2026-07-21 / 状態: 実装着手可能

設計の正は[ADR 0011](adr/0011-local-dashboard.md)と[08-dashboard](08-dashboard.md)、
レビュー入力は[08-dashboard-fb](08-dashboard-fb.md)とする。本書は、確定した設計を安全に実装する
順序、変更境界、テスト、停止条件を定める。画面仕様を重複定義しない。

## 1. 固定済みの判断

- 人間向けv1はSSR-MPA。filterはGET form、遷移は実在するlinkを使う。
- serverはPython標準`ThreadingHTTPServer`。新しいruntime依存を追加しない。
- `127.0.0.1:48127`を既定とし、`METSUKE_DASHBOARD_PORT`だけで変更可能にする。公開bind、
  `localhost`、IPv6待受はv1に入れない。
- DBは既存ファイルを`mode=ro`で開き、`query_only`とallowlist authorizerを重ねる（spike実測で確定。
  `mode=rw`＋`query_only`はclose時auto-checkpointでmain DBを書くため棄却。08-dashboard §6.4）。
- busy timeoutは250ms。ingesterを待たせず、dashboard側が503 `ledger_busy`を表示する。
- 60秒・一回性bootstrap nonceから12時間の署名cookieを発行する。server再起動を跨いで検証する。
- heartbeat/idle shutdownはv1に入れない。logout/reboot/明示stopまで待受し、timer pollingはしない。
- statuslineの高額promptは既存`file://` traceを維持する。
- trace cacheは既定30日かつ256MiB。manifestでfingerprint、size、last accessを管理する。
- traceのHTTP配信は、response CSP/XFOだけでなくopaque originまたはcookie非共有originを成立させた
  場合だけ出荷する。不成立なら`file://`へ安全に縮退する。

## 2. 依存順

```text
P0 現行値の固定
  ↓
P1 純粋view model ──→ CLI / 既存HTMLの回帰
  ↓
P2 query-only reader
  ↓
P3 SSR-MPA MVP ──→ overview / period / prompt / session
  ↓
P4 全ビュー統合
  ↓
P5 trace job・cache・origin隔離 gate
  ↓
P6 Metsuke.app・installer・doctor
  ↓
P7 regime記録・4週間評価
```

P1より前にserverを作らない。dashboard専用SQLを先行させると、CLI/静的HTMLとの定義分裂を
既成事実化するためである。P5の安全性gateはP3/P4と独立に失敗でき、失敗しても数値dashboardを出荷できる。

## 3. 作業パッケージ

### P0 — baseline固定

変更:

- 現行V1〜V4のfixture出力は値レベルsnapshotとして保存し、実台帳の代表期間はローカルで比較する。
- prompt KPIを「non-synthetic cost-bearing requestのdistinct prompt ID」に固定する。
- 前期間比較、unknown cost、tie-break、timezoneの期待値をfixture化する。

主な対象:

- `tests/test_views.py`
- 新規`tests/fixtures/dashboard/`
- `docs/BENCH.md`

完了条件:

- markupをparseしなくても比較できる期待値が揃う。
- 実台帳snapshotは本文、project名、ID、集約値をcommitせず、公開用にはschema契約の合成fixtureだけを残す。

### P1 — 純粋view modelへの分離

変更:

- 新規`src/metsuke/viewmodel/`へ`Window`、page、overview、period、trend、cache、dist、prompt、sessionの
  immutable DTOとquery関数を置く。
- 現行`viewgen/v1_period.py`等からSQL/集計を移し、`render.py`にはview modelだけを渡す。
- `render.py`をmarkupの唯一の出力元とする既存不変条件は維持する。
- CLI text/JSON、静的HTML、将来のSSRが同じview modelを使う。

主な対象:

- 新規`src/metsuke/viewmodel/*.py`
- `src/metsuke/viewgen/*.py`
- `src/metsuke/report.py`
- `tests/test_views.py`
- 新規`tests/test_viewmodel.py`

完了条件:

- P0 baselineとV1〜V4の値・順序が一致する。
- view modelのgoldenがmarkup rendererから独立して失敗する。
- `viewmodel/`がHTML、filesystem出力、browser起動、spool書込みを行わない。

### P2 — dashboard reader

変更:

- dashboard専用接続関数を追加する。既存`connect_readonly()`の全用途を一括置換しない。
- 既存通常ファイル確認 → `mode=ro` → busy 250ms → `query_only` → **その後に**authorizer設置を
  一関数で不可分に行う。順序を逆にするとauthorizerがPRAGMA設定を拒否して初期化に失敗する。
- authorizerは**allowlist方式**。write、DDL、ATTACH/DETACHを拒否し、PRAGMAは安全名だけを通す。
  denylistは`query_only=OFF`を取りこぼすため採らない。routeはallowlist済みSQLだけを呼ぶ。
- busy/DB不在/破損を内部例外へ正規化し、HTTP層がSQLや絶対pathを表示しないようにする。

主な対象:

- `src/metsuke/ledger.py`または新規`src/metsuke/dashboard/db.py`
- 新規`tests/test_dashboard_reader.py`

必須fixture:

- WALに未checkpoint commitがある。
- `-shm`不在から接続する。
- writerをSIGKILLしてcrash recoveryが必要なWALを残す。
- writerがtransactionを保持する。
- INSERT/UPDATE/DELETE/DDL、ATTACH、`PRAGMA query_only=OFF`、`PRAGMA writable_schema=ON`を試行する。

完了条件:

- 最新commitを読めるか、250ms以内に明示的なbusyへ収束する。
- 接続の開閉を繰り返してもmain DBのhash/mtimeとschemaが変わらない（close時checkpoint退行の検出）。
- `query_only`が1のまま保たれ、authorizerをdenylistへ書き換えると当該testが落ちる。

### P3 — SSR-MPA MVP

変更:

- 新規`src/metsuke/dashboard/`へserver、route、auth、page renderer、CSSを追加する。
- `/dashboard`、`/prompts/<id>`、`/sessions/<id>`、`/healthz`を実装する。
- overview/period、昨日既定、preset/custom期間、単一project、OFFSET pagination、前期間差を実装する。
- server instance ID付きbootstrap nonce、署名cookie、Host/Origin、POST時CSRF、
  CSP/no-store/no-referrer、CORSなし、queryを記録しないaccess logを実装する。
- stale、unknown、empty、404、busy、401、port競合をserver-rendered pageで説明する。

主な対象:

- 新規`src/metsuke/dashboard/{server,routes,auth,pages}.py`
- 新規`src/metsuke/dashboard/static/app.css`
- `src/metsuke/config.py`
- `src/metsuke/cli.py`（保守用`dashboard serve/status/stop`。通常入口にはしない）
- 新規`tests/test_dashboard.py`
- 新規`tests/test_dashboard_security.py`

完了条件:

- JavaScript無効で、昨日の概要から最大promptの数値detailへ到達できる。
- URL直打ち、戻る/進む、reload、別タブを独自history codeなしで再現できる。
- 誤Host、cookieなし/期限切れ、cross-origin POST、CSRFなしPOSTを拒否する。
- bootstrap nonceの再利用、期限超過、別server instanceへの持越しを拒否し、logにqueryを残さない。
- dashboard利用中のLLM/API通信が0件である。

### P4 — 全ビュー統合

変更:

- trend/cache/distを共通Window/project/paginationへ接続する。
- overviewのKPI優先度を、総額、前期間差、cost-bearing prompt、最大promptから実データで調整する。
- `dashboard_view_opened`を成功した明示page表示に記録する。本文、ID、project、filter値は残さない。
- 狭幅、keyboard、focus-visible、reduced motion、色以外の閾値表現を検証する。

完了条件:

- 同一Windowの全指標がCLI/静的HTML/dashboardで一致する。
- loading/empty/stale/partial/errorが正常画面と同じ操作導線を持つ。
- 1MB response上限、40件既定/200件上限、性能予算を満たす。

### P5 — trace job・cache・配信安全性gate

変更:

- session単位排他を持つin-memory jobと、HTML/最小JSONの状態応答を実装する。
- trace template/schema version、session freshness fingerprint、0600 manifestを新設する。
- 30日/256MiBのLRU purgeを実装し、生成中・配信中のfileを除外する。
- `dashboard_trace_opened`をcache hit/missにかかわらず明示操作時に記録する。
- trace responseへ専用CSP、XFO DENY、no-store、no-referrerを付ける。

安全性gate:

1. response CSP `sandbox allow-scripts`で既存traceのhash navigation、DOM操作、focusが動くか実ブラウザ検証。
2. 攻撃fixtureからdashboard page/job responseを読めず、POSTを成立させられないことを検証。
3. sandboxが成立しない場合だけ、cookie非共有の別origin案をprototypeする。単なる別portはcookieを
   分離しないため合格としない。
4. どちらも成立しなければ`/traces/`を出荷せず、生成後に既存`file://`を開く導線へ縮退する。

主な対象:

- 新規`src/metsuke/dashboard/jobs.py`
- `src/metsuke/trace_html.py`
- `src/metsuke/config.py`
- 新規`tests/test_dashboard_trace.py`
- `tests/test_report.py`
- `docs/BENCH.md`

完了条件:

- cache hitは200ms以内、missはp95 2秒を目標に非blockingで遷移する。
- fingerprint/age/size purgeとdoctor表示が一致する。
- 安全性gateの結果、採用案、棄却理由をADR 0011へ追記する。

### P6 — macOS入口と運用

変更:

- `~/Applications/Metsuke.app`をinstallerが冪等生成し、sync → server再利用/起動 → bootstrap →
  cmux workspaceまたは既定browserで開く。
- single-instance stateにPID、process start time、portを持たせ、health確認後だけ再利用する。
- `dashboard stop/status`、auth/port/cache/manifestのdoctor項目、uninstall cleanupを追加する。
- statuslineのprompt `file://`リンクを回帰testで維持する。総額からのapp導線は実ブラウザ検証後だけ追加する。

主な対象:

- `scripts/install.sh`、`scripts/uninstall.sh`
- `src/metsuke/cli.py`、`src/metsuke/doctor.py`
- `src/metsuke/trace_html.py`または専用launcher module
- `tests/test_install.py`、新規`tests/test_dashboard_lifecycle.py`
- `docs/RUNBOOK.md`、`README.md`

完了条件:

- 初回/再install、server稼働中、stale state、port競合、logout後を再現できる。
- dashboard停止中もstatusline、hooks、sync、CLI、既存HTMLが動く。
- uninstallがarchive/ledgerを削除せず、app/server state/cacheだけを明示契約どおり扱う。

### P7 — rolloutと評価

変更:

- dashboard有効化時に`regime_event`を1件記録する。
- 4週間、利用日数、view表示、filter/detail/trace到達、失敗率、生成時間、保守時間をローカル集計する。
- 分母は`dashboard_view_opened + view_html_generated`、traceは
  `dashboard_trace_opened + 手動CLIのtrace_html_generated`とする。statusline自動生成を除外する。
- Stage 7のTTL実験はdashboard導入日を境に併読し、導入前後を無条件に同一regimeとして比較しない。

完了条件:

- 4週間後に継続、画面縮小、静的昨日summaryへの撤退を実測で判断できる。
- prompt本文を自動分類せず、AIへ操作方法を尋ねたかは利用者が定性的に確認する。

## 4. commitとレビューの境界

変更は次の単位を混ぜない。

1. baseline/view model（出力不変）。
2. query-only reader（DB安全性だけ）。
3. server/auth/SSR MVP（traceなし）。
4. 全ビュー統合。
5. trace cacheと配信安全性gate。
6. app/install/doctor。
7. rollout計装と文書。

各単位で`pytest`、`ruff check`、文書リンク検査を通し、既存のdirty worktree上では対象外差分を
commitへ混ぜない。P5とP6はmacOS実ブラウザ/LaunchServicesを使うため、unit test合格だけで完了扱いにしない。

## 5. 現時点の判断待ち

利用者判断が必要な項目は現時点ではない。次に判断を仰ぐのは以下のどちらかが起きた場合に限定する。

- trace sandboxが主要操作を壊し、別origin化が新しい常駐process・URL・認証面を増やす場合。
- overviewの狭幅表示で、優先KPIを4個以下へ削る必要があり、実データでも優先順位が決まらない場合。

安全性gateが不成立の場合は、判断待ちの間も数値dashboardを先に完成できる。trace HTTP配信を暗黙に
弱い条件で有効化して進行を止めない。
