# koto_dental_HP
ホームページあるなし仕分け

江東区の歯科医院を一覧化し、歯科医師会の会員／非会員を付けたうえで、ホームページの状況で分類して CSV に出力します。

## 使い方

```bash
pip install -r requirements.txt
python koto_dental.py -o koto_dental_clinics.csv            # APIキー不要
PLACES_API_KEY=xxxx python koto_dental.py --places-cache places.json   # Google 検索も加える
```

| オプション | 内容 |
|---|---|
| `--places-cache p.json` | Places 検索結果を保存し、次回は API を呼ばずに再利用 |
| `--towns 豊洲,亀戸` | Places で検索する町名を絞る |
| `--input-csv list.csv` | Places の代わりに手元の CSV（医院名,公式URL,住所,電話）を使う |
| `--cache-dir .cache` | 厚生局PDF・医療情報ネットZIPの保存先（同じファイルは再ダウンロードしない） |
| `--no-kouseikyoku` / `--no-navi` / `--no-members` | 各データ源を使わない |
| `--web-urls path.csv` | Web検索結果のCSV（既定: `data/web_search_urls.csv`） |

## データ源と統合

| データ源 | 役割 |
|---|---|
| [関東信越厚生局 コード内容別医療機関一覧表（東京都・歯科）](https://kouseikyoku.mhlw.go.jp/kantoshinetsu/chousa/shitei.html) | 保険診療をしている全歯科診療所。医療機関番号の上2桁 `08` が江東区。一覧の母集団 |
| Google Places API（任意） | 医院名・住所・電話・公式URL |
| [江東区歯科医師会 会員診療所案内](https://kotoku-shikaishikai.com/clinics/) | 会員判定、HPリンク |
| [医療情報ネット オープンデータ（歯科診療所・施設票）](https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iryou/newpage_43373.html) | HPアドレスの補完 |
| `data/web_search_urls.csv` | 上記のどこにもURLが無かった医院を手作業でWeb検索した結果（URL空欄＝ポータル掲載のみ等で公式サイト無し）。電話番号で突合 |

- 突合の順：①電話番号一致 → ②住所（町名+丁目+番）一致かつ医院名が部分一致 → ③医院名一致かつ同じ町丁目
- 歯科医師会名簿に一致した医院が `会員`、それ以外が `非会員`
- どこにも一致しない行は、その出典だけの行として追加（`出典` 列で確認可）。厚生局一覧に無い行は `突合` 列に「移転/閉院/病院歯科の可能性」と注記
- 公式URLは Google → 歯科医師会名簿 → 医療情報ネット → Web検索 の順に、SNS・ポータルでないものを採用（`URL出典` 列）

## HP の解析

- 予約システム：リンク先ドメインで判定（EPARK / Apotool / デンタルアポ / ジコノート / Airリザーブ / LINE予約 など）。トップに無ければ自院内の「ネット予約」ページも1〜2枚たどる
- 制作会社：「Powered by／Supported by／制作：」の直後のリンク文字・ロゴ画像のalt・ドメイン、またはテキスト表記（CMS名は除外）
- コピーライト年・viewport・https・CMS

| 区分 | 条件 |
|---|---|
| HPなし | URL未登録 / SNS・ポータルのみ / リンク切れ（404・ドメインなし） |
| HPあり(解析不可) | サイトはあるが 403・429・5xx・SSLエラー等で取得できない（ブラウザで要確認） |
| 予約システムあり | 予約システムへのリンクを検出 |
| 昔風・簡易 | viewportなし・httpsなし・©が5年以上前・フレーム構成・本文が極端に少ない のいずれか |
| 今風 | 上記に当てはまらない |
