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
from telegram.ext import Application, ApplicationBuilder

# ---------------------------
# CONFIG (Environment)
# ---------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()  # e.g. "@news_forexq" or "-100xxxxxxxxxx"

# RSS feeds: comma-separated
# Example:
# RSS_FEEDS="https://ar.fxstreet.com/rss/news,https://www.dailyforex.com/ar/rss"
RSS_FEEDS = [x.strip() for x in os.getenv("RSS_FEEDS", "").split(",") if x.strip()]

# Arabic-only behavior:
ARABIC_ONLY = os.getenv("ARABIC_ONLY", "true").lower() in ("1", "true", "yes", "y")

# Try translating EN->AR for English headlines (optional).
TRANSLATE_EN = os.getenv("TRANSLATE_EN", "false").lower() in ("1", "true", "yes", "y")

# Polling interval seconds
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))

# Simple rate limiting (avoid Flood control)
SEND_DELAY_SECONDS = float(os.getenv("SEND_DELAY_SECONDS", "3.5"))

# Paths
STATE_FILE = os.getenv("STATE_FILE", "state.json")
EVENTS_FILE = os.getenv("EVENTS_FILE", "events.json")

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("forex-news-bot")

# ---------------------------
# Helpers
# ---------------------------
ARABIC_RE = re.compile(r"[\u0600-\u06FF]")
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

def has_arabic(text: str) -> bool:
    return bool(ARABIC_RE.search(text or ""))

def strip_links(text: str) -> str:
    if not text:
        return ""
    return URL_RE.sub("", text).strip()

def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()

def short_hash(*parts: str) -> str:
    h = hashlib.sha256(("|".join(parts)).encode("utf-8")).hexdigest()
    return h[:16]

def now_kuwait() -> datetime:
    # Kuwait is UTC+3 (no DST typically)
    return datetime.now(timezone(timedelta(hours=3)))

def fmt_dt(dt: datetime) -> str:
    # Example: 25 ديسمبر 2025 – 10:09
    # We'll keep numbers (Telegram-friendly). If you want words, tell me.
    return dt.strftime("%Y-%m-%d %H:%M")

def guess_source(entry: Dict[str, Any], feed_url: str) -> str:
    # Prefer feed title, then domain
    src = ""
    if "source" in entry and entry["source"]:
        try:
            src = entry["source"].get("title") or ""
        except Exception:
            pass
    if not src:
        # feedparser may store feed title separately; we pass fallback from URL
        src = feed_url
    # Clean to domain-ish name
    src = src.replace("https://", "").replace("http://", "").split("/")[0]
    # Make nicer
    if "fxstreet" in src.lower():
        return "FXStreet"
    if "investing" in src.lower():
        return "Investing"
    if "dailyforex" in src.lower():
        return "DailyForex"
    if "forexlive" in src.lower():
        return "ForexLive"
    if "dailyfx" in src.lower():
        return "DailyFX"
    return src[:40] if src else "مصدر"

def detect_strength(text: str) -> str:
    t = (text or "").lower()
    # crude strength detection
    strong_kw = ["high impact", "breaking", "urgent", "قوي", "قوية", "عاجل", "هام", "مهم", "تنبيه", "تدخل", "intervention"]
    medium_kw = ["moderate", "متوسط", "متوسطة"]
    low_kw = ["low impact", "منخفض", "منخفضة"]

    if any(k in t for k in strong_kw):
        return "عالي"
    if any(k in t for k in medium_kw):
        return "متوسط"
    if any(k in t for k in low_kw):
        return "منخفض"
    return "متوسط"

def detect_sentiment(text: str) -> str:
    t = (text or "").lower()
    pos_kw = ["rises", "rise", "up", "gains", "bull", "positive", "إيجابي", "ارتفاع", "يصعد", "تصعد", "مكاسب", "قوة"]
    neg_kw = ["falls", "fall", "down", "drops", "bear", "negative", "سلبي", "هبوط", "ينخفض", "تراجع", "خسائر", "ضعف"]
    if any(k in t for k in pos_kw):
        return "إيجابي"
    if any(k in t for k in neg_kw):
        return "سلبي"
    return "محايد"

def market_brain_icon(sentiment: str) -> str:
    return "🧠"

def sentiment_icon(sentiment: str) -> str:
    if sentiment == "إيجابي":
        return "✅"
    if sentiment == "سلبي":
        return "⛔"
    return "⚪"

def strength_icon(strength: str) -> str:
    if strength == "عالي":
        return "🔥"
    if strength == "منخفض":
        return "🟢"
    return "⚡"

def build_message(
    title: str,
    body: str,
    source: str,
    when_dt: datetime,
    strength: str,
    sentiment: str,
    currency: Optional[str] = None,
    country: Optional[str] = None,
    is_star: bool = False,
) -> str:
    # Clean inputs
    title = normalize_spaces(strip_links(title))
    body = normalize_spaces(strip_links(body))
    source = normalize_spaces(strip_links(source))

    # Optional flags
    top_badge = f"{sentiment_icon(sentiment)} {sentiment}"
    if currency:
        top_badge += f" | {currency.strip().upper()}"

    star_line = "⭐ <b>خبر مميز اليوم</b>\n\n" if is_star else ""

    msg = (
        f"{star_line}"
        f"<b>{top_badge}</b>\n\n"
        f"🔔 <b>صدر الآن!!</b>\n\n"
        f"📌 <b>{title}</b>\n\n"
    )

    if body:
        msg += f"📝 <b>التفاصيل:</b>\n{body}\n\n"

    if country or currency:
        msg += "━━━━━━━━━━━━━━\n"
        if country:
            msg += f"📍 <b>الدولة:</b> {country}\n"
        if currency:
            msg += f"💱 <b>العملة:</b> {currency.strip().upper()}\n"
        msg += "\n"

    msg += (
        "━━━━━━━━━━━━━━\n"
        f"{strength_icon(strength)} <b>قوة الخبر:</b> {strength}\n"
        f"{market_brain_icon(sentiment)} <b>اتجاه السوق:</b> {sentiment}\n\n"
        f"🕒 <b>{fmt_dt(when_dt)}</b>\n"
        f"📰 <b>المصدر:</b> {source}\n"
        f"\n— @news_forexq"
    )

    return msg

# ---------------------------
# Translation (Optional)
# ---------------------------
_translator = None
def translate_to_ar(text: str) -> str:
    global _translator
    if not TRANSLATE_EN:
        return text
    try:
        if _translator is None:
            from deep_translator import GoogleTranslator
            _translator = GoogleTranslator(source="auto", target="ar")
        return _translator.translate(text)
    except Exception:
        # If translation fails, return original
        return text

# ---------------------------
# State (dedupe + alerts)
# ---------------------------
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

def mark_sent(state: Dict[str, Any], item_id: str) -> None:
    state.setdefault("sent", {})[item_id] = int(time.time())

def is_sent(state: Dict[str, Any], item_id: str) -> bool:
    return item_id in state.get("sent", {})

def set_alert_sent(state: Dict[str, Any], alert_id: str) -> None:
    state.setdefault("alerts", {})[alert_id] = int(time.time())

def is_alert_sent(state: Dict[str, Any], alert_id: str) -> bool:
    return alert_id in state.get("alerts", {})

# ---------------------------
# Sending Queue (Flood Control Safe)
# ---------------------------
class Sender:
    def __init__(self, bot: Bot, channel_id: str, delay: float = 3.5):
        self.bot = bot
        self.channel_id = channel_id
        self.delay = delay
        self.queue: asyncio.Queue[Tuple[str, Optional[int]]] = asyncio.Queue()
        self.worker_task: Optional[asyncio.Task] = None

    async def start(self):
        if self.worker_task is None:
            self.worker_task = asyncio.create_task(self._worker())

    async def send(self, text: str, reply_to_message_id: Optional[int] = None):
        await self.queue.put((text, reply_to_message_id))

    async def _worker(self):
        while True:
            text, reply_to = await self.queue.get()
            try:
                await self._send_with_retry(text, reply_to)
            finally:
                await asyncio.sleep(self.delay)
                self.queue.task_done()

    async def _send_with_retry(self, text: str, reply_to: Optional[int]):
        # Basic retry logic for Telegram
        for _ in range(6):
            try:
                await self.bot.send_message(
                    chat_id=self.channel_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,  # remove previews
                    reply_to_message_id=reply_to,
                )
                return
            except RetryAfter as e:
                wait = int(getattr(e, "retry_after", 5)) + 1
                logger.warning("RetryAfter: waiting %s seconds", wait)
                await asyncio.sleep(wait)
            except (TimedOut, NetworkError):
                logger.warning("Network/Timeout, retrying...")
                await asyncio.sleep(3)
            except Exception as e:
                logger.error("Send failed: %s", e)
                await asyncio.sleep(2)

# ---------------------------
# RSS Fetching
# ---------------------------
def extract_entry_text(entry: Dict[str, Any]) -> Tuple[str, str]:
    title = entry.get("title", "") or ""
    summary = entry.get("summary", "") or entry.get("description", "") or ""
    # feedparser summaries may include HTML tags; we keep it simple (strip links)
    summary = re.sub(r"<[^>]+>", " ", summary)
    summary = normalize_spaces(summary)
    return title, summary

def parse_entry_time(entry: Dict[str, Any]) -> datetime:
    # Prefer published, then updated, else now
    for key in ("published", "updated", "created"):
        if entry.get(key):
            try:
                dt = dtparser.parse(entry[key])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone(timedelta(hours=3)))
            except Exception:
                pass
    return now_kuwait()

async def fetch_rss_items() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:30]:
                title, summary = extract_entry_text(e)
                pub_dt = parse_entry_time(e)
                source = guess_source(e, url)

                # Optional Arabic-only filter
                txt_all = f"{title} {summary}"
                if ARABIC_ONLY and not has_arabic(txt_all):
                    # Try translate if enabled, otherwise skip
                    if TRANSLATE_EN:
                        title_ar = translate_to_ar(title)
                        summary_ar = translate_to_ar(summary)
                        if not has_arabic(title_ar + " " + summary_ar):
                            continue
                        title, summary = title_ar, summary_ar
                    else:
                        continue

                item_id = short_hash(source, title, str(pub_dt))
                items.append({
                    "id": item_id,
                    "title": title,
                    "summary": summary,
                    "source": source,
                    "published": pub_dt,
                })
        except Exception as ex:
            logger.warning("RSS fetch failed for %s: %s", url, ex)
    # Newest first
    items.sort(key=lambda x: x["published"], reverse=True)
    return items

# ---------------------------
# Events (High Impact Alerts) via events.json
# ---------------------------
# events.json example:
# [
#   {"id":"usd_nfp", "title":"NFP - التغير في الوظائف غير الزراعية", "currency":"USD", "country":"الولايات المتحدة", "datetime":"2025-12-26 16:30", "impact":"high", "source":"Economic Calendar"}
# ]
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
                dt = dt.replace(tzinfo=timezone(timedelta(hours=3)))
            out.append({
                "id": ev.get("id") or short_hash(ev.get("title", ""), ev.get("datetime", "")),
                "title": ev.get("title", ""),
                "currency": ev.get("currency"),
                "country": ev.get("country"),
                "dt": dt,
                "impact": (ev.get("impact") or "").lower(),
                "source": ev.get("source") or "Economic Calendar",
            })
        return out
    except Exception as e:
        logger.warning("Failed reading events.json: %s", e)
        return []

def is_high_impact(ev: Dict[str, Any]) -> bool:
    return ev.get("impact") in ("high", "strong", "عالي")

def build_alert_message(ev: Dict[str, Any], minutes_before: int) -> str:
    title = normalize_spaces(strip_links(ev.get("title", "")))
    currency = (ev.get("currency") or "").strip().upper()
    source = normalize_spaces(strip_links(ev.get("source", "Economic Calendar")))

    if minutes_before == 30:
        header = "⚠️ <b>تنبيه خبر قوي بعد 30 دقيقة</b>"
    else:
        header = "🔥 <b>خبر قوي بعد 5 دقائق!</b>"

    msg = (
        f"{header}\n\n"
        f"📌 <b>{title}</b>\n"
    )
    if currency:
        msg += f"💱 <b>العملة:</b> {currency}\n"
    msg += f"📰 <b>المصدر:</b> {source}\n\n"
    fdt = fmt_dt(ev["dt"].astimezone(timezone(timedelta(hours=3))))
    msg += f"🕒 <b>وقت الخبر:</b> {fdt}\n\n— @news_forexq"
    return msg

# ---------------------------
# Main loop
# ---------------------------
async def run_bot(app: Application):
    if not BOT_TOKEN or not CHANNEL_ID:
        raise RuntimeError("Missing BOT_TOKEN or CHANNEL_ID in environment variables.")

    bot = app.bot
    sender = Sender(bot, CHANNEL_ID, delay=SEND_DELAY_SECONDS)
    await sender.start()

    state = load_state()
    logger.info("Bot Running...")

    while True:
        try:
            # 1) Send scheduled alerts for high impact events
            events = load_events()
            now_dt = now_kuwait()

            for ev in events:
                if not is_high_impact(ev):
                    continue

                # Alert 30 minutes
                t30 = ev["dt"] - timedelta(minutes=30)
                alert30_id = f"{ev['id']}_a30"
                if t30 <= now_dt <= t30 + timedelta(minutes=1) and not is_alert_sent(state, alert30_id):
                    await sender.send(build_alert_message(ev, 30))
                    set_alert_sent(state, alert30_id)
                    save_state(state)

                # Alert 5 minutes
                t5 = ev["dt"] - timedelta(minutes=5)
                alert5_id = f"{ev['id']}_a05"
                if t5 <= now_dt <= t5 + timedelta(minutes=1) and not is_alert_sent(state, alert5_id):
                    await sender.send(build_alert_message(ev, 5))
                    set_alert_sent(state, alert5_id)
                    save_state(state)

            # 2) Fetch RSS news & post
            items = await fetch_rss_items()

            # Post only a few newest each cycle to avoid flooding
            for it in items[:5]:
                if is_sent(state, it["id"]):
                    continue

                title = it["title"]
                summary = it["summary"]
                source = it["source"]
                pub_dt = it["published"]

                # Remove any duplicated title from summary (some feeds repeat it)
                if summary and normalize_spaces(summary).startswith(normalize_spaces(title)):
                    summary = normalize_spaces(summary[len(title):])

                # Strength & sentiment
                strength = detect_strength(title + " " + summary)
                sentiment = detect_sentiment(title + " " + summary)

                # ⭐ Star for strong news
                is_star = (strength == "عالي")

                # If you want to detect currency/country from text, tell me، حالياً اختياري
                msg = build_message(
                    title=title,
                    body=summary,
                    source=source,
                    when_dt=pub_dt,
                    strength=strength,
                    sentiment=sentiment,
                    currency=None,
                    country=None,
                    is_star=is_star,
                )

                await sender.send(msg)
                mark_sent(state, it["id"])
                save_state(state)

            await asyncio.sleep(POLL_SECONDS)

        except Exception as e:
            logger.exception("Loop error: %s", e)
            await asyncio.sleep(5)

async def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    await run_bot(application)

if __name__ == "__main__":
    asyncio.run(main())