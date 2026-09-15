"""
한국증시 오후장 리포트 -> 카드 이미지로 만들어서 텔레그램 전송

카드 구성
1. 코스피/코스닥 (지수+등락률, 그 아래 개인/외국인/기관 순매수를 한 줄로)
2. 상승/하락 상위 종목
3. 관련 뉴스 헤드라인

데이터 출처
- 지수: yfinance (^KS11, ^KQ11)
- 외국인·기관 순매수: 네이버 모바일 증권 비공식 API (m.stock.naver.com/api/index/{market}/trend)
  ※ 정식 공개 문서가 없는 API라 필드 이름이 예상과 다를 수 있습니다. 그런 경우
     "[경고] ... 원본 키" 로그에 실제 필드 이름이 찍히니, 그걸 보고 다시 맞추면 됩니다.
- 상승/하락 상위 종목: 네이버 증권 시세 순위 페이지 (finance.naver.com/sise/sise_rise.naver 등)
- 뉴스: 구글 뉴스 RSS 검색

실행에 필요한 값 (환경변수):
- TELEGRAM_TOKEN   : BotFather에게 받은 토큰
- TELEGRAM_CHAT_ID : 내 채팅방 chat id

필요 패키지: requirements 참고 (playwright install --with-deps chromium 최초 1회 필요, 카드 이미지 렌더링에만 사용)
"""

import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

KST = ZoneInfo("Asia/Seoul")

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = BASE_DIR / "templates"
OUTPUT_IMAGE_PATH = BASE_DIR / "report.png"

INDEX_TICKERS = {
    "KOSPI": ("^KS11", "코스피"),
    "KOSDAQ": ("^KQ11", "코스닥"),
}

WATCH_STOCKS = {
    "005930.KS": "삼성전자",
    "000660.KS": "SK하이닉스",
    "005380.KS": "현대차",
    "066570.KS": "LG전자",
    "009150.KS": "삼성전기",
    "012330.KS": "현대모비스",
    "108490.KQ": "로보티즈",
    "058610.KQ": "에스피지",
    "010170.KQ": "대한광통신",
    "083450.KQ": "GST",
    "161580.KQ": "필옵틱스",
    "006400.KS": "삼성SDI",
    "034020.KS": "두산에너빌리티",
    "010120.KS": "LS일렉트릭",
    "000500.KS": "가온전선",
    "062040.KS": "산일전기",
    "356680.KQ": "엑스게이트",
    "272210.KS": "한화시스템",
}

NEWS_RSS_URL = "https://news.google.com/rss/search"
NEWS_QUERY = "코스피"
NEWS_MAX_ITEMS = 5

MOVERS_COUNT = 5

UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


# ------------------------------------------------------------------
# 1) 코스피/코스닥 지수 + 개인/외국인/기관 순매수
# ------------------------------------------------------------------
def _get_index_price(ticker: str):
    """(현재가, 등락률) 튜플을 돌려줍니다. 실패하면 (None, None)."""
    try:
        hist = yf.Ticker(ticker).history(period="5d")["Close"].dropna()
        if len(hist) < 2:
            return None, None
        prev, last = hist.iloc[-2], hist.iloc[-1]
        pct = (last - prev) / prev * 100
        return last, pct
    except Exception as e:
        print(f"[경고] {ticker} 지수 가져오기 실패: {e}", file=sys.stderr)
        return None, None


def _find_value(d: dict, keywords):
    """딕셔너리에서 키워드가 포함된 키의 값을 찾습니다. (정확한 필드 이름을 몰라도 대응하기 위함)"""
    for k, v in d.items():
        lk = k.lower()
        if any(kw in lk for kw in keywords):
            return v
    return None


def get_investor_trend(market: str):
    """당일 기준 개인/외국인/기관 순매수를 가져옵니다."""
    url = f"https://m.stock.naver.com/api/index/{market}/trend"
    try:
        resp = requests.get(url, headers=UA_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        item = data[-1] if isinstance(data, list) and data else data
        if not isinstance(item, dict):
            return None

        foreign = _find_value(item, ["foreign"])
        organ = _find_value(item, ["organ", "institut"])
        individual = _find_value(item, ["individ", "person"])

        if foreign is None and organ is None:
            print(
                f"[경고] {market} 순매수 데이터에서 원하는 필드를 못 찾았습니다. "
                f"원본 키: {list(item.keys())}",
                file=sys.stderr,
            )
            return None
        return {"individual": individual, "foreign": foreign, "organ": organ}
    except Exception as e:
        print(f"[경고] {market} 투자자 매매동향 가져오기 실패: {e}", file=sys.stderr)
        return None


def _numeric_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _flow_item(label: str, value):
    num = _numeric_or_none(value)
    if num is None:
        return {"label": label, "value_fmt": "-" if value is None else str(value), "up": False, "down": False}
    up = num > 0
    down = num < 0
    fmt = f"{num:+,.0f}" if num == int(num) else f"{num:+,.2f}"
    return {"label": label, "value_fmt": fmt, "up": up, "down": down}


def build_market_boxes():
    """코스피/코스닥 박스 데이터를 만듭니다. 각 박스는 지수+등락률, 그리고 개인/외국인/기관 한 줄로 구성됩니다."""
    boxes = []
    for market, (ticker, name) in INDEX_TICKERS.items():
        price, pct = _get_index_price(ticker)
        valid = price is not None and pct is not None
        up = valid and pct > 0
        down = valid and pct < 0

        trend = get_investor_trend(market)
        if trend:
            flow = [
                _flow_item("개인", trend["individual"]),
                _flow_item("외국인", trend["foreign"]),
                _flow_item("기관", trend["organ"]),
            ]
        else:
            flow = []

        boxes.append(
            {
                "name": name,
                "valid": valid,
                "price_fmt": f"{price:,.2f}" if valid else "-",
                "pct_fmt": f"{pct:+.2f}%" if valid else "",
                "up": up,
                "down": down,
                "flow": flow,
            }
        )
    return boxes


def get_watchlist():
    """관심종목의 등락률을 한 번에 가져옵니다."""
    tickers = list(WATCH_STOCKS)
    try:
        data = yf.download(
            tickers=tickers,
            period="5d",
            group_by="ticker",
            threads=True,
            progress=False,
            auto_adjust=True,
        )
    except Exception as e:
        print(f"[경고] 관심종목 데이터 다운로드 실패: {e}", file=sys.stderr)
        data = None

    items = []
    for ticker, name in WATCH_STOCKS.items():
        pct = None
        try:
            if data is not None:
                close = data[ticker]["Close"].dropna()
                if len(close) >= 2:
                    prev, last = close.iloc[-2], close.iloc[-1]
                    if prev:
                        pct = (last - prev) / prev * 100
        except Exception as e:
            print(f"[경고] {name}({ticker}) 처리 실패: {e}", file=sys.stderr)

        valid = pct is not None
        items.append(
            {
                "name": name,
                "valid": valid,
                "pct_fmt": f"{pct:+.2f}%" if valid else "-",
                "up": valid and pct > 0,
                "down": valid and pct < 0,
            }
        )
    return items


# ------------------------------------------------------------------
# 2) 상승/하락 상위 종목
# ------------------------------------------------------------------
def get_top_movers(direction: str, count: int = MOVERS_COUNT):
    """네이버 증권 순위 페이지에서 상승률/하락률 상위 종목을 가져옵니다. (코스피+코스닥 합산)"""
    page = "sise_rise" if direction == "rise" else "sise_fall"
    results = []
    for sosok in (0, 1):  # 0: 코스피, 1: 코스닥
        url = f"https://finance.naver.com/sise/{page}.naver?sosok={sosok}"
        try:
            resp = requests.get(url, headers=UA_HEADERS, timeout=15)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding
            soup = BeautifulSoup(resp.text, "html.parser")
            table = soup.find("table", {"class": "type_2"})
            if not table:
                continue
            for row in table.find_all("tr"):
                name_tag = row.find("a", {"class": "tltle"})
                if not name_tag:
                    continue
                name = name_tag.get_text(strip=True)
                rate_text = None
                for td in row.find_all("td"):
                    text = td.get_text(strip=True)
                    if "%" in text:
                        rate_text = text
                        break
                if name and rate_text:
                    results.append({"name": name, "rate": rate_text})
        except Exception as e:
            print(f"[경고] {url} 가져오기 실패: {e}", file=sys.stderr)
    return results[:count]


# ------------------------------------------------------------------
# 3) 관련 뉴스
# ------------------------------------------------------------------
def get_news(query: str = NEWS_QUERY, max_items: int = NEWS_MAX_ITEMS):
    """구글 뉴스 RSS에서 키워드 검색 결과 헤드라인을 가져옵니다."""
    params = {"q": query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"}
    try:
        resp = requests.get(NEWS_RSS_URL, params=params, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)

        headlines = []
        for item in root.findall("./channel/item"):
            title_el = item.find("title")
            if title_el is None or not title_el.text:
                continue
            headlines.append(title_el.text.strip())
            if len(headlines) >= max_items:
                break
        return headlines
    except Exception as e:
        print(f"[경고] 뉴스 가져오기 실패: {e}", file=sys.stderr)
        return []


# ------------------------------------------------------------------
# 데이터 조립 + 이미지 렌더링 + 전송
# ------------------------------------------------------------------
def build_data() -> dict:
    now = datetime.now(KST)
    return {
        "today": now.strftime("%Y년 %m월 %d일 (%a) %H:%M"),
        "markets": build_market_boxes(),
        "watch": get_watchlist(),
        "risers": get_top_movers("rise"),
        "fallers": get_top_movers("fall"),
        "news": get_news(),
    }


def render_image(data: dict) -> Path:
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template("card_template.html")
    html = template.render(**data)

    html_path = BASE_DIR / "_rendered.html"
    html_path.write_text(html, encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 820, "height": 100})
        page.goto(f"file://{html_path}")
        height = page.evaluate("document.body.scrollHeight")
        page.set_viewport_size({"width": 820, "height": height})
        page.screenshot(path=str(OUTPUT_IMAGE_PATH), full_page=True)
        page.close()
        browser.close()

    html_path.unlink(missing_ok=True)
    return OUTPUT_IMAGE_PATH


def send_telegram_photo(image_path: Path, caption: str = ""):
    import os

    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_TOKEN 또는 TELEGRAM_CHAT_ID 환경변수가 설정되지 않았습니다.")

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    with open(image_path, "rb") as f:
        resp = requests.post(url, data={"chat_id": chat_id, "caption": caption}, files={"photo": f})
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"텔레그램 전송 실패: {result}")
    print("텔레그램 이미지 전송 완료!")


if __name__ == "__main__":
    data = build_data()
    image_path = render_image(data)
    send_telegram_photo(image_path, caption=f"📊 한국증시 오후장 리포트 ({data['today']})")
