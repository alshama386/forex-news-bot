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
SIGN = "\n✈️ @news_forexq"

FEEDS = [
    "https://www.investing.com/rss/news_1.rss",
    "https://ar.fxstreet.com/rss/news",
    "https://www.arabictrader.com/rss/news",
    "https://arab.dailyforex.com/rss/arab/forexnews.xml"
]

KEYWORDS = {
    "إيجابي": ["rise", "up", "bullish", "gain", "strong"],
    "سلبي": ["fall", "down", "bearish", "loss", "weak"],
}

DB="posted.db"

def db():
    c=sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS p(id TEXT PRIMARY KEY)")
    c.commit();c.close()

def seen(i):
    c=sqlite3.connect(DB);r=c.execute("SELECT 1 FROM p WHERE id=?", (i,)).fetchone();c.close()
    return r

def mark(i):
    c=sqlite3.connect(DB);c.execute("INSERT OR IGNORE INTO p VALUES(?)",(i,));c.commit();c.close()

def mood(txt):
    t=txt.lower()
    for k,v in KEYWORDS.items():
        if any(x in t for x in v): return k
    return "محايد"

def strength(txt):
    t=txt.lower()
    if any(x in t for x in ["rate","fed","powell","cpi","nfp","gold","oil"]): return "عالي جداً"
    return "متوسط"

def build(t,s,l,src):
    m=mood(t+s)
    st=strength(t+s)
    warn="🟡 تحذير ذهبي" if st=="عالي جداً" else "—"
    return f"""
📰 <b>{t}</b>

📊 <b>قوة الخبر:</b> {st}
🧠 <b>اتجاه السوق:</b> {m}
🚨 <b>{warn}</b>

🔗 المصدر ({src})
{l}

🕒 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}
{SIGN}
"""

def main():
    db()
    bot=Bot(TOKEN)
    while True:
        for url in FEEDS:
            f=feedparser.parse(url)
            src=url.split("//")[1].split("/")[0]
            for e in f.entries[:20]:
                t=e.get("title","")
                l=e.get("link","")
                s=e.get("summary","")
                h=hashlib.md5((t+l).encode()).hexdigest()
                if seen(h): continue
                msg=build(t,s,l,src)
                bot.send_message(CHANNEL,msg,parse_mode=ParseMode.HTML,disable_web_page_preview=False)
                mark(h)
                time.sleep(1)
        time.sleep(30)

if __name__=="__main__":
    main()