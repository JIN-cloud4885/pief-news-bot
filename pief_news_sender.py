import csv
import html
import json
import re
import smtplib
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "sent_news.db"


def load_config():
    if not CONFIG_PATH.exists():
        raise FileNotFoundError("config.json 파일이 없습니다.")

    with open(CONFIG_PATH, "r", encoding="utf-8") as file:
        return json.load(file)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sent_news (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        link TEXT NOT NULL UNIQUE,
        source TEXT,
        published TEXT,
        sent_at TEXT NOT NULL
    )
    """)

    conn.commit()
    return conn


def clean_text(text):
    text = html.unescape(text or "")
    text = text.replace("<b>", "").replace("</b>", "")
    return text.strip()


def get_article_date(pub_date):
    try:
        return parsedate_to_datetime(pub_date).date()
    except Exception:
        return None


def format_pub_date(pub_date):
    try:
        dt = parsedate_to_datetime(pub_date)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return pub_date


def extract_press_name_from_url(url):
    if not url:
        return "언론사 미확인"

    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"}
        )

        with urllib.request.urlopen(request, timeout=5) as response:
            raw_html = response.read().decode("utf-8", errors="ignore")

        patterns = [
            r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:site_name["\']',
            r'<meta[^>]+name=["\']application-name["\'][^>]+content=["\']([^"\']+)["\']'
        ]

        for pattern in patterns:
            match = re.search(pattern, raw_html, re.IGNORECASE)
            if match:
                press_name = clean_text(match.group(1))
                if press_name:
                    return press_name

    except Exception:
        pass

    domain = urllib.parse.urlparse(url).netloc.replace("www.", "")

    if domain:
        return domain

    return "언론사 미확인"


def fetch_naver_yesterday_news(config, query):
    client_id = config["naver_client_id"].strip()
    client_secret = config["naver_client_secret"].strip()

    yesterday = datetime.now().date() - timedelta(days=1)

    display = 100
    max_pages = int(config.get("max_pages", 10))

    all_news = []
    press_cache = {}

    for page in range(max_pages):
        start = page * display + 1
        encoded_query = urllib.parse.quote(query)

        url = (
            "https://openapi.naver.com/v1/search/news.json"
            f"?query={encoded_query}"
            f"&display={display}"
            f"&start={start}"
            "&sort=date"
        )

        request = urllib.request.Request(url)
        request.add_header("X-Naver-Client-Id", client_id)
        request.add_header("X-Naver-Client-Secret", client_secret)

        print("네이버 API 요청:", url)

        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))

        items = data.get("items", [])

        if not items:
            break

        stop_search = False

        for item in items:
            title = clean_text(item.get("title", ""))
            description = clean_text(item.get("description", ""))
            link = item.get("originallink") or item.get("link")
            published = item.get("pubDate", "")

            article_date = get_article_date(published)

            if article_date is None:
                continue

            if article_date == yesterday:
                if link in press_cache:
                    press_name = press_cache[link]
                else:
                    press_name = extract_press_name_from_url(link)
                    press_cache[link] = press_name

                all_news.append({
                    "title": title,
                    "link": link,
                    "source": press_name,
                    "published": format_pub_date(published),
                    "description": description
                })

            elif article_date < yesterday:
                stop_search = True

        if stop_search:
            break

    print("어제 기사 수:", len(all_news))
    return all_news


def is_already_sent(conn, link):
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM sent_news WHERE link = ?", (link,))
    return cursor.fetchone() is not None


def mark_as_sent(conn, news):
    cursor = conn.cursor()

    cursor.execute("""
    INSERT OR IGNORE INTO sent_news (
        title,
        link,
        source,
        published,
        sent_at
    )
    VALUES (?, ?, ?, ?, ?)
    """, (
        news["title"],
        news["link"],
        news.get("source", ""),
        news.get("published", ""),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))

    conn.commit()


def save_log_csv(news_list):
    log_path = BASE_DIR / "latest_news_log.csv"

    with open(log_path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["title", "source", "published", "description", "link"])

        for news in news_list:
            writer.writerow([
                news.get("title", ""),
                news.get("source", ""),
                news.get("published", ""),
                news.get("description", ""),
                news.get("link", "")
            ])


def build_email_body(news_list, query, report_date):
    if not news_list:
        return f"""
<html>
<body>
<p>안녕하세요.</p>
<p><b>{report_date}</b> 등록 기준 <b>{query}</b> 관련 네이버 신규 뉴스가 없습니다.</p>
<p style="color:#777;font-size:12px;margin-top:20px;">
이 메일은 Python 자동화 프로그램으로 발송되었습니다.
</p>
</body>
</html>
"""

    rows = []

    for idx, news in enumerate(news_list, start=1):
        rows.append(f"""
        <tr>
            <td style="padding:8px;border-bottom:1px solid #ddd;width:50px;">{idx}</td>
            <td style="padding:8px;border-bottom:1px solid #ddd;">
                <a href="{news['link']}"><b>{html.escape(news['title'])}</b></a><br>
                <span style="color:#333;font-size:13px;">
                    출처: <b>{html.escape(news.get("source", "언론사 미확인"))}</b>
                </span><br>
                <span style="color:#666;font-size:12px;">
                    등록일: {html.escape(news.get("published", ""))}
                </span><br>
                <span style="font-size:13px;">
                    {html.escape(news.get("description", ""))}
                </span>
            </td>
        </tr>
        """)

    return f"""
<html>
<body>
<p>안녕하세요.</p>
<p><b>{report_date}</b> 등록 기준 <b>{query}</b> 관련 네이버 신규 뉴스입니다.</p>

<table style="border-collapse:collapse;width:100%;font-family:Arial, sans-serif;font-size:14px;">
    <thead>
        <tr>
            <th style="text-align:left;padding:8px;border-bottom:2px solid #333;width:50px;">번호</th>
            <th style="text-align:left;padding:8px;border-bottom:2px solid #333;">뉴스</th>
        </tr>
    </thead>
    <tbody>
        {''.join(rows)}
    </tbody>
</table>

<p style="color:#777;font-size:12px;margin-top:20px;">
이 메일은 Python 자동화 프로그램으로 발송되었습니다.
</p>
</body>
</html>
"""


def send_email(config, subject, html_body):
    email_config = config["email"]

    msg = MIMEMultipart("alternative")
    msg["From"] = email_config["sender_email"]
    msg["To"] = ", ".join(email_config["receiver_emails"])
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(
        email_config["smtp_server"],
        int(email_config["smtp_port"])
    ) as server:
        server.starttls()
        server.login(
            email_config["sender_email"],
            email_config["sender_password"]
        )
        server.sendmail(
            email_config["sender_email"],
            email_config["receiver_emails"],
            msg.as_string()
        )


def main():
    config = load_config()
    conn = init_db()

    query = config.get("query", "평택시")
    send_when_empty = bool(config.get("send_when_empty", True))

    report_date = datetime.now().date() - timedelta(days=1)
    report_date_text = report_date.strftime("%Y-%m-%d")

    fetched_news = fetch_naver_yesterday_news(config, query)

    new_news = [
        news for news in fetched_news
        if not is_already_sent(conn, news["link"])
    ]

    print("신규 뉴스:", len(new_news))

    save_log_csv(new_news)

    if not new_news and not send_when_empty:
        print("어제 신규 뉴스가 없어 발송하지 않았습니다.")
        conn.close()
        return

    subject = f"[일일 언론동향] {report_date_text} {query} 네이버 뉴스 {len(new_news)}건"
    body = build_email_body(new_news, query, report_date_text)

    if config.get("email", {}).get("enabled", True):
        send_email(config, subject, body)
        print("이메일 발송 완료")

    for news in new_news:
        mark_as_sent(conn, news)

    conn.close()


if __name__ == "__main__":
    main()