#!/usr/bin/env python3
"""江東区の歯科医院を一覧化し、ホームページの状況で分類して CSV に出力する。

手順
  1. 一覧づくり(下の順に突き合わせて1院1行に統合)
     - 関東信越厚生局「コード内容別医療機関一覧表(東京都・歯科)」PDF … 保険診療の全歯科診療所(母集団)
     - Google Places API(PLACES_API_KEY があれば)… 「江東区<町名> 歯科」を町名ごとに検索
     - 江東区歯科医師会「会員診療所案内」… 一致すれば会員、しなければ非会員
     - 医療情報ネット(厚労省)オープンデータ … HP アドレスの補完
  2. 各公式URLの HTML を取得し、予約システム・制作会社・コピーライト年・viewport・https を抽出
  3. 「HPなし / HPあり(解析不可) / 昔風・簡易 / 今風 / 予約システムあり」を判定し、根拠付きで CSV へ

使い方
  python koto_dental.py                         # APIキーなしでも動く(Google 検索だけ省略)
  PLACES_API_KEY=xxxx python koto_dental.py --places-cache places.json
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import warnings

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# ---------------------------------------------------------------- 設定

# 江東区の町名(住居表示・地番の町名)
KOTO_TOWNS = [
    "亀戸", "大島", "北砂", "南砂", "東砂", "新砂", "東陽", "木場", "冬木", "富岡",
    "門前仲町", "永代", "佐賀", "福住", "深川", "清澄", "白河", "三好", "平野", "常盤",
    "新大橋", "森下", "高橋", "千石", "石島", "海辺", "扇橋", "猿江", "住吉", "毛利",
    "千田", "塩浜", "枝川", "潮見", "豊洲", "東雲", "有明", "辰巳", "青海", "若洲",
    "新木場", "夢の島", "牡丹", "古石場", "越中島", "海の森",
]

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_FIELDS = ",".join([
    "places.id", "places.displayName", "places.formattedAddress",
    "places.nationalPhoneNumber", "places.websiteUri", "places.types",
    "places.businessStatus", "nextPageToken",
])

# 予約システム: 名称 → リンク先の URL(ホスト+パス)に含まれる文字列
RESERVATION_SYSTEMS = {
    "EPARK": ["epark.jp", "haisha-yoyaku.jp"],
    "Apotool": ["apotool.jp", "stransa.co.jp", "apotool"],
    "デンタルアポ": ["apodent.jp", "dentalapo", "dental-apo"],
    "ジコノート": ["jiconote"],
    "Airリザーブ": ["airrsv.net", "airreserve"],
    "LINE予約": ["lin.ee/", "line.me/r/ti/p", "page.line.me", "liff.line.me",
               "line.me/ti/p", "official.line"],
    "489Pro": ["489pro.com", "489ban.net"],
    "メディカル革命": ["mdrevo.jp", "byoin.jp"],
    "Coubic": ["coubic.com"],
    "RESERVA": ["reserva.be"],
    "SelectType": ["select-type.com"],
    "Dentaltray(ネット予約)": ["dentaltray", "dentnet", "dent-net"],
    "ネット受付(その他)": ["mrso.jp", "caloo.jp/reservations", "doctorsfile.jp/reserve",
                      "medicalnote", "atlink", "toyokumo", "jeeves", "reserve.ne.jp",
                      "yoyaku.", "reserve.", "reservation."],
}

# 公式HPではない(SNS・ポータル・地図)と見なすドメイン
NON_OFFICIAL_HOSTS = [
    "instagram.com", "facebook.com", "twitter.com", "x.com", "ameblo.jp", "line.me",
    "lin.ee", "tiktok.com", "youtube.com", "google.com", "goo.gl", "g.page",
    "epark.jp", "haisha-yoyaku.jp", "caloo.jp", "shika-town.com", "doctorsfile.jp",
    "byoinnavi.jp", "hotpepper.jp", "ekiten.jp", "qlife.jp", "fdoc.jp",
    "medicaldoc.jp", "minkou.jp", "haisha-guide.jp", "dentalclinic.jp",
    "localplace.jp", "jda.or.jp", "maps.app.goo.gl",
]

PRODUCER_PATTERNS = [
    r"(?:ホームページ|HP|ＨＰ|サイト|Web|WEB)?\s*制作\s*(?:会社)?\s*[:：/／|｜]\s*([^\n|｜©]{2,40})",
    r"(?:Produced|Designed|Created|Developed|Powered|Supported|Made|Site|Website|Web\s*design)\s+by\s*[:：]?\s*([^\n|｜©]{2,40})",
    r"(?:Design|Produce)\s*[:：]\s*([^\n|｜©]{2,40})",
]

CMS_NAMES = r"(WordPress|Webnode|Wix|Jimdo|STUDIO|ペライチ|Goope|グーペ|BiND|Shopify|Movable\s*Type|a-blog|Squarespace|WebSite\s*X5)"

COPYRIGHT_RE = re.compile(
    r"(?:©|&copy;|\(c\)|（c）|copyright|コピーライト)[^0-9\n]{0,60}"
    r"((?:19|20)\d{2})(?:\s*[-–—~〜～]\s*((?:19|20)\d{2}))?",
    re.I,
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
THIS_YEAR = dt.date.today().year
OLD_YEARS = 5  # コピーライト年がこれ以上古ければ「昔風」の根拠にする


# ---------------------------------------------------------------- 1. Places 検索

def search_places(api_key: str, towns: list[str], sleep: float = 0.2) -> list[dict]:
    """町名ごとに Text Search し、place_id で重複を除いた一覧を返す。"""
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": PLACES_FIELDS,
    }
    found: dict[str, dict] = {}
    for town in towns:
        query = f"江東区{town} 歯科"
        token = None
        for _page in range(3):  # Text Search は最大 60 件(20件 × 3ページ)
            body = {"textQuery": query, "languageCode": "ja", "regionCode": "JP",
                    "pageSize": 20}
            if token:
                body["pageToken"] = token
            r = requests.post(PLACES_URL, headers=headers, json=body, timeout=30)
            if r.status_code != 200:
                sys.exit(f"Places API エラー ({r.status_code}): {r.text[:300]}")
            data = r.json()
            new = 0
            for p in data.get("places", []):
                pid = p["id"]
                if pid in found:
                    found[pid]["検索町名"].add(town)
                    continue
                found[pid] = {
                    "place_id": pid,
                    "医院名": p.get("displayName", {}).get("text", ""),
                    "住所": p.get("formattedAddress", ""),
                    "電話": p.get("nationalPhoneNumber", ""),
                    "公式URL": p.get("websiteUri", ""),
                    "types": p.get("types", []),
                    "営業状況": p.get("businessStatus", ""),
                    "検索町名": {town},
                }
                new += 1
            token = data.get("nextPageToken")
            print(f"  {query}: {len(data.get('places', []))}件 (新規 {new})", file=sys.stderr)
            if not token:
                break
            time.sleep(sleep)
        time.sleep(sleep)

    clinics = []
    for c in found.values():
        if not is_koto_dental(c):
            continue
        c["検索町名"] = "・".join(sorted(c["検索町名"]))
        clinics.append(c)
    return clinics


def is_koto_dental(c: dict) -> bool:
    """江東区内の歯科か(検索結果に他区や歯科以外が混ざるため絞り込む)。"""
    if "江東区" not in c["住所"]:
        return False
    if c.get("営業状況") == "CLOSED_PERMANENTLY":
        return False
    name = c["医院名"]
    return "dentist" in c.get("types", []) or bool(
        re.search(r"歯科|歯医者|デンタル|dental|矯正|口腔|歯", name, re.I))


# ---------------------------------------------------------------- 2. HP 解析

def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_non_official(url: str) -> bool:
    h = host_of(url)
    return any(h == d or h.endswith("." + d) for d in NON_OFFICIAL_HOSTS)


def fetch(url: str, session: requests.Session) -> tuple[requests.Response | None, str]:
    last_err = ""
    for attempt in range(2):
        try:
            r = session.get(url, timeout=20, allow_redirects=True)
            if r.status_code in (429, 503) and attempt == 0:
                time.sleep(5)  # アクセス集中の一時拒否は少し待って取り直す
                continue
            return r, ""
        except requests.exceptions.SSLError:
            # 証明書不備のサイトは http で取り直す(https 判定は「なし」になる)
            if url.startswith("https://"):
                url = "http://" + url[len("https://"):]
                last_err = "SSL証明書エラー"
                continue
            return None, "SSL証明書エラー"
        except requests.RequestException as e:
            last_err = type(e).__name__
            time.sleep(1.5)
    return None, last_err


def decode_html(r: requests.Response) -> BeautifulSoup:
    # meta charset を尊重させるため bytes のまま渡す(Shift_JIS の古いサイト対策)
    return BeautifulSoup(r.content, "lxml")


def collect_links(soup: BeautifulSoup, base: str) -> list[tuple[str, str]]:
    """(絶対URL, アンカーテキスト) の一覧。a/iframe/script/form/area を対象にする。"""
    out = []
    for tag, attr in (("a", "href"), ("area", "href"), ("iframe", "src"),
                      ("script", "src"), ("form", "action"), ("link", "href")):
        for el in soup.find_all(tag):
            v = el.get(attr)
            if not v or v.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            text = el.get_text(" ", strip=True) if tag in ("a", "area") else ""
            if not text and tag in ("a", "area"):
                img = el.find("img")
                text = (img.get("alt") or "") if img else ""
            out.append((urljoin(base, v), text))
    # onclick="location.href='...'" 形式のボタン
    for el in soup.find_all(attrs={"onclick": True}):
        m = re.search(r"https?://[^'\"\s)]+", el["onclick"])
        if m:
            out.append((m.group(0), el.get_text(" ", strip=True)))
    return out


def detect_reservation(links: list[tuple[str, str]], site_host: str) -> tuple[list[str], list[str]]:
    systems, evidence = [], []
    for url, text in links:
        h = host_of(url)
        if not h or h == site_host:
            continue
        target = (h + urlparse(url).path).lower()
        for name, pats in RESERVATION_SYSTEMS.items():
            if any(p in target for p in pats):
                # LINE は友だち追加だけのことも多いので、予約文言かLINE予約専用ドメインのときに限る
                if name == "LINE予約" and not (
                        re.search(r"予約|reserve|booking", text + url, re.I)
                        or "liff.line.me" in target):
                    continue
                # 汎用パターン(reserve. など)は予約らしい文言がある場合のみ
                if name == "ネット受付(その他)" and not re.search(
                        r"予約|受付|reserve|booking", text + target, re.I):
                    continue
                if name not in systems:
                    systems.append(name)
                    evidence.append(f"{name}:{h}")
                break
    return systems, evidence


def internal_reservation_pages(links: list[tuple[str, str]], site_host: str) -> list[str]:
    pages = []
    for url, text in links:
        if host_of(url) != site_host:
            continue
        path = urlparse(url).path.lower()
        if (re.search(r"(ネット|web|ウェブ|オンライン|24時間).{0,4}予約|予約(はこちら|フォーム)", text, re.I)
                or re.search(r"reserv|yoyaku|booking|appointment", path)):
            u = url.split("#")[0]
            if u not in pages:
                pages.append(u)
    return pages


def footer_text(soup: BeautifulSoup) -> str:
    parts = [f.get_text("\n", strip=True) for f in soup.find_all("footer")]
    for el in soup.find_all(attrs={"id": re.compile("foot|copy", re.I)}):
        parts.append(el.get_text("\n", strip=True))
    for el in soup.find_all(attrs={"class": re.compile("foot|copy", re.I)}):
        parts.append(el.get_text("\n", strip=True))
    if not parts:
        # フッター要素が無い古いサイトはページ末尾を見る
        parts.append(soup.get_text("\n", strip=True)[-1500:])
    return "\n".join(parts)


PRODUCER_LEAD = re.compile(
    r"(produced|designed|created|developed|powered|supported|made|site|website|web\s*design)\s+by"
    r"|^\s*(ホームページ|HP|ＨＰ|サイト|web)?制作(会社)?\s*[:：/／|｜]?\s*$"
    r"|制作(会社)?\s*[:：]\s*$", re.I)
NOT_PRODUCER = re.compile(r"^(トップページ|top|page\s*top|home|ホーム|サイトマップ|プライバシー.*|個人情報.*)$", re.I)


def clean_producer(name: str) -> str:
    name = re.sub(r"\s+", " ", name or "").strip(" 　:：-–|｜/")
    name = re.split(r"\s{2,}|All rights|Copyright|©", name, flags=re.I)[0].strip()
    if len(name) < 2 or re.fullmatch(r"[\d\s.]+", name) or NOT_PRODUCER.match(name):
        return ""
    # 「Powered by WordPress」のような CMS 名は制作会社ではない
    if re.match(CMS_NAMES, name, re.I) or re.search(r"wordpress|wix|jimdo|goope|peraichi", name, re.I):
        return ""
    return name[:60]


def detect_producer(soups: list[BeautifulSoup], ftext: str) -> str:
    # 1) 「Powered by」「制作:」の直後にあるリンク(文字・ロゴ画像の alt・ドメイン)
    for soup in soups:
        for node in soup.find_all(string=PRODUCER_LEAD):
            a = node.parent if node.parent.name == "a" else node.find_next("a")
            if a is None:
                continue
            # 遠く離れたリンクを拾わないよう、間の文字数が少ないときだけ採用
            between = node.parent.get_text(" ", strip=True) if node.parent.name != "a" else ""
            if len(between) > 120:
                continue
            img = a.find("img")
            for cand in (a.get_text(" ", strip=True), img.get("alt") if img else "",
                         a.get("title", ""), host_of(a.get("href", "")).removeprefix("www.")):
                name = clean_producer(cand)
                if name and not PRODUCER_LEAD.fullmatch(name):
                    return name
    # 2) テキストのみの表記(「制作：○○」「Designed by ○○」)
    for pat in PRODUCER_PATTERNS:
        for m in re.finditer(pat, ftext, re.I):
            name = clean_producer(m.group(1))
            if name:
                return name
    return ""


def detect_cms(soup: BeautifulSoup, html: str) -> str:
    gen = soup.find("meta", attrs={"name": re.compile("^generator$", re.I)})
    if gen and gen.get("content"):
        return gen["content"][:40]
    for name, pat in (("WordPress", "wp-content"), ("Wix", "wixstatic"),
                      ("Jimdo", "jimdo"), ("Studio", "studio.design"),
                      ("ペライチ", "peraichi"), ("Goope", "goope.jp"),
                      ("Shopify", "cdn.shopify")):
        if pat in html:
            return name
    return ""


def detect_copyright(text: str) -> int | None:
    years = []
    for m in COPYRIGHT_RE.finditer(text):
        years += [int(y) for y in m.groups() if y]
    years = [y for y in years if 1990 <= y <= THIS_YEAR + 1]
    return max(years) if years else None


def analyze(url: str, session: requests.Session) -> dict:
    res = {"最終URL": "", "HTTPステータス": "", "取得エラー": "", "予約システム": "",
           "予約リンク": "", "制作会社": "", "CMS": "", "コピーライト年": "",
           "viewport": "", "https": "", "フレーム": "", "本文文字数": ""}
    if not url:
        return res
    if not re.match(r"https?://", url):
        url = "http://" + url
    r, err = fetch(url, session)
    if r is None:
        res["取得エラー"] = err
        return res
    res["最終URL"] = r.url
    res["HTTPステータス"] = r.status_code
    if r.status_code >= 400:
        res["取得エラー"] = f"HTTP {r.status_code}"
        return res

    soup = decode_html(r)
    # <meta http-equiv="refresh"> による転送を1回だけ追う
    meta = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
    if meta and "url=" in (meta.get("content") or "").lower():
        nxt = urljoin(r.url, meta["content"].split("=", 1)[1].strip(" '\""))
        r2, _ = fetch(nxt, session)
        if r2 is not None and r2.status_code < 400:
            r, soup = r2, decode_html(r2)
            res["最終URL"] = r.url

    # フレーム構成の古いサイトは中身のフレームも見る
    frames = soup.find_all(["frame", "iframe"]) if soup.find("frameset") else []
    soups = [(soup, r.url)]
    if frames:
        res["フレーム"] = "あり"
        for fr in frames[:3]:
            if fr.get("src"):
                fu = urljoin(r.url, fr["src"])
                rf, _ = fetch(fu, session)
                if rf is not None and rf.status_code < 400:
                    soups.append((decode_html(rf), rf.url))

    html = "\n".join(str(s) for s, _ in soups)
    text = "\n".join(s.get_text("\n", strip=True) for s, _ in soups)
    site_host = host_of(r.url)
    links = [l for s, b in soups for l in collect_links(s, b)]

    systems, evidence = detect_reservation(links, site_host)
    if not systems:
        # 「ネット予約」ボタンが自院内の予約ページに飛び、そこに予約システムが埋め込まれている場合
        for page in internal_reservation_pages(links, site_host)[:2]:
            rp, _ = fetch(page, session)
            if rp is None or rp.status_code >= 400:
                continue
            sp = decode_html(rp)
            s2, e2 = detect_reservation(collect_links(sp, rp.url), site_host)
            if s2:
                systems = s2
                evidence = [f"{e}(予約ページ {urlparse(rp.url).path} 経由)" for e in e2]
                break
    ftext = "\n".join(footer_text(s) for s, _ in soups)
    year = detect_copyright(ftext) or detect_copyright(text)

    res.update({
        "予約システム": " / ".join(systems),
        "予約リンク": " ".join(evidence),
        "制作会社": detect_producer([x for x, _ in soups], ftext),
        "CMS": detect_cms(soup, html),
        "コピーライト年": year or "",
        "viewport": "あり" if any(s.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
                                for s, _ in soups) else "なし",
        "https": "あり" if r.url.startswith("https://") else "なし",
        "本文文字数": len(text),
    })
    return res


# ---------------------------------------------------------------- 1b. 公的な一覧・歯科医師会名簿

KDA_INDEX = "https://kotoku-shikaishikai.com/clinics/"
KOUSEIKYOKU_PAGE = "https://kouseikyoku.mhlw.go.jp/kantoshinetsu/chousa/shitei.html"
NAVI_PAGE = "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iryou/newpage_43373.html"
KOTO_KOUSEIKYOKU_CODE = "08"   # 関東信越厚生局の医療機関番号で江東区を表す上2桁
KOTO_CITY_CODE = "108"         # 全国地方公共団体コード 13108 の市区町村部分


def download(url: str, session: requests.Session, cache_dir: str) -> str:
    """URL をキャッシュディレクトリに保存してパスを返す(同名ファイルがあれば再利用)。"""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, os.path.basename(urlparse(url).path))
    if not os.path.exists(path):
        r = session.get(url, timeout=120)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    return path


def find_link(session: requests.Session, page: str, pattern: str) -> str:
    r = session.get(page, timeout=30)
    r.raise_for_status()
    for a in decode_html(r).find_all("a", href=True):
        if re.search(pattern, a["href"]):
            return urljoin(page, a["href"])
    sys.exit(f"{page} に {pattern} のリンクが見つかりません(ページ構成が変わった可能性)")


def fetch_kouseikyoku(session: requests.Session, cache_dir: str) -> list[dict]:
    """関東信越厚生局「コード内容別医療機関一覧表(東京都・歯科)」PDF から江東区分を取り出す。

    保険診療をしている歯科診療所はすべて載るため、区内の歯科医院の母集団として使う。
    """
    import pdfplumber
    import pypdf

    url = find_link(session, KOUSEIKYOKU_PAGE, r"shitei_shika_tokyo.*\.pdf$")
    path = download(url, session, cache_dir)
    code_re = re.compile(rf"(?<!\d){KOTO_KOUSEIKYOKU_CODE},\d{{4}},\d")
    # 1) 江東区のレコードがあるページを高速に特定
    reader = pypdf.PdfReader(path)
    pages = [i for i, pg in enumerate(reader.pages) if code_re.search(pg.extract_text() or "")]
    if not pages:
        sys.exit("厚生局PDFに江東区の医療機関が見つかりません")
    # 2) 座標で列を切り分けて1件ずつ組み立てる
    #    列の x 位置: 項番+医療機関番号 <145 / 名称 145-250 / 所在地 250-370 / 電話 370-455
    recs, cur = [], None
    with pdfplumber.open(path) as pdf:
        for pi in range(max(0, pages[0] - 1), min(len(pdf.pages), pages[-1] + 2)):
            lines: dict[int, list] = {}
            for w in pdf.pages[pi].extract_words():
                if w["top"] > 122:  # 見出し行より下
                    lines.setdefault(round(w["top"]), []).append(w)
            for top in sorted(lines):
                row = sorted(lines[top], key=lambda w: w["x0"])
                m = re.fullmatch(r"(\d+?)(\d\d),(\d{4}),(\d)", row[0]["text"]) if row[0]["x0"] < 145 else None
                if m:
                    cur = {"code": m.group(2), "no": f"{m.group(2)},{m.group(3)},{m.group(4)}",
                           "name": [], "addr": [], "phone": []}
                    recs.append(cur)
                    row = row[1:]
                if cur is None:
                    continue
                for w in row:
                    x = w["x0"]
                    col = "name" if 145 <= x < 250 else "addr" if 250 <= x < 370 else \
                          "phone" if 370 <= x < 455 else None
                    if col:
                        cur[col].append(w["text"])
    out = []
    for r in recs:
        if r["code"] != KOTO_KOUSEIKYOKU_CODE:
            continue
        ph = re.search(r"\d{2,4}-\d{2,4}-\d{3,4}", " ".join(r["phone"]))
        addr = re.sub(r"^〒\d{3}[－-]\d{4}", "", "".join(r["addr"]))
        out.append({"医院名": unicodedata.normalize("NFKC", "".join(r["name"])),
                    "住所": unicodedata.normalize("NFKC", addr),
                    "電話": ph.group(0) if ph else "", "医療機関番号": r["no"]})
    print(f"厚生局 保険医療機関一覧: 江東区 {len(out)} 院 ({os.path.basename(path)})", file=sys.stderr)
    return out


def fetch_navi(session: requests.Session, cache_dir: str) -> list[dict]:
    """医療情報ネット(ナビイ)オープンデータの歯科診療所・施設票から江東区分と HP アドレスを取る。"""
    import io
    import zipfile

    url = find_link(session, NAVI_PAGE, r"03-1_dental_facility_info.*\.zip$")
    path = download(url, session, cache_dir)
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.endswith(".csv"))
        text = z.read(name).decode("utf-8-sig")
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("都道府県コード") == "13" and row.get("市区町村コード") == KOTO_CITY_CODE \
                and "保健所" not in row["正式名称"]:
            out.append({"医院名": row["正式名称"], "住所": unicodedata.normalize("NFKC", row["所在地"]),
                        "電話": "", "ナビURL": (row.get("案内用ホームページアドレス") or "").strip(),
                        "ナビID": row["ID"]})
    print(f"医療情報ネット: 江東区 {len(out)} 院 ({os.path.basename(path)})", file=sys.stderr)
    return out


def fetch_members(session: requests.Session) -> list[dict]:
    """江東区歯科医師会「会員診療所案内」の地区別ページから会員一覧を取る。"""
    r = session.get(KDA_INDEX, timeout=30)
    r.raise_for_status()
    idx = decode_html(r)
    districts = [(a["href"], a.get_text(" ", strip=True).split(" ")[0])
                 for a in idx.select("a.vk_button_link") if a.get("href")]
    if not districts:
        sys.exit("歯科医師会ページの地区リンクが見つかりません(ページ構成が変わった可能性)")
    members = []
    for url, district in districts:
        rr = session.get(url, timeout=30)
        rr.raise_for_status()
        for tr in decode_html(rr).select("table tr"):
            tds = tr.find_all("td")
            if len(tds) < 3 or tds[0].get_text(strip=True) == "歯科医院名":
                continue
            a = tds[0].find("a", href=True)
            members.append({
                "医院名": tds[0].get_text(" ", strip=True),
                "住所": tds[1].get_text(" ", strip=True),
                "電話": tds[2].get_text(" ", strip=True).replace("tel:", "").strip(),
                "名簿URL": a["href"].strip() if a else "",
                "地区": district,
            })
        time.sleep(0.3)
    print(f"歯科医師会 会員: {len(members)} 院({len(districts)} 地区)", file=sys.stderr)
    return members


# ---------------------------------------------------------------- 1c. 突合

KANJI_NUM = {c: i for i, c in enumerate("〇一二三四五六七八九")}


def kanji_to_int(k: str) -> int:
    if "十" in k:
        a, _, b = k.partition("十")
        return (KANJI_NUM.get(a, 1) if a else 1) * 10 + (KANJI_NUM.get(b, 0) if b else 0)
    return int("".join(str(KANJI_NUM[c]) for c in k))


def norm_phone(p: str) -> str:
    d = re.sub(r"\D", "", unicodedata.normalize("NFKC", p or ""))
    return d[-10:] if len(d) >= 10 else d


ITAIJI = str.maketrans("髙﨑栁邉邊齋齊濵濱嶋德惠廣澤櫻ッ", "高崎柳辺辺斎斉浜浜島徳恵広沢桜ツ")
PUA = re.compile("[-]")  # PDF の外字(𠮷・邊など)は私用領域の文字として出てくる


def name_core(n: str) -> str:
    n = unicodedata.normalize("NFKC", n or "").lower().translate(ITAIJI).replace("𠮷", "吉")
    n = re.sub(r"(医療法人|社会医療法人|\(医\)|医\))(社団|財団)?\S*?会", "", n)
    n = re.sub(r"^(医療法人|社会医療法人)(社団|財団)?", "", n)
    n = re.sub(r"[\s・･\-ー‐()（）「」.、]", "", n)
    return re.sub(r"(歯科医院|歯科クリニック|デンタルクリニック|デンタルオフィス|歯科診療所|"
                  r"矯正歯科|小児歯科|歯科|歯医者|クリニック|医院|診療所|dentalclinic|dental)", "", n)


def names_match(a: str, b: str, exact: bool = False) -> bool:
    """医院名の中核部分が一致(exact=False なら一方が他方を含む)。外字は任意の1文字として扱う。"""
    ca, cb = name_core(a), name_core(b)
    if not ca or not cb:
        return False
    if PUA.search(ca) or PUA.search(cb):
        pa, pb = (".".join(re.escape(x) for x in PUA.split(c)) for c in (ca, cb))
        if exact:
            return bool(re.fullmatch(pa, cb) or re.fullmatch(pb, ca))
        return bool(re.search(pa, cb) or re.search(pb, ca))
    return ca == cb if exact else (ca in cb or cb in ca)


def addr_parts(a: str) -> tuple[str, str, str, str]:
    """住所を (町名, 丁目, 番, 号) に分ける。「森下二丁目14番5号」「森下2-14-5」どちらも可。"""
    a = unicodedata.normalize("NFKC", a or "")
    a = re.sub(r"([〇一二三四五六七八九十]+)丁目", lambda m: f"{kanji_to_int(m.group(1))}丁目", a)
    a = re.sub(r"[−‐―ーｰ–—]", "-", a)
    a = re.sub(r"(\d+)丁目", r"\1-", a).replace("番地", "-").replace("番", "-")
    m = re.search(r"江東区\s*([^\d\s]+?)\s*(\d+)\s*-\s*(\d+)(?:\s*-\s*(\d+))?", a)
    return (m.group(1), m.group(2), m.group(3), m.group(4) or "") if m else ("", "", "", "")


def addr_key(a: str) -> str:
    """「江東区森下二丁目14番5号」「森下2-14-5」→「森下2-14」(町名+丁目+番)。"""
    t, c, b, _ = addr_parts(a)
    return f"{t}{c}-{b}" if t else ""


def find_match(c: dict, pool: list[dict]) -> tuple[dict | None, str]:
    ph = norm_phone(c.get("電話", ""))
    if len(ph) >= 9:
        for m in pool:
            if norm_phone(m.get("電話", "")) == ph:
                return m, "電話一致"
    ak, name = addr_key(c.get("住所", "")), c.get("医院名", "")
    for m in pool:
        if ak and ak == addr_key(m["住所"]) and names_match(name, m["医院名"]):
            return m, "住所+医院名一致"
    town = ak.split("-")[0]
    for m in pool:
        if town and town == addr_key(m["住所"]).split("-")[0] and names_match(name, m["医院名"], exact=True):
            return m, "医院名+町丁目一致"
    for m in pool:
        if len(name_core(name)) >= 4 and names_match(name, m["医院名"], exact=True):
            return m, "医院名一致(住所表記違い)"
    full = addr_parts(c.get("住所", ""))
    for m in pool:
        both_phone = len(ph) >= 9 and len(norm_phone(m.get("電話", ""))) >= 9
        if full[3] and full == addr_parts(m["住所"]) and not both_phone:
            return m, "住所一致のみ(名称違い・要確認)"
    return None, ""


def dedupe(rows: list[dict]) -> list[dict]:
    """同じ医院が名簿に2回載っている場合などを1件にまとめる(電話+医院名で判定)。"""
    seen, out = set(), []
    for r in rows:
        k = (norm_phone(r.get("電話", "")), name_core(r.get("医院名", "")))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def absorb(clinics: list[dict], others: list[dict], label: str, fields: list[str],
           on_match=None, add_unmatched: bool = True) -> None:
    """others の各行を clinics に突き合わせ、一致すれば fields を写し、無ければ新しい行として足す。"""
    taken: set[int] = set()
    for o in others:
        pool = [c for c in clinics if id(c) not in taken]
        m, how = find_match(o, pool)
        if m:
            taken.add(id(m))
            for f in fields:
                if o.get(f) and not m.get(f):
                    m[f] = o[f]
            if PUA.search(m["医院名"]) and not PUA.search(o["医院名"]):
                m["医院名"] = o["医院名"]  # 厚生局PDFの外字化けを読める名前に
            m["出典"].append(label)
            m.setdefault("突合", []).append(f"{label}:{how}")
            if on_match:
                on_match(m, o)
        elif add_unmatched:
            row = {k: o.get(k, "") for k in ("医院名", "住所", "電話", *fields)}
            row["出典"] = [label]
            if clinics and clinics[0]["出典"][0] == "厚生局":
                row["突合"] = [f"{label}のみ(厚生局の保険医療機関一覧に無し・移転/閉院/病院歯科の可能性)"]
            if on_match:
                on_match(row, o)
            clinics.append(row)
            taken.add(id(row))


def mark_member(c: dict, m: dict) -> None:
    c.update({"歯科医師会": "会員", "地区": m["地区"], "名簿の医院名": m["医院名"]})


def pick_official_url(c: dict) -> None:
    """Google → 歯科医師会名簿 → 医療情報ネット の順に、SNS/ポータルでない URL を採用する。"""
    cands = [("Google", c.get("GoogleURL", "")), ("歯科医師会名簿", c.get("名簿URL", "")),
             ("医療情報ネット", c.get("ナビURL", "")), ("Web検索", c.get("WebURL", ""))]
    cands = [(s, u) for s, u in cands if u]
    best = next(((s, u) for s, u in cands if not is_non_official(u)), cands[0] if cands else ("", ""))
    c["URL出典"], c["公式URL"] = best


# ---------------------------------------------------------------- 3. 分類

def classify(c: dict) -> tuple[str, str]:
    url = c.get("公式URL", "")
    if not url:
        why = f"公式URLの登録なし({'・'.join(x for x in c.get('出典', []) if x != 'Web検索') or '-'} のいずれにも無し)"
        if c.get("Webメモ"):
            why += f" / Web検索: {c['Webメモ']}"
        return "HPなし", why
    if is_non_official(url):
        return "HPなし", f"登録URLがSNS/ポータル({host_of(url)})"
    err = c.get("取得エラー")
    if err:
        # 404・DNS 不明はリンク切れ。403/429/5xx/SSL などはサイト自体はあるがこちらから見られない
        if re.search(r"HTTP (404|410)|ConnectionError|NameResolution", err):
            return "HPなし", f"URL登録はあるがリンク切れ({err})"
        return "HPあり(解析不可)", f"サイトはあるが取得を拒否/失敗({err})。ブラウザで要確認"
    if c.get("最終URL") and is_non_official(c["最終URL"]):
        return "HPなし", f"転送先がSNS/ポータル({host_of(c['最終URL'])})"

    if c.get("予約システム"):
        return "予約システムあり", f"予約リンク検出: {c['予約リンク']}"

    old, new = [], []
    (new if c["viewport"] == "あり" else old).append(f"viewport{c['viewport']}")
    (new if c["https"] == "あり" else old).append(f"https{c['https']}")
    y = c.get("コピーライト年")
    if y:
        if THIS_YEAR - int(y) >= OLD_YEARS:
            old.append(f"©{y}(古い)")
        else:
            new.append(f"©{y}")
    else:
        new.append("©年なし") if not old else old.append("©年なし")
    if c.get("フレーム"):
        old.append("フレーム構成")
    if c.get("本文文字数") and int(c["本文文字数"]) < 400:
        old.append(f"本文{c['本文文字数']}字(簡易)")

    if old:
        return "昔風・簡易", " / ".join(old + new)
    return "今風", " / ".join(new)


# ---------------------------------------------------------------- main

COLUMNS = ["区分", "判定根拠", "歯科医師会", "地区", "医院名", "名簿の医院名", "住所", "電話",
           "公式URL", "URL出典", "最終URL", "予約システム", "予約リンク", "制作会社", "コピーライト年",
           "viewport", "https", "CMS", "フレーム", "本文文字数", "HTTPステータス", "取得エラー",
           "出典", "突合", "医療機関番号", "ナビID", "place_id"]


def load_web_urls(path: str) -> list[dict]:
    """手作業の Web 検索で見つけた公式URL(URL 空欄=見つからなかった)を読み込む。"""
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r.update({"住所": "", "WebURL": r.get("URL", "").strip(), "Webメモ": r.get("メモ", "").strip()})
    print(f"Web検索結果: {len(rows)} 件 ({path})", file=sys.stderr)
    return rows


def load_input_csv(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["GoogleURL"] = r.pop("公式URL", "")
        for k in ("医院名", "住所", "電話"):
            r.setdefault(k, "")
    return rows


def load_places(args) -> list[dict] | None:
    if args.input_csv:
        return load_input_csv(args.input_csv)
    if args.places_cache and os.path.exists(args.places_cache):
        with open(args.places_cache, encoding="utf-8") as f:
            clinics = json.load(f)
        print(f"Places: キャッシュから {len(clinics)} 件", file=sys.stderr)
    else:
        key = os.environ.get("PLACES_API_KEY")
        if not key:
            print("Places: PLACES_API_KEY が無いため Google 検索はスキップ", file=sys.stderr)
            return None
        towns = args.towns.split(",") if args.towns else KOTO_TOWNS
        print(f"Places API で {len(towns)} 町を検索中...", file=sys.stderr)
        clinics = search_places(key, towns)
        if args.places_cache:
            with open(args.places_cache, "w", encoding="utf-8") as f:
                json.dump(clinics, f, ensure_ascii=False, indent=1)
    for c in clinics:
        if "公式URL" in c:
            c["GoogleURL"] = c.pop("公式URL")
    return clinics


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output", default="koto_dental_clinics.csv")
    ap.add_argument("--places-cache", help="Places 検索結果の JSON。存在すれば再利用し、無ければ保存する")
    ap.add_argument("--input-csv", help="Places の代わりに、この CSV(医院名,公式URL,住所,電話 列)を使う")
    ap.add_argument("--towns", help="Places で検索する町名をカンマ区切りで指定(既定: 江東区の全町名)")
    ap.add_argument("--cache-dir", default=".cache", help="厚生局PDF・医療情報ネットZIPの保存先")
    ap.add_argument("--no-kouseikyoku", action="store_true", help="厚生局の保険医療機関一覧を使わない")
    ap.add_argument("--no-navi", action="store_true", help="医療情報ネットのオープンデータを使わない")
    ap.add_argument("--no-members", action="store_true", help="歯科医師会名簿との突合をしない")
    ap.add_argument("--web-urls", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                       "data", "web_search_urls.csv"),
                    help="Web検索で調べた公式URLの CSV(医院名,電話,URL,メモ)")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})
    adapter = requests.adapters.HTTPAdapter(pool_connections=args.workers, pool_maxsize=args.workers)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    # 1. 一覧づくり: 厚生局(母集団) ← Google ← 歯科医師会名簿 ← 医療情報ネット の順に突き合わせて統合
    clinics: list[dict] = []
    if not args.no_kouseikyoku:
        for c in fetch_kouseikyoku(session, args.cache_dir):
            c["出典"] = ["厚生局"]
            clinics.append(c)
    places = load_places(args)
    if places:
        absorb(clinics, places, "Google", ["GoogleURL", "place_id", "検索町名"])
    if not args.no_members:
        absorb(clinics, dedupe(fetch_members(session)), "歯科医師会", ["名簿URL"], on_match=mark_member)
    if not args.no_navi:
        absorb(clinics, fetch_navi(session, args.cache_dir), "医療情報ネット", ["ナビURL", "ナビID"])
    web = load_web_urls(args.web_urls)
    if web:
        absorb(clinics, web, "Web検索", ["WebURL", "Webメモ"], add_unmatched=False)
    for c in clinics:
        c.setdefault("歯科医師会", "非会員" if not args.no_members else "")
        pick_official_url(c)
    print(f"対象: {len(clinics)} 院", file=sys.stderr)

    # 2. HP 解析(同じURLは1回だけ取得)
    urls = sorted({c["公式URL"] for c in clinics if c.get("公式URL") and not is_non_official(c["公式URL"])})
    print(f"HP取得: {len(urls)} URL", file=sys.stderr)
    with ThreadPoolExecutor(args.workers) as ex:
        results = dict(zip(urls, ex.map(lambda u: analyze(u, session), urls)))

    # 3. 分類・出力
    rows = []
    for c in clinics:
        c.update(results.get(c.get("公式URL", ""), analyze("", session)))
        c["区分"], c["判定根拠"] = classify(c)
        c["出典"] = "・".join(c.get("出典", []))
        c["突合"] = " / ".join(c.get("突合", []))
        rows.append(c)
    order = {"HPなし": 0, "HPあり(解析不可)": 1, "昔風・簡易": 2, "今風": 3, "予約システムあり": 4}
    rows.sort(key=lambda r: (r.get("歯科医師会", ""), order[r["区分"]], r.get("住所", "")))

    with open(args.output, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    counts = {k: sum(r["区分"] == k for r in rows) for k in order}
    print(f"出力: {args.output}  " + "  ".join(f"{k}:{v}" for k, v in counts.items()), file=sys.stderr)
    for g in ("会員", "非会員"):
        n = [r for r in rows if r.get("歯科医師会") == g]
        if n:
            print(f"  {g} {len(n)}院: " + "  ".join(
                f"{k}:{sum(r['区分'] == k for r in n)}" for k in order), file=sys.stderr)


if __name__ == "__main__":
    main()
