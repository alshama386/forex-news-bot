import os
import time
import sqlite3
import hashlib
from datetime import datetime, timezone

import feedparser
from telegram import Bot
from telegram.constants import ParseMode

# =====================
# CONFIG
# =====================
TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL = "@news_forexq"
SIGNATURE = "\n✈️ @news_forexq"

FEEDS = [
    "https://www.investing.com/rss/news_1.rss",
    "https://ar.fxstreet.com/rss/news",
    "https://www.arabictrader.com/rss/news",
    "https://arab.dailyforex.com/rss/arab/forexnews.xml"
]

POLL = 40
MAX = 20

# =====================
# DATABASE
# =====================
def db():
    conn = sqlite3.connect("posted.db")
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS posted (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

def exists(i):
    conn = sqlite3.connect("posted.db")
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM posted WHERE id=?", (i,))
    r = cur.fetchone()
    conn.close()
    return r is not None

def save(i):
    conn = sqlite3.connect("posted.db")
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO posted VALUES(?)", (i,))
    conn.commit()
    conn.close()

# =====================
# AI ANALYSIS (RULE BASED)
# =====================
def analyze(text):
    t = text.lower()

    sentiment = "محايد"
    if any(w in t for w in ["rise","gain","up","strong","bull","positive"]):
        sentiment = "إيجابي"
    if any(w in t for w in ["fall","drop","down","weak","bear","negative"]):
        sentiment = "سلبي"

    impact = "متوسط"
    if any(w in t for w in ["fed","cpi","inflation","interest","nfp","rate"]):
        impact = "عالي جدًا"
    elif any(w in t for w in ["gold","usd","oil","eurusd"]):
        impact = "عالي"

    asset = "عام"
    if "gold" in t or "xau" in t:
        asset = "الذهب"
    elif "usd" in t:
        asset = "الدولار"
    elif "oil" in t:
        asset = "النفط"

    golden = "⚠️ تحذير ذهبي محتمل" if asset == "الذهب" else ""

    return sentiment, impact, asset, golden

# =====================
# FORMAT
# =====================
def build(title, summary, link, src):
    s, i, a, g = analyze(title + " " + summary)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    msg = f"""
<b>{title}</b>

{summary[:240]}...

━━━━━━━━━━━━━━
📊 <b>قوة الخبر:</b> {i}
🧠 <b>اتجاه السوق:</b> {s}
🎯 <b>الأصول المتأثرة:</b> {a}
{g}
🕒 {now}
🔗 <b>المصدر ({src})</b>
{link}
━━━━━━━━━━━━━━
{SIGNATURE}
"""
    return msg

# =====================
# MAIN
# =====================
def main():
    db()
    bot = Bot(TOKEN)

    while True:
        for url in FEEDS:
            feed = feedparser.parse(url)
            src = "FX"

            for e in feed.entries[:MAX]:
                title = e.get("title","")
                link = e.get("link","")
                summary = e.get("summary","")

                hid = hashlib.md5((title+link).encode()).hexdigest()
                if exists(hid):
                    continue

                text = build(title, summary, link, src)
                bot.send_message(CHANNEL, text, parse_mode=ParseMode.HTML, disable_web_page_preview=False)

                save(hid)
                time.sleep(2)

        time.sleep(POLL)

if __name__ == "__main__":
    main()