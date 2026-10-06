# Hyperliquid Whale Tracker v3

این نسخه به‌جای ۵ والت ثابت، یک سیستم پویا برای انتخاب و رتبه‌بندی نهنگ‌هاست.

## منطق
- هر ۵ دقیقه: Top 10 فعلی مانیتور می‌شوند.
- هر ۶ ساعت: کاندیدهای جدید از لیست عمومی نهنگ‌های Hyperliquid کشف می‌شوند و با API رسمی Hyperliquid دوباره امتیاز می‌گیرند.
- امتیاز کیفیت نهنگ 0 تا 100 بر اساس PnL ماه/هفته/کل، اندازه حساب، اندازه پوزیشن و ریسک leverage است.
- نهنگ‌های ضعیف می‌توانند از Top 10 خارج و نهنگ‌های بهتر جایگزین شوند.

## سیگنال‌ها
### MAIN
وقتی چند نهنگ با کیفیت روی یک Coin و جهت هم‌راستا باشند:
- حداقل 4 نهنگ
- weighted consensus حداقل 68%
- حداقل $5M پوزیشن ترکیبی
- یک نهنگ به تنهایی بیش از 50% سمت را تشکیل ندهد

A+ MAIN: حداقل 6 نهنگ و weighted consensus حداقل 78%.

### Secondary
حرکت یک نهنگ قوی هم می‌تواند سیگنال بدهد. هر event امتیاز 0..100 می‌گیرد و بر اساس کیفیت نهنگ، حجم معامله، conviction و نوع action درجه‌بندی می‌شود.

## GitHub Actions
workflow قبلی همچنان هر ۵ دقیقه اجرا می‌شود. Secrets همان قبلی هستند:
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID

## تست دستی
```bash
python whale_tracker.py --test-telegram
python whale_tracker.py --refresh-ranking
python whale_tracker.py
```
