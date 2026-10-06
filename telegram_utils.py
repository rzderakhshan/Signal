import logging
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()


def get_telegram_chat_ids():
    raw = os.getenv("TELEGRAM_CHAT_IDS", "").strip()
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]
    one = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return [one] if one else []


def send_telegram_message(text: str) -> bool:
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
        try:
            r = requests.post(url, data=payload, timeout=20)
            if r.status_code == 429:
                try:
                    delay = int(r.json().get("parameters", {}).get("retry_after", 2))
                except Exception:
                    delay = 2
                time.sleep(max(1, min(delay, 30)))
                r = requests.post(url, data=payload, timeout=20)
            if r.status_code != 200:
                logging.error("Telegram HTTP %s: %s", r.status_code, r.text[:200])
                ok = False
        except requests.RequestException as exc:
            logging.error("Telegram error: %s", exc)
            ok = False
    return ok
