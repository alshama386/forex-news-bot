import os
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
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise Exception("BOT_TOKEN missing in environment variables (BOT_TOKEN).")

CHANNEL = "@news_forexq"
SIGNATURE = "\n\n— @news_forexq"

FEEDS = [
    "https://www.investing.com/rss/news_1.rss",
    "https://ar.fxstreet.com/rss/news",
    "https://www.arabictrader.com/rss/news",
    "https://arab.dailyforex.com/rss/arab/forexnews.xml",
]

URGENT_KEYWORDS = [
    "breaking", "flash", "urgent", "عاجل",
    "fed", "powell", "interest rate", "inflation", "cpi", "nfp",
    "jobs report", "gold", "xau", "dollar", "usd",
    "brent", "wti", "oil"
]

POLL_SECONDS = 25
MAX_PER_FEED = 25
SUMMARY_MAX_CHARS = 260

DB_FILE = "posted.db"

# =========================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS posted (id TEXT PRIMARY KEY, created_at TEXT)")
    conn.commit()
    conn.close()

def already_posted(item_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM posted WHERE id=?", (item_id,))
    r = cur.fetchone()
    conn.close()
    return r is not None

def mark_posted(item_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO posted (id, created_at) VALUES (?, ?)",
                (item_id, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def clean(t):
    return " ".join((t or "").replace("\n", " ").split()).strip()

def make_hash_id(title, link):
    return hashlib.sha256((clean(title)+clean(link)).encode()).hexdigest()

def is_urgent(title, summary):
    t = (title + " " + summary).lower()
    return any(k in t for k in URGENT_KEYWORDS)

# =========================
def analyze_news(text):
    t = text.lower()

    إيجابي = ["rise","surge","gain","strong","beat","rebound","up"]
    سلبي = ["fall","drop","weak","miss","cut","down","slump","decline"]
    عالي = ["fed","powell","interest rate","inflation","cpi","nfp","gdp","fomc"]

    التأثير = "🟡 متوسط"
    المزاج = "⚪ محايد"

    if any(k in t for k in عالي):
        التأثير = "🔴 عالي جداً"
    if any(k in t for k in إيجابي):
        المزاج = "🟢 إيجابي"
    if any(k in t for k in سلبي):
        المزاج = "🔴 سلبي"

    الأصول = []
    if "gold" in t or "xau" in t or "ذهب" in t: الأصول.append("XAUUSD")
    if "usd" in t or "dollar" in t or "الدولار" in t: الأصول.append("USD")
    if "oil" in t or "brent" in t or "wti" in t or "نفط" in t: الأصول.append("OIL")
    if "nasdaq" in t or "nas100" in t: الأصول.append("NAS100")

    return التأثير, المزاج, ", ".join(الأصول) if الأصول else "السوق العام"

def source_label(url):
    if "investing" in url: return "Investing"
    if "fxstreet" in url: return "FXStreet"
    if "arabictrader" in url: return "ArabicTrader"
    if "dailyforex" in url: return "DailyForex"
    return "Source"

def build_message(title, summary, link, urgent, src):
    title = clean(title)
    summary = clean(summary)
    summary = summary[:SUMMARY_MAX_CHARS] + ("..." if len(summary)>SUMMARY_MAX_CHARS else "")

    التأثير, المزاج, الأصول = analyze_news(title + " " + summary)

    header = "🚨 <b>خبر عاجل</b>\n" if urgent else "📰 <b>أخبار الفوركس</b>\n"

    # Golden warning for very high impact news
    golden_warning = ""
    if التأثير == "🔴 عالي جداً":
        golden_warning = "⚠️ <b>تحذير ذهبي:</b> توقع حركة قوية جداً في السوق خلال الدقائق القادمة.\n\n"

    msg = f"""
━━━━━━━━━━━━━━━━━━
{header}
🗞 <b>{title}</b>

{summary}

{golden_warning}━━━━━━━━━━━━━━━━━━
📊 <b>قوة الخبر:</b> {التأثير}
🧠 <b>اتجاه السوق:</b> {المزاج}
📌 <b>الأصول المتأثرة:</b> {الأصول}
🕰 {datetime.now().strftime('%Y-%m-%d %H:%M')}

🔗 المصدر ({src}):
{link}
━━━━━━━━━━━━━━━━━━
📡 @news_forexq
"""
    return msg

# =========================
def main():
    init_db()
    bot = Bot(token=TOKEN)

    while True:
        try:
            for url in FEEDS:
                feed = feedparser.parse(url)
                src = source_label(url)

                for e in feed.entries[:MAX_PER_FEED]:
                    title = clean(e.get("title"))
                    link = clean(e.get("link"))
                    summary = clean(e.get("summary") or e.get("description") or "")

                    if not title: continue
                    uid = e.get("id") or make_hash_id(title, link)
                    if already_posted(uid): continue

                    urgent = is_urgent(title, summary)
                    text = build_message(title, summary, link, urgent, src)

                    bot.send_message(chat_id=CHANNEL, text=text, parse_mode=ParseMode.HTML)
                    mark_posted(uid)
                    time.sleep(1.2)

            time.sleep(POLL_SECONDS)
        except Exception as ex:
            print("Error:", ex)
            time.sleep(10)

if __name__ == "__main__":
    main()