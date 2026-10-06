# Hyperliquid Whale Tracker v4 → Telegram

نسخه v4 یک Top-10 پویا از نهنگ‌های Hyperliquid می‌سازد، هر 5 دقیقه آن‌ها را مانیتور می‌کند و هر 6 ساعت رتبه‌بندی را به‌روزرسانی می‌کند.

## سیگنال‌ها
- MAIN A/A+: هم‌جهتی چند نهنگ قوی روی یک Coin با وزن کیفیت و حجم پوزیشن.
- Secondary A+/A/B: Fill یا Limit Order مهم یک نهنگ باکیفیت.
- Ranking update: ورود/خروج نهنگ‌ها از Top 10.

## اصلاح v4 برای Telegram
- پیام‌های MAIN جدا و با اولویت بالا فرستاده می‌شوند.
- Alertهای ثانویه یک بازه در `WHALE ACTIVITY DIGEST` جمع می‌شوند تا تعداد پیام‌ها کم شود.
- بین پیام‌ها فاصله زمانی قرار داده شده است.
- در HTTP 429، `retry_after` تلگرام رعایت می‌شود و چند بار retry انجام می‌شود.

## اجرای خودکار
GitHub Actions هر 5 دقیقه اجرا می‌شود. اجرای هر 5 دقیقه به معنی ارسال پیام هر 5 دقیقه نیست؛ فقط رویداد مهم پیام می‌فرستد.

## نصب/آپدیت یک‌کلیکی در Windows
ZIP را داخل `D:\Program` دانلود کن و `DEPLOY_AND_RUN.ps1` را اجرا کن. اسکریپت:
1. جدیدترین `Signal_whale_tracker_v*.zip` را پیدا می‌کند.
2. Repo را در `D:\Program\mnt\data\Signal_whale_tracker` آماده می‌کند.
3. فایل‌های نسخه جدید را جایگزین می‌کند.
4. Commit و Push می‌کند.
5. Workflow را با refresh اجباری Top 10 اجرا می‌کند.
6. تا پایان Run صبر می‌کند و خط‌های مهم Log را نشان می‌دهد.

اگر اجرای PowerShell script محدود بود، از PowerShell این را اجرا کن:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
& "D:\Program\mnt\data\Signal_whale_tracker\DEPLOY_AND_RUN.ps1"
```

GitHub Secrets لازم:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
