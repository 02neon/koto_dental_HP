# koto_dental_HP
ホームページあるなし仕分け

江東区の歯科医院を Google Places API で一覧化し、ホームページの状況で分類して CSV に出力します。

## 使い方

```bash
pip install -r requirements.txt
export PLACES_API_KEY=xxxx   # Places API (New) を有効にしたキー
python koto_dental.py --places-cache places.json -o koto_dental_clinics.csv
```

- `--places-cache` : 検索結果を保存し、2回目以降は API を呼ばずに再利用（課金節約）
- `--towns 豊洲,亀戸` : 町名を絞って試す
- `--input-csv list.csv` : Places を使わず、`医院名,公式URL` 列の CSV を解析

## 処理内容

1. 「江東区<町名> 歯科」を江東区の全町名（46）で Text Search（各最大60件）。place_id で重複排除し、住所が江東区・歯科のものだけ残す
2. 公式URLの HTML を取得し抽出
   - 予約システム：リンク先ドメインで判定（EPARK / Apotool / デンタルアポ / ジコノート / Airリザーブ / LINE予約 など）。トップに無ければ自院内の「ネット予約」ページも1〜2枚たどる
   - 制作会社：フッターの「制作」「Produced by」「Designed by」等（CMS名は除外）
   - コピーライト年・viewport・https・CMS
3. 区分を判定し根拠と一緒に出力

| 区分 | 条件 |
|---|---|
| HPなし | URL未登録 / SNS・ポータルのみ / 取得不可 |
| 予約システムあり | 予約システムへのリンクを検出 |
| 昔風・簡易 | viewportなし・httpsなし・©が5年以上前・フレーム構成・本文が極端に少ない のいずれか |
| 今風 | 上記に当てはまらない |
