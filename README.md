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
- `--members-only` : Places を使わず、江東区歯科医師会の会員だけを解析（APIキー不要。非会員は識別できない）
- `--no-members` : 歯科医師会名簿との突合をしない

## 歯科医師会 会員／非会員

[江東区歯科医師会の会員診療所案内](https://kotoku-shikaishikai.com/clinics/)（地区別13ページ）から医院名・住所・電話・HPリンクを取り込み、Places の結果と突き合わせます。

- 突合の順：①電話番号一致 → ②住所（町名+丁目+番）一致かつ医院名が部分一致 → ③医院名一致かつ同じ町丁目
- 一致 → `会員`、一致なし → `非会員`。Places に出てこない会員も「名簿のみ」として行を追加
- 公式URLは Google の登録を優先し、無い（または SNS・ポータル）場合は名簿のリンクを使う（`URL出典` 列）

## 処理内容

1. 「江東区<町名> 歯科」を江東区の全町名（46）で Text Search（各最大60件）。place_id で重複排除し、住所が江東区・歯科のものだけ残す
2. 公式URLの HTML を取得し抽出
   - 予約システム：リンク先ドメインで判定（EPARK / Apotool / デンタルアポ / ジコノート / Airリザーブ / LINE予約 など）。トップに無ければ自院内の「ネット予約」ページも1〜2枚たどる
   - 制作会社：「Powered by／Supported by／制作：」の直後のリンク文字・ロゴ画像のalt・ドメイン、またはテキスト表記（CMS名は除外）
   - コピーライト年・viewport・https・CMS
3. 区分を判定し根拠と一緒に出力

| 区分 | 条件 |
|---|---|
| HPなし | URL未登録 / SNS・ポータルのみ / リンク切れ（404・ドメインなし） |
| HPあり(解析不可) | サイトはあるが 403・429・5xx・SSLエラー等で取得できない（ブラウザで要確認） |
| 予約システムあり | 予約システムへのリンクを検出 |
| 昔風・簡易 | viewportなし・httpsなし・©が5年以上前・フレーム構成・本文が極端に少ない のいずれか |
| 今風 | 上記に当てはまらない |
