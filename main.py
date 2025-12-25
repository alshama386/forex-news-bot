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
    raise Exception("BOT_TOKEN missing in environment variables (set BOT_TOKEN)")

# تقدر تخليها متغير بيئة لو تحب:
# CHANNEL = os.environ.get("CHANNEL_ID", "@news_forexq")
CHANNEL = "@news_forexq"

SIGNATURE = "\n\n— @news_forexq"

FOLLOW_FOOTER = (
    "\n\n🌟 اذا استفدت من هذا المحتوى فإن المتابعة و النشر يساعدنا كثيراً\n"
    "أخبار الفوركس forex news\n"
    "https://t.me/news_forexq"
)

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
    return " ".join(text.replace("\n", " ").split()).strip()

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
    text = clean(text)
    if not text:
        return ""
    try:
        return GoogleTranslator(source="auto", target="ar").translate(text)
    except:
        return text

# =========================
# FILTER: Remove Israel ECONOMIC only (keep war/politics)
# =========================
ISRAEL_ECON_WORDS = [
    "israel","israeli",
    "tel aviv","jerusalem",
    "تل ابيب","تل أبيب","القدس",
    "بنك اسرائيل","بنك إسرائيل",
    "shekel","ils","₪","الشيكل","شيكل",
    "tase","ta-35","israel bonds",
    "israel economy","economic israel",
    "اقتصاد اسرائيل","الاقتصاد الاسرائيلي","الاقتصاد الإسرائيلي"
]

ECONOMIC_WORDS = [
    # EN
    "rate","interest","inflation","cpi","gdp","jobs","nfp","unemployment",
    "central bank","bond","bonds","stocks","stock","market","index","yield",
    "currency","fx","forex",
    # AR
    "الفائدة","رفع الفائدة","خفض الفائدة","التضخم","مؤشر أسعار","الناتج","الناتج المحلي",
    "الوظائف","الرواتب","البطالة","البنك المركزي","سندات","أسهم","سوق","مؤشر","عائد",
    "عملة","فوركس"
]

# كلمات سياسية/حرب نستخدمها كـ "استثناء" (حتى لو فيه اقتصاد، نخلي الخبر يمر إذا واضح أنه سياسي/حرب)
WAR_POLITICS_WORDS = [
    "war","strike","airstrike","attack","missile","rocket","ceasefire","truce",
    "conflict","tension","escalation","sanctions","diplomacy","talks",
    "حرب","قصف","غارة","هجوم","صاروخ","هدنة","وقف إطلاق النار",
    "صراع","توتر","تصعيد","عقوبات","محادثات","دبلوماسية","سياسي","سياسة"
]

def should_block_news(raw_title: str, raw_summary: str, link: str) -> bool:
    combined = (raw_title + " " + raw_summary + " " + (link or "")).lower()

    has_israel = any(k in combined for k in ISRAEL_ECON_WORDS)
    has_econ = any(k in combined for k in ECONOMIC_WORDS)
    has_war_pol = any(k in combined for k in WAR_POLITICS_WORDS)

    # ❌ امنع فقط: إسرائيل + اقتصادي  (لكن إذا واضح حرب/سياسة، خله يمر)
    return (has_israel and has_econ and not has_war_pol)

# =========================
# ANALYSIS (Professional Tags)
# =========================
URGENT_KEYWORDS = [
    # EN
    "breaking", "flash", "urgent",
    "fed", "powell", "rate decision", "interest rate",
    "cpi", "inflation", "nfp", "jobs report",
    "boj", "ecb", "bank of england",
    "intervention", "sanctions", "war", "conflict",
    "crash", "plunge", "surge",
    # AR
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
    # EN
    "rise", "rises", "up", "gain", "gains", "surge", "strong", "bullish",
    "improve", "improves", "optimism", "beats", "higher",
    # AR
    "يرتفع", "ارتفاع", "يصعد", "مكاسب", "قوي", "إيجابي", "تفاؤل", "أفضل", "أعلى",
]

NEGATIVE_WORDS = [
    # EN
    "fall", "falls", "down", "drop", "drops", "plunge", "weak", "bearish",
    "worse", "risk", "recession", "concern", "lower",
    # AR
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
        return "إيجابي"
    if neg > pos and neg >= 1:
        return "سلبي"
    return "محايد"

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
        return "عالي جداً"
    if score >= 3:
        return "عالي"
    if score >= 1:
        return "متوسط"
    return "منخفض"

def affected_assets(raw_title: str, raw_summary: str) -> str:
    combined = (raw_title + " " + raw_summary).lower()
    assets = []

    if any(k in combined for k in GOLD_KEYWORDS):
        assets.append("الذهب")
    if any(k in combined for k in OIL_KEYWORDS):
        assets.append("النفط")
    if any(k in combined for k in USD_KEYWORDS):
        assets.append("الدولار")
    if any(k in combined for k in JPY_KEYWORDS):
        assets.append("الين")
    if any(k in combined for k in EUR_KEYWORDS):
        assets.append("اليورو")
    if any(k in combined for k in GBP_KEYWORDS):
        assets.append("الجنيه الإسترليني")

    if not assets:
        return "العملات / الأسواق"
    return "، ".join(dict.fromkeys(assets))

def golden_warning_flag(raw_title: str, raw_summary: str) -> str:
    combined = (raw_title + " " + raw_summary).lower()
    if any(k in combined for k in GOLD_KEYWORDS):
        return "🟡 <b>تحذير ذهبي</b>: خبر قد يؤثر على الذهب (XAUUSD)"
    return ""

# =========================
# MESSAGE BUILDER (No Link, Source only)
# =========================
def build_message(
    title_ar: str,
    summary_ar: str,
    src: str,
    urgent: bool,
    strength_ar: str,
    sentiment_ar: str,
    assets_ar: str,
    golden_warning: str
) -> str:
    title_ar = clean(title_ar)
    summary_ar = clean(summary_ar)

    if summary_ar:
        summary_ar = summary_ar[:SUMMARY_MAX_CHARS] + ("..." if len(summary_ar) > SUMMARY_MAX_CHARS else "")

    header = "🚨 <b>عاجل</b>\n" if urgent else "📰 "
    kuwait_time = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=3))).strftime('%Y-%m-%d %H:%M')

    msg = f"{header}<b>{title_ar}</b>\n"
    if summary_ar:
        msg += f"\n{summary_ar}\n"

    if golden_warning:
        msg += f"\n{golden_warning}\n"

    msg += (
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>قوة الخبر</b>: {strength_ar}\n"
        f"🧠 <b>اتجاه السوق</b>: {sentiment_ar}\n"
        f"📌 <b>الأصول المتأثرة</b>: {assets_ar}\n"
        f"🕒 <b>الوقت</b>: {kuwait_time} (الكويت)\n"
        f"🔗 <b>المصدر</b>: {src}\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

    msg += SIGNATURE
    msg += FOLLOW_FOOTER
    return msg

# =========================
# MAIN LOOP
# =========================
async def main() -> None:
    init_db()
    bot = Bot(token=TOKEN)

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

                    # ✅ فلترة: شيل أخبار إسرائيل الاقتصادية فقط
                    if should_block_news(raw_title, raw_summary, link):
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
                        src=src,
                        urgent=urgent,
                        strength_ar=strength_ar,
                        sentiment_ar=sentiment_ar,
                        assets_ar=assets_ar,
                        golden_warning=golden_warning
                    )

                    await bot.send_message(
                        chat_id=CHANNEL,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True  # لأن ماكو رابط، وخليها True عشان ما يطلع preview مزعج
                    )

                    mark_posted(item_id)
                    await asyncio.sleep(1.2)

            await asyncio.sleep(POLL_SECONDS)

        except Exception as ex:
            print("Error:", ex)
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())