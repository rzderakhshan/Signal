import os
import requests
from dotenv import load_dotenv

def main():
    load_dotenv()
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token:
        print("Error: No TELEGRAM_BOT_TOKEN in .env")
        return
    
    # 1. Fetch updates to find channel ID
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    resp = requests.get(url).json()
    chat_id = None
    
    if resp.get('ok') and resp.get('result'):
        for update in resp['result']:
            if 'channel_post' in update:
                chat_id = str(update['channel_post']['chat']['id'])
                break
            elif 'message' in update:
                chat_id = str(update['message']['chat']['id'])
                break
                
    if not chat_id:
        print("Error: Could not find any messages or channel posts in getUpdates.")
        return
        
    print("Found Chat ID successfully.")
    
    # 2. Save chat_id to .env
    env_path = '.env'
    with open(env_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    with open(env_path, 'w', encoding='utf-8') as f:
        for line in lines:
            if line.startswith('TELEGRAM_CHAT_ID='):
                f.write(f"TELEGRAM_CHAT_ID={chat_id}\n")
            else:
                f.write(line)
                
    print("Chat ID saved to .env")
    
    # 3. Send test message
    send_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "✅ <b>Intraday Signal Scanner</b>\nTest message sent successfully. Connection is verified!",
        "parse_mode": "HTML"
    }
    send_resp = requests.post(send_url, data=payload)
    if send_resp.status_code == 200:
        print("Telegram test message sent successfully!")
    else:
        print("Failed to send test message:", send_resp.text)

if __name__ == '__main__':
    main()
