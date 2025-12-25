import os
import re
import json
import time
import asyncio
import logging
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional, Tuple

import feedparser
from dateutil import parser as dtparser
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError


# =========================
# ENV CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()  # e.g. "@news_forexq" or "-100..."

# RSS feeds (comma-separated)
# Example:
# RSS_FEEDS="https://ar.fxstreet.com/rss/news,https://www.investing.com/rss/news_1.rss,https://arab.dailyforex.com/rss/arab/forexnews.xml"
RSS_FEEDS = [x.strip() for x in os.getenv("RSS_FEEDS", "").split(",") if x.strip()]

ARABIC_ONLY = os.getenv("ARABIC_ONLY", "true").lower() in ("1", "true", "yes", "y")
TRANSLATE_EN = os.getenv("TRANSLATE_EN", "false").lower() in ("1", "true", "yes", "y")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
SEND_DELAY_SECONDS = float(os.getenv("SEND_DELAY_SECONDS", "3.0"))

STATE_FILE = os.getenv("STATE_FILE", "state.json")
EVENTS_FILE = os.getenv("EVENTS_FILE", "events.json")  # optional (calendar alerts)


# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("forex-news-bot")


# =========================
# HELPERS
# =========================
ARABIC_RE = re.compile(r"[\u0600-\u06FF]")
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")

KUWAIT_TZ = timezone(timedelta(hours=3))


def now_kw() -> datetime:
    return datetime.now(KUWAIT_TZ)


def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(KUWAIT_TZ).strftime("%Y-%m-%d %H:%M")


def has_arabic(text: str) -> bool:
    return bool(ARABIC_RE.search(text or ""))


def strip_links(text: str) -> str:
    return URL_RE.sub("", text or "").strip()


def strip_html(text: str) -> str:
    return TAG_RE.sub(" ", text or "")


def clean(text: str) -> str:
    text = strip_html(text)
    text = strip_links(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def short_hash(*parts: str) -> str:
    h = hashlib.sha256(("|".join(parts)).encode("utf-8", errors="ignore")).hexdigest()
    return h[:16]


def source_name(entry: Any, feed_url: str) -> str:
    # best-effort label without links
    dom = feed_url.replace("https://", "").replace("http://", "").split("/")[0].lower()

    if "fxstreet" in dom:
        return "FXStreet"
    if "investing" in dom:
        return "Investing"
    if "dailyforex" in dom:
        return "DailyForex"
    if "arabictrader" in dom:
        return "ArabicTrader"

    return dom[:40] if dom else "مصدر"


def parse_entry_time(entry: Any) -> datetime:
    for key in ("published", "updated", "created"):
        val = getattr(entry, key, None)
        if val:
            try:
                dt = dtparser.parse(val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(KUWAIT_TZ)
            except Exception:
                pass
    return now_kw()


# =========================
# OPTIONAL TRANSLATION (EN->AR)
# =========================
_translator = None

def translate_to_ar(text: str) -> str:
    # Only if enabled
    global _translator
    if not TRANSLATE_EN:
        return text
    try:
        if _translator is None:
            from deep_translator import GoogleTranslator
            _translator = GoogleTranslator(source="auto", target="ar")
        return _translator.translate(text)
    except Exception:
        return text


# =========================
# CLASSIFY: strength + sentiment
# =========================
HIGH_KW = [
    # Central banks & rates
    "rate decision", "interest rate", "fomc", "fed", "powell", "ecb", "boe", "boj",
    "قرار الفائدة", "سعر الفائدة", "الفيدرالي", "باول", "المركزي الأوروبي", "بنك إنجلترا", "بنك اليابان",
    # Major data
    "cpi", "inflation", "nfp", "jobs report", "unemployment", "gdp", "pmi",
    "التضخم", "مؤشر أسعار المستهلك", "الوظائف", "البطالة", "الناتج المحلي", "مديري المشتريات",
    # Risk / shocks
    "breaking", "urgent", "intervention", "sanction", "war",
    "عاجل", "تحذير", "تدخل", "عقوبات", "حرب",
]

MED_KW = [
    "retail sales", "ppi", "consumer confidence", "housing", "minutes", "speech",
    "مبيعات التجزئة", "أسعار المنتجين", "ثقة المستهلك", "الإسكان", "محضر", "خطاب", "تصريحات",
    "gold", "xau", "oil", "brent", "wti",
    "الذهب", "النفط",
    "usd", "eur", "gbp", "jpy", "chf", "cad", "aud", "nzd",
    "الدولار", "اليورو", "الإسترليني", "الين",
]

POS_KW = ["يرتفع", "ارتفاع", "يصعد", "صعود", "مكاسب", "إيجابي", "قوي", "يتحسن", "قفز", "زيادة",
          "rise", "up", "gains", "bullish", "beats", "strong"]
NEG_KW = ["ينخفض", "انخفاض", "يهبط", "هبوط", "خسائر", "سلبي", "ضعيف", "يتراجع", "هبوط حاد", "تراجع",
          "fall", "down", "losses", "bearish", "misses", "weak"]


def classify_strength(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in HIGH_KW):
        return "HIGH"
    if any(k in t for k in MED_KW):
        return "MED"
    return "LOW"


def classify_sentiment(text: str) -> str:
    t = (text or "").lower()
    p = sum(1 for k in POS_KW if k in t)
    n = sum(1 for k in NEG_KW if k in t)
    if p > n and p > 0:
        return "إيجابي"
    if n > p and n > 0:
        return "سلبي"
    return "محايد"


def strength_label(strength: str) -> str:
    if strength == "HIGH":
        return "عالي جداً 🔥"
    if strength == "MED":
        return "متوسط ⚡"
    return "منخفض ✨"


def sentiment_badge(sentiment: str) -> str:
    if sentiment == "إيجابي":
        return "🟢 <b>إيجابي</b>"
    if sentiment == "سلبي":
        return "🔴 <b>سلبي</b>"
    return "⚪️ <b>محايد</b>"


# =========================
# STATE (dedupe)
# =========================
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"sent": {}, "alerts": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sent": {}, "alerts": {}}


def save_state(state: Dict[str, Any]) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Failed saving state: %s", e)


def is_sent(state: Dict[str, Any], item_id: str) -> bool:
    return item_id in state.get("sent", {})


def mark_sent(state: Dict[str, Any], item_id: str) -> None:
    state.setdefault("sent", {})[item_id] = int(time.time())


# =========================
# EVENTS ALERTS (optional)
# =========================
def load_events() -> List[Dict[str, Any]]:
    if not os.path.exists(EVENTS_FILE):
        return []
    try:
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        out = []
        for ev in raw:
            dt = dtparser.parse(ev["datetime"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KUWAIT_TZ)
            out.append({
                "id": ev.get("id") or short_hash(ev.get("title", ""), ev.get("datetime", "")),
                "title": clean(ev.get("title", "")),
                "currency": clean(ev.get("currency", "")).upper(),
                "country": clean(ev.get("country", "")),
                "dt": dt.astimezone(KUWAIT_TZ),
                "impact": (ev.get("impact") or "").lower(),
                "source": clean(ev.get("source") or "Economic Calendar"),
            })
        return out
    except Exception as e:
        logger.warning("Failed reading events.json: %s", e)
        return []


def is_high_impact_event(ev: Dict[str, Any]) -> bool:
    return ev.get("impact") in ("high", "strong", "عالي")


def set_alert_sent(state: Dict[str, Any], alert_id: str) -> None:
    state.setdefault("alerts", {})[alert_id] = int(time.time())


def is_alert_sent(state: Dict[str, Any], alert_id: str) -> bool:
    return alert_id in state.get("alerts", {})


def build_event_alert(ev: Dict[str, Any], minutes_before: int) -> str:
    header = "⚠️ <b>تنبيه خبر قوي بعد 30 دقيقة</b>" if minutes_before == 30 else "🔥 <b>خبر قوي بعد 5 دقائق!</b>"
    msg = (
        f"{header}\n\n"
        f"📌 <b>{ev['title']}</b>\n"
        f"💱 <b>العملة:</b> {ev['currency']}\n"
        f"🕒 <b>وقت الخبر:</b> {fmt_dt(ev['dt'])}\n"
        f"📰 <b>المصدر:</b> {ev['source']}\n\n"
        f"— @news_forexq"
    )
    return msg


# =========================
# TELEGRAM SENDER (Flood-safe queue)
# =========================
class Sender:
    def __init__(self, bot: Bot, channel_id: str, delay: float):
        self.bot = bot
        self.channel_id = channel_id
        self.delay = delay
        self.q: asyncio.Queue[str] = asyncio.Queue()
        self.task: Optional[asyncio.Task] = None

    async def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.worker())

    async def enqueue(self, text: str):
        await self.q.put(text)

    async def worker(self):
        while True:
            text = await self.q.get()
            try:
                await self.send_with_retry(text)
            finally:
                await asyncio.sleep(self.delay)
                self.q.task_done()

    async def send_with_retry(self, text: str):
        for _ in range(8):
            try:
                await self.bot.send_message(
                    chat_id=self.channel_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                return
            except RetryAfter as e:
                wait = int(getattr(e, "retry_after", 5)) + 1
                logger.warning("Flood control: wait %ss", wait)
                await asyncio.sleep(wait)
            except (TimedOut, NetworkError):
                await asyncio.sleep(3)
            except Exception as ex:
                logger.error("Send failed: %s", ex)
                await asyncio.sleep(2)


# =========================
# MESSAGE TEMPLATE (احترافي مرتب)
# =========================
def build_news_message(title: str, summary: str, src: str, strength: str, sentiment: str, when_dt: datetime) -> str:
    title = clean(title)
    summary = clean(summary)

    # summary مختصر ونظيف
    if summary:
        if summary.startswith(title):
            summary = summary[len(title):].strip()
        if len(summary) > 520:
            summary = summary[:520].rstrip() + "..."

    badge = sentiment_badge(sentiment)
    power = strength_label(strength)

    star = "⭐ <b>خبر مميز اليوم</b>\n\n" if strength == "HIGH" else ""

    msg = (
        f"{star}"
        f"{badge}\n\n"
        f"🔔🌐 <b>صدر الآن</b> ‼️\n\n"
        f"📌 <b>{title}</b>\n\n"
    )

    if summary:
        msg += f"📝 <b>ملخص الخبر:</b>\n{summary}\n\n"

    msg += (
        "━━━━━━━━━━━━━━\n"
        f"⚡ <b>قوة الخبر:</b> {power}\n"
        f"🧠 <b>اتجاه السوق:</b> {sentiment}\n\n"
        f"🕒 <b>{fmt_dt(when_dt)}</b>\n"
        f"📰 <b>المصدر:</b> {src}\n\n"
        f"— @news_forexq"
    )

    # ضمان نهائي: ما فيه روابط
    return strip_links(msg)


# =========================
# RSS FETCH
# =========================
async def fetch_items() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for url in RSS_FEEDS:
        try:
            d = feedparser.parse(url)
            for e in getattr(d, "entries", [])[:40]:
                title = clean(getattr(e, "title", "") or "")
                summary = clean(getattr(e, "summary", "") or getattr(e, "description", "") or "")
                pub_dt = parse_entry_time(e)
                src = source_name(e, url)

                if not title:
                    continue

                # عربي فقط + خيار ترجمة
                all_text = f"{title} {summary}"
                if ARABIC_ONLY and not has_arabic(all_text):
                    if TRANSLATE_EN:
                        title = translate_to_ar(title)
                        summary = translate_to_ar(summary)
                        if ARABIC_ONLY and not has_arabic(f"{title} {summary}"):
                            continue
                    else:
                        continue

                strength = classify_strength(all_text)
                sentiment = classify_sentiment(all_text)

                # ✅ المطلوب: أخبار اقتصادية عامة (HIGH + MED) فقط
                if strength not in ("HIGH", "MED"):
                    continue

                item_id = short_hash(src, title, str(pub_dt))
                items.append({
                    "id": item_id,
                    "title": title,
                    "summary": summary,
                    "src": src,
                    "published": pub_dt,
                    "strength": strength,
                    "sentiment": sentiment,
                })

        except Exception as ex:
            logger.warning("RSS fetch failed for %s: %s", url, ex)

    # newest first
    items.sort(key=lambda x: x["published"], reverse=True)
    return items


# =========================
# MAIN LOOP
# =========================
async def run():
    if not BOT_TOKEN or not CHANNEL_ID:
        raise RuntimeError("Missing BOT_TOKEN or CHANNEL_ID in environment variables.")
    if not RSS_FEEDS:
        raise RuntimeError("Missing RSS_FEEDS env var (comma-separated).")

    bot = Bot(token=BOT_TOKEN)
    sender = Sender(bot, CHANNEL_ID, delay=SEND_DELAY_SECONDS)
    await sender.start()

    state = load_state()
    logger.info("Bot Running...")

    while True:
        try:
            # 1) Calendar alerts (optional) — if you keep events.json
            events = load_events()
            now_dt = now_kw()

            for ev in events:
                if not is_high_impact_event(ev):
                    continue

                alert30 = f"{ev['id']}_30"
                alert5 = f"{ev['id']}_5"

                if (ev["dt"] - timedelta(minutes=30)) <= now_dt < ev["dt"] and not is_alert_sent(state, alert30):
                    # window 1 minute
                    if now_dt <= (ev["dt"] - timedelta(minutes=29, seconds=0)):
                        await sender.enqueue(build_event_alert(ev, 30))
                        set_alert_sent(state, alert30)
                        save_state(state)

                if (ev["dt"] - timedelta(minutes=5)) <= now_dt < ev["dt"] and not is_alert_sent(state, alert5):
                    if now_dt <= (ev["dt"] - timedelta(minutes=4, seconds=0)):
                        await sender.enqueue(build_event_alert(ev, 5))
                        set_alert_sent(state, alert5)
                        save_state(state)

            # 2) Economic news RSS (HIGH + MED)
            items = await fetch_items()

            # prevent flooding: post up to 6 per cycle
            posted = 0
            for it in items:
                if posted >= 6:
                    break
                if is_sent(state, it["id"]):
                    continue

                msg = build_news_message(
                    title=it["title"],
                    summary=it["summary"],
                    src=it["src"],
                    strength=it["strength"],
                    sentiment=it["sentiment"],
                    when_dt=it["published"],
                )

                await sender.enqueue(msg)

                mark_sent(state, it["id"])
                save_state(state)
                posted += 1

            await asyncio.sleep(POLL_SECONDS)

        except Exception as ex:
            logger.exception("Loop error: %s", ex)
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(run())