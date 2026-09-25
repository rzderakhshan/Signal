import os
import requests
import logging
from dotenv import load_dotenv

load_dotenv()

def get_telegram_chat_ids():
    chat_ids_str = os.getenv("TELEGRAM_CHAT_IDS", "")
    if chat_ids_str:
        chat_ids = [cid.strip() for cid in chat_ids_str.split(",") if cid.strip()]
    else:
        single_id = os.getenv("TELEGRAM_CHAT_ID", "")
        chat_ids = [single_id.strip()] if single_id.strip() else []
    return chat_ids

def send_telegram_message(text: str) -> bool:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_ids = get_telegram_chat_ids()

    if not bot_token or not chat_ids:
        logging.info("Telegram credentials not configured. Skipping Telegram send.")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    success = True

    for chat_id in chat_ids:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            resp = requests.post(url, data=payload, timeout=20)
            if resp.status_code != 200:
                logging.error(f"Failed to send Telegram message to {chat_id}: {resp.text}")
                success = False
        except Exception as e:
            logging.error(f"Failed to send Telegram message to {chat_id}: {e}")
            success = False
            
    return success

def send_telegram_document(file_path: str, caption: str = "") -> bool:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_ids = get_telegram_chat_ids()

    if not bot_token or not chat_ids:
        logging.info("Telegram credentials not configured. Skipping Telegram send.")
        return False

    if not os.path.exists(file_path):
        logging.error(f"Telegram document not found: {file_path}")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    success = True

    for chat_id in chat_ids:
        try:
            with open(file_path, "rb") as pdf_file:
                files = {"document": pdf_file}
                data = {
                    "chat_id": chat_id,
                    "caption": caption[:1000],
                }
                response = requests.post(url, data=data, files=files, timeout=60)

            if response.status_code != 200:
                logging.error(f"Telegram PDF response for {chat_id}: {response.text}")
                success = False
            else:
                logging.info(f"Telegram PDF sent successfully to {chat_id}.")
        except Exception as exc:
            logging.error(f"FAILED to send Telegram PDF to {chat_id}: {exc}")
            success = False

    return success
