from datetime import datetime, timezone
from pathlib import Path
from mobile_signal_report import build_mobile_signal_pdf


def test_mobile_pdf_is_created(tmp_path):
    out=tmp_path/'report.pdf'
    rows=[{
        'symbol':'TEST','asset':'stocks','stage':'CONFIRMED_SETUP','direction':'BUY',
        'signal_score':82,'activity_score':70,'trend_5m':'Bullish','trend_15m':'Bullish',
        'trend_relation':'WITH_TREND','rsi':55,'atr_percent':'1.2%','relative_volume':'2.1x',
        'market_structure':'Uptrend','fundamental':'Score: 75/100 (STRONG)','coverage':'90%',
        'catalyst':'POSITIVE','reasons':['price_re_entry_bullish','volume_spike'], 'conflicts':[],
        'closes':[100,101,100.5,102,103]
    }]
    build_mobile_signal_pdf(str(out), rows, datetime.now(timezone.utc))
    assert out.exists() and out.stat().st_size > 1000
    assert out.read_bytes().startswith(b'%PDF')
