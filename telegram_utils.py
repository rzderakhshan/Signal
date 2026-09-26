import logging
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()


def get_telegram_chat_ids():
    chat_ids_str = os.getenv("TELEGRAM_CHAT_IDS", "")
    if chat_ids_str:
        return [cid.strip() for cid in chat_ids_str.split(",") if cid.strip()]
    single_id = os.getenv("TELEGRAM_CHAT_ID", "")
    return [single_id.strip()] if single_id.strip() else []


def _retry_after_seconds(resp) -> int:
    try:
        body = resp.json()
        return max(1, min(60, int(body.get("parameters", {}).get("retry_after", 1))))
    except (ValueError, TypeError, AttributeError):
        return 1


def send_telegram_message(text: str) -> bool:
    """Send sequentially; on HTTP 429 respect retry_after and retry once only."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_ids = get_telegram_chat_ids()
    if not bot_token or not chat_ids:
        logging.info("Telegram credentials not configured. Skipping Telegram send.")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    success = True
    for chat_id in chat_ids:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        try:
            resp = requests.post(url, data=payload, timeout=20)
            if resp.status_code == 429:
                delay = _retry_after_seconds(resp)
                logging.warning("Telegram rate limited; retrying once after %ss", delay)
                time.sleep(delay)
                resp = requests.post(url, data=payload, timeout=20)
            if resp.status_code != 200:
                logging.error("Telegram send failed with HTTP %s", resp.status_code)
                success = False
        except requests.RequestException as exc:
            logging.error("Telegram request failed: %s", type(exc).__name__)
            success = False
    return success


def send_telegram_document(file_path: str, caption: str = "") -> bool:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_ids = get_telegram_chat_ids()
    if not bot_token or not chat_ids:
        logging.info("Telegram credentials not configured. Skipping Telegram send.")
        return False
    if not os.path.exists(file_path):
        logging.error("Telegram document not found")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    success = True
    for chat_id in chat_ids:
        try:
            with open(file_path, "rb") as pdf_file:
                response = requests.post(
                    url,
                    data={"chat_id": chat_id, "caption": caption[:1000]},
                    files={"document": pdf_file},
                    timeout=60,
                )
            if response.status_code != 200:
                logging.error("Telegram document send failed with HTTP %s", response.status_code)
                success = False
        except requests.RequestException as exc:
            logging.error("Telegram document request failed: %s", type(exc).__name__)
            success = False
    return success
