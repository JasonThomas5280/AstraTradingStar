"""Selection-biased diagnostic of today's scanned universe, not OOS proof."""
from datetime import datetime,timedelta,time
from decimal import Decimal as D, ROUND_DOWN
import argparse,hashlib,json
from pathlib import Path
from alphagrid.execution.broker import PaperBroker
from alphagrid.ledger import timestamp,utcnow
from alphagrid.strategies.intraday import evaluate,regular_bars,ET

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--feed',choices=('sip','iex'),default='sip')
parser.add_argument('--policy',choices=('v1','v2'),default='v1')
parser.add_argument('--cached',action='store_true')
parser.add_argument('--universe-report',type=Path,default=root/'reports/intraday_latest.json')
args=parser.parse_args()
scan=json.loads(args.universe_report.read_text())
symbols=[r['symbol'] for r in scan['candidates']]
broker=PaperBroker();today=utcnow().astimezone(ET).date();start=today-timedelta(days=60)
end=(today-timedelta(days=1)).isoformat()+'T23:59:59Z'
data={};rawdata={};previous={};hashes={}
for s in symbols:
    path=root/'data'/f'intraday_history_{args.feed}_{s}.json'
    daily_path=root/'data'/f'intraday_daily_{args.feed}_{s}.json'
    if args.cached:
        raw=json.loads(path.read_text());daily=json.loads(daily_path.read_text())
    else:
        raw=broker.intraday_bars(s,start.isoformat(),end,feed=args.feed)
        daily=broker.bars(s,(start-timedelta(days=10)).isoformat(),end)
        path.write_text(json.dumps(raw));daily_path.write_text(json.dumps(daily))
    hashes[s]=hashlib.sha256(path.read_bytes()).hexdigest()
    rawdata[s]={}
    for r in raw:
        stamp=timestamp(r['t']);rawdata[s].setdefault(stamp.astimezone(ET).date(),[]).append((stamp,r))
    data[s]={b['t']:b for b in regular_bars(raw,utcnow())}
    previous[s]={timestamp(b['t']).astimezone(ET).date():D(str(a['c'])) for a,b in zip(daily,daily[1:])}
    print(json.dumps({'downloaded':s,'bars':len(raw)}),flush=True)
times=sorted(set(t for bars in data.values() for t in bars))
cash=D('2500');positions={};marks={};used=set();trades=[];days=[];session=None
peak=cash;maxdd=D(0);daystart=cash;halted=False
for t in times:
    day=t.astimezone(ET).date()
    if day!=session:
        if session is not None:
            assert not positions,'Every prior-day holding must have a verified simulated exit'
            days.append({'date':str(session),'return':float(cash/daystart-1),'ending_equity':float(cash)})
        session=day;daystart=cash;used=set();halted=False
    equity=cash+sum(p['qty']*marks[s] for s,p in positions.items())
    def priority(s):
        if day not in previous[s]:return D(0)
        history=[r for stamp,r in rawdata[s].get(day,[]) if stamp<t]
        signal=evaluate(history,previous[s][day],t,minimum_session_volume=10000 if args.feed=='iex' else 1000000)
        return D(signal.get('relative_volume','0'))
    for s in sorted(symbols,key=priority,reverse=True) if args.policy=='v2' else symbols:
        bar=data[s].get(t)
        if bar is None:continue
        marks[s]=bar['c']
        if s not in positions and s not in used and not halted and t.astimezone(ET).hour<15 and day in previous[s]:
            # Only bars completed before this entry bar are passed to the signal.
            history=[r for stamp,r in rawdata[s].get(day,[]) if stamp<t]
            signal=evaluate(history,previous[s][day],t,minimum_session_volume=10000 if args.feed=='iex' else 1000000)
            if signal.get('eligible'):
                limit=D(signal['entry_limit']);stop=D(signal['stop']);entry=bar['o']*D('1.001')
                qty=int(min(equity*D('.0075')/(limit-stop),equity*D('.14')/limit,cash/(limit*D('1.0005'))))
                if args.policy=='v2':
                    qty=int(min(qty,D(signal['signal_bar_volume'])*D('.01'),max(D(0),cash-D('100'))/(limit*D('1.0005'))))
                if stop<entry<=limit and qty>0 and len(positions)<12 and D(qty)<=bar['v']*D('.01'):
                    cash-=qty*entry*D('1.0005');used.add(s)
                    positions[s]={'qty':qty,'entry':entry,'stop':stop,'target':D(signal['target']),'t':t,'peak':entry}
        if s in positions:
            p=positions[s];price=None;reason=None
            p['peak']=max(p['peak'],bar['h'])
            if bar['l']<=p['stop']:price=min(bar['o'],p['stop'])*D('.999');reason='stop'
            elif bar['h']>p['target']:price=p['target']*D('.999');reason='target'
            elif args.policy=='v2' and t+timedelta(minutes=5)-p['t']>=timedelta(minutes=20) and p['peak']<p['entry']+(p['entry']-p['stop'])*D('.5') and bar['c']<=p['entry']:price=bar['c']*D('.999');reason='stall_exit'
            elif t-p['t']>=timedelta(minutes=45) or (t.astimezone(ET).hour,t.astimezone(ET).minute)>=(15,50):price=bar['c']*D('.999');reason='time_exit'
            if price:
                cash+=p['qty']*price*D('.9995')
                trades.append({'symbol':s,'entry_at':p['t'].isoformat(),'exit_at':t.isoformat(),'pnl':float(p['qty']*(price*D('.9995')-p['entry']*D('1.0005'))),'reason':reason})
                del positions[s]
    equity=cash+sum(p['qty']*marks[s] for s,p in positions.items());peak=max(peak,equity);maxdd=max(maxdd,1-equity/peak)
    if equity<=daystart*D('.975'):
        halted=True
        # Price already observed; conservative slippage, no assumption of a future bar.
        for s,p in list(positions.items()):
            exit_price=marks[s]*D('.999');cash+=p['qty']*exit_price*D('.9995')
            trades.append({'symbol':s,'entry_at':p['t'].isoformat(),'exit_at':t.isoformat(),'pnl':float(p['qty']*(exit_price*D('.9995')-p['entry']*D('1.0005'))),'reason':'daily_loss_mark_exit'})
            del positions[s]
if session is not None:
    assert not positions,'Incomplete session data: no invented terminal fills'
    days.append({'date':str(session),'return':float(cash/daystart-1),'ending_equity':float(cash)})
result={'strategy':'intraday_iex_news_5m_v1' if args.feed=='iex' else 'intraday_continuation_5m_v1',
        'feed':args.feed,'execution_policy':args.policy,'news_used_in_entry_rule':False,'source_universe_observed_at':scan['at'],'symbols':symbols,
        'sessions':len(days),'trades':len(trades),'return':float(cash/D('2500')-1),'ending_equity':float(cash),
        'max_mark_drawdown':float(maxdd),'target_daily_return':.10,'days_reaching_target':sum(d['return']>=.10 for d in days),
        'best_day':max((d['return'] for d in days),default=None),'worst_day':min((d['return'] for d in days),default=None),
        'cost_bps_per_side':5,'slippage_bps_per_side':10,'daily_results':days,'trade_details':trades,'data_sha256':hashes,
        'limitations':['Today-selected universe creates severe selection bias; not unbiased or out-of-sample validation.',
                       'Five-minute OHLC, stop-first ambiguous bars; spreads, halts, fills and liquidation are approximated.',
                       'Common data timestamps and observed marks approximate portfolio checks; no guaranteed execution.',
                       'No intraday stock execution enabled by this diagnostic; no claim of achievable 10% daily return.',
                       'Cash begins unallocated for research; not a simulation of the currently invested ETF account.',
                       'Daily-loss circuit resets next session in diagnostic; live halt requires resolution.',
                       'Historical quote spreads unavailable: spread/risk gate not simulated. V2 uses bar highs for peak versus sampled marks in worker; not identical execution.',
                       'Bar-level diagnostic retains baseline fill assumptions and does not qualify a strategy.']}
(root/'reports'/f'intraday_backtest_{args.feed}_{args.policy}.json').write_text(json.dumps(result,indent=2,allow_nan=False))
print(json.dumps({k:v for k,v in result.items() if k not in ('daily_results','trade_details','data_sha256','limitations')}))
