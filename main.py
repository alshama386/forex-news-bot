import os
import time
import sqlite3
import hashlib
from datetime import datetime, timezone

import feedparser
from telegram import Bot
from telegram.constants import ParseMode


# ================= CONFIG =================
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise Exception("BOT_TOKEN missing")

CHANNEL = "@news_forexq"
SIGNATURE = "\n\n🚀 @news_forexq"

FEEDS = [
    "https://ar.fxstreet.com/rss/news",
    "https://www.arabictrader.com/rss/news",
    "https://arab.dailyforex.com/rss/arab/forexnews.xml"
]

POLL_SECONDS = 30
MAX_PER_FEED = 20

# كلمات عاجل
URGENT_KEYWORDS = ["fed", "powell", "rate", "cpi", "nfp", "gold", "oil", "usd", "eur", "jpy"]

DB_FILE = "posted.db"


# ================= DATABASE =================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS posted (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

def is_posted(pid):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM posted WHERE id=?", (pid,))
    r = c.fetchone()
    conn.close()
    return r is not None

def mark_posted(pid):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO posted VALUES (?)", (pid,))
    conn.commit()
    conn.close()


# ================= TOOLS =================
def clean(t):
    return " ".join(t.replace("\n", " ").split()) if t else ""

def make_id(title, link):
    return hashlib.sha256((title + link).encode()).hexdigest()

def is_urgent(title, summary):
    txt = (title + summary).lower()
    return any(k in txt for k in URGENT_KEYWORDS)

def classify_strength(title, summary):
    txt = (title + summary).lower()
    if any(x in txt for x in ["rate", "inflation", "fed", "powell", "cpi", "nfp"]):
        return "عالي جداً"
    if any(x in txt for x in ["gold", "oil", "usd", "eur", "jpy"]):
        return "عالي"
    return "متوسط"

def market_mood(title, summary):
    txt = (title + summary).lower()
    if any(x in txt for x in ["drop", "fall", "decline", "risk"]):
        return "سلبي 🔴"
    if any(x in txt for x in ["rise", "gain", "growth"]):
        return "إيجابي 🟢"
    return "محايد 🟡"

def assets(title, summary):
    txt = (title + summary).lower()
    a = []
    if "gold" in txt: a.append("الذهب")
    if "oil" in txt: a.append("النفط")
    if "usd" in txt: a.append("الدولار")
    if "eur" in txt: a.append("اليورو")
    if "jpy" in txt: a.append("الين")
    return "، ".join(a) if a else "السوق العام"


# ================= FORMAT =================
def build(title, summary, link):
    urgent = is_urgent(title, summary)
    strength = classify_strength(title, summary)
    mood = market_mood(title, summary)
    asset = assets(title, summary)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    gold_warn = "⚠️ تحذير ذهبي\n" if urgent else ""

    msg = f"""
{gold_warn}📰 <b>{title}</b>

{summary}

📊 <b>قوة الخبر:</b> {strength}
🧠 <b>مزاج السوق:</b> {mood}
📌 <b>الأصول المتأثرة:</b> {asset}
🕒 {now}

🔗 المصدر:
{link}
—————————————
{SIGNATURE}
"""
    return msg.strip()


# ================= MAIN =================
def main():
    init_db()
    bot = Bot(token=TOKEN)

    while True:
        try:
            for url in FEEDS:
                feed = feedparser.parse(url)

                for e in feed.entries[:MAX_PER_FEED]:
                    title = clean(e.get("title", ""))
                    summary = clean(e.get("summary", "") or e.get("description", ""))
                    link = clean(e.get("link", ""))

                    if not title:
                        continue

                    pid = make_id(title, link)
                    if is_posted(pid):
                        continue

                    text = build(title, summary, link)

                    bot.send_message(
                        chat_id=CHANNEL,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=False
                    )

                    mark_posted(pid)
                    time.sleep(1.5)

            time.sleep(POLL_SECONDS)

        except Exception as ex:
            print("ERROR:", ex)
            time.sleep(10)


if __name__ == "__main__":
    main()