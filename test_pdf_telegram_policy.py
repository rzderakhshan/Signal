from pathlib import Path


def test_scanner_pdf_is_hourly_and_review_only():
    text=Path('scanner.py').read_text(encoding='utf-8')
    assert 'PDF_REPORT_INTERVAL_MINUTES' in text
    assert 'last_mobile_pdf_report' in text
    assert 'send_telegram_document' in text
    assert 'signal_direction in ("BUY", "SELL")' in text
