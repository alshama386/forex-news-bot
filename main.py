import os
import time
import sqlite3
import hashlib
from datetime import datetime, timezone
import feedparser
from telegram import Bot
from telegram.constants import ParseMode

TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL = "@news_forexq"

FEEDS = [
    "https://www.investing.com/rss/news_1.rss",
    "https://ar.fxstreet.com/rss/news",
    "https://www.arabictrader.com/rss/news",
    "https://arab.dailyforex.com/rss/arab/forexnews.xml"
]

DB_FILE = "posted.db"
POLL_SECONDS = 40

# ================= DB =================
def init_db():
    with sqlite3.connect(DB_FILE) as c:
        c.execute("CREATE TABLE IF NOT EXISTS posted (id TEXT PRIMARY KEY)")
        c.commit()

def is_posted(i):
    with sqlite3.connect(DB_FILE) as c:
        return c.execute("SELECT 1 FROM posted WHERE id=?", (i,)).fetchone()

def mark_posted(i):
    with sqlite3.connect(DB_FILE) as c:
        c.execute("INSERT OR IGNORE INTO posted VALUES(?)", (i,))
        c.commit()

# ================= LOGIC =================
def clean(t):
    return " ".join((t or "").split())

def hid(t,l):
    return hashlib.md5((t+l).encode()).hexdigest()

def impact(text):
    t = text.lower()
    if any(x in t for x in ["cpi","nfp","fed","interest","rate"]):
        return "🔥 عالي جداً"
    if any(x in t for x in ["gold","oil","usd","eur"]):
        return "⚡ عالي"
    return "🟡 متوسط"

def mood(text):
    t = text.lower()
    if any(x in t for x in ["rise","up","bull"]):
        return "🟢 إيجابي"
    if any(x in t for x in ["fall","down","bear"]):
        return "🔴 سلبي"
    return "⚪ محايد"

def asset(text):
    t=text.lower()
    if "gold" in t: return "الذهب"
    if "oil" in t: return "النفط"
    if "usd" in t: return "الدولار"
    if "eur" in t: return "اليورو"
    return "السوق العام"

def build(t,s,l):
    return f"""
🚨 <b>{clean(t)}</b>

🧠 <b>الاتجاه:</b> {mood(t+s)}
📊 <b>قوة الخبر:</b> {impact(t+s)}
📌 <b>الأصل المتأثر:</b> {asset(t+s)}

🕒 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC

🔗 المصدر:
{l}

━━━━━━━━━━━━━━
⚠️ <b>تحذير ذهبي:</b>
تجنب الدخول قبل تأكيد الحركة

✈️ @news_forexq
"""

# ================= MAIN =================
def main():
    init_db()
    bot = Bot(TOKEN)
    print("Bot Running...")

    while True:
        try:
            for f in FEEDS:
                feed = feedparser.parse(f)
                for e in feed.entries[:15]:
                    t = clean(e.get("title",""))
                    l = clean(e.get("link",""))
                    if not t: continue
                    i = hid(t,l)
                    if is_posted(i): continue
                    bot.send_message(CHANNEL, build(t,"",l), parse_mode=ParseMode.HTML, disable_web_page_preview=False)
                    mark_posted(i)
                    time.sleep(1.5)
            time.sleep(POLL_SECONDS)
        except Exception as ex:
            print("ERR:", ex)
            time.sleep(10)

if __name__ == "__main__":
    main()