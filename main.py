import os, time, sqlite3, hashlib, asyncio, feedparser
from datetime import datetime, timezone
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
    c.execute("CREATE TABLE IF NOT EXISTS p (id TEXT PRIMARY KEY)")
    c.close()

def seen(i):
    c = sqlite3.connect(DB)
    r = c.execute("SELECT 1 FROM p WHERE id=?", (i,)).fetchone()
    c.close()
    return r

def mark(i):
    c = sqlite3.connect(DB)
    c.execute("INSERT OR IGNORE INTO p VALUES(?)", (i,))
    c.commit()
    c.close()

def h(t,l): return hashlib.md5((t+l).encode()).hexdigest()

def analyze(text):
    t = text.lower()
    if any(x in t for x in ["fed","powell","rate","inflation","cpi","nfp"]):
        return "عالي جداً","⚠️ تحذير ذهبي","سلبي" if "inflation" in t else "إيجابي"
    if any(x in t for x in ["gold","xau","oil","brent","wti"]):
        return "قوي","تحذير","إيجابي"
    return "متوسط","—","محايد"

def build(title,summary,link):
    قوة,تحذير,مزاج = analyze(title+summary)
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    return f"""
<b>{title}</b>

{summary[:240]}

⚡ <b>قوة الخبر:</b> {قوة}
🧠 <b>اتجاه السوق:</b> {مزاج}
🚨 <b>{تحذير}</b>

🕒 {now}
🔗 {link}

— @news_forexq
"""

async def main():
    init_db()
    bot = Bot(TOKEN)

    while True:
        for url in FEEDS:
            feed = feedparser.parse(url)
            for e in feed.entries[:15]:
                t = e.get("title","")
                l = e.get("link","")
                s = e.get("summary","")
                i = h(t,l)
                if seen(i): continue

                msg = build(t,s,l)
                await bot.send_message(CHANNEL,msg,parse_mode=ParseMode.HTML,disable_web_page_preview=False)
                mark(i)
                await asyncio.sleep(1.2)
        await asyncio.sleep(30)

asyncio.run(main())