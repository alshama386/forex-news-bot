import os
import time
import sqlite3
import hashlib
import asyncio
from datetime import datetime

import feedparser
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError


# =========================
# CONFIG
# =========================
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise Exception("BOT_TOKEN missing in environment variables")

CHANNEL = "@news_forexq"   # اسم قناتك
FEEDS = [
    "https://ar.fxstreet.com/rss/news",
    "https://arab.dailyforex.com/rss/arab/forexnews.xml"
]

POLL_SECONDS = 60          # كل كم ثانية يفحص RSS
MAX_PER_FEED = 10          # لا ترفعها وايد علشان ما يصير Flood
SEND_DELAY = 2.5           # تأخير بين كل رسالة ورسالة (مهم جداً)
SUMMARY_MAX_CHARS = 350

DB_FILE = "posted.db"


# =========================
# DB
# =========================
def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS posted (id TEXT PRIMARY KEY, created_at TEXT)")
    con.commit()
    con.close()

def already_posted(item_id: str) -> bool:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM posted WHERE id=?", (item_id,))
    row = cur.fetchone()
    con.close()
    return row is not None

def mark_posted(item_id: str):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO posted (id, created_at) VALUES (?, ?)",
        (item_id, datetime.utcnow().isoformat())
    )
    con.commit()
    con.close()


# =========================
# HELPERS
# =========================
def clean(t: str) -> str:
    if not t:
        return ""
    return " ".join(t.replace("\n", " ").split()).strip()

def make_hash_id(title: str, link: str) -> str:
    raw = (clean(title) + "||" + clean(link)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def source_label(feed_url: str) -> str:
    u = (feed_url or "").lower()
    if "fxstreet" in u:
        return "FXStreet"
    if "dailyforex" in u:
        return "DailyForex"
    if "investing" in u:
        return "Investing"
    if "arabictrader" in u:
        return "ArabicTrader"
    return "المصدر"

def sentiment_ar(text: str) -> str:
    """
    تصنيف بسيط (يعتمد على كلمات شائعة).
    تقدر نطوره بعدين.
    """
    t = (text or "").lower()

    pos = ["يرتفع", "ارتفاع", "يصعد", "صعود", "مكاسب", "قوي", "تحسن", "إيجابي",
           "rise", "up", "gain", "bullish", "strong", "beats"]
    neg = ["ينخفض", "انخفاض", "يهبط", "هبوط", "خسائر", "ضعيف", "سلبي", "تراجع",
           "fall", "down", "loss", "bearish", "weak", "misses"]

    if any(w in t for w in pos):
        return "إيجابي ✅"
    if any(w in t for w in neg):
        return "سلبي ❌"
    return "محايد ⚪️"

def strength_ar(text: str) -> str:
    """
    قوة الخبر (تقريبية) حسب كلمات اقتصادية قوية.
    """
    t = (text or "").lower()
    very_high = ["nfp", "cpi", "inflation", "rate decision", "fed", "powell",
                 "fomc", "interest rate", "gdp", "jobs report", "قرار الفائدة",
                 "التضخم", "الوظائف", "الفيدرالي", "باول"]
    high = ["gold", "oil", "usd", "eurusd", "gbpusd", "usdjpy", "xau",
            "الذهب", "النفط", "الدولار", "اليورو", "ين"]

    if any(w in t for w in very_high):
        return "عالي جداً 🔥"
    if any(w in t for w in high):
        return "عالي 🔥"
    return "متوسط ✨"

def build_message(title: str, summary: str, src: str) -> str:
    title = clean(title)
    summary = clean(summary)

    if summary:
        summary = summary[:SUMMARY_MAX_CHARS] + ("..." if len(summary) > SUMMARY_MAX_CHARS else "")

    mood = sentiment_ar(title + " " + summary)
    power = strength_ar(title + " " + summary)

    # بدون روابط + ذكر المصدر فقط
    msg = f"""<b>{title}</b>

{summary}

⚡ <b>قوة الخبر:</b> {power}
🧠 <b>اتجاه السوق:</b> {mood}

🕒 {datetime.now().strftime("%Y-%m-%d %H:%M")}
🔗 <b>المصدر:</b> ({src})

— @news_forexq
"""
    return msg.strip()


# =========================
# SENDING (handles flood control)
# =========================
async def safe_send(bot: Bot, text: str):
    while True:
        try:
            await bot.send_message(
                chat_id=CHANNEL,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            return

        except RetryAfter as e:
            # تيليجرام يقول انتظر X ثانية
            wait_s = int(getattr(e, "retry_after", 5))
            print(f"Flood control: retry after {wait_s}s")
            await asyncio.sleep(wait_s + 1)

        except TimedOut:
            print("Timed out. Retrying in 5s...")
            await asyncio.sleep(5)

        except NetworkError as e:
            print("Network error:", e, "Retrying in 5s...")
            await asyncio.sleep(5)


# =========================
# MAIN LOOP (ASYNC)
# =========================
async def run():
    init_db()
    bot = Bot(token=TOKEN)
    print("Bot Running...")

    while True:
        try:
            for url in FEEDS:
                feed = feedparser.parse(url)
                src = source_label(url)

                for entry in feed.entries[:MAX_PER_FEED]:
                    title = clean(entry.get("title", ""))
                    link = clean(entry.get("link", ""))
                    summary = clean(entry.get("summary") or entry.get("description") or "")

                    if not title:
                        continue

                    item_id = entry.get("id") or make_hash_id(title, link)

                    if already_posted(item_id):
                        continue

                    msg = build_message(title, summary, src)

                    await safe_send(bot, msg)
                    mark_posted(item_id)

                    await asyncio.sleep(SEND_DELAY)

            await asyncio.sleep(POLL_SECONDS)

        except Exception as ex:
            print("Error:", ex)
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(run())