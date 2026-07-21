# metsuke — LLM利用量のお目付役

コーディングエージェント（Claude Code ほか）のLLM利用コストを、**行動変容によって**最適化する
個人向けシステム。数字を見せることではなく、**行動が変わること**（セッション切替・委任・
モデル選択・依頼の仕方）を目的関数に置く。

ローカルのトランスクリプトを原本として費用を再現し、作業の手が止まる場所へフィードバックを返す。
金額の算出も画面の描画もローカルで完結し、**参照のためにLLMを呼ばない**。

```
⛽$32.4/$150  $18/h  着地$74 | sess $2.1  ctx 34%
```

statuslineの1行（常時視界）、hooksによる介入（再開時のcold-cache警告・暴走ガード・
Stop時の領収書）、そしてブラウザで探索するローカルdashboardが主な接点になる。

## 対応状況

| 対象 | 状態 |
|---|---|
| Claude Code | 実装済み（トランスクリプト取込・statusline・hooks・CLI・dashboard・週次レポート） |
| Codex | **未対応**。データソース調査・設計とも未着手 |
| その他のLLM/エージェント | 未対応 |

複数プロバイダ対応は本リポジトリの主目的だが、現時点では設計方針であって実装ではない。

## 必要なもの

- **macOS**（launchd・通知・`open` に依存する。他OSは未対応）
- Python 3.12以上
- [uv](https://docs.astral.sh/uv/)、`jq`
- Claude Code

任意: `otelcol-contrib`（OTelタップ）、`restic`（暗号化バックアップ）。無ければその機能だけskipされる。

## 導入

```sh
git clone https://github.com/taku7777777/metsuke.git
cd metsuke
uv sync
./scripts/install.sh
metsuke sync
```

`install.sh` は実行前に**変更対象とバックアップ先を一覧表示**する。個別に見送れる。

```sh
./scripts/install.sh --skip-otel --skip-launchd   # 統合を選ぶ
./scripts/install.sh --with-git-hooks             # git hookは既定で導入しない
```

導入されるもの: Claude Code の hooks と statusline、launchd の定期ジョブ（取込・アーカイブ・
週次レポート・死活監視）、OTel collector 設定、`~/.metsuke/` 以下のデータ領域。
取り消しは `metsuke uninstall`（既定はdry-run）。

### 予算を設定する

予算は**既定で未設定**で、設定するまで予算警告は出ない。`~/.metsuke/config.env` に自分の上限を書く。

```sh
METSUKE_BUDGET_DAY=20
METSUKE_BUDGET_WEEK=100
METSUKE_BUDGET_MONTH=300
METSUKE_BUDGET_WARN_ENABLED=1
```

表示するUSDは**API単価換算の利用量**であり、定額購読の請求額そのものではない。

## 使う

```sh
metsuke today          # 今日いくら使ったか
metsuke week           # 直近7日の要約
metsuke explain <id>   # このプロンプトはなぜこの値段か
metsuke doctor         # 自己診断（取込の鮮度・計測の健全性）
```

期間やプロジェクトを変えながら探索したいときは、ブラウザで開くdashboardを使う。

```sh
metsuke dashboard serve --open
```

`127.0.0.1` だけで待ち受け、SQLiteを読み取り専用で参照する。概要・期間・推移・キャッシュ・分布の
5つの切り口を同じ期間フィルタで見比べ、高額なプロンプトやセッションの行からその内訳へ降りられる。

コマンドを覚えずに使いたい場合の入口はstatuslineとdashboardで、CLIはAI・自動化・障害調査のための
再現可能な口として残してある。

## もっと詳しく

| 知りたいこと | 場所 |
|---|---|
| 何を目的にどう設計したか | [docs/00-vision.md](docs/00-vision.md) |
| 全体構成とデータの流れ | [docs/01-architecture.md](docs/01-architecture.md) |
| 台帳のスキーマ | [docs/SCHEMA.md](docs/SCHEMA.md) / [docs/02-data-model.md](docs/02-data-model.md) |
| 指標の定義と読み方 | [docs/METRICS.md](docs/METRICS.md) |
| CLIリファレンス | [docs/cli/commands.md](docs/cli/commands.md) |
| 日々の運用と障害対応 | [docs/RUNBOOK.md](docs/RUNBOOK.md) |
| 単価表の更新 | [docs/PRICES.md](docs/PRICES.md) |
| dashboardの設計 | [docs/08-dashboard.md](docs/08-dashboard.md) |
| 主要な設計判断の記録 | [docs/adr/](docs/adr/) |

## 設計の要旨

1. **一次ソースはローカルトランスクリプト**。エージェント個体ID・ツールリンク・中断の記録という、
   OTel telemetry が構造的に落とす情報を埋められる上位ソース。
   ただし実測2週間〜30日で消えるため、**生ログの永久アーカイブが全ての前提**。
2. **事実だけを永続化し、導出金額は保存しない**。トークン×版管理単価表（SCD2）の読み出し時JOINで導出。
   単価校正・定義変更・分類器改良が**全履歴に遡及**する。ledger はいつでも捨てて再生できる。
3. **人間には迷わない入口、AIには構造化された口を提供する**。statusline/hooks で行動の瞬間に介入し、
   pull型の探索はローカルdashboardへ。CLI/SQL は AI・自動化・fallback として残す。

## データの扱い

- 台帳・アーカイブ・生成物は `~/.metsuke/` 配下に置き、**リポジトリには含まない**。
- プロンプト本文は台帳に保存せず、必要な箇所だけ読み出し時にredactionを通す。
- 外部送信はしない。dashboardは `127.0.0.1` のみで待ち受け、外部への接続をCSPで禁止する。
- バックアップ（`restic`）は明示的に設定したときだけ有効になる。

## 命名について

**目付**（めつけ）は江戸幕府の監察役。日常語としては「お目付役」で通り、
「見張る」ことがそのまま職務である点が、このシステムの責務と一致する。
`ccwatch` は Claude Code に特化した名前だったため、対応プロバイダの拡張にあわせて改名した。

| 世代 | 構成 | 状態 |
|---|---|---|
| `claude-code-monitoring` | OTel → Loki → Grafana | 退役 |
| `ccwatch` | ローカルトランスクリプト → ledger → statusline/hooks/CLI/dashboard | 移行元 |
| `metsuke` | 上記をプロバイダ非依存へ一般化 | 本リポジトリ |

## ライセンス

[MIT](LICENSE)
