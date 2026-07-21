# RUNBOOK — metsuke 運用手順書

> 対象: 運用者（= 利用者本人）。設計は [01-architecture](01-architecture.md)、
> データ契約は [SCHEMA.md](SCHEMA.md) / [METRICS.md](METRICS.md)。
> 原則: **アーカイブが唯一の入力** — ledger.db はいつでも捨てて `metsuke rebuild` で再生できる。
> 壊れて困るのは `~/.metsuke/archive/` と restic リポジトリ（と `state/restic.pass`）だけ。

`metsuke` = `<repo>/.venv/bin/metsuke`。PATHに無い場合は
`ln -s <repo>/.venv/bin/metsuke ~/.local/bin/metsuke` を推奨。

## 0. 定常運転（人間の作業）

| 頻度 | 作業 | 所要 |
|---|---|---|
| 常時 | statusline をちら見（⛽本日/予算・燃焼レート$/h・⏵実行中＋完了済み3件の色〈黄≥$3/赤≥$7.5〉と詳細リンク・🔥TTL残・⚠stale — 読み方は [statusline/spec.md](statusline/spec.md)） | 0秒 |
| 随時 | OS通知に反応: 暴走ガード（不要なら Esc）/ TTL事前通知（続けるなら今）/任意の高コスト通知 | — |
| 週次（月曜朝） | `~/.metsuke/reports/<週>.md` を読む（5分）→ 提案を `metsuke approve <name>` | 5分 |
| 月次 | console.anthropic.com の請求額を `metsuke invoice <YYYY-MM> <usd>` → `metsuke invoice --check <YYYY-MM>`（**請求額を確定できない定額サブスク環境ではスキップ** — [Q1](06-open-questions.md)） | 30秒 |
| 4週ごと | `metsuke ttl-review --days 28`。insufficient_dataなら延期、deprioritizeなら通知施策を縮小候補へ | 2分 |
| 四半期 | `metsuke roi --days 90` と nudge 棚卸し（conversion<10% のルールは文面改訂）、hook_eventの `trace_html_generated` 件数確認・`metsuke backup-verify` | 15分 |

## 1. 自動ジョブ一覧（launchd）

| label | 頻度 | 役割 | ログ |
|---|---|---|---|
| com.metsuke.archiver | 毎時 | トランスクリプト増分アーカイブ | logs/archiver.* |
| com.metsuke.tick | 5分 | sync（ingest＋state.json＋TTL事前通知の発火主体） | logs/tick.* |
| com.metsuke.analyst | 月曜 07:00 | 週次AIアナリスト（前ISO週レポート） | logs/analyst.* |
| com.metsuke.deadman | 月曜 09:30 | 欠報検知（レポート無ければ通知） | logs/deadman.* |
| com.metsuke.backup（任意） | 毎日 02:30 | 明示設定したresticリポジトリへの暗号化バックアップ | logs/backup.* |
| com.metsuke.otelcol | 常駐（KeepAlive） | ネイティブOTelタップ（バックグラウンド計測・規則7） | logs/otelcol.* |

再インストール: `<repo>/scripts/install-launchd.sh`（冪等。生成設定が同じで登録済みのjobは
停止せず、変更時だけbootout完了を待ってbootstrapを限定再試行する。
otelcol は `otelcol-contrib` 未導入ならスキップされる）。

## 2. 内部予算ガードの操作

- 予算は初期状態では未設定で、警告も無効。利用者自身の上限を設定した場合だけ50/80/100%で警告する。
  100%を超えてもプロンプト送信は**停止しない**。
- `metsuke unlock` は旧環境との互換用no-op。既存スクリプトを壊さないため残しているが、操作は不要。
- このUSDはAPI単価換算の利用量であり、定額購読の請求や利用上限ではない。購読環境では内部予算として扱う。
  購読/APIモードの自動分離は未実装である。
- 予算**金額**は `~/.metsuke/config.env` の `METSUKE_BUDGET_DAY` / `WEEK` / `MONTH` に
  自分の上限を設定し、`METSUKE_BUDGET_WARN_ENABLED=1` で警告を有効にする。
  止める場合は `0`。無効でも利用量と日次集計は継続し、未設定の予算額は表示しない。
  一時的なプロセス環境変数が中央設定より優先される。`metsuke config` で実効値を確認する。
  プロンプトの黄/赤閾値は `METSUKE_PROMPT_WARN_USD` / `METSUKE_PROMPT_CRIT_USD`。
  context絶対量の黄/赤閾値は `METSUKE_CONTEXT_WARN_TOKENS` /
  `METSUKE_CONTEXT_CRIT_TOKENS`（既定200000 / 500000）。
  OSの高コスト通知は既定OFFで、`METSUKE_RECEIPT_NOTIFY_ENABLED=1`で赤閾値以上だけを有効化する。
  env で変えられる閾値／ベタ書きの閾値の一覧は [hooks/spec.md §2](hooks/spec.md)
  （予算の段 50/80/100% と hook 側の日次上限はベタ書き）。
- 介入の発火条件・上限・文面の一覧は [hooks/spec.md](hooks/spec.md)。コマンドの詳細は
  [cli/commands.md](cli/commands.md)。

## 3. 施策（マーカー）の回し方

```
metsuke mark start --category <c> --hypothesis "..." --expected "..."   # 開始
metsuke mark end [marker_id]                                            # 終了（省略時=最新のopen）
metsuke mark verdict <marker_id> win|loss|inconclusive [--saving-usd N] # 勝敗（人間が打つ）
```
- open marker は常時 ≤2（帰属不能化を防ぐ）。判定はアナリストの前後比較提案を参考に人間が確定。
- 外生ショック（休暇・CLAUDE.md大改変・MCP追加）は `metsuke regime add <kind> <detail>` を即入力。

### 実タスクと成果の記録

```
metsuke task start "ログイン障害を修正" --category incident --goal "再発テストを追加"
# 以降のプロンプトはactive taskへ自動帰属
metsuke task finish --outcome completed --quality 4 --rework-minutes 10 --note "翌日確認済み"
metsuke sync
metsuke task status
```

- categoryは feature / incident / design / refactor / chore。比較は同category内で行う。
- 開始し忘れた場合は `metsuke task attach <task_id> --prompt <prompt_id|last>` で補正する。
- 4週間は完了・部分完了・中止を欠かさず記録し、終了タスクのoutcome付与率80%以上を先に目指す。
- TTL施策は `metsuke mark start --category ttl ...` で期間を明示し、28日後に `metsuke ttl-review` と
  regime_eventを確認して継続・優先度低下を人間が決める。

## 4. 障害対応

### statusline に ⚠stale が出た
点灯条件は [statusline/spec.md §6](statusline/spec.md)（state.json の `stale` フラグ、
または state.json 自体が15分以上更新されていない）。
1. `metsuke doctor` — どの検査が赤いか見る。
2. tick が死んでいる: `launchctl print gui/$(id -u)/com.metsuke.tick | head` → 無ければ §1 の再インストール。
3. 手動で疎通: `metsuke sync`（エラーが見える）。
4. spool が滞留（doctor ④）: sync ロック残留の可能性 → `ls ~/.metsuke/state/sync.lock`、
   プロセス不在なら削除して `metsuke sync`。

### 週次レポートが来ない（deadman 通知）
1. `tail -50 ~/.metsuke/logs/analyst.err.log` と `logs/analyst.out.log`。
2. よくある原因: max-turns/予算($10)/watchdog(20分) 到達 — `scripts/run-analyst.sh` の該当値を確認。
3. 手動再実行: `bash <repo>/scripts/run-analyst.sh`（対象は常に前ISO週）。

### 台帳が壊れた・数字が疑わしい
```
metsuke rebuild      # ledger.db を捨ててアーカイブ全量から再構築（判断データも復元される）
```
数十秒で完了。**rebuild で直らない破損はアーカイブ側** → §5 のリストアへ。

### VIEW 定義を直したのに数字が変わらない
`views.sql` の VIEW は **書き込み接続（`ledger.connect()`）を開いたときにだけ張り直される**。
`metsuke view` / `metsuke explain` / `metsuke trace` などの読み取り専用コマンドは再適用しないため、
`views.sql` を変更した直後の初回実行は**古い定義のまま**になりうる。
定義変更を反映するには `metsuke sync`（通常運転なら自動で走る）か `metsuke rebuild` を一度通すこと。
DBのコピーを使って検証する場合も同じ罠を踏むので、コピー側で一度書き込み接続を開いてから比較する。

### 通知が来ない
- macOS通知: システム設定 > 通知 > スクリプトエディタ（osascript）の許可。
- 目視テスト: `metsuke notify-test`。`macos=accepted`はosascriptが受理したことを示す。
  バナーが出ない場合は通知センターと集中モードも確認する。
- LaunchAgentからの失敗詳細: `tail -50 ~/.metsuke/logs/tick.err.log`。
- 携帯にも欲しい: `echo https://ntfy.sh/<秘密トピック名> > ~/.metsuke/state/ntfy.url && chmod 600 同`
  （ntfyアプリで同トピックを購読。トピック名は推測されないランダム文字列にする）。

## 5. バックアップとリストア

`~/.metsuke/traces/` は原本から再生成できるPII含有の導出物なのでバックアップ対象外。
対象は archive / reports / proposals / handoffs / `config.env`。外部共有・Artifactへのアップロードは禁止。`--open` の file:// URLは
ブラウザ履歴（同期を含む）に残る点に注意する。

- 実体: `METSUKE_RESTIC_REPO`で明示したresticリポジトリのみ。
  未設定時はクラウドストレージや同一端末のパスを推測せず、バックアップ機能と日次ジョブを無効にする。
- **パスワードは `~/.metsuke/state/restic.pass`。これを失うとバックアップは開けない — 
  パスワードマネージャに必ず控える。**
- 手動実行: `metsuke backup` / 検証: `metsuke backup-verify`（manifestとsegmentを復元し、展開後SHA一致を確認）。
- 全損からの復旧: 新マシンで restic restore → `~/.metsuke/archive/` を配置 → `metsuke rebuild`。

## 6. インストール一式（新環境・再設定）

対応OSはmacOS（arm64 / x86_64）。Python 3.12以上、`uv`、`jq`が必須で、installerが変更前に検査する。
OTel collectorは任意依存で、未導入時は理由を最後に表示してskipする。
OTLP receiverは`METSUKE_OTEL_PORT`（既定4319）からcollector設定とClaude側endpointの両方を生成し、
新規設定時にloopbackポートを確保できなければ変更前に停止する。

```
uv sync                              # .venv 構築
./scripts/install.sh                 # 中央設定・Claude hooks・OTel・launchd（冪等）
./scripts/install.sh --with-git-hooks  # 新規post-commit hookだけを明示的に導入
metsuke sync && metsuke doctor               # 初期取込と自己診断
```

実行前に変更対象と設定ファイルのバックアップ先を表示する。Claude hooks/statusline、OTel、launchdは
それぞれ `--skip-claude-hooks` / `--skip-statusline` / `--skip-otel` / `--skip-launchd` で除外できる。
Git hookは`--with-git-hooks`を指定した場合だけ導入し、既存hookがあるrepositoryは警告して変更しない。
別のgitルートは `--git-root <path>` で指定する。退役はまず `metsuke uninstall` で計画だけ確認し、
`metsuke uninstall --apply` でこのcheckoutが追加した設定だけを除去する。`--purge-data` は明示時のみで、
データを削除せずTrashへ移動する。

## 7. 退役条件と縮退

- 旧システム（claude-code-monitoring / Grafana+Loki）は **Stage 5-1（OTelタップ）稼働後に停止**
  （それまでは query_source/effort の観測が新系に無い）。
- ツール自身のROIが2四半期連続で赤字（`metsuke roi --days 90`）なら、週次アナリストを止めて
  Stage 2 構成（台帳＋statuslineの色/詳細導線＋緊急通知）へ縮退する（roadmap の撤退基準）。

## 8. ccwatch から metsuke への手動切替

この切替はメンテナンス窓でだけ行う。コードは旧環境変数・旧データディレクトリを読まず、
自動移行もしない。切替前に `~/.ccwatch/archive/` と
`~/.ccwatch/state/restic.pass` の退避を確認する。

1. `com.ccwatch.archiver` / `tick` / `analyst` / `deadman` を `launchctl bootout` する。
   有効なら `com.ccwatch.backup` / `otelcol` も停止する。
2. 旧プロセスが停止したことを確認してから `~/.ccwatch` を `~/.metsuke` へ移動する。
3. `~/.metsuke/config.env` のキー接頭辞を `CCWATCH_` から `METSUKE_` へ変更し、
   `METSUKE_HOME=$HOME/.metsuke` を確認する。旧キーは残さない。
4. このcheckoutで `uv sync` を実行し、旧 `ccw` のPATH設定を新しい `metsuke` コマンドへ
   差し替える。続けて `scripts/install-claude-hooks.sh` と `scripts/install-launchd.sh` を実行する。
5. `metsuke sync && metsuke doctor` を実行し、`com.metsuke.*` の登録、ログ、台帳件数を確認する。
   問題がなければ退避した旧plistと旧CLIリンクを片付ける。
