# Hyperliquid Whale Tracker v5

نسخه v5 علاوه بر Top-10 پویا و سیگنال اجماع، چرخه کامل هر سیگنال را نگه می‌دارد تا باز و بسته شدن آن با یک ID ثابت قابل پیگیری باشد.

## Signal ID و چرخه معامله
سیگنال اصلی نمونه:

`MAIN-ETH-LONG-20261006-001`

سیگنال نهنگ منفرد نمونه:

`WHL-SOL-SHORT-20261006-002`

همان ID تا پایان سیگنال ثابت می‌ماند.

Telegram می‌تواند سه نوع پیام بدهد:
- `OPEN MAIN SIGNAL` / `OPEN WHALE SIGNAL`
- `UPDATE MAIN SIGNAL` / `UPDATE WHALE SIGNAL`
- `CLOSE MAIN SIGNAL` / `CLOSE WHALE SIGNAL`

در پیام CLOSE موارد زیر می‌آید:
- Signal ID
- Coin و جهت LONG/SHORT
- قیمت مرجع باز شدن
- قیمت بسته شدن
- نتیجه درصدی از زمان صدور سیگنال
- مدت باز بودن
- دلیل بسته شدن

برای MAIN SIGNAL بسته شدن زمانی رخ می‌دهد که Consensus دیگر شروط اصلی را نداشته باشد یا جهت اجماع برگردد.
برای WHALE SIGNAL بسته شدن زمانی ثبت می‌شود که همان نهنگ پوزیشن مربوطه را ببندد/برگرداند یا از Top-10 خارج شود.

## Top-10 پویا
- هر 5 دقیقه مانیتور می‌شود.
- هر 6 ساعت Ranking دوباره ارزیابی می‌شود.
- نهنگ بهتر می‌تواند جای نهنگ ضعیف‌تر را بگیرد.
- امتیاز نهنگ از PnL اخیر، PnL ماهانه/کل، اندازه حساب، اندازه پوزیشن و ریسک ساخته می‌شود.

## Main Signal
MAIN فقط وقتی باز می‌شود که چند نهنگ قوی هم‌جهت باشند و شروط `MAIN_MIN_WHALES`, `MAIN_MIN_WEIGHTED_SHARE` و حداقل Notional برقرار باشد.

## State
تمام Signal IDها و وضعیت OPEN/CLOSED در `.state/whale_state.json` نگه‌داری می‌شوند و GitHub Actions آن را بین اجراها Cache می‌کند.

## Telegram
نسخه v5 قابلیت‌های v4 را حفظ می‌کند:
- batching پیام‌های فرعی
- اولویت بالاتر برای MAIN
- فاصله بین پیام‌ها
- retry خودکار برای HTTP 429

## GitHub Secrets
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## اجرای خودکار
`.github/workflows/scanner.yml` هر 5 دقیقه اجرا می‌شود.
