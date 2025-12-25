import os
import time
import re
import sqlite3
import hashlib
from datetime import datetime, timezone

import feedparser
import requests

# =========================
# CONFIG
# =========================
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise Exception("BOT_TOKEN missing in environment variables (BOT_TOKEN)")

# قناة تيليجرام (يوزرنيم القناة بدون @ أو مع @ الاثنين يمشون)
CHANNEL = "@news_forexq"
SIGNATURE = "\n\n— @news_forexq"

# مصادر عربية (RSS)
FEEDS = [
    "https://ar.fxstreet.com/rss/news",                  # FXStreet Arabic
    "https://www.arabictrader.com/rss/news",             # ArabicTrader
    "https://arab.dailyforex.com/rss/arab/forexnews.xml" # DailyForex Arabic
]

POLL_SECONDS = 25
MAX_PER_FEED = 25
SUMMARY_MAX_CHARS = 360

# كلمات "عاجل/ذهبي" (عربي + إنجليزي لو طلع ضمن النص)
URGENT_KEYWORDS = [
    "عاجل", "خبر عاجل", "تنبيه", "تحذير",
    "الفيدرالي", "باول", "فايدة", "قرار الفائدة",
    "التضخم", "cpi", "nfp", "الوظائف",
    "ذهب", "xau", "الدولار", "usd",
    "eurusd", "gbpusd", "usdjpy",
    "برنت", "wti", "نفط", "oil",
    "تدخل", "intervention"
]

# تصنيف "قوة الخبر" (تقريبي حسب كلمات)
IMPACT_HIGH = ["قرار", "الفائدة", "الفيدرالي", "باول", "cpi", "nfp", "تضخم", "jobs", "تدخل", "intervention"]
IMPACT_MED  = ["توقعات", "بيانات", "مؤشر", "تصريحات", "محضر", "gdp", "pmi", "مبيعات", "بطالة"]
IMPACT_LOW  = ["تحليل", "نظرة", "ملخص", "تعليق", "افتتاح", "إغلاق", "استقرار"]

# تصنيف "اتجاه السوق" (تقريبي)
POS_WORDS = ["ارتفاع", "صعود", "مكاسب", "إيجابي", "يتحسن", "قوي", "انتعاش", "يرتفع", "يزيد"]
NEG_WORDS = ["هبوط", "انخفاض", "خسائر", "سلبي", "يتراجع", "ضعيف", "تراجع", "ينخفض", "يهبط"]

# =========================
# DATABASE (dedup)
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
        (item_id, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()

# =========================
# HELPERS
# =========================
URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)

def clean(text: str) -> str:
    if not text:
        return ""
    return " ".join(text.replace("\n", " ").split()).strip()

def remove_urls(text: str) -> str:
    """يشيل أي رابط من النص نهائياً."""
    if not text:
        return ""
    text = URL_RE.sub("", text)  # remove urls
    # remove any leftover lines containing 'http'
    lines = [ln for ln in text.splitlines() if "http" not in ln.lower()]
    return " ".join(" ".join(lines).split()).strip()

def make_hash_id(title: str, published: str, src: str) -> str:
    raw = (clean(title) + "||" + clean(published) + "||" + clean(src)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def source_label(feed_url: str) -> str:
    u = feed_url.lower()
    if "fxstreet" in u:
        return "FXStreet"
    if "arabictrader" in u:
        return "المتداول العربي"
    if "dailyforex" in u:
        return "DailyForex"
    return "المصدر"

def is_urgent(title: str, summary: str) -> bool:
    combined = (title + " " + summary).lower()
    return any(k.lower() in combined for k in URGENT_KEYWORDS)

def impact_level(title: str, summary: str) -> str:
    t = (title + " " + summary).lower()
    if any(k.lower() in t for k in IMPACT_HIGH):
        return "عالي جداً"
    if any(k.lower() in t for k in IMPACT_MED):
        return "متوسط"
    if any(k.lower() in t for k in IMPACT_LOW):
        return "منخفض"
    return "متوسط"

def market_sentiment(title: str, summary: str) -> str:
    t = (title + " " + summary).lower()
    pos = sum(1 for w in POS_WORDS if w in t)
    neg = sum(1 for w in NEG_WORDS if w in t)
    if pos > neg and pos >= 1:
        return "إيجابي"
    if neg > pos and neg >= 1:
        return "سلبي"
    return "محايد"

def affected_assets(title: str, summary: str) -> str:
    t = (title + " " + summary).lower()
    assets = []
    # عملات/أزواج شائعة
    for key, name in [
        ("eurusd", "EUR/USD"),
        ("gbpusd", "GBP/USD"),
        ("usdjpy", "USD/JPY"),
        ("usdchf", "USD/CHF"),
        ("audusd", "AUD/USD"),
        ("usdcad", "USD/CAD"),
        ("nzdusd", "NZD/USD"),
        ("xau", "الذهب"),
        ("gold", "الذهب"),
        ("oil", "النفط"),
        ("brent", "النفط (برنت)"),
        ("wti", "النفط (WTI)"),
        ("usd", "الدولار"),
        ("eur", "اليورو"),
        ("gbp", "الإسترليني"),
        ("jpy", "الين"),
        ("chf", "الفرنك"),
        ("aud", "الأسترالي"),
        ("cad", "الكندي"),
        ("nzd", "النيوزيلندي"),
    ]:
        if key in t and name not in assets:
            assets.append(name)

    if not assets:
        return "—"
    # لا نطوّل
    return "، ".join(assets[:4]) + ("…" if len(assets) > 4 else "")

def short_summary(summary: str) -> str:
    s = clean(summary)
    s = remove_urls(s)  # ✅ أهم سطر: شيل الروابط من الوصف نفسه
    if not s:
        return ""
    return s[:SUMMARY_MAX_CHARS] + ("..." if len(s) > SUMMARY_MAX_CHARS else "")

def tg_send_message(html_text: str) -> None:
    """إرسال رسالة تيليجرام (HTML) بدون روابط ومع إيقاف المعاينة."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL,
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    r = requests.post(url, data=payload, timeout=20)
    if r.status_code != 200:
        raise Exception(f"Telegram API error: {r.status_code} {r.text}")

def build_news_message(title: str, summary: str, src: str, published: str) -> str:
    title = remove_urls(clean(title))  # ✅ حتى لو عنوانه فيه رابط
    summary = short_summary(summary)

    imp = impact_level(title, summary)
    mood = market_sentiment(title, summary)
    assets = affected_assets(title, summary)

    # شارة "تنبيه ذهبي" إذا عاجل
    urgent = is_urgent(title, summary)
    header = "🟨 <b>تحذير ذهبي</b>\n" if urgent else "📰 "

    msg = f"{header}<b>{title}</b>\n\n"
    if summary:
        msg += f"{summary}\n\n"

    msg += "ــــــــــــــــــــــــــــــ\n"
    msg += f"📊 <b>قوة الخبر:</b> {imp}\n"
    msg += f"🧠 <b>اتجاه السوق:</b> {mood}\n"
    msg += f"📌 <b>الأموال المتأثرة:</b> {assets}\n"
    msg += f"🕒 <b>الوقت:</b> {published}\n"
    msg += f"🔗 <b>المصدر:</b> {src}\n"
    msg += "ــــــــــــــــــــــــــــــ"
    msg += SIGNATURE

    # ✅ ضمان نهائي: لا روابط أبداً
    msg = remove_urls(msg)
    return msg

def format_published(entry) -> str:
    # نحاول نطلع وقت جميل، وإذا ما توفر نستخدم UTC الحالي
    dt = None
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        try:
            dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        except Exception:
            dt = None

    if not dt:
        dt = datetime.now(timezone.utc)

    # صيغة واضحة
    return dt.strftime("%Y-%m-%d %H:%M UTC")

# =========================
# MAIN LOOP
# =========================
def main() -> None:
    init_db()
    print("Bot Running...")

    while True:
        try:
            for feed_url in FEEDS:
                feed = feedparser.parse(feed_url)
                src = source_label(feed_url)

                for entry in feed.entries[:MAX_PER_FEED]:
                    title = entry.get("title", "")
                    link = entry.get("link", "")  # ما راح نستخدمه (طلبك إزالة الروابط)
                    summary = entry.get("summary") or entry.get("description") or ""
                    published = format_published(entry)

                    # Dedup by id or stable hash
                    item_id = entry.get("id") or make_hash_id(title, published, src)

                    if already_posted(item_id):
                        continue

                    msg = build_news_message(title=title, summary=summary, src=src, published=published)

                    # إرسال
                    tg_send_message(msg)

                    # تعليم أنه انرسل
                    mark_posted(item_id)

                    time.sleep(1.0)

            time.sleep(POLL_SECONDS)

        except Exception as ex:
            print("Error:", ex)
            time.sleep(10)

if __name__ == "__main__":
    main()