# 08-FB — ローカルdashboard設計レビュー

日付: 2026-07-21 / 対象: [ADR 0011](adr/0011-local-dashboard.md)・[08-dashboard.md](08-dashboard.md)・
[04-roadmap Stage 8](04-roadmap.md) / 方法: ドキュメント精読＋独立コンテキストでのコード実地照合
（viewgen・ledger・trace_html・state・config・cli・tests・実台帳）

## 1. 総評

**方針は妥当。** 実運用で観測された摩擦（引数の暗記、状態が残らないURL、降下動線の断絶）から出発し、
仮説ではなく事実で動機づけられている。既存ADRとの関係整理（0003部分置換・0006維持・0007提供面のみ置換）、
撤退基準、loopback限定の安全境界、query model共有による集計定義のSSOT化はいずれも筋が良い。
棄却した代替案の比較（自己完結HTML肥大・SQLite WASM・ネイティブアプリ）も説得的で、再考の必要はない。

以下は「方針転換」ではなく、実装前に潰すべき穴と、実装量を減らせる可能性の指摘である。
重要度順: **S**=設計修正を推奨 / **F**=記述と事実の食い違い / **P**=検討提案。

## 2. 重要指摘（S: 設計修正を推奨）

### S1. trace HTTP配信によるオリジン統合が ADR 0006 の防御層を一段崩す

trace HTMLは file:// 前提の自己完結物として設計され、meta CSPの `script-src 'unsafe-inline'` を
許した上で「破られても外部送信できない」多層防御を組んでいた（ADR 0006 §4）。
`/traces/<session_id>.html` を dashboard と**同一オリジン**（`http://127.0.0.1:port`）で配信すると:

- trace内スクリプトから dashboard API への same-origin fetch が可能になる。HttpOnly cookieは
  同一オリジンリクエストに自動付与されるため、認証はバイパスされる。
- meta CSPは `frame-ancestors` を表現できない（仕様上無視される）ため、配信時のクリックジャッキング
  防御が欠ける。

trace CSPの `default-src 'none'`（connect-src閉鎖）が健在なら実害は限定的だが、「traceのCSPが
破れた場合の第二層」が消える構造変化であり、08 §11 の脅威表に現れていない。対策案:

1. `/traces/` 配信時に**応答ヘッダでもCSPを重ねる**（`connect-src 'none'` を含む。ヘッダとmetaは
   両方適用され、厳しい方が効く）＋ `X-Frame-Options: DENY`。
2. `Content-Security-Policy: sandbox allow-scripts` 等での opaque origin 化、または別ポート配信を
   Phase 2 プロトタイプで比較する。
3. 脅威表へ「trace内XSS → dashboard API到達」の行を追加する。

### S2. `mode=ro` + WAL の既知の脆さを設計が前提にしていない

writer は `PRAGMA journal_mode=WAL`（ledger.py:23）であり、WALはDBに永続する。WAL DBに対する
read-onlyオープナーは `-shm` を作成できず、sidecarの状態によって `SQLITE_CANTOPEN` になる古典的な
穴がある。**実地確認**: 現時点の実台帳は Python の `connect_readonly()`（bare `?mode=ro`）で開けたが、
macOSシステムの `sqlite3` CLIでは同条件で error 14 を観測し、`immutable=1` で初めて成功した。

dashboardはHTTPリクエストごとに高頻度でro接続を開くため、この足場は明示的に固めるべき:

- 推奨: URI `mode=ro` ではなく**通常接続＋`PRAGMA query_only=1`**（読み取り専有を接続レベルで強制
  しつつ shm 問題を回避）。採るなら受入基準「HTTP処理中のSQLite接続が`mode=ro`である」（08 §15）の
  文言も `query_only` を許す形へ更新する。
- 併せて reader 側の busy_timeout を契約として明記する（現行は `timeout=10` 引数に暗黙依存 —
  ledger.py:89-93）。性能予算 p95 300ms と busy 待ちの関係も一言必要。

### S3. SPA実装と「JS無効時のserver-rendered fallbackを目標」は二重実装 — MPA主体を比較対象に

現設計は「静的shell＋JSON API＋JSレンダリング」で、pushState/popstate/スクロール復元/
frontend-APIバージョン不一致検出/ARIA live/JS無効fallbackを個別に作り込む必要がある（08 §4.2, §9, §13）。
**server-rendered なマルチページ構成（GETフォーム＋実在の`a`リンク）なら、このうち大半が
ブラウザのネイティブ機能で無料になる**: URL=状態、戻る/進む/reload/別タブ復元、version skew消滅、
JS無効動作。loopback p95 300ms ではページ遷移コストは体感差にならない。JSは trace job の進捗
ダイアログ等の漸進強化に限定でき、app.js と故障面が大幅に縮む。JSON APIは人間UIの経路ではなく
CLI/機械用として viewmodel から出せばよい。

単一保守者の保守予算という本プロジェクトの一貫した判断基準に照らすと、MPA優位の可能性が高い。
少なくとも 08 §16 の Phase 1 固定項目へ「**SSR-MPA vs 静的shell+JSON API の比較**」を追加し、
「JS無効fallbackは目標か要件か」の曖昧さを確定すべき（両建ては最悪の選択肢）。

### S4. heartbeat × ブラウザのタブスロットリング × idle shutdown の相互作用が未定義

バックグラウンドタブのタイマーは強くスロットリング/凍結される（Chromeの省エネモード、Safari）。
「visibleなdashboardはheartbeatを送る」（08 §10.2）は意図的な設計だが、帰結として
**放置タブ → server終了 → 戻った利用者の操作が全て失敗**が通常系になる。定義すべきこと:

- fetch失敗時にページ内で「serverは停止しています。Metsuke.appを開き直してください」を表示する
  （§13の表に「**表示中ページからのserver消失**」行を追加。現在の「server停止」行はページを
  開き直す状況しか想定していない）。
- trace生成job実行中は idle shutdown を延期する（30分より長い生成は現実には無いが、jobが
  activityに数えられるかが未定義）。
- `visibilitychange` 復帰時に即時heartbeat＋健全性確認を行う。

### S5. trace cacheのディスク管理が不在

ADR 0006 のstress testでtrace HTMLは**複数MB/ファイル**になり得る。dashboardのクリック起点生成は
蓄積を加速するが、現行のpurgeは redaction_version 基準のみ（trace_html.py:587 `_purge_old()`）で、
サイズ・鮮度によるpurgeは存在しない。08 §8.2 の「既存の原子的置換とpurge規則に従う」は
実態より多くを約束している。サイズ/日数上限（LRU）と `metsuke doctor` での使用量可視化を設計に加えるべき。

## 3. 記述と事実の食い違い（F: 文書修正）

コード照合で確認した、設計文書の前提と現状コードのずれ。いずれも方針は変えないが、
Phase計画の作業量見積もりと文書の正確さに効く。

| # | 記述 | 事実 | 対応 |
|---|---|---|---|
| F1 | 「viewgen/v1_period.py 等に混在するSQLとHTML構築を分離」（08 §5.1） | 生HTML構築は既に render.py へ分離済みで、テストで機械強制されている（tests/test_views.py:141）。builderに欠けるのは**純データviewmodel**（戻り値が `Html` 文字列 — v1_period.py:37,373） | Phase 0 の作業自体は正当。表現を「SQL＋render呼び出しの結合を、JSON化可能なviewmodelとrendererへ分離」と正確化（render.py再設計と誤読させない） |
| F2 | cache freshness keyに「parser/redaction/**template version**」（ADR §4・08 §8.2） | template/schema version 定数はコードに**存在しない**（grep 0件）。session最終request時刻のfreshnessも state.py:399 のmtime比較のみで、generate()側cache keyには未反映 | freshness判定は**新設実装**であることをPhase 2の作業項目に明記 |
| F3 | 「脅威モデルは実装前に05-risks.mdへ追記する」（08 §11） | 既に2行追記済み（05-risks.md:18-19） | 記述を現状（追記済み・実装時に詳細化）へ更新 |
| F4 | ADRのprompt数定義 | 2つの数え方で約2割食い違い、台帳成長では説明できなかった | prompt の数え方（agent/sidechain除外の有無等）を脚注で定義。dashboardのKPI「prompt数」の定義とも直結する |
| F5 | 「高額値から直接traceへ入る既存statusline導線は維持」（08 §3.4） | 現行statuslineリンクは **file:// URI**（state.py:414-419 → OSC 8）であり、serverレスで機能する。Phase 3 で http URL に張り替えると**server非稼働時にリンクが死ぬ**という新しい退行が生じる | Phase 3 に方針を明記。推奨: prompt金額→traceは自己完結ゆえ file:// を維持し、dashboard起動リンクは総額側のみ http（またはMetsuke.app起動）にする |

## 4. 中程度の提案（P）

- **P1. 概要KPIに前期比較がない。** 概要は水準（いくら使ったか）のみで、変化（前の同じ長さの期間比）が
  V2タブまで降りないと見えない。「最初に見るべき外れ値」という概要の問いには Δ% が効く。
  KPIタイルへの軽量な前期比表示を検討（Phase 4 で削る前提なら追加コストは小さい）。
- **P2. detail URLの期間パラメータは不要な結合。** `/prompts/<id>?from=...&to=...`（08 §4.1）の
  promptはIDで一意であり、期間はdetailの意味に寄与しない。戻り時のfilter復元はhistoryで足りる。
  `/prompts/<id>` へ純化を推奨（S3でMPAを採るなら戻る復元はブラウザ任せで自然解決）。
- **P3. cursor paginationはv1では過剰。** Window束縛のクエリは高々数千行で、ro接続ごとに
  スナップショット一貫性もある。LIMIT/OFFSET＋上限200で十分。opaque cursorは任意sort列との
  直積で複雑化するため、後日の最適化に回す。
- **P4. 認証cookieのライフサイクルが未定義。** 有効期限、server再起動を跨ぐ有効性（per-install
  secretで署名すれば可能）、cookie無し/期限切れアクセス時の401画面UX（「Metsuke.appから開き直す」
  案内）、bootstrap URLの一回性。§16の固定項目に追加すべき。
- **P5. 利用実測の分母定義がADR 0006/0007とずれる。** ADR 0006はstatusline自動生成を分母から除外し
  「手動CLI生成のみ」を数える。dashboardクリック起点の生成は明確に意図的利用であり、数えるべき。
  また提供面がdashboardへ移ると `view_html_generated`（ADR 0007の四半期棚卸し分母）は激減する。
  棚卸し分母を「dashboard view表示イベント＋CLI生成」へ更新することを設計に明記。
- **P6. Phase 4評価とStage 7の4週間実験が同時進行。** dashboard導入自体が行動regimeの変化であり、
  Stage 7実験の交絡になる。roadmapのプロトコルどおり **dashboard導入日を regime_event に記録**する
  ことを 8-5 に一行加える。
- **P7. `range=7d` 短縮URLの安定性を契約に含める。** canonical化（§4.1）は履歴の意味固定として正しいが、
  副作用として「常に直近7日」の生きたブックマークが作れない。`range=` URLを恒久的な入力面として
  保証する一文を足す（実装は既にそうなる設計だが、契約として明文化されていない）。
- **P8. HTTP server選定（§16-1）は標準ライブラリ推奨。** 本プロジェクトは外部依存ゼロを一貫させて
  おり、ASGIサーバの導入は依存・監査面積を増やす。この規模なら `ThreadingHTTPServer`＋接続毎の
  SQLite ro接続で性能予算を満たせる見込み。比較の判断基準に「新規ランタイム依存ゼロ」を明記。

## 5. 軽微

- URLは常に `127.0.0.1` リテラルを使う規範を明文化（`localhost` は環境により `::1` へ解決され、
  IPv4のみbindでは接続不能）。IPv6 loopbackへの併記bindも一考。
- 既定portの選定基準（8000/8080/3000等の常用開発ポート回避）と、port変更時にブックマークが壊れる
  旨を §16-2 に含める。
- `/healthz` は無認証で良いが、version等の情報を返さない現設計を維持すること（fingerprinting回避）。
- CORSヘッダ（`Access-Control-Allow-Origin`）を一切返さないことを明記（Host/Origin検査と対に
  なる構造的防御）。
- 8-1受入の「golden＋実台帳snapshot一致」はgolden基盤が既存（test_ingest/test_report等）で実現可能
  だが、値レベル照合は viewmodel 分離後でないとマークアップのパースになる。Phase 0 完了条件に
  「viewmodel値のgolden」を含めると順序が明確。
- timezone可変の既知限界（TZ変更後は同じ from/to URL が別データを指す）を §6.1 に一言。既知として
  受け入れる判断で良い。

## 6. Phase 1 着手時に固定すべき項目（08 §16への追加案)

既存の5項目に加えて:

6. SSR-MPA主体か静的shell＋JSON APIか（S3）。JS無効fallbackの目標/要件の確定。
7. reader接続方式: `query_only=1` vs `mode=ro`、busy_timeout、WAL sidecar欠損時の挙動（S2）。
8. 認証cookieの有効期限・再起動跨ぎ・401 UX・bootstrap URL一回性（P4）。
9. trace配信時の応答ヘッダCSP/XFO、オリジン分離の要否（S1 — Phase 2着手前でも可）。
10. trace cacheのサイズ/日数上限とdoctor表示（S5）。

## 7. 結論

採択して良い。ただし **S1（オリジン統合）と S2（mode=ro+WAL）は実装前に設計へ反映すべき**穴で、
放置するとそれぞれ「安全境界の暗黙の緩み」「本番で不定期に落ちるreader」として現れる。
S3（MPA比較）は実装量を大きく左右するため、Phase 1 の最初のプロトタイプで決めるのが安い。
F群は文書の正確さの問題で、修正は小さい。設計の骨格 — loopback限定・read-only・URL契約・
query model共有・trace遅延生成・撤退基準 — に変更を要する指摘は無かった。
