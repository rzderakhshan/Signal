import logging
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

_MIN_INTERVAL = float(os.getenv("TELEGRAM_MIN_INTERVAL_SEC", "1.25"))
_MAX_RETRIES = int(os.getenv("TELEGRAM_MAX_RETRIES", "4"))
_last_send_at = 0.0
_session = requests.Session()


def get_telegram_chat_ids():
    raw = os.getenv("TELEGRAM_CHAT_IDS", "").strip()
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]
    one = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return [one] if one else []


def _throttle():
    global _last_send_at
    elapsed = time.monotonic() - _last_send_at
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)


def _retry_after(resp) -> float:
    try:
        return float(resp.json().get("parameters", {}).get("retry_after", 2))
    except Exception:
        return 2.0


def send_telegram_message(text: str) -> bool:
    """Send safely with spacing and repeated 429 handling.

    Telegram may rate-limit bursts even when each message is valid. This helper
    serializes sends, waits between messages and respects retry_after.
    """
    global _last_send_at
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = get_telegram_chat_ids()
    if not token or not chat_ids:
        logging.warning("Telegram credentials are missing; message skipped")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    for chat_id in chat_ids:
        payload = {
            "chat_id": chat_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        sent = False
        for attempt in range(_MAX_RETRIES + 1):
            _throttle()
            try:
                r = _session.post(url, data=payload, timeout=25)
                _last_send_at = time.monotonic()
                if r.status_code == 200:
                    sent = True
                    break
                if r.status_code == 429:
                    delay = max(_MIN_INTERVAL, min(_retry_after(r) + 0.5, 60.0))
                    logging.warning("Telegram rate limited; waiting %.1fs (attempt %s/%s)", delay, attempt + 1, _MAX_RETRIES + 1)
                    time.sleep(delay)
                    continue
                logging.error("Telegram HTTP %s: %s", r.status_code, r.text[:250])
                break
            except requests.RequestException as exc:
                logging.warning("Telegram request error: %s", exc)
                if attempt < _MAX_RETRIES:
                    time.sleep(min(2 ** attempt, 10))
                    continue
                break
        if not sent:
            ok = False
    return ok
