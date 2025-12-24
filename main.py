import time
import sqlite3
import hashlib
from datetime import datetime

import feedparser
from telegram import Bot
from telegram.constants import ParseMode

# =========================
# CONFIG
# =========================
import os
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise Exception("BOT_TOKEN missing in Render environment variables")
CHANNEL = "@newsforexq"
SIGNATURE = "\n\n— @newsforexq"

# Arabic news sources (via RSS). You can add/remove feeds anytime.
FEEDS = [
    "https://www.investing.com/rss/news_1.rss",          # Investing (often markets/forex related)
    "https://ar.fxstreet.com/rss/news",                  # FXStreet Arabic
    "https://www.arabictrader.com/rss/news",             # ArabicTrader
    "https://arab.dailyforex.com/rss/arab/forexnews.xml" # DailyForex Arabic
]

# "Urgent" detection keywords (ENGLISH ONLY to keep code fully English).
# If you want, I can add Arabic keyword "عاجل" later.
URGENT_KEYWORDS = [
    "breaking", "flash", "urgent",
    "fed", "powell", "rate decision", "interest rate",
    "cpi", "inflation", "nfp", "jobs report",
    "gold", "xau", "dollar", "usd",
    "eurusd", "gbpusd", "usdjpy",
    "brent", "wti", "oil"
]

POLL_SECONDS = 25     # how often to poll feeds
MAX_PER_FEED = 25     # how many items to scan per feed each cycle
SUMMARY_MAX_CHARS = 260

# =========================
# PERSISTENT DE-DUP (SQLite)
# =========================
DB_FILE = "posted.db"

def init_db() -> None:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS posted (
            id TEXT PRIMARY KEY,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def already_posted(item_id: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM posted WHERE id=?", (item_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None

def mark_posted(item_id: str) -> None:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO posted (id, created_at) VALUES (?, ?)",
        (item_id, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

# =========================
# HELPERS
# =========================
def clean(text: str) -> str:
    if not text:
        return ""
    return " ".join(text.replace("\n", " ").split()).strip()

def make_hash_id(title: str, link: str) -> str:
    raw = (clean(title) + "||" + clean(link)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def is_urgent(title: str, summary: str) -> bool:
    combined = (title + " " + summary).lower()
    return any(k.lower() in combined for k in URGENT_KEYWORDS)

def source_label(feed_url: str) -> str:
    u = feed_url.lower()
    if "investing" in u:
        return "Investing"
    if "fxstreet" in u:
        return "FXStreet"
    if "arabictrader" in u:
        return "ArabicTrader"
    if "dailyforex" in u:
        return "DailyForex"
    return "Source"

def build_message(title: str, summary: str, link: str, urgent: bool, src: str) -> str:
    title = clean(title)
    summary = clean(summary)

    # Keep summary short to avoid copying full articles
    if summary:
        summary = summary[:SUMMARY_MAX_CHARS] + ("..." if len(summary) > SUMMARY_MAX_CHARS else "")

    prefix = "🚨 <b>URGENT</b>\n" if urgent else "📰 "
    msg = f"{prefix}<b>{title}</b>\n"

    if summary:
        msg += f"\n{summary}\n"

    if link:
        msg += f"\n<b>Source</b> ({src}): {clean(link)}"

    msg += SIGNATURE
    return msg

# =========================
# MAIN LOOP
# =========================
def main() -> None:
    init_db()
    bot = Bot(token=TOKEN)

    while True:
        try:
            for url in FEEDS:
                feed = feedparser.parse(url)
                src = source_label(url)

                for entry in feed.entries[:MAX_PER_FEED]:
                    title = clean(entry.get("title"))
                    link = clean(entry.get("link"))
                    summary = clean(entry.get("summary") or entry.get("description") or "")

                    if not title and not link:
                        continue

                    item_id = entry.get("id") or make_hash_id(title, link)
                    if already_posted(item_id):
                        continue

                    urgent = is_urgent(title, summary)
                    text = build_message(title, summary, link, urgent, src)

                    bot.send_message(
                        chat_id=CHANNEL,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=False
                    )

                    mark_posted(item_id)
                    time.sleep(1.2)  # gentle rate limiting

            time.sleep(POLL_SECONDS)

        except Exception as ex:
            print("Error:", ex)
            time.sleep(10)

if __name__ == "__main__":
    main()
