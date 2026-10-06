# Hyperliquid Whale Tracker → Telegram

این نسخه جایگزین `intraday_signal_scanner` قدیمی است. هیچ معامله‌ای انجام نمی‌دهد و فقط داده عمومی Hyperliquid را می‌خواند و به Telegram هشدار می‌فرستد.

## چه چیزهایی را می‌گیرد؟
- Fill جدید نهنگ: Open Long / Open Short / Add / Reduce / Close
- قیمت واقعی Fill و اندازه دلاری آن
- سفارش Limit باز جدید نهنگ و قیمت سفارش
- حذف/پرشدن سفارش بزرگ
- وضعیت فعلی Position: جهت، Entry، uPnL، Leverage و Liquidation
- Consensus: وقتی حداقل 2 نهنگ روی یک Coin هم‌جهت باشند

## نهنگ‌های پیش‌فرض
فایل `whales.json` پنج آدرس منتخب را دارد. هر زمان خواستی می‌توانی آدرس‌ها را عوض کنی.

## GitHub Secrets
در Repository → Settings → Secrets and variables → Actions این دو Secret باید وجود داشته باشند:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

توکن را هرگز داخل فایل یا commit نگذار.

## اجرای دستی
```bash
pip install -r requirements.txt
python whale_tracker.py --test-telegram
python whale_tracker.py
```

## اجرای خودکار
`.github/workflows/scanner.yml` هر 5 دقیقه اجرا می‌شود. اجرای اول فقط baseline می‌سازد تا پیام‌های قدیمی اسپم نشوند. از اجرای بعدی فقط تغییرات جدید ارسال می‌شوند.

## Thresholdها
پیش‌فرض:
- Fill: حداقل `$100,000`
- Limit Order: حداقل `$250,000`
- Consensus: حداقل 2 نهنگ

از GitHub workflow یا Environment Variable قابل تغییر است.

## فایل‌های قدیمی
در نسخه نهایی لازم نیست `scanner.py`, `signal_engine.py`, فایل‌های backtest، universe و گزارش PDF قبلی باقی بمانند. این پروژه فقط Whale Tracker است.
