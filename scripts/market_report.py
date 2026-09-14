"""
한국증시 오후장 리포트 -> 텔레그램 전송

카드 구성 (텍스트 메시지)
1. 코스피/코스닥 지수 (전일 대비 등락률)
2. 외국인·기관 순매수 (코스피/코스닥, 당일 기준)
3. 상승/하락 상위 종목 (코스피+코스닥 합산)
4. 관련 뉴스 헤드라인 (구글 뉴스 RSS, "코스피" 키워드)

데이터 출처
- 지수: yfinance (^KS11, ^KQ11)
- 외국인·기관 순매수: 네이버 모바일 증권 비공식 API (m.stock.naver.com/api/index/{market}/trend)
  ※ 정식 공개 문서가 없는 API라 필드 이름이 예상과 다를 수 있습니다. 그런 경우
     "[경고] ... 원본 키" 로그에 실제 필드 이름이 찍히니, 그걸 보고 다시 맞추면 됩니다.
- 상승/하락 상위 종목: 네이버 증권 시세 순위 페이지 (finance.naver.com/sise/sise_rise.naver 등)
- 뉴스: 구글 뉴스 RSS 검색 (전세계 어디서 접속해도 막히지 않아 안정적입니다)

실행에 필요한 값 (환경변수):
- TELEGRAM_TOKEN   : BotFather에게 받은 토큰
- TELEGRAM_CHAT_ID : 내 채팅방 chat id
"""

import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from bs4 import BeautifulSoup

KST = ZoneInfo("Asia/Seoul")

INDEX_TICKERS = {
    "^KS11": "코스피",
    "^KQ11": "코스닥",
}

TREND_MARKETS = ["KOSPI", "KOSDAQ"]

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
# 1) 코스피/코스닥 지수
# ------------------------------------------------------------------
def get_index_data():
    lines = ["📈 코스피/코스닥"]
    for ticker, name in INDEX_TICKERS.items():
        try:
            hist = yf.Ticker(ticker).history(period="5d")["Close"].dropna()
            if len(hist) < 2:
                lines.append(f"  · {name}: 데이터 없음")
                continue
            prev, last = hist.iloc[-2], hist.iloc[-1]
            pct = (last - prev) / prev * 100
            arrow = "🔺" if pct > 0 else ("🔻" if pct < 0 else "➖")
            lines.append(f"  {arrow} {name}: {last:,.2f} ({pct:+.2f}%)")
        except Exception as e:
            print(f"[경고] {name} 지수 가져오기 실패: {e}", file=sys.stderr)
            lines.append(f"  · {name}: 데이터 없음")
    return "\n".join(lines)


# ------------------------------------------------------------------
# 2) 외국인·기관 순매수
# ------------------------------------------------------------------
def _find_value(d: dict, keywords):
    """딕셔너리에서 키워드가 포함된 키의 값을 찾습니다. (정확한 필드 이름을 몰라도 대응하기 위함)"""
    for k, v in d.items():
        lk = k.lower()
        if any(kw in lk for kw in keywords):
            return v
    return None


def get_investor_trend(market: str):
    """당일 기준 외국인/기관/개인 순매수를 가져옵니다."""
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
        return {"foreign": foreign, "organ": organ, "individual": individual}
    except Exception as e:
        print(f"[경고] {market} 투자자 매매동향 가져오기 실패: {e}", file=sys.stderr)
        return None


def format_trend_section():
    lines = ["🌊 외국인·기관 순매수 (당일)"]
    for market in TREND_MARKETS:
        name = "코스피" if market == "KOSPI" else "코스닥"
        trend = get_investor_trend(market)
        if not trend:
            lines.append(f"  · {name}: 데이터 없음")
            continue
        parts = []
        if trend["foreign"] is not None:
            parts.append(f"외국인 {trend['foreign']:+,}")
        if trend["organ"] is not None:
            parts.append(f"기관 {trend['organ']:+,}")
        if trend["individual"] is not None:
            parts.append(f"개인 {trend['individual']:+,}")
        lines.append(f"  · {name}: " + " / ".join(parts) if parts else f"  · {name}: 데이터 없음")
    return "\n".join(lines)


# ------------------------------------------------------------------
# 3) 상승/하락 상위 종목
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
                    results.append(f"{name} ({rate_text})")
        except Exception as e:
            print(f"[경고] {url} 가져오기 실패: {e}", file=sys.stderr)
    return results[:count]


def format_movers_section():
    risers = get_top_movers("rise")
    fallers = get_top_movers("fall")
    lines = ["🏆 상승/하락 상위 종목"]
    lines.append("  [상승]")
    if risers:
        lines.extend(f"   🔺 {r}" for r in risers)
    else:
        lines.append("   · 데이터 없음")
    lines.append("  [하락]")
    if fallers:
        lines.extend(f"   🔻 {f}" for f in fallers)
    else:
        lines.append("   · 데이터 없음")
    return "\n".join(lines)


# ------------------------------------------------------------------
# 4) 관련 뉴스
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


def format_news_section():
    headlines = get_news()
    lines = ["📰 관련 뉴스"]
    if headlines:
        lines.extend(f"  - {h}" for h in headlines)
    else:
        lines.append("  · 뉴스를 불러오지 못했습니다")
    return "\n".join(lines)


# ------------------------------------------------------------------
# 리포트 조립
# ------------------------------------------------------------------
def build_report() -> str:
    now = datetime.now(KST)
    header = f"📊 한국증시 오후장 리포트 ({now.strftime('%Y-%m-%d (%a) %H:%M')})"

    sections = [
        header,
        get_index_data(),
        format_trend_section(),
        format_movers_section(),
        format_news_section(),
    ]
    return "\n\n".join(sections)


if __name__ == "__main__":
    print(build_report())
