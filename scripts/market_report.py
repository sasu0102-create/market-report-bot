import os
import requests
import yfinance as yf

def get_index_data():
    sp500 = yf.Ticker("^GSPC").history(period="1d")["Close"].iloc[-1]
    nasdaq = yf.Ticker("^IXIC").history(period="1d")["Close"].iloc[-1]
    dow = yf.Ticker("^DJI").history(period="1d")["Close"].iloc[-1]
    sox = yf.Ticker("^SOX").history(period="1d")["Close"].iloc[-1]
    kospi = yf.Ticker("^KS11").history(period="1d")["Close"].iloc[-1]

    return f"""🇺🇸 미국 증시
S&P500: {sp500:.2f}, 나스닥: {nasdaq:.2f}, 다우: {dow:.2f}

📈 SOX 반도체 지수
SOX: {sox:.2f}

🇰🇷 한국 증시
KOSPI: {kospi:.2f}
"""

def get_news(api_key, query, label):
    url = f"https://newsapi.org/v2/everything?q={query}&language=ko&apiKey={api_key}"
    r = requests.get(url).json()
    articles = r.get("articles", [])[:5]  # 최대 5개 헤드라인
    headlines = [a["title"] for a in articles]
    return f"📰 {label} 뉴스\n" + "\n".join(f"- {h}" for h in headlines)

if __name__ == "__main__":
    api_key = os.environ["NEWS_API_KEY"]

    report = []
    report.append(get_index_data())
    report.append(get_news(api_key, "미국 증시", "미국"))
    report.append(get_news(api_key, "반도체", "SOX"))
    report.append(get_news(api_key, "한국 증시", "한국"))

    print("\n\n".join(report))
