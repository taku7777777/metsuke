# ADR 0004: 正典ストアは SQLite、書き込みは spool 経由の single-writer に一本化する

日付: 2026-07-17 / 状態: 採択

## 決定

- 正典ストアは **SQLite（WALモード）** の単一ファイル `ledger.db`。
- **DBに書けるのは ingester ただ1プロセス**。他の全生産者（hooks・statusline・
  `metsuke mark/done/invoice`・AIアナリストの提案）は **spool/（追記専用NDJSON）に書くだけ**で、
  ingester が取り込む。
- 重い分析は DuckDB が ledger.db を **read-only ATTACH** して行う（正典にはしない）。
- ホットパス（statusline・hooks）はDBに触らず、ingester が原子的に書き出す `state.json` のみを読む。

## 根拠

1. **DuckDBのクロスプロセス排他は「単一writer＋複数reader」を満たさない**（批評パネルで実機検証:
   writerがread-write接続保持中は他プロセスのread-only openが失敗し、逆も起きる）。
   常時読み（statusline）・随時読み（CLI/AI）・定期書き（ingest）が共存する本システムでは
   SQLite WAL＋busy_timeout の枯れた並行モデルが唯一素直に成立する。
2. 5つの設計案のうち3案が「単一writer」を宣言しながら、marker・label・AI書込みで**設計内で
   原則を自壊**させていた（批評の共通指摘）。宣言でなく**経路の構造**（spool一本道）で保証する。
3. spool は追記専用ファイルなので hooks の <10ms・fail-open 制約と両立し、ingester 停止中も
   イベントを失わない（再開時に排水）。判断データ（marker等）も spool 原本が archive に残るため
   `metsuke rebuild` を生き残る。

## spool の規約（並行性と改竄耐性）

- **原子性**: 並行 append の行交錯を避けるため、writer 別ファイル（`spool/<component>-<pid>.ndjson`）
  とし、1行サイズに上限を設ける（超過分はファイル退避＋参照）。
- **特権レコードの出所検証**: `approve` / `invoice` / `price` 変更は spool からの生入力を受理せず、
  **`metsuke` の対話確認（TTY必須）経由のみ** ingester が受理する。コーディングエージェントに Bash を
  与える環境では「インジェクションされた別セッションのAIが偽 approve を spool に書く」経路が
  現実的に存在するため、承認ゲートの迂回を構造で塞ぐ。

## 棄却した代替案

- DuckDB正典＋read-only readers: 上記ロック問題。tick毎open/closeやParquetスナップショット参照で
  回避可能だが、複雑さの割に利得がない（DuckDBは分析エンジンとしてATTACHで十分）。
- 各プロセスが直接DBへ書く（WALなら技術的には可能）: 書込み経路が分散すると
  デデュープ・監査・rebuild保証が崩れる。構造で縛る。
