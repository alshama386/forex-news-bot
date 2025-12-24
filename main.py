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

DB = "posted.db"

def init_db():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS posted(id TEXT PRIMARY KEY)")
    c.commit()
    c.close()

def posted(pid):
    c = sqlite3.connect(DB)
    r = c.execute("SELECT 1 FROM posted WHERE id=?", (pid,)).fetchone()
    c.close()
    return r is not None

def mark(pid):
    c = sqlite3.connect(DB)
    c.execute("INSERT OR IGNORE INTO posted VALUES(?)", (pid,))
    c.commit()
    c.close()

def clean(t): return " ".join(str(t).replace("\n"," ").split())

def hid(t,l):
    return hashlib.sha1((t+l).encode()).hexdigest()

def mood(text):
    t = text.lower()
    if any(x in t for x in ["rate hike","inflation","hawkish","tightening"]):
        return "🔴 سلبي جداً","⚠️ عالي جداً"
    if any(x in t for x in ["gold","safe haven","demand","bullish"]):
        return "🟢 إيجابي","⬆️ عالي"
    return "⚪ محايد","➡️ متوسط"

def build(title,summary,link,src):
    m,lvl = mood(title+summary)
    warn = "🚨 تحذير ذهبي" if lvl=="⚠️ عالي جداً" else "🟡 مراقبة"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    return f"""
<b>{title}</b>

{summary[:280]}...

{warn}
📊 <b>قوة الخبر:</b> {lvl}
🧠 <b>مزاج السوق:</b> {m}
📌 <b>الأصول المتأثرة:</b> ذهب – دولار – يورو – نفط

🕒 {now}
🔗 المصدر: {src}
{link}

✈️ @news_forexq
"""

def main():
    init_db()
    bot = Bot(TOKEN)

    while True:
        try:
            for url in FEEDS:
                feed = feedparser.parse(url)
                src = url.split("/")[2]
                for e in feed.entries[:20]:
                    t = clean(e.get("title",""))
                    l = clean(e.get("link",""))
                    s = clean(e.get("summary",""))
                    if not t or not l: continue
                    pid = hid(t,l)
                    if posted(pid): continue

                    bot.send_message(CHANNEL, build(t,s,l,src), parse_mode=ParseMode.HTML, disable_web_page_preview=False)
                    mark(pid)
                    time.sleep(1.5)

            time.sleep(40)
        except Exception as ex:
            print(ex)
            time.sleep(10)

if __name__ == "__main__":
    main()