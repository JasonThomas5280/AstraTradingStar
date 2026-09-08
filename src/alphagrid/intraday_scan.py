"""Prospective intraday stock scanner. Read-only: cannot submit broker orders."""
import argparse
from datetime import timedelta
from decimal import Decimal as D
import json
from pathlib import Path
import re
import time
from .execution.broker import PaperBroker,BrokerError
from .ledger import utcnow,timestamp,Ledger
from .process_lock import process_lock
from .service import clock_time
from .strategies.intraday import evaluate,ET


def common_equity(asset):
    name=asset.get('name','').lower()
    return (asset.get('tradable') is True and asset.get('status')=='active'
            and asset.get('class')=='us_equity'
            and asset.get('exchange') in ('NYSE','NASDAQ','ARCA','AMEX','BATS')
            and not any(word in name for word in ('warrant','right','unit','preferred','etf','fund','trust','leveraged','inverse')))


def scan(root,broker=None):
    root=Path(root);b=broker or PaperBroker();now=utcnow();clock=b.clock()
    session=clock_time(clock)
    if not clock['is_open']:
        return {'at':now.isoformat(),'status':'market_closed','candidates':[]}
    movers=b.movers(50)
    if not 0<=(now-timestamp(movers['last_updated'])).total_seconds()<=120:
        raise ValueError('stale_mover_feed')
    current={'at':now.isoformat(),'mover_asof':movers['last_updated'],
             'status':'observing','daily_portfolio_return_target':.10,'orders_submitted':0,
             'execution_enabled':False,'candidates':[]}
    state=Ledger(root/'state/runtime.db')
    report=state.get('experimental_report') or {}
    if state.get('start_equity') and report.get('equity'):
        current['last_reported_portfolio_return']=str(D(report['equity'])/D(state.get('start_equity'))-1)
        current['portfolio_report_at']=report.get('at')
    for mover in movers['gainers']:
        symbol=mover.get('symbol','')
        if not re.fullmatch('[A-Z]{1,5}',symbol) or D(str(mover['percent_change']))<10 or D(str(mover['price']))<1:
            continue
        asset=b.asset(symbol)
        if not common_equity(asset):
            continue
        row={'symbol':symbol,'name':asset['name'],'day_gain_percent':mover['percent_change']}
        try:
            start=session.replace(hour=9,minute=30,second=0,microsecond=0)
            cutoff=now
            delayed=False
            try:
                raw=b.intraday_bars(symbol,start.isoformat(),cutoff.isoformat())
            except BrokerError as exc:
                if exc.status!=403:
                    raise
                cutoff=now-timedelta(minutes=16)
                raw=b.intraday_bars(symbol,start.isoformat(),cutoff.isoformat())
                delayed=True
            row['feed']='sip_delayed' if delayed else 'sip'
            row['data_cutoff']=cutoff.isoformat()
            (root/'data').mkdir(exist_ok=True)
            (root/'data'/f'intraday_{symbol}_{session.date()}.json').write_text(json.dumps(raw))
            daily=b.bars(symbol,(session.date()-timedelta(days=10)).isoformat(),(session.date()-timedelta(days=1)).isoformat()+'T23:59:59Z')
            prior=[r for r in daily if timestamp(r['t']).astimezone(ET).date()<session.date()]
            if not prior:
                raise ValueError('previous_close_missing')
            # Evaluate at the last completed historical bar for diagnostic use;
            # delayed data are never promoted into a currently executable signal.
            evaluation_time=cutoff
            if delayed and raw:
                completed=[timestamp(r['t'])+timedelta(minutes=5) for r in raw if timestamp(r['t'])+timedelta(minutes=5)<=cutoff]
                if completed:
                    evaluation_time=max(completed)+timedelta(seconds=1)
            row.update(evaluate(raw,prior[-1]['c'],evaluation_time))
            if delayed:
                row['historical_signal']=row.get('eligible',False)
                row['historical_reason']=row.get('reason')
                row.update(eligible=False,reason='realtime_sip_access_required')
            quote=b.quote(symbol);quote_age=(utcnow()-timestamp(quote['t'])).total_seconds()
            bid,ask=D(str(quote['bp'])),D(str(quote['ap']))
            if not bid.is_finite() or not ask.is_finite() or not 0<bid<=ask or not 0<=quote_age<=30:
                row.update(eligible=False,reason='stale_or_invalid_quote')
            else:
                row['spread_fraction']=str((ask-bid)/ask)
                if (ask-bid)/ask>D('.005'):
                    row.update(eligible=False,reason='wide_spread')
                elif row.get('eligible') and not D(row['entry_limit'])*D('.995')<=ask<=D(row['entry_limit']):
                    row.update(eligible=False,reason='entry_price_moved')
        except BrokerError as exc:
            row.update(eligible=False,reason=exc.code,http_status=exc.status)
        except (ValueError,KeyError,ArithmeticError):
            row.update(eligible=False,reason='invalid_or_missing_data')
        current['candidates'].append(row)
    reports=root/'reports';reports.mkdir(exist_ok=True)
    (reports/'intraday_latest.json').write_text(json.dumps(current,indent=2))
    folder=root/'data'/'prospective_movers';folder.mkdir(exist_ok=True)
    with (folder/f'{session.date()}.jsonl').open('a') as f:
        f.write(json.dumps({'movers':movers,'scan':current})+'\n')
    return current


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True);p.add_argument('--once',action='store_true')
    args=p.parse_args()
    with process_lock(args.root/'state/intraday_scanner.lock'):
        while True:
            try:
                result=scan(args.root)
                (args.root/'reports/intraday_latest.json').write_text(json.dumps(result,indent=2))
                print(json.dumps({'at':result['at'],'status':result['status'],
                    'candidates':len(result['candidates']),'qualified_signals':sum(r.get('eligible') is True for r in result['candidates'])}),flush=True)
            except Exception as exc:
                failure={'at':utcnow().isoformat(),'status':'scanner_failed','error_type':type(exc).__name__}
                (args.root/'reports/intraday_latest.json').write_text(json.dumps(failure,indent=2))
                print(json.dumps(failure),flush=True)
                if args.once:
                    raise SystemExit(1)
            if args.once:
                break
            time.sleep(60)
