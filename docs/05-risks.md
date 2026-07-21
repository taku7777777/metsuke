# 05 — リスク・故障モード・正直な限界

## 主要リスクと対策

| リスク | 影響 | 対策 |
|---|---|---|
| **トランスクリプト形式のサイレント変更**（非公式仕様・最大リスク） | 取込が静かに欠ける/誤読する | 生アーカイブ＋寛容パーサ（未知はquarantine・未知フィールド素通し）＋`version`新値の初観測アサーション＋golden fixture回帰＋**第2推定器の日次突合**（意味的破壊を検知する唯一の自動カナリア）。壊れても「気づける・直せる・遡れる」 |
| 非公開サーフェス複数依存（transcripts / hooks / statusline / claude -p） | Claude Code更新1回で複数同時破損 | 同上＋`regime_event`（バージョン変化台帳）で破損時刻の特定を容易に。OTelタップ（公式仕様）が最低限の冗長系 |
| **hooks がプロンプト送信を阻害** | 監視ツールが本業を止める→ユーザーがhookを外す→計測全滅 | 全hook「spool追記 or state.json参照のみ・<10ms・fail-open」をテストで強制。SQL/LLM呼び出し禁止 |
| ingester の取込ゼロ（glob空振り・TCC権限剥奪） | 鮮度が新鮮なまま履歴が途切れる | 鮮度=「最後に正常パースしたイベント時刻」＋「hooks発火中にrequest増ゼロ」の独立検知＋`metsuke doctor`（OSアップデート後の実行を運用規約に） |
| ナッジ疲れ（banner blindness） | 介入が無視され J8 が死ぬ | ルール別cooldown・日次上限3発・conversion計測で棚卸し（自動ミュートはしない）。警告には必ず代替行動＋金額 |
| 週次アナリストの静かな停止（認証切れ等） | 閉ループの心臓が止まる | 欠報デッドマン（月曜朝チェック→プッシュ通知） |
| 単価表の陳腐化 | コストの系統誤差 | 未知モデル=NULL＋赤ドット。月次請求突合＋校正の二段分離（誤校正による全履歴汚染の防止） |
| デデュープ/分岐処理のバグ | 全数値の系統誤差 | golden fixture＋三点照合（自前計算 / statusline推計 / 月次実請求）。**実請求を取得できない契約形態では二点に縮退**（[Q1](06-open-questions.md)）— 両者に同じ向きで乗る系統誤差はこの構成では検出できない |
| アーカイブ＝PII・秘匿情報の資産化 | 漏洩・インジェクション | リダクション・パーミッション・暗号化バックアップ・AI最小権限（[ADR 0005](adr/0005-ai-analyst-least-privilege.md)） |
| ラップトップ喪失 | 全履歴喪失 | restic暗号化バックアップ＋リストア検証（Stage 5-6） |
| 過剰最適化（節約が手戻りを生む） | 総コスト・総時間で損 | `v_counter`（再試行率・revert率・モデル差し戻し率）を週次レポートの固定項目に |
| **ローカルdashboardのHTTP面**（DNS rebinding・XSS・誤bind） | PII閲覧・trace生成の悪用 | `127.0.0.1`固定、60秒bootstrap＋12時間cookie、Host/Origin/CSRF検査、CORSなし、外部通信禁止CSP、redaction、攻撃fixture（[ADR 0011](adr/0011-local-dashboard.md)） |
| **trace HTTP配信によるorigin統合** | trace内XSSから認証済みdashboardデータへ到達 | trace専用response CSP/XFOを必須化し、opaque-origin sandboxまたはcookie非共有originを出荷gateにする。不成立ならHTTP配信せず`file://`を維持 |
| dashboard readerとWAL sidecar/busyの競合 | 不定期な`SQLITE_CANTOPEN`、長時間待ち、誤書込み | `mode=rw`直後の`query_only=ON`、SQLite authorizer、250ms timeout。WAL/sidecar/write/DDL/ATTACHの回帰fixture |
| dashboardとCLIの集計ドリフト | 人間とAIが異なる数字で判断する | SQLを共有view modelへ分離し、同一WindowのCLI/静的HTML/dashboard SSRをgolden＋実台帳snapshotで照合 |

## 正直な限界（設計で消えないもの）

1. **計上 ≤ 実請求 は消えない**。中断の入力側は部分捕捉（usage保持群とassistant行不在群がある）、
   **出力側（thinking含む）は構造的に計測不能**。ストリーム開始前の中断はレコード自体が残らない。
   → 下限推計＋月次校正係数で「管理された誤差」として扱う。数字を過信しない旨を METRICS.md に明記。
2. **リモート/コンテナagentは低忠実度**。OTLP粒度のみで系譜・ツールリンク・中断が取れない。
   J5の精密分析はローカル限定。J2（総支出）には計上するが、ラップトップ不達時の欠測があり得る
   （リモート側スプールは 06 の調査項目）。
3. **APIエラー・リトライの課金セマンティクスは未解明**（06参照）。解明までは恒等式検査と
   未計上推計に説明不能な残差として現れうる。
4. **成果の帰属は粗い**。git commit は複数プロンプトに跨るのが常態で、$/成果は
   task_label×期間の水準比較まで（プロンプト単位の精密帰属は狙わない）。
5. **効果検証は準実験**。単一ユーザーに統制実験は不可能。マーカー前後のマクロ比較＋叙述であり、
   因果の証明ではない（意図的な割り切り）。
