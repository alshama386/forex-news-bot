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

# تقدر تحطها @news_forexq أو ID مثل: -1003235703803
CHANNEL_ID = os.environ.get("CHANNEL_ID") or os.environ.get("CHANNEL")
if not CHANNEL_ID:
    raise RuntimeError("Missing CHANNEL_ID (or CHANNEL) in environment variables.")

# التوقيع
SIGNATURE = os.environ.get("SIGNATURE", "\n\n— @news_forexq")

# RSS feeds: إذا RSS_FEEDS موجودة (comma-separated) نستخدمها، غير جذي نستخدم الافتراضي
DEFAULT_FEEDS = [
    "https://www.investing.com/rss/news_1.rss",
    "https://ar.fxstreet.com/rss/news",
    "https://www.arabictrader.com/rss/news",
    "https://arab.dailyforex.com/rss/arab/forexnews.xml",
]
RSS_FEEDS_ENV = (os.environ.get("RSS_FEEDS") or "").strip()
FEEDS = [f.strip() for f in RSS_FEEDS_ENV.split(",") if f.strip()] if RSS_FEEDS_ENV else DEFAULT_FEEDS

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))  # خله 60 أأمن من flood
MAX_PER_FEED = int(os.environ.get("MAX_PER_FEED", "25"))
SUMMARY_MAX_CHARS = int(os.environ.get("SUMMARY_MAX_CHARS", "320"))

DB_FILE = os.environ.get("DB_FILE", "posted.db")

# توقيت الكويت UTC+3
KUWAIT_TZ = timezone(timedelta(hours=3))

# =========================
# DB (Persistent De-dup)
# =========================
def init_db() -> None:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS posted (
            id TEXT PRIMARY KEY,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def already_posted(item_id: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM posted WHERE id=?", (item_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None

def mark_posted(item_id: str) -> None:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO posted (id, created_at) VALUES (?, ?)",
        (item_id, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()

# =========================
# HELPERS
# =========================
def clean(text: str) -> str:
    if not text:
        return ""
    return " ".join(str(text).replace("\n", " ").split()).strip()

def make_hash_id(title: str, link: str) -> str:
    raw = (clean(title) + "||" + clean(link)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def source_label(feed_url: str) -> str:
    u = (feed_url or "").lower()
    if "investing" in u:
        return "Investing"
    if "fxstreet" in u:
        return "FXStreet"
    if "arabictrader" in u:
        return "ArabicTrader"
    if "dailyforex" in u:
        return "DailyForex"
    return "Source"

def to_arabic(text: str) -> str:
    """Translate any non-Arabic text to Arabic."""
    text = clean(text)
    if not text:
        return ""
    try:
        return GoogleTranslator(source="auto", target="ar").translate(text)
    except Exception:
        return text

def safe_html(text: str) -> str:
    """Escape HTML to prevent Telegram parse errors."""
    return html.escape(text or "")

# -------------------------
# ANALYSIS (Professional Tags)
# -------------------------
URGENT_KEYWORDS = [
    "breaking", "flash", "urgent",
    "fed", "powell", "rate decision", "interest rate",
    "cpi", "inflation", "nfp", "jobs report",
    "boj", "ecb", "bank of england",
    "intervention", "sanctions", "war", "conflict",
    "crash", "plunge", "surge",
    "عاجل", "فلاش", "سريع",
    "الفيدرالي", "باول", "رفع الفائدة", "خفض الفائدة",
    "التضخم", "مؤشر أسعار", "الوظائف", "الرواتب",
    "تدخل", "عقوبات", "حرب", "توتر",
    "انهيار", "هبوط حاد", "ارتفاع قوي",
]

GOLD_KEYWORDS = ["gold", "xau", "xauusd", "ذهب", "الذهب"]
OIL_KEYWORDS = ["oil", "brent", "wti", "نفط", "النفط"]
USD_KEYWORDS = ["usd", "dollar", "الدولار"]
JPY_KEYWORDS = ["jpy", "yen", "الين"]
EUR_KEYWORDS = ["eur", "euro", "اليورو"]
GBP_KEYWORDS = ["gbp", "pound", "الجنيه"]

POSITIVE_WORDS = [
    "rise", "rises", "up", "gain", "gains", "surge", "strong", "bullish",
    "improve", "improves", "optimism", "beats", "higher",
    "يرتفع", "ارتفاع", "يصعد", "مكاسب", "قوي", "إيجابي", "تفاؤل", "أفضل", "أعلى",
]
NEGATIVE_WORDS = [
    "fall", "falls", "down", "drop", "drops", "plunge", "weak", "bearish",
    "worse", "risk", "recession", "concern", "lower",
    "يهبط", "هبوط", "ينخفض", "خسائر", "ضعيف", "سلبي", "مخاطر", "ركود", "قلق", "أقل",
]

def is_urgent(raw_title: str, raw_summary: str) -> bool:
    combined = (raw_title + " " + raw_summary).lower()
    return any(k.lower() in combined for k in URGENT_KEYWORDS)

def market_sentiment(raw_title: str, raw_summary: str) -> str:
    combined = (raw_title + " " + raw_summary).lower()
    pos = sum(1 for w in POSITIVE_WORDS if w.lower() in combined)
    neg = sum(1 for w in NEGATIVE_WORDS if w.lower() in combined)
    if pos > neg and pos >= 1:
        return "إيجابي ✅"
    if neg > pos and neg >= 1:
        return "سلبي ⚠️"
    return "محايد ⚪"

def news_strength(raw_title: str, raw_summary: str, urgent: bool) -> str:
    combined = (raw_title + " " + raw_summary).lower()
    score = 0
    if urgent:
        score += 3
    for k in ["fed", "fomc", "powell", "cpi", "inflation", "nfp", "rate",
              "الفيدرالي", "باول", "التضخم", "الوظائف", "الفائدة"]:
        if k.lower() in combined:
            score += 2

    if score >= 5:
        return "عالي جداً ⚡"
    if score >= 3:
        return "عالي 🔥"
    if score >= 1:
        return "متوسط ✨"
    return "منخفض"

def affected_assets(raw_title: str, raw_summary: str) -> str:
    combined = (raw_title + " " + raw_summary).lower()
    assets = []
    if any(k in combined for k in GOLD_KEYWORDS):
        assets.append("الذهب (XAUUSD)")
    if any(k in combined for k in OIL_KEYWORDS):
        assets.append("النفط")
    if any(k in combined for k in USD_KEYWORDS):
        assets.append("الدولار (USD)")
    if any(k in combined for k in JPY_KEYWORDS):
        assets.append("الين (JPY)")
    if any(k in combined for k in EUR_KEYWORDS):
        assets.append("اليورو (EUR)")
    if any(k in combined for k in GBP_KEYWORDS):
        assets.append("الجنيه (GBP)")
    return "، ".join(dict.fromkeys(assets)) if assets else "الأسواق / العملات"

def golden_warning_flag(raw_title: str, raw_summary: str) -> str:
    combined = (raw_title + " " + raw_summary).lower()
    if any(k in combined for k in GOLD_KEYWORDS):
        return "🟡 <b>تنبيه الذهب</b>: قد يؤثر على الذهب (XAUUSD)"
    return ""

# =========================
# MESSAGE BUILDER (Clean Format)
# =========================
def build_message(
    title_ar: str,
    summary_ar: str,
    link: str,
    src: str,
    urgent: bool,
    strength_ar: str,
    sentiment_ar: str,
    assets_ar: str,
    golden_warning: str
) -> str:
    title_ar = safe_html(clean(title_ar))
    summary_ar = safe_html(clean(summary_ar))
    link = clean(link)

    if summary_ar:
        summary_ar = summary_ar[:SUMMARY_MAX_CHARS] + ("..." if len(summary_ar) > SUMMARY_MAX_CHARS else "")

    header = "🚨 <b>عاجل</b>\n" if urgent else "📰 <b>خبر اقتصادي</b>\n"
    now_kw = datetime.now(KUWAIT_TZ).strftime("%Y-%m-%d %H:%M")

    msg = f"{header}<b>{title_ar}</b>\n"

    if summary_ar:
        msg += f"\n{summary_ar}\n"

    if golden_warning:
        msg += f"\n{golden_warning}\n"

    msg += (
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>قوة الخبر</b>: {safe_html(strength_ar)}\n"
        f"🧠 <b>اتجاه السوق</b>: {safe_html(sentiment_ar)}\n"
        f"📌 <b>الأصول المتأثرة</b>: {safe_html(assets_ar)}\n"
        f"🕒 <b>الوقت</b>: {safe_html(now_kw)} (الكويت)\n"
        f"🔗 <b>المصدر</b> ({safe_html(src)}):\n{safe_html(link)}\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

    msg += SIGNATURE
    return msg

# =========================
# SAFE SEND (Handle Flood/Timeout)
# =========================
async def safe_send(bot: Bot, text: str) -> None:
    while True:
        try:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False
            )
            return
        except RetryAfter as e:
            wait_s = int(getattr(e, "retry_after", 5))
            print(f"RetryAfter: sleeping {wait_s}s")
            await asyncio.sleep(wait_s)
        except (TimedOut, NetworkError) as e:
            print("Network/Timeout:", e)
            await asyncio.sleep(5)
        except Exception as e:
            print("Send error:", e)
            # إذا صار خطأ غير متوقع، نطلع عشان ما نعلق للأبد
            return

# =========================
# MAIN LOOP
# =========================
async def run() -> None:
    init_db()
    bot = Bot(token=TOKEN)

    print("Bot Running...")
    print("Channel:", CHANNEL_ID)
    print("Feeds:", FEEDS)

    while True:
        try:
            for url in FEEDS:
                feed = feedparser.parse(url)
                src = source_label(url)

                for entry in feed.entries[:MAX_PER_FEED]:
                    raw_title = clean(entry.get("title", ""))
                    link = clean(entry.get("link", ""))
                    raw_summary = clean(entry.get("summary") or entry.get("description") or "")

                    if not raw_title and not link:
                        continue

                    item_id = entry.get("id") or make_hash_id(raw_title, link)
                    if already_posted(item_id):
                        continue

                    urgent = is_urgent(raw_title, raw_summary)

                    # Translate to Arabic
                    title_ar = to_arabic(raw_title)
                    summary_ar = to_arabic(raw_summary)

                    sentiment_ar = market_sentiment(raw_title, raw_summary)
                    strength_ar = news_strength(raw_title, raw_summary, urgent)
                    assets_ar = affected_assets(raw_title, raw_summary)
                    golden_warning = golden_warning_flag(raw_title, raw_summary)

                    text = build_message(
                        title_ar=title_ar,
                        summary_ar=summary_ar,
                        link=link,
                        src=src,
                        urgent=urgent,
                        strength_ar=strength_ar,
                        sentiment_ar=sentiment_ar,
                        assets_ar=assets_ar,
                        golden_warning=golden_warning
                    )

                    await safe_send(bot, text)
                    mark_posted(item_id)

                    # تهدئة بسيطة عشان ما يصير Flood
                    await asyncio.sleep(1.5)

            await asyncio.sleep(POLL_SECONDS)

        except Exception as ex:
            print("Loop Error:", ex)
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(run())