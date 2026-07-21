# ADR 0001: 一次データソースをローカルトランスクリプトにする

日付: 2026-07-17 / 状態: 採択

## 決定

コスト計測の一次ソースを `~/.claude/projects/**` のトランスクリプトJSONL（subagents含む）とする。
OTel telemetry は補完タップ（query_source・effort・SDK cost_usd・リモートagent受け）に限定する。

## 根拠（2026-07-17 実機検証）

| OTel telemetry の構造的欠陥 | トランスクリプトでの解決 |
|---|---|
| エージェント個体IDが無い（同型並列サブエージェントを区別不能） | `subagents/agent-*.jsonl` の `agentId`＋`meta.json.toolUseId` で個体識別・親子リンクが**確定的** |
| APIリクエスト→ツール実行のリンク属性が無い（時刻推定のみ） | `tool_use.id` ↔ `tool_result.tool_use_id` 一致率100%＋`sourceToolAssistantUUID` |
| 中断リクエストはイベント自体が出ない | `interruptedMessageId`＋書込済みusage（保持される群とassistant行自体が無い群がある） |
| cache書込TTLの内訳が無い（2.0x単一仮定） | `usage.cache_creation` に **5m/1h別内訳が実在** → 正確な書込単価適用 |

さらに全assistantレコードに `requestId`・`usage`・`model` が100%存在し、`service_tier`/`speed`/
`inference_geo`/`server_tool_use` など課金乗算に必要な属性も揃う。

## 限界（この決定が引き受けるもの）

- **非公式フォーマット依存**が最大リスク → 生ログ永久アーカイブ＋寛容パーサ＋golden fixture＋
  第2推定器突合で「壊れても気づける・直せる・遡れる」（[05-risks](../05-risks.md)）。
- `query_source`（背景機能）と `effort` はトランスクリプトに**存在しない** → OTelタップを併設。
- 中断の出力側課金・リモートagentの詳細は取れない → 正直な限界として明記。

## 棄却した代替案

- **OTel一次（旧構成の延長）**: 上表の3欠陥が恒久化する。
- **公式トレースBeta（claude_code.*スパン）基盤**: Development段階・独自名前空間で不安定。
  トランスクリプトが持つ情報の劣化コピーを第二のストアに貯める二重化になる（四半期ウォッチのみ）。
