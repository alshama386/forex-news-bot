import os, time, sqlite3, hashlib
from datetime import datetime, timezone
import feedparser
from telegram import Bot
from telegram.constants import ParseMode

TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL = "@news_forexq"

FEEDS = [
    "https://ar.fxstreet.com/rss/news",
    "https://arab.dailyforex.com/rss/arab/forexnews.xml"
]

DB_FILE = "posted.db"

def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS posted(id TEXT PRIMARY KEY)")
    con.commit(); con.close()

def posted(i):
    con = sqlite3.connect(DB_FILE); cur = con.cursor()
    cur.execute("SELECT 1 FROM posted WHERE id=?", (i,))
    r = cur.fetchone(); con.close()
    return r is not None

def mark(i):
    con = sqlite3.connect(DB_FILE); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO posted VALUES(?)", (i,))
    con.commit(); con.close()

def clean(t):
    return " ".join(t.replace("\n"," ").split()) if t else ""

def hid(title, link):
    return hashlib.sha256((title+link).encode()).hexdigest()

def sentiment(t):
    t=t.lower()
    if any(x in t for x in ["rise","up","positive","gain"]): return "إيجابي"
    if any(x in t for x in ["fall","down","negative","loss"]): return "سلبي"
    return "محايد"

def strength(t):
    if any(x in t for x in ["fed","rate","cpi","nfp","inflation"]): return "عالي"
    return "متوسط"

def build(title, summary, src):
    mood = sentiment(title+summary)
    power = strength(title+summary)
    s = f"""
<b>📌 {title}</b>

📝 <b>ملخص الخبر:</b>
{summary}

⚡ <b>قوة الخبر:</b> {power}
🧠 <b>اتجاه السوق:</b> {mood}

🕒 {datetime.now().strftime('%Y-%m-%d %H:%M')}
🔗 <b>المصدر:</b> {src}

— @news_forexq
"""
    return s

def source(u):
    if "fxstreet" in u: return "FXStreet"
    if "dailyforex" in u: return "DailyForex"
    return "Source"

def main():
    init_db()
    bot = Bot(TOKEN)

    while True:
        for f in FEEDS:
            feed = feedparser.parse(f)
            src = source(f)

            for e in feed.entries[:20]:
                title = clean(e.get("title",""))
                summary = clean(e.get("summary",""))
                link = clean(e.get("link",""))

                i = e.get("id") or hid(title,link)
                if posted(i): continue

                msg = build(title, summary, src)
                bot.send_message(CHANNEL, msg, parse_mode=ParseMode.HTML)
                mark(i)
                time.sleep(1)

        time.sleep(60)

if __name__ == "__main__":
    main()