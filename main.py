import os
import re
import time
import sqlite3
import hashlib
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedparser
from bs4 import BeautifulSoup

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError


# =========================
# CONFIG
# =========================
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise Exception("BOT_TOKEN missing in environment variables")

CHANNEL = "@news_forexq"  # غيّرها إذا احتجت
SIGNATURE = "— @news_forexq"

# إذا تبي فقط الأخبار العربية (يوصي فيها لأنك قلت تبيني كلهم عربي)
ARABIC_ONLY = True

# عدد أحرف الملخص (علشان تكون الرسالة نظيفة)
SUMMARY_MAX_CHARS = 550

# مدة الانتظار بين دورات الفحص
POLL_SECONDS = 60

# مصادر RSS (خلك على العربي قدر الإمكان)
FEEDS = [
    # FXStreet Arabic
    "https://ar.fxstreet.com/rss/news",
    # Investing (أحياناً يطلع انجليزي) - إذا ARABIC_ONLY=True رح ينسكب
    "https://www.investing.com/rss/news_1.rss",
    # DailyForex Arabic (إذا عندك رابط RSS عربي حطه هنا)
    # "https://arabic.dailyforex.com/rss",
]

# المنطقة الزمنية (الكويت)
TZ = ZoneInfo("Asia/Kuwait")

DB_PATH = "seen.db"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("forex-news-bot")


# =========================
# Helpers: text cleaning
# =========================
ARABIC_RE = re.compile(r"[\u0600-\u06FF]")  # Arabic unicode block
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def is_arabic_text(s: str) -> bool:
    if not s:
        return False
    return bool(ARABIC_RE.search(s))


def strip_html(text: str) -> str:
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    return soup.get_text(" ", strip=True)


def clean(text: str) -> str:
    if not text:
        return ""
    text = strip_html(text)
    text = text.replace("\xa0", " ")
    text = URL_RE.sub("", text)  # إزالة الروابط من أي مكان
    text = re.sub(r"\s+", " ", text).strip()
    return text


def source_name_from_entry(entry: dict, fallback: str = "Unknown") -> str:
    # نحاول نطلع اسم المصدر بدون روابط
    # feedparser يعطينا أحياناً source/author/domain
    src = None

    if isinstance(entry, dict):
        src = entry.get("source", None)
        if isinstance(src, dict):
            src = src.get("title") or src.get("href")

    if not src:
        src = entry.get("publisher") or entry.get("author") or fallback

    src = clean(str(src))
    # قصّ اسم المصدر لو كان طويل
    if len(src) > 40:
        src = src[:40] + "…"
    return src or fallback


def hash_item(title: str, summary: str, src: str) -> str:
    h = hashlib.sha256()
    payload = f"{title}||{summary}||{src}".encode("utf-8", errors="ignore")
    h.update(payload)
    return h.hexdigest()


# =========================
# DB: Dedup
# =========================
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS seen (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
        """
    )
    con.commit()
    con.close()


def already_seen(item_id: str) -> bool:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM seen WHERE id = ?", (item_id,))
    row = cur.fetchone()
    con.close()
    return row is not None


def mark_seen(item_id: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO seen (id, created_at) VALUES (?, ?)",
        (item_id, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    con.close()


# =========================
# Simple “strength/sentiment”
# =========================
def strength_ar(text: str) -> str:
    t = (text or "").lower()
    high_words = ["عاجل", "فوري", "هبوط", "صعود", "يتراجع", "يقفز", "يحطم", "قياسي", "تدخل", "فائدة", "تضخم"]
    score = sum(1 for w in high_words if w in t)
    if score >= 2:
        return "عالي 🔥"
    if score == 1:
        return "متوسط ⚡"
    return "منخفض ✨"


def sentiment_ar(text: str) -> str:
    t = (text or "").lower()
    pos = ["يرتفع", "يصعد", "مكاسب", "إيجابي", "قوي", "يدعم", "تفاؤل"]
    neg = ["ينخفض", "يهبط", "خسائر", "سلبي", "ضعيف", "مخاوف", "تراجع"]
    p = sum(1 for w in pos if w in t)
    n = sum(1 for w in neg if w in t)
    if p > n and p > 0:
        return "إيجابي ✅"
    if n > p and n > 0:
        return "سلبي ❌"
    return "محايد ⚪️"


# =========================
# Message formatting (مرتب مثل المثال)
# =========================
def build_message(title: str, summary: str, src: str) -> str:
    title = clean(title)
    summary = clean(summary)
    src = clean(src)

    if summary:
        summary = summary[:SUMMARY_MAX_CHARS] + ("..." if len(summary) > SUMMARY_MAX_CHARS else "")

    mood = sentiment_ar(title + " " + summary)
    power = strength_ar(title + " " + summary)

    # شارة أعلى
    if "✅" in mood:
        badge = "🟢 <b>إيجابي</b>"
    elif "❌" in mood:
        badge = "🔴 <b>سلبي</b>"
    else:
        badge = "⚪️ <b>محايد</b>"

    now_txt = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")

    msg = f"""{badge}

🔔🌐 <b>صدر الآن</b> ‼️

<b>{title}</b>

{summary}

⚡ <b>قوة الخبر:</b> {power}
🧠 <b>اتجاه السوق:</b> {mood}

🕒 <b>{now_txt}</b>
🔗 <b>المصدر:</b> ({src})

{SIGNATURE}
"""
    return msg.strip()


# =========================
# Telegram send with retry (Flood/Timeout)
# =========================
async def send_with_retry(bot: Bot, text: str, max_tries: int = 5):
    for attempt in range(1, max_tries + 1):
        try:
            await bot.send_message(
                chat_id=CHANNEL,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        except RetryAfter as e:
            wait = int(getattr(e, "retry_after", 5))
            log.warning("Flood control: retry after %s seconds", wait)
            await asyncio.sleep(wait + 1)
        except (TimedOut, NetworkError) as e:
            wait = min(10 * attempt, 40)
            log.warning("Network/Timeout (%s). retry in %s sec", e, wait)
            await asyncio.sleep(wait)
        except Exception as e:
            log.exception("Send failed: %s", e)
            await asyncio.sleep(2 * attempt)


# =========================
# RSS fetch loop
# =========================
def parse_feed(url: str):
    return feedparser.parse(url)


async def rss_worker(bot: Bot):
    while True:
        try:
            for feed_url in FEEDS:
                d = parse_feed(feed_url)
                entries = getattr(d, "entries", []) or []

                # أحدث أولاً
                for entry in entries[:25]:
                    title = clean(getattr(entry, "title", "") or "")
                    summary = clean(getattr(entry, "summary", "") or getattr(entry, "description", "") or "")

                    # إذا ما فيه عنوان لا تنزل
                    if not title:
                        continue

                    # عربي فقط
                    if ARABIC_ONLY and (not is_arabic_text(title) and not is_arabic_text(summary)):
                        continue

                    # اسم المصدر بدون روابط
                    src = source_name_from_entry(entry, fallback=feed_url.split("/")[2])

                    item_id = hash_item(title, summary, src)
                    if already_seen(item_id):
                        continue

                    msg = build_message(title, summary, src)
                    await send_with_retry(bot, msg)
                    mark_seen(item_id)

                    # هدّئ شوي بين الرسائل حتى ما يطق Flood بسرعة
                    await asyncio.sleep(1.2)

        except Exception as e:
            log.exception("RSS worker error: %s", e)

        await asyncio.sleep(POLL_SECONDS)


# =========================
# BIG NEWS ALERTS (30min & 5min) - يدوي / جاهز للربط لاحقاً
# =========================
# حط الأحداث القادمة هنا (بتوقيت الكويت)
# مثال:
# BIG_EVENTS = [
#   {"time": "2025-12-30 16:30", "title": "بيانات التضخم الأمريكية (CPI)", "currency": "USD", "impact": "عالي"},
# ]
BIG_EVENTS = []


def parse_event_time(s: str) -> datetime:
    # "YYYY-MM-DD HH:MM" Kuwait time
    dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
    return dt.replace(tzinfo=TZ)


async def big_events_worker(bot: Bot):
    # نخزن تنبيهات تم إرسالها
    sent_flags = set()

    while True:
        try:
            now = datetime.now(TZ)

            for ev in BIG_EVENTS:
                ev_time = parse_event_time(ev["time"])
                name = clean(ev.get("title", "خبر اقتصادي"))
                cur = clean(ev.get("currency", ""))
                impact = clean(ev.get("impact", "عالي"))

                # مفاتيح منع تكرار التنبيه
                key_30 = f"{ev['time']}|30"
                key_5 = f"{ev['time']}|5"

                # قبل 30 دقيقة
                if key_30 not in sent_flags and now >= (ev_time - timedelta(minutes=30)) and now < ev_time:
                    msg = f"""⭐️ <b>تنبيه قبل خبر مهم بـ 30 دقيقة</b>

🔔 <b>{name}</b>
💱 <b>العملة:</b> {cur}
⚡ <b>التأثير:</b> {impact}

🕒 <b>وقت الخبر:</b> {ev_time.strftime("%Y-%m-%d %H:%M")}

{SIGNATURE}"""
                    await send_with_retry(bot, msg)
                    sent_flags.add(key_30)
                    await asyncio.sleep(1.0)

                # قبل 5 دقائق
                if key_5 not in sent_flags and now >= (ev_time - timedelta(minutes=5)) and now < ev_time:
                    msg = f"""⭐️ <b>تنبيه قبل خبر مهم بـ 5 دقائق</b>

🔔 <b>{name}</b>
💱 <b>العملة:</b> {cur}
⚡ <b>التأثير:</b> {impact}

🕒 <b>وقت الخبر:</b> {ev_time.strftime("%Y-%m-%d %H:%M")}

{SIGNATURE}"""
                    await send_with_retry(bot, msg)
                    sent_flags.add(key_5)
                    await asyncio.sleep(1.0)

        except Exception as e:
            log.exception("Big events worker error: %s", e)

        await asyncio.sleep(30)


# =========================
# Main
# =========================
async def main():
    init_db()
    bot = Bot(token=TOKEN)

    log.info("Bot Running...")

    # شغّل المهام
    await asyncio.gather(
        rss_worker(bot),
        big_events_worker(bot),
    )


if __name__ == "__main__":
    asyncio.run(main())