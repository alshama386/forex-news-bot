import os
import re
import time
import sqlite3
import hashlib
import asyncio
from datetime import datetime

import feedparser
from telegram import Bot
from telegram.constants import ParseMode

# =========================
# CONFIG
# =========================
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise Exception("BOT_TOKEN missing in environment variables")

CHANNEL = "@news_forexq"  # ✅ اسم قناتك الجديد
SIGNATURE = "\n\n— @news_forexq"

FEEDS = [
    "https://ar.fxstreet.com/rss/news",
    "https://www.arabictrader.com/rss/news",
    "https://arab.dailyforex.com/rss/arab/forexnews.xml",
    "https://www.investing.com/rss/news_1.rss",
]

POLL_SECONDS = 25
MAX_PER_FEED = 25
SUMMARY_MAX_CHARS = 260

# كلمات “خبر كبير”
BIG_EVENT_KEYWORDS = [
    "cpi", "inflation", "nfp", "jobs report", "employment",
    "rate decision", "interest rate", "fed", "powell",
    "ecb", "boj", "boe",
    "gdp", "pmi", "unemployment",
    "قرار الفائدة", "الفائدة", "التضخم", "الوظائف", "الرواتب",
    "مؤشر أسعار المستهلك", "البطالة", "الناتج المحلي", "مديري المشتريات",
]

# =========================
# PERSISTENT DE-DUP (SQLite)
# =========================
DB_FILE = "posted.db"

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
        (item_id, datetime.utcnow().isoformat())
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
    if "fxstreet" in u:
        return "FXStreet"
    if "arabictrader" in u:
        return "ArabicTrader"
    if "dailyforex" in u:
        return "DailyForex"
    if "investing" in u:
        return "Investing"
    return "Source"

def is_big_event(title: str, summary: str) -> bool:
    combined = (title + " " + summary).lower()
    return any(k.lower() in combined for k in BIG_EVENT_KEYWORDS)

def guess_currency(title: str, summary: str) -> str:
    text = (title + " " + summary).lower()
    # أزواج/عملات شائعة
    if "usd" in text or "الدولار" in text:
        return "USD 🇺🇸"
    if "eur" in text or "اليورو" in text:
        return "EUR 🇪🇺"
    if "gbp" in text or "الجنيه" in text:
        return "GBP 🇬🇧"
    if "jpy" in text or "الين" in text:
        return "JPY 🇯🇵"
    if "chf" in text or "الفرنك" in text:
        return "CHF 🇨🇭"
    if "cad" in text or "الكندي" in text:
        return "CAD 🇨🇦"
    if "aud" in text or "الأسترالي" in text:
        return "AUD 🇦🇺"
    if "nzd" in text or "النيوزلندي" in text:
        return "NZD 🇳🇿"
    if "gold" in text or "xau" in text or "الذهب" in text:
        return "GOLD 🟡"
    return "—"

def guess_country_from_currency(cur: str) -> str:
    if cur.startswith("USD"):
        return "الولايات المتحدة"
    if cur.startswith("EUR"):
        return "منطقة اليورو"
    if cur.startswith("GBP"):
        return "بريطانيا"
    if cur.startswith("JPY"):
        return "اليابان"
    if cur.startswith("CHF"):
        return "سويسرا"
    if cur.startswith("CAD"):
        return "كندا"
    if cur.startswith("AUD"):
        return "أستراليا"
    if cur.startswith("NZD"):
        return "نيوزيلندا"
    if cur.startswith("GOLD"):
        return "الذهب (سلعة)"
    return "—"

def sentiment_label(title: str, summary: str) -> str:
    """
    محاولة بسيطة: إذا النص فيه ارتفاع/إيجابي/قوي => إيجابي
    إذا فيه هبوط/سلبي/ضعيف => سلبي
    وإلا محايد
    """
    t = (title + " " + summary).lower()
    positive = ["يرتفع", "صعود", "إيجابي", "قوي", "يتحسن", "زيادة", "يتقدم", "مكاسب", "bull", "up"]
    negative = ["ينخفض", "هبوط", "سلبي", "ضعيف", "يتراجع", "انخفاض", "خسائر", "bear", "down"]
    if any(w in t for w in positive):
        return "إيجابي ✅"
    if any(w in t for w in negative):
        return "سلبي ❌"
    return "محايد ⚖️"

def impact_label(title: str, summary: str) -> str:
    """
    قوة الخبر: عالي جداً للخبر الكبير، وإلا متوسط.
    """
    if is_big_event(title, summary):
        return "عالي جداً 🔥"
    return "متوسط ⚡"

def extract_numbers_hint(text: str):
    """
    محاولة التقاط أرقام مثل 224K / 3.2% / 0.78
    لا تعتبر رسمية، بس “تلميح” لو موجودة.
    """
    text = clean(text)
    nums = re.findall(r"[-+]?\d+(?:\.\d+)?\s?(?:%|k|K|M|B)?", text)
    nums = [n.strip() for n in nums if n.strip()]
    # نرجع أول 3 كحد أقصى
    return nums[:3]

def build_message(title: str, summary: str, src: str) -> str:
    title = clean(title)
    summary = clean(summary)

    if summary:
        summary = summary[:SUMMARY_MAX_CHARS] + ("..." if len(summary) > SUMMARY_MAX_CHARS else "")

    cur = guess_currency(title, summary)
    country = guess_country_from_currency(cur)
    mood = sentiment_label(title, summary)
    impact = impact_label(title, summary)

    # تلميح أرقام إن وجدت (مو رسمي)
    nums = extract_numbers_hint(title + " " + summary)
    prev = nums[0] if len(nums) > 0 else "—"
    forecast = nums[1] if len(nums) > 1 else "—"
    actual = nums[2] if len(nums) > 2 else "—"

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ✅ ترتيب مثل اللي بالصورة
    msg = f"""
<b>{mood}</b>

🌐🔔 <b>صدر الآن</b> ‼️

📌 <b>{title}</b>

🎯 <b>الخبر:</b> {title}
📍 <b>الدولة:</b> {country}
🏳️ <b>العملة:</b> {cur}

🔎 <b>السابق:</b> {prev}
🧾 <b>التوقع:</b> {forecast}
🟠 <b>الحالي:</b> {actual}

✨ <b>قوة الخبر:</b> {impact}
🧠 <b>اتجاه السوق:</b> {mood}

🕒 <b>{now_str}</b>
🔗 <b>المصدر:</b> ({src})
{SIGNATURE}
""".strip()

    return msg

# =========================
# MAIN (ASYNC)
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
                    title = clean(entry.get("title"))
                    link = clean(entry.get("link"))  # موجود بس ما راح نعرضه
                    summary = clean(entry.get("summary") or entry.get("description") or "")

                    if not title:
                        continue

                    item_id = entry.get("id") or make_hash_id(title, link)
                    if already_posted(item_id):
                        continue

                    text = build_message(title, summary, src)

                    await bot.send_message(
                        chat_id=CHANNEL,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True  # ✅ يمنع أي معاينة رابط
                    )

                    mark_posted(item_id)
                    await asyncio.sleep(1.2)

            await asyncio.sleep(POLL_SECONDS)

        except Exception as ex:
            print("Error:", ex)
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())