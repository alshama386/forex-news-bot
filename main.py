import os, time, sqlite3, hashlib
from datetime import datetime, timezone
import feedparser
from telegram import Bot
from telegram.constants import ParseMode

TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL = "@news_forexq"
SIGNATURE = "\n\n✈️ @news_forexq"

FEEDS = [
    "https://ar.fxstreet.com/rss/news",
    "https://www.arabictrader.com/rss/news",
    "https://arab.dailyforex.com/rss/arab/forexnews.xml"
]

DB = "posted.db"

def init_db():
    with sqlite3.connect(DB) as c:
        c.execute("CREATE TABLE IF NOT EXISTS posted(id TEXT PRIMARY KEY)")
        c.commit()

def is_posted(i):
    with sqlite3.connect(DB) as c:
        return c.execute("SELECT 1 FROM posted WHERE id=?", (i,)).fetchone() is not None

def mark(i):
    with sqlite3.connect(DB) as c:
        c.execute("INSERT OR IGNORE INTO posted VALUES(?)", (i,))
        c.commit()

def clean(t):
    return " ".join((t or "").split())

def hash_id(t,l):
    return hashlib.sha256((t+l).encode()).hexdigest()

def analyze(text):
    t=text.lower()
    if any(k in t for k in ["gold","usd","fed","powell","cpi","nfp"]):
        return "عالي جداً","تأثير قوي جداً","ذهب، دولار"
    if any(k in t for k in ["oil","brent","wti"]):
        return "مرتفع","إيجابي","النفط"
    return "متوسط","محايد","الأسواق"

def build(title,summary,link,src):
    قوة, مزاج, أصول = analyze(title+summary)
    وقت = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")

    return f"""
🚨 <b>{title}</b>

{summary[:260]}

━━━━━━━━━━━━━━
📊 <b>قوة الخبر:</b> {قوة}
🧠 <b>اتجاه السوق:</b> {مزاج}
📌 <b>الأصول المتأثرة:</b> {أصول}
🕒 {وقت}
🔗 المصدر ({src})
{link}
━━━━━━━━━━━━━━
{SIGNATURE}
"""

def main():
    init_db()
    bot = Bot(TOKEN)

    while True:
        for url in FEEDS:
            feed = feedparser.parse(url)
            src = url.split("//")[1].split("/")[0]

            for e in feed.entries[:20]:
                t = clean(e.get("title",""))
                l = clean(e.get("link",""))
                s = clean(e.get("summary",""))

                if not t: continue
                i = hash_id(t,l)
                if is_posted(i): continue

                msg = build(t,s,l,src)
                bot.send_message(CHANNEL,msg,parse_mode=ParseMode.HTML,disable_web_page_preview=False)
                mark(i)
                time.sleep(1.5)

        time.sleep(30)

if __name__ == "__main__":
    main()