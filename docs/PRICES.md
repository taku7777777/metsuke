# 価格データの更新

`src/metsuke/prices.json`が価格の唯一の原本である。`metsuke prices`は外部へ接続せず、
同梱データまたはledgerへ取り込まれた同梱データを表示するだけである。

- 出典はJSONの`source_url`、最終確認日は`checked_at`に記録する。
- 更新時は公式pricing文書とモデル・server tool・cache/batch/Fast/地域係数を照合する。
- 価格変更は既存行を上書きせず、`valid_to`で旧期間を閉じて新しい`valid_from`行を追加する。
- `version`と`checked_at`を更新し、`.venv/bin/python -m pytest -q tests/test_pricing.py`を実行する。
- 公開価格を確認できない、または同梱値が古い疑いがある場合は推測で補正しない。
  `metsuke prices`の出典・確認日を利用者へ示し、該当期間の金額を暫定値として扱う。

最終確認日: 2026-07-21。
