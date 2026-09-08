"""Sole mixed paper worker, adopting the existing independent watchdog."""
import argparse
from datetime import timedelta
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import time

from .paper_experiment import MomentumEngine, entry_checks
from .process_lock import process_lock
from .ledger import StateError, utcnow, timestamp
from .service import TERMINAL, clock_time
from .strategies.intraday import evaluate
from .intraday_scan import common_equity


def authorized(root):
    cfg=json.loads((Path(root)/'config/intraday_execution.json').read_text())
    if cfg != {'mode':'paper','enabled':True,'strategy':'intraday_iex_news_5m_v1',
               'max_intraday_names':3,'rotate_etf_on_signal':True}:
        raise StateError('intraday_authorization_missing')
    return cfg


def fresh_watchdog(ledger):
    hb=ledger.get('watchdog_heartbeat')
    if not hb or not 0 <= (utcnow()-timestamp(hb)).total_seconds() <= 15:
        raise StateError('watchdog_missing')


def order_for(signal,symbol,account):
    price,stop,target=(D(signal[k]) for k in ('entry_limit','stop','target'))
    equity,cash=D(account['equity']),D(account['cash'])
    if not all(v.is_finite() and v>0 for v in (price,stop,target,equity,cash)) or not stop<price<target:
        raise StateError('invalid_intraday_order')
    bar_volume=D(signal['signal_bar_volume'])
    if not bar_volume.is_finite() or bar_volume<0:
        raise StateError('invalid_signal_liquidity')
    qty=int(min(equity*D('.14')/price,max(D(0),cash-D('100'))/price,equity*D('.0075')/(price-stop),bar_volume*D('.01')))
    return {'symbol':symbol,'qty':str(qty),'limit_price':str(price),'stop_price':str(stop),'target_price':str(target)}


def setup_priority(candidate):
    # Rank confirmed setups by observed volume expansion, not prior daily gain.
    try:
        relative=D(candidate['relative_volume'])
        if relative.is_finite() and relative>0:
            return relative
    except (KeyError,ValueError,ArithmeticError):
        pass
    return D(0)


def stalled_trade(entry,stop,mark,observed_peak,age_seconds):
    entry,stop,mark,observed_peak=map(D,(entry,stop,mark,observed_peak))
    if not all(v.is_finite() and v>0 for v in (entry,stop,mark,observed_peak)) or stop>=entry:
        raise StateError('invalid_stall_observation')
    return age_seconds>=20*60 and observed_peak<entry+(entry-stop)*D('.5') and mark<=entry


class IntradayEngine(MomentumEngine):
    def intraday_step(self,account,positions,session):
        authorized(self.root)
        fresh_watchdog(self.ledger)
        held={p['symbol'] for p in positions}
        active=self.ledger.active_intents()
        for item in active:
            if item['client_id'].startswith('ag-i-') and item['symbol'] in held and item['snapshot'].get('filled_at'):
                entered=timestamp(item['snapshot']['filled_at'])
                if (utcnow()-entered).total_seconds()>=45*60 or (session.hour,session.minute)>=(15,50):
                    self.time_exit(item)
                    return {'status':'intraday_time_exit_submitted','symbol':item['symbol']}
                position=next(p for p in positions if p['symbol']==item['symbol'])
                entry=D(item['snapshot']['filled_avg_price'])
                mark=D(position['current_price'])
                key='intraday_observed_peak_'+item['client_id']
                peak=max(entry,mark,D(self.ledger.get(key,str(entry))))
                self.ledger.set(key,str(peak))
                if stalled_trade(entry,D(item['payload']['stop_loss']['stop_price']),mark,peak,(utcnow()-entered).total_seconds()):
                    self.time_exit(item)
                    return {'status':'intraday_stall_exit_submitted','symbol':item['symbol']}
        if session.hour>=15:
            return {'status':'intraday_entry_window_closed'}
        path=self.root/'reports/intraday_latest.json'
        if not path.exists():
            return {'status':'intraday_waiting_for_scan'}
        try:
            scan=json.loads(path.read_text())
        except (ValueError,OSError):
            return {'status':'intraday_waiting_for_scan'}
        if (scan.get('strategy')!='intraday_iex_news_5m_v1' or scan.get('status')!='observing'
                or not 0 <= (utcnow()-timestamp(scan['at'])).total_seconds() <= 120):
            return {'status':'intraday_stale_scan'}
        if sum(i['client_id'].startswith('ag-i-') and i['symbol'] in held for i in active)>=3:
            return {'status':'intraday_capacity'}
        for candidate in sorted(scan.get('candidates',[]),key=setup_priority,reverse=True):
            if candidate.get('eligible') is True and candidate.get('feed')=='iex' and candidate['symbol'] not in held:
                result=self.try_candidate(candidate['symbol'])
                if result:
                    return result
        return {'status':'intraday_waiting_for_qualified_signal'}

    def try_candidate(self,symbol):
        # Fresh broker data, not saved report prices, determine the actual order.
        with process_lock(self.root/'state/execution.lock',timeout=20):
            authorized(self.root)
            account,positions,orders,clock,marked=self.refresh()
            session=clock_time(clock)
            if self.ledger.get('halted',True) or not clock['is_open'] or session.hour>=15:
                return None
            fresh_watchdog(self.ledger)
            if any(i['snapshot']['status'] not in TERMINAL for i in self.ledger.active_intents()):
                return None
            if any(p['symbol']==symbol for p in positions):
                return None
            client='ag-i-'+hashlib.sha256(f'{session.date()}:{symbol}'.encode()).hexdigest()[:30]
            if any(i['client_id']==client for i in self.ledger.intents()):
                return None
            if not common_equity(self.broker.asset(symbol)):
                return None
            now=utcnow()
            raw=self.broker.intraday_bars(symbol,session.replace(hour=9,minute=30,second=0,microsecond=0).isoformat(),now.isoformat(),feed='iex')
            daily=self.broker.bars(symbol,(session.date()-timedelta(days=10)).isoformat(),(session.date()-timedelta(days=1)).isoformat()+'T23:59:59Z')
            if not daily:
                return None
            signal=evaluate(raw,daily[-1]['c'],utcnow(),minimum_session_volume=10000)
            if not signal['eligible']:
                return None
            quote=self.broker.quote(symbol)
            bid,ask=D(str(quote['bp'])),D(str(quote['ap']))
            if (not bid.is_finite() or not ask.is_finite() or not 0<bid<=ask
                    or not 0 <= (utcnow()-timestamp(quote['t'])).total_seconds() <= 30
                    or (ask-bid)/ask>D('.005')
                    or ask-bid>(D(signal['entry_limit'])-D(signal['stop']))*D('.20')
                    or not D(signal['entry_limit'])*D('.995')<=ask<=D(signal['entry_limit'])):
                return None
            sized=order_for(signal,symbol,account)
            if int(sized['qty'])<1:
                return None
            # Release one smallest ETF only after a signal survives live checks.
            # The next cycle must reconcile its sale and revalidate the signal.
            if len(positions)>=12:
                candidates=[i for i in self.ledger.active_intents() if i['client_id'].startswith('ag-m-') and any(p['symbol']==i['symbol'] for p in positions)]
                rotation_key='intraday_rotation_'+client
                if not candidates or self.ledger.get(rotation_key):
                    return None
                values={p['symbol']:D(p['market_value']) for p in positions}
                rotation=min(candidates,key=lambda i:values[i['symbol']])
                self.ledger.set(rotation_key,True)
            else:
                rotation=None
                order=sized
                if int(order['qty'])<1:
                    return None
                entry_checks(order,account,positions)
                payload={'symbol':symbol,'qty':order['qty'],'side':'buy','type':'limit','time_in_force':'gtc',
                         'order_class':'bracket','client_order_id':client,'limit_price':order['limit_price'],
                         'stop_loss':{'stop_price':order['stop_price']},'take_profit':{'limit_price':order['target_price']}}
                if not self.ledger.reserve(payload,'Experimental IEX continuation execution policy v2; volume ranking, 1% bar participation, spread/risk gate, 20-minute stall exit, 3R bracket and 45-minute/15:50 ET exit. No established profitability.'):
                    return None
                fresh_watchdog(self.ledger)
                if self.ledger.get('halted',True):
                    raise StateError('halted_before_post')
                try:
                    self.ledger.record(client,self.broker.submit_bracket(payload))
                except Exception:
                    self.halt('submission_requires_reconciliation')
                    raise StateError('submission_requires_reconciliation') from None
                return {'status':'intraday_submitted','symbol':symbol,'qty':order['qty']}
        # time_exit owns the execution lock itself.
        self.time_exit(rotation)
        return {'status':'intraday_rotation_exit_submitted','symbol':rotation['symbol'],'candidate':symbol}


def run(root):
    e=IntradayEngine(root)
    authorized(root)
    with process_lock(Path(root)/'state/supervisor.lock'):
        with process_lock(Path(root)/'state/worker.lock'):
            fresh_watchdog(e.ledger)
            try:
                while True:
                    fresh_watchdog(e.ledger)
                    result=e.cycle_experiment()
                    result.update(execution_enabled=result['status']!='halted',paper=True,execution_policy='iex_execution_v2',reported_at=utcnow().isoformat())
                    e.ledger.set('intraday_execution_report',result)
                    (Path(root)/'reports/intraday_execution.json').write_text(json.dumps(result,indent=2))
                    print(json.dumps(result),flush=True)
                    if result['status']=='halted':
                        return
                    time.sleep(5 if result['status'] in ('intraday_submitted','intraday_rotation_exit_submitted','awaiting_entry_fill') else 15)
            except BaseException:
                e.halt('intraday_worker_failure')
                raise StateError('intraday_worker_failure') from None


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True)
    run(p.parse_args().root.resolve())
