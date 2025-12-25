import os
import time
import sqlite3
import hashlib
import asyncio
from datetime import datetime, timezone, timedelta
import html

import feedparser
from deep_translator import GoogleTranslator
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError

# =========================
# CONFIG (Railway Env Vars)
# =========================
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing BOT_TOKEN in environment variables.")

CHANNEL_ID = os.environ.get("CHANNEL_ID") or os.environ.get("CHANNEL")
if not CHANNEL_ID:
    raise RuntimeError("Missing CHANNEL_ID (or CHANNEL) in environment variables.")

SIGNATURE = os.environ.get("SIGNATURE", "\n\n— @news_forexq")

DEFAULT_FEEDS = [
    "https://www.investing.com/rss/news_1.rss",
    "https://ar.fxstreet.com/rss/news",
    "https://www.arabictrader.com/rss/news",
    "https://arab.dailyforex.com/rss/arab/forexnews.xml",
]
RSS_FEEDS_ENV = (os.environ.get("RSS_FEEDS") or "").strip()
FEEDS = [f.strip() for f in RSS_FEEDS_ENV.split(",") if f.strip()] if RSS_FEEDS_ENV else DEFAULT_FEEDS

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
MAX_PER_FEED = int(os.environ.get("MAX_PER_FEED", "25"))
SUMMARY_MAX_CHARS = int(os.environ.get("SUMMARY_MAX_CHARS", "320"))
DB_FILE = os.environ.get("DB_FILE", "posted.db")
KUWAIT_TZ = timezone(timedelta(hours=3))

CTA_FOOTER = (
    "\n\n🌟 اذا استفدت من هذا المحتوى فإن المتابعة و النشر يساعدنا كثيراً\n"
    "أخبار الفوركس | Forex News\n"
    "https://t.me/news_forexq"
)

# =========================
# DATABASE
# =========================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS posted (id TEXT PRIMARY KEY, created_at TEXT)")
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
def make_hash_id(t,l): return hashlib.sha256((clean(t)+clean(l)).encode()).hexdigest()

def source_label(u):
    u=(u or "").lower()
    if "investing" in u: return "Investing"
    if "fxstreet" in u: return "FXStreet"
    if "arabictrader" in u: return "ArabicTrader"
    if "dailyforex" in u: return "DailyForex"
    return "News Source"

def to_ar(t):
    try: return GoogleTranslator(source="auto", target="ar").translate(clean(t))
    except: return clean(t)

def safe_html(t): return html.escape(t or "")

# =========================
# ANALYSIS
# =========================
URGENT = ["breaking","urgent","عاجل","الفيدرالي","باول","cpi","nfp","inflation","التضخم"]
POS = ["rise","gain","bullish","ارتفاع","مكاسب","إيجابي"]
NEG = ["fall","drop","bearish","هبوط","سلبي","مخاطر"]
GOLD = ["gold","xau","ذهب"]

def is_urgent(t,s): return any(k in (t+s).lower() for k in URGENT)
def sentiment(t,s):
    c=(t+s).lower()
    if sum(k in c for k in POS)>sum(k in c for k in NEG): return "إيجابي"
    if sum(k in c for k in NEG)>sum(k in c for k in POS): return "سلبي"
    return "محايد"
def strength(t,s,u): return "عالي" if u else "متوسط"
def assets(t,s): return "الذهب" if any(k in (t+s).lower() for k in GOLD) else "العملات"

# =========================
# MESSAGE BUILDER (FINAL TEMPLATE)
# =========================
def build(title, summary, src, urgent, strength_ar, sentiment_ar, assets_ar):
    title = safe_html(title)
    summary = safe_html(summary[:SUMMARY_MAX_CHARS])
    head = "🚨 عاجل\n" if urgent else "📰 خبر اقتصادي\n"

    msg = (
        f"{head}📉 {title}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ قوة الخبر : {strength_ar}\n"
        f"🧠 اتجاه السوق : {sentiment_ar}\n"
        f"💱 الأصول المتأثرة : {assets_ar}\n"
        f"🕒 الوقت : {datetime.now(KUWAIT_TZ).strftime('%Y-%m-%d | %H:%M')} (الكويت)\n"
        f"📰 المصدر : {src}\n"
        "━━━━━━━━━━━━━━━━━━━━"
        f"{CTA_FOOTER}"
    )
    return msg

# =========================
# MAIN LOOP
# =========================
async def run():
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
                msg = build(t_ar, s_ar, src, u, strength(t,s,u), sentiment(t,s), assets(t,s))
                await bot.send_message(chat_id=CHANNEL_ID, text=msg, parse_mode=ParseMode.HTML)
                mark_posted(hid)
                await asyncio.sleep(1.5)
        await asyncio.sleep(POLL_SECONDS)

if __name__ == "__main__":
    asyncio.run(run())