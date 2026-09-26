from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import Iterable

from reportlab.pdfgen import canvas
from reportlab.lib import colors

PAGE_W, PAGE_H = 360, 640  # phone-friendly portrait points
MARGIN = 18


def _num(v, default=0.0):
    try:
        return float(str(v).replace('%','').replace('x',''))
    except Exception:
        return default


def _spark(c, values, x, y, w, h):
    vals = [_num(v) for v in values if v is not None]
    if len(vals) < 2:
        c.setFillColor(colors.grey); c.setFont('Helvetica', 8); c.drawString(x, y+h/2, 'Chart unavailable'); return
    lo, hi = min(vals), max(vals); span = max(hi-lo, 1e-9)
    pts=[]
    for i,v in enumerate(vals):
        px=x+(i/(len(vals)-1))*w; py=y+((v-lo)/span)*h; pts.append((px,py))
    c.setStrokeColor(colors.HexColor('#D9DDE5')); c.line(x,y,x+w,y)
    c.setStrokeColor(colors.HexColor('#243B53')); c.setLineWidth(1.5)
    p=c.beginPath(); p.moveTo(*pts[0])
    for pt in pts[1:]: p.lineTo(*pt)
    c.drawPath(p)
    c.setFillColor(colors.HexColor('#52606D')); c.setFont('Helvetica',7)
    c.drawString(x, y-10, f'{vals[0]:.4g}'); c.drawRightString(x+w, y-10, f'{vals[-1]:.4g}')


def _txt(c, text, x, y, size=9, bold=False, max_chars=58):
    c.setFont('Helvetica-Bold' if bold else 'Helvetica', size)
    c.setFillColor(colors.HexColor('#1F2933'))
    s=str(text)
    if len(s)>max_chars: s=s[:max_chars-1]+'…'
    c.drawString(x,y,s)


def _candidate_page(c, r, idx, total):
    direction=r.get('direction','NEUTRAL')
    title=('BUY REVIEW' if direction=='BUY' else 'SELL REVIEW' if direction=='SELL' else 'WATCH')
    c.setFillColor(colors.HexColor('#F5F7FA')); c.rect(0,0,PAGE_W,PAGE_H,fill=1,stroke=0)
    c.setFillColor(colors.white); c.roundRect(MARGIN, MARGIN, PAGE_W-2*MARGIN, PAGE_H-2*MARGIN, 12, fill=1, stroke=0)
    _txt(c, f"{title}  |  {r.get('symbol','')}", 30, 594, 16, True)
    _txt(c, f"{r.get('stage','')}   {r.get('asset','')}", 30, 575, 9)
    _txt(c, f"Signal {r.get('signal_score',0)}/100   Activity {r.get('activity_score',0)}/100",30,548,11,True)
    _txt(c, f"5m {r.get('trend_5m','N/A')}   |   15m {r.get('trend_15m','N/A')}   |   {r.get('trend_relation','N/A')}",30,530,9)
    _spark(c, r.get('closes',[]), 30, 405, 300, 95)
    _txt(c,'Recent 5-minute price path',30,510,8,True)
    _txt(c, f"RSI {r.get('rsi','N/A')}   ATR {r.get('atr_percent','N/A')}   RelVol {r.get('relative_volume','N/A')}",30,375,9)
    _txt(c, f"Structure: {r.get('market_structure','N/A')}",30,357,9)
    _txt(c, f"Fundamental: {r.get('fundamental','N/A')}",30,326,9,True)
    _txt(c, f"Coverage: {r.get('coverage','N/A')}   Catalyst: {r.get('catalyst','N/A')}",30,308,9)
    reasons=', '.join(r.get('reasons',[])[:5]) or 'none'
    _txt(c,'Why it is on the review list',30,275,9,True)
    y=258
    for chunk in [reasons[i:i+52] for i in range(0,len(reasons),52)][:3]:
        _txt(c,chunk,30,y,8); y-=14
    conflicts=', '.join(r.get('conflicts',[])[:3]) or 'none'
    _txt(c,'Conflicts / caution',30,202,9,True)
    for chunk in [conflicts[i:i+52] for i in range(0,len(conflicts),52)][:2]:
        _txt(c,chunk,30,185,8); break
    _txt(c,'For review only - not an automated trade instruction.',30,64,7)
    _txt(c,f'{idx}/{total}',300,42,7)
    c.showPage()


def build_mobile_signal_pdf(path: str, candidates: Iterable[dict], generated_at: datetime, max_items: int = 12) -> str:
    rows=list(candidates)
    rows.sort(key=lambda r: (r.get('stage')=='CONFIRMED_SETUP', r.get('signal_score',0), r.get('activity_score',0)), reverse=True)
    rows=rows[:max_items]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    c=canvas.Canvas(path,pagesize=(PAGE_W,PAGE_H),pageCompression=1)
    c.setTitle('Signal Scanner Mobile Review')
    c.setFillColor(colors.HexColor('#F5F7FA')); c.rect(0,0,PAGE_W,PAGE_H,fill=1,stroke=0)
    _txt(c,'SIGNAL SCANNER',24,595,18,True)
    _txt(c,'Mobile review report',24,572,11)
    _txt(c,generated_at.strftime('%Y-%m-%d %H:%M'),24,554,8)
    buys=[r for r in rows if r.get('direction')=='BUY']; sells=[r for r in rows if r.get('direction')=='SELL']
    _txt(c,f'BUY review: {len(buys)}',24,510,13,True)
    y=486
    for r in buys[:6]:
        _txt(c,f"{r['symbol']}  score {r.get('signal_score',0)}  {r.get('stage','')}",32,y,9); y-=18
    _txt(c,f'SELL review: {len(sells)}',24,350,13,True)
    y=326
    for r in sells[:6]:
        _txt(c,f"{r['symbol']}  score {r.get('signal_score',0)}  {r.get('stage','')}",32,y,9); y-=18
    _txt(c,'Ranking uses the scanner output; open each following page',24,125,8)
    _txt(c,'for technical, activity, fundamental and catalyst context.',24,111,8)
    _txt(c,'Review only - no automatic buy/sell execution.',24,72,8,True)
    c.showPage()
    total=len(rows)
    for i,r in enumerate(rows,1): _candidate_page(c,r,i,total)
    c.save()
    return path
