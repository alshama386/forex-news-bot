import os
import time
import sqlite3
import hashlib
import asyncio
from datetime import datetime, timezone, timedelta

import feedparser
from deep_translator import GoogleTranslator
from telegram import Bot
from telegram.constants import ParseMode

# =========================
# CONFIG
# =========================
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise Exception("BOT_TOKEN missing in environment variables")

CHANNEL = "@news_forexq"
SIGNATURE = "\n\n— @news_forexq"

FEEDS = [
    "https://www.investing.com/rss/news_1.rss",
    "https://ar.fxstreet.com/rss/news",
    "https://www.arabictrader.com/rss/news",
    "https://arab.dailyforex.com/rss/arab/forexnews.xml",
]

POLL_SECONDS = 25
MAX_PER_FEED = 25
SUMMARY_MAX_CHARS = 320
DB_FILE = "posted.db"

# =========================
# DATABASE
# =========================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS posted (id TEXT PRIMARY KEY, created_at TEXT)""")
    conn.commit()
    conn.close()

def already_posted(i):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM posted WHERE id=?", (i,))
    r = cur.fetchone()
    conn.close()
    return r is not None

def mark_posted(i):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO posted VALUES (?,?)", (i, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()

# =========================
# HELPERS
# =========================
def clean(t): return " ".join((t or "").replace("\n"," ").split())

def make_hash_id(t,l):
    return hashlib.sha256((clean(t)+clean(l)).encode()).hexdigest()

def source_label(u):
    u=u.lower()
    if "investing" in u: return "Investing"
    if "fxstreet" in u: return "FXStreet"
    if "arabictrader" in u: return "ArabicTrader"
    if "dailyforex" in u: return "DailyForex"
    return "News Source"

def to_ar(t):
    try: return GoogleTranslator(source="auto", target="ar").translate(clean(t))
    except: return clean(t)

# =========================
# ANALYSIS
# =========================
URGENT = ["breaking","urgent","عاجل","الفيدرالي","باول","cpi","nfp","inflation","التضخم"]
POS = ["rise","gain","bullish","ارتفاع","مكاسب","إيجابي"]
NEG = ["fall","drop","bearish","هبوط","سلبي","مخاطر"]
GOLD = ["gold","xau","ذهب"]

def is_urgent(t,s):
    c=(t+s).lower()
    return any(k in c for k in URGENT)

def sentiment(t,s):
    c=(t+s).lower()
    p=sum(k in c for k in POS)
    n=sum(k in c for k in NEG)
    if p>n: return "إيجابي"
    if n>p: return "سلبي"
    return "محايد"

def strength(t,s,u):
    score = 3 if u else 1
    return "عالي" if score>2 else "متوسط"

def assets(t,s):
    return "الذهب" if any(k in (t+s).lower() for k in GOLD) else "العملات"

# =========================
# MESSAGE
# =========================
def build(title, summary, src, urgent, strength_ar, sentiment_ar, assets_ar):
    head = "🚨 <b>عاجل</b>\n" if urgent else "📰 "
    return (
        f"{head}<b>{title}</b>\n\n{summary[:SUMMARY_MAX_CHARS]}"
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 قوة الخبر: {strength_ar}\n"
        f"🧠 اتجاه السوق: {sentiment_ar}\n"
        f"📌 الأصول المتأثرة: {assets_ar}\n"
        f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"🔗 المصدر: {src}\n"
        "━━━━━━━━━━━━━━━━━━━━"
        + SIGNATURE
    )

# =========================
# MAIN LOOP
# =========================
async def main():
    init_db()
    bot = Bot(token=TOKEN)
    while True:
        for url in FEEDS:
            feed = feedparser.parse(url)
            src = source_label(url)
            for e in feed.entries[:MAX_PER_FEED]:
                t = clean(e.get("title"))
                s = clean(e.get("summary",""))
                if not t: continue
                hid = e.get("id") or make_hash_id(t, "")
                if already_posted(hid): continue

                t_ar = to_ar(t)
                s_ar = to_ar(s)

                u = is_urgent(t,s)
                text = build(
                    t_ar,
                    s_ar,
                    src,
                    u,
                    strength(t,s,u),
                    sentiment(t,s),
                    assets(t,s)
                )
                await bot.send_message(chat_id=CHANNEL, text=text, parse_mode=ParseMode.HTML)
                mark_posted(hid)
                await asyncio.sleep(1.5)
        await asyncio.sleep(POLL_SECONDS)

asyncio.run(main())