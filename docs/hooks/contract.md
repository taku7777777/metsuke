# hooks — 実行契約

Claude Code 本体と metsuke の境界。**どのイベントを登録し・各が何をし・何を返すか**の正。
介入の中身は [spec.md](spec.md)、要件は [requirements.md](requirements.md)。

実装: `scripts/install-claude-hooks.sh`（登録）＋ `scripts/hook-sensor.sh`（本体・単一スクリプト）。

## 1. 登録するイベント（7種）

`install-claude-hooks.sh` が `~/.claude/settings.json` へ冪等に書き込む。全イベントが
**同一スクリプト**を第1引数付きで呼ぶ（`hook-sensor.sh <EventName>`）。

| イベント | 役割 |
|---|---|
| `SessionStart` | spool へ記録のみ |
| `UserPromptSubmit` | **唯一の介入点**（§3）＋ spool 記録 |
| `Stop` | spool 記録 ＋ `metsuke sync` を起動（スロットルなし） |
| `PreCompact` | spool 記録のみ（compaction の事実を台帳へ） |
| `PostCompact` | `compacted-<sid>` marker を置く ＋ ctx警告 marker を **re-arm**（両方削除）＋ spool 記録 |
| `PostToolUse` | **spool へ記録しない**。`metsuke sync` を**30秒スロットル**で起動するだけ |
| `Notification` | spool へ記録のみ |

`statusLine` も同じインストーラが設定するが、既存値があれば**上書きせず警告してスキップ**する。

> **未登録**: `SubagentStop` / `SessionEnd` は登録していない。サブエージェントの個体情報は
> transcript 側（`subagents/agent-*.jsonl`）から取れ、セッション終了は最終 `Stop` で足りるため。

## 2. spool 書き出し

```
~/.metsuke/spool/hooks/<epoch_ns>-<pid>-<EventName>.ndjson
```
```json
{"metsuke_event":"<EventName>","metsuke_ts":<epoch_s>,"payload":<stdin JSON 全体>}
```

- statusline センサーと違い、**hook は stdin を丸ごと保存する**（`payload` に全体）。
  リダクションは**取込時**に ingester が行う（[SCHEMA.md](../SCHEMA.md)）。
- PostToolUse だけは書かない — 頻度が桁違いで、内容は transcript 側が持つため。

nudge の発火自体も spool へ落ちる:
```json
{"metsuke_event":"nudge_fired","metsuke_ts":…,"payload":{"rule":…,"session_id":…,"detail":{…}}}
```

## 3. UserPromptSubmit の出力契約

Claude Code が解釈する JSON を stdout に1行出す。**出すものが無ければ何も出さない**（空出力）。

| 出力 | 形 | 用途 |
|---|---|---|
| 人間向けメッセージ | `{"systemMessage":"…"}` | 警告全般。複数ルールが同時成立したら改行で連結 |
| モデル向け文脈 | `{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"…"}}` | compact_recovery |

送信停止の出力は持たない。予算100%到達時も `systemMessage` として通知し、同じターンの
`additionalContext` と共存する。

## 4. state/ marker の一覧

hook・statusline・ingester の間の受け渡しは全て `~/.metsuke/state/` のファイル存在で行う
（DB を介さない。[ADR 0004](../adr/0004-sqlite-single-writer-spool.md)）。

| ファイル | 置く側 | 消す側 | 意味 |
|---|---|---|---|
| `ctxwarn-<sid>` | statusline | UserPromptSubmit / PostCompact | context 水位が閾値超（中身は %） |
| `ctxwarned-<sid>` | UserPromptSubmit | PostCompact | この context 世代では警告済み |
| `compacted-<sid>` | PostCompact | UserPromptSubmit | 復旧注入が未消費 |
| `nudge-coldcache-<sid>-<last_ts>` | UserPromptSubmit | （残す） | この再開に対しては警告済み |
| `nudge-cap-<rule>-<date>` | UserPromptSubmit | （日付で自然失効） | 日次カウンタ／一回性フラグ |
| `sync-trigger.last` | PostToolUse | — | sync 起動の30秒スロットル |
| `sl-<sid>.last` | statusline | — | サンプル書出の15秒スロットル |
| `unlock-until` | 旧版の `metsuke unlock` | — | 互換のため残り得る旧marker。現行hookは参照しない |

`<sid>` は `tr -cd 'A-Za-z0-9._-'` でサニタイズ済みの値のみを使う。

## 5. state.json の読み方

hook は `~/.metsuke/state.json` を**読むだけ**（契約は [03-interfaces §4](../03-interfaces.md)）。

**鮮度ゲート**: `now - generated_at > 900`（15分）なら state.json 由来の判定を**全てスキップ**する。
古い数字で不要な予算警告を出さないようにする。
marker 由来の判定（ctx_warn・compact_recovery）は state.json に依存しないため鮮度に関わらず動く。

## 6. conversion の判定（ingester 側）

`ingest.py::_derive_nudges` が担う。hook は判定に一切関与しない。

1. `hook_event(kind='nudge_fired')` → `nudge` 表へ materialize（`INSERT OR IGNORE`）。
2. `fired_ts + 600 ≤ now` の未判定行について述語を評価（[spec.md §5](spec.md)）。
3. `outcome`（followed / not_followed / unknown）、`outcome_reason`、`observed_json`、
   `followed`（unknownならNULL）と `decided_ts = fired_ts + 600` を書く。
   沈黙を成功にせず、観測不能をunknownとしてconversion分母から除外する。

## 7. 規律（テストで強制）

| 規律 | 理由 |
|---|---|
| **<10ms**・bash + jq のみ | 送信のたびに走る。遅い hook は体験を壊す |
| **SQL・LLM 呼び出し禁止** | ホットパスの分離（`tests/test_hotpath.py::test_hotpath_discipline` が静的検査） |
| **fail-open**: 常に exit 0 | 計測の失敗で作業を妨げない。hookは送信停止を返さない |
| 書き込みは spool と `state/` marker のみ | 単一writer原則。DB反映は ingester の一本道 |
| `metsuke sync` は `nohup … &` で非同期起動 | hook の応答時間に ingest 時間を持ち込まない |
