import os
import re
import time
import asyncio
import sqlite3
import hashlib
from datetime import datetime, timezone, timedelta

import feedparser
from telegram import Bot
from telegram.constants import ParseMode

# =========================
# CONFIG
# =========================
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise Exception("BOT_TOKEN missing in environment variables (BOT_TOKEN)")

CHANNEL = "@news_forexq"
SIGNATURE = "\n\n— @news_forexq"

# ✅ عربي فقط (شيلنا Investing لأنه يطلع إنجليزي)
NEWS_FEEDS = [
    "https://ar.fxstreet.com/rss/news",
    "https://www.arabictrader.com/rss/news",
    "https://arab.dailyforex.com/rss/arab/forexnews.xml",
]

# ✅ تقويم اقتصادي (للتنبيهات قبل الأخبار الكبيرة)
# ملاحظة: هذا مصدر تقويم اقتصادي (قد يتغير رابطهم مستقبلاً)
CALENDAR_FEEDS = [
    "https://www.myfxbook.com/rss/forex-economic-calendar-events",
]

POLL_NEWS_SECONDS = 30
POLL_CALENDAR_SECONDS = 30

MAX_PER_FEED = 25
SUMMARY_MAX_CHARS = 320

ALERTS_MINUTES = [30, 5]  # تنبيه قبل 30 دقيقة وقبل 5 دقائق

# كلمات “أحداث كبيرة” للتنبيه
MAJOR_EVENT_KEYWORDS = [
    "fomc", "fed", "powell", "interest rate", "rate decision",
    "cpi", "inflation", "ppi",
    "nfp", "nonfarm", "jobs", "unemployment",
    "gdp", "retail sales",
    "boe", "ecb", "boj",
]

# =========================
# DB (SQLite) - Dedup & Alerts
# =========================
DB_FILE = "posted.db"

def db_conn():
    return sqlite3.connect(DB_FILE)

def init_db() -> None:
    conn = db_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS posted (
            id TEXT PRIMARY KEY,
            created_at TEXT
        )
    """)

    # alerts table to prevent sending same alert repeatedly
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY,
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()

def seen(table: str, item_id: str) -> bool:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(f"SELECT 1 FROM {table} WHERE id=?", (item_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None

def mark_seen(table: str, item_id: str) -> None:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        f"INSERT OR IGNORE INTO {table} (id, created_at) VALUES (?, ?)",
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
    # remove html tags loosely
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.replace("\n", " ").split()).strip()

def make_hash_id(*parts: str) -> str:
    raw = "||".join(clean(p) for p in parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def source_label(feed_url: str) -> str:
    u = feed_url.lower()
    if "fxstreet" in u:
        return "FXStreet"
    if "arabictrader" in u:
        return "المتداول العربي"
    if "dailyforex" in u:
        return "DailyForex"
    if "myfxbook" in u:
        return "Myfxbook Calendar"
    return "المصدر"

def is_mostly_arabic(text: str) -> bool:
    # if Arabic letters ratio is low -> treat as non-arabic
    if not text:
        return False
    arabic = sum(1 for ch in text if "\u0600" <= ch <= "\u06FF")
    latin = sum(1 for ch in text if ("A" <= ch <= "Z") or ("a" <= ch <= "z"))
    # allow some latin (pairs like USD/JPY) لكن إذا طغى الإنجليزي نرفضه
    return arabic >= 10 and latin < arabic

def infer_market_mood(text: str) -> str:
    t = text.lower()
    pos = ["يرتفع", "يصعد", "مكاسب", "يدعم", "قوي", "تعافى", "تحسن", "gains", "rises", "strong"]
    neg = ["ينخفض", "يهبط", "خسائر", "ضعيف", "ضغط", "يتراجع", "هبوط", "falls", "drops", "weak"]
    if any(w in t for w in pos):
        return "إيجابي"
    if any(w in t for w in neg):
        return "سلبي"
    return "محايد"

def infer_strength(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ["عاجل", "breaking", "urgent", "flash"]):
        return "عالي جداً"
    if any(k in t for k in MAJOR_EVENT_KEYWORDS):
        return "عالي"
    if any(k in t for k in ["تحذير", "تنبيه", "مفاجأة"]):
        return "متوسط"
    return "متوسط"

def golden_warning_tag(text: str) -> str:
    # "تحذير ذهبي" إذا الخبر يحمل كلمات خطورة
    t = text.lower()
    danger = ["تحذير", "مخاطر", "تدخل", "تقلبات", "هبوط حاد", "انهيار", "crash", "intervention", "risk"]
    return "🟡 تحذير ذهبي" if any(w in t for w in danger) else ""

def short_summary(summary: str) -> str:
    s = clean(summary)
    if not s:
        return ""
    return s[:SUMMARY_MAX_CHARS] + ("..." if len(s) > SUMMARY_MAX_CHARS else "")

def build_news_message(title: str, summary: str, src: str) -> str:
    title = clean(title)
    summary = short_summary(summary)

    mood = infer_market_mood(title + " " + summary)
    strength = infer_strength(title + " " + summary)
    gold = golden_warning_tag(title + " " + summary)

    msg = f"📰 <b>{title}</b>\n"
    if summary:
        msg += f"\n{summary}\n"

    if gold:
        msg += f"\n{gold}\n"

    msg += f"\n⚡ <b>قوة الخبر:</b> {strength}"
    msg += f"\n🧠 <b>اتجاه السوق:</b> {mood}"
    msg += f"\n🔗 <b>المصدر:</b> {src}"

    msg += SIGNATURE
    return msg

def is_major_event(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in MAJOR_EVENT_KEYWORDS)

def parse_entry_datetime(entry) -> datetime | None:
    """
    Try to get an event/news time:
    - published_parsed if present (RSS)
    - otherwise try to parse date from summary/title
    """
    if getattr(entry, "published_parsed", None):
        dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        return dt

    blob = clean(entry.get("summary") or entry.get("description") or "") + " " + clean(entry.get("title") or "")
    # Try common formats: YYYY-MM-DD HH:MM
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", blob)
    if m:
        try:
            return datetime.fromisoformat(f"{m.group(1)} {m.group(2)}").replace(tzinfo=timezone.utc)
        except:
            return None
    return None

def format_dt_kuwait(dt_utc: datetime) -> str:
    # Kuwait = UTC+3
    kuwait_tz = timezone(timedelta(hours=3))
    return dt_utc.astimezone(kuwait_tz).strftime("%Y-%m-%d %H:%M")

def build_alert_message(event_title: str, minutes: int, src: str, when_utc: datetime) -> str:
    title = clean(event_title)
    tag = "🚨" if minutes <= 5 else "⏰"
    return (
        f"{tag} <b>تنبيه مهم</b>\n"
        f"\n<b>بعد {minutes} دقيقة:</b> {title}\n"
        f"\n🕒 <b>وقت الحدث (الكويت):</b> {format_dt_kuwait(when_utc)}"
        f"\n🔗 <b>المصدر:</b> {src}"
        f"{SIGNATURE}"
    )

# =========================
# ASYNC WORKERS
# =========================
async def send_html(bot: Bot, text: str) -> None:
    await bot.send_message(
        chat_id=CHANNEL,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True  # ✅ بدون روابط/بدون معاينة
    )

async def news_worker(bot: Bot) -> None:
    while True:
        try:
            for url in NEWS_FEEDS:
                feed = feedparser.parse(url)
                src = source_label(url)

                for entry in feed.entries[:MAX_PER_FEED]:
                    title = clean(entry.get("title") or "")
                    summary = clean(entry.get("summary") or entry.get("description") or "")

                    if not title:
                        continue

                    # ✅ فلترة: أبي عربي فقط
                    if not is_mostly_arabic(title + " " + summary):
                        continue

                    item_id = entry.get("id") or make_hash_id("news", src, title, summary)
                    if seen("posted", item_id):
                        continue

                    msg = build_news_message(title, summary, src)
                    await send_html(bot, msg)

                    mark_seen("posted", item_id)
                    await asyncio.sleep(1.0)

            await asyncio.sleep(POLL_NEWS_SECONDS)

        except Exception as ex:
            print("NEWS Error:", ex)
            await asyncio.sleep(10)

async def calendar_worker(bot: Bot) -> None:
    while True:
        try:
            now_utc = datetime.now(timezone.utc)

            for url in CALENDAR_FEEDS:
                feed = feedparser.parse(url)
                src = source_label(url)

                for entry in feed.entries[:MAX_PER_FEED]:
                    title = clean(entry.get("title") or "")
                    summary = clean(entry.get("summary") or entry.get("description") or "")

                    if not title:
                        continue

                    event_time = parse_entry_datetime(entry)
                    if not event_time:
                        continue

                    # نركز على الأحداث الكبيرة فقط
                    if not is_major_event(title + " " + summary):
                        continue

                    # جهّز ID للحدث
                    base_id = entry.get("id") or make_hash_id("cal", src, title, str(event_time))

                    for minutes in ALERTS_MINUTES:
                        alert_time = event_time - timedelta(minutes=minutes)

                        # إذا وقت التنبيه صار أو قرب (± 60 ثانية)
                        if alert_time <= now_utc <= alert_time + timedelta(seconds=60):
                            alert_id = make_hash_id(base_id, f"alert_{minutes}")

                            if seen("alerts", alert_id):
                                continue

                            msg = build_alert_message(title, minutes, src, event_time)
                            await send_html(bot, msg)

                            mark_seen("alerts", alert_id)
                            await asyncio.sleep(0.8)

            await asyncio.sleep(POLL_CALENDAR_SECONDS)

        except Exception as ex:
            print("CALENDAR Error:", ex)
            await asyncio.sleep(10)

async def main() -> None:
    init_db()
    bot = Bot(token=TOKEN)

    print("Bot Running...")
    await asyncio.gather(
        news_worker(bot),
        calendar_worker(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())