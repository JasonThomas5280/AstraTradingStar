"""Frozen intraday continuation signal; completed regular-session bars only."""
from datetime import timedelta
from decimal import Decimal as D, ROUND_UP, ROUND_DOWN
from statistics import median
from zoneinfo import ZoneInfo
from ..ledger import timestamp
from ..risk.numbers import number

ET=ZoneInfo('America/New_York')


def regular_bars(raw, asof):
    bars=[]
    previous=None
    for row in raw:
        t=timestamp(row['t'])
        if previous is not None and t<=previous:
            raise ValueError('unordered_bars')
        previous=t
        vals={k:number(row[k],positive=k!='v') for k in ('o','h','l','c','v')}
        if vals['v']<0 or vals['l']>min(vals['o'],vals['c']) or vals['h']<max(vals['o'],vals['c']) or vals['l']>vals['h']:
            raise ValueError('invalid_ohlcv')
        local=t.astimezone(ET)
        if t+timedelta(minutes=5)<=asof and (9,30)<=(local.hour,local.minute)<(16,0):
            bars.append(dict(vals,t=t))
    return bars


def evaluate(raw, previous_close, asof, *, minimum_session_volume=1000000):
    minimum_session_volume=number(minimum_session_volume,positive=True)
    bars=regular_bars(raw,asof)
    today=asof.astimezone(ET).date()
    bars=[b for b in bars if b['t'].astimezone(ET).date()==today]
    if len(bars)<8:
        return {'eligible':False,'reason':'insufficient_session_bars'}
    last=bars[-1]; age=(asof-(last['t']+timedelta(minutes=5))).total_seconds()
    if not 0<=age<=90:
        return {'eligible':False,'reason':'stale_completed_bar'}
    if asof.astimezone(ET).hour>=15:
        return {'eligible':False,'reason':'entry_window_closed'}
    close=number(previous_close,positive=True)
    gain=last['c']/close-1
    volume=sum(b['v'] for b in bars)
    if last['c']<1 or gain<D('.10') or volume<minimum_session_volume:
        return {'eligible':False,'reason':'price_gain_or_volume','gain':str(gain),'session_volume':str(volume)}
    vwap=sum((b['h']+b['l']+b['c'])/3*b['v'] for b in bars)/volume
    level=max(b['h'] for b in bars[-4:-1])
    prior_volume=median(b['v'] for b in bars[-7:-1])
    if last['c']<=vwap or last['c']<=level or prior_volume<=0 or last['v']<prior_volume*D('1.5'):
        return {'eligible':False,'reason':'no_confirmed_breakout','gain':str(gain),'vwap':str(vwap)}
    entry=(last['c']*D('1.005')).quantize(D('.01'),rounding=ROUND_UP)
    stop=min(b['l'] for b in bars[-4:-1]).quantize(D('.01'),rounding=ROUND_DOWN)
    distance=(entry-stop)/entry
    if not D('.005')<=distance<=D('.05'):
        return {'eligible':False,'reason':'stop_distance','stop_fraction':str(distance)}
    return {'eligible':True,'reason':'confirmed_continuation','signal_at':last['t'].isoformat(),
            'gain':str(gain),'vwap':str(vwap),'session_volume':str(volume),
            'entry_limit':str(entry),'stop':str(stop),'target':str(entry+3*(entry-stop)),
            'maximum_holding_minutes':45}
