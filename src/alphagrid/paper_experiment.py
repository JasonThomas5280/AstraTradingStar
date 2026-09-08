"""Operator-authorized experimental paper service. Never routes to live trading.

Separate from the qualified pullback worker. Uses its existing durable ledger,
reconciliation and independent watchdog, with an explicit experimental mandate.
"""
import argparse
from datetime import date, timedelta
from decimal import Decimal as D, ROUND_UP
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from .execution.broker import BrokerError
from .ledger import StateError, utcnow, timestamp
from .process_lock import process_lock
from .service import Engine, TERMINAL, clock_time, quote_prices
from .strategies.momentum import rank, plan


def entry_checks(order, account, positions):
    qty, price, stop = (D(order[k]) for k in ('qty','limit_price','stop_price'))
    equity, cash = D(account['equity']), D(account['cash'])
    if (not all(v.is_finite() and v > 0 for v in (qty,price,stop,equity,cash))
            or qty != int(qty) or not stop < price or len(positions) >= 12
            or any(p['symbol']==order['symbol'] for p in positions)
            or qty*price > equity*D('.14') or qty*price > cash-D('100')
            or qty*(price-stop) > equity*D('.0075')):
        raise StateError('experimental_entry_limits')


class MomentumEngine(Engine):
    def settings(self):
        cfg=json.loads((self.root/'config/paper_experiment.json').read_text())
        if (cfg.get('mode')!='paper' or cfg.get('operator_authorized') is not True
                or cfg.get('strategy')!='diversified_momentum_paper_v1'
                or cfg.get('cash_target')!='150' or cfg.get('cash_ceiling')!='250'):
            raise StateError('experimental_authorization_missing')
        return cfg

    def submit_experiment(self, order, signal_date):
        with process_lock(self.root/'state/execution.lock',timeout=20):
            self.settings()
            account,positions,orders,clock,marked=self.refresh()
            pending=any(D(i['snapshot']['filled_qty'])==0 and i['snapshot']['status'] not in TERMINAL for i in self.ledger.active_intents())
            if self.ledger.get('halted',True) or not clock['is_open'] or pending:
                raise StateError('experimental_entry_not_ready')
            hb=self.ledger.get('watchdog_heartbeat')
            if not hb or not 0 <= (utcnow()-timestamp(hb)).total_seconds() <= 15:
                raise StateError('watchdog_missing')
            entry_checks(order,account,positions)
            asset=self.broker.asset(order['symbol'])
            if asset.get('tradable') is not True or asset.get('status')!='active' or asset.get('exchange') not in ('NYSE','NASDAQ','ARCA','BATS','AMEX'):
                raise StateError('asset_unavailable')
            _,ask=quote_prices(self.broker.quote(order['symbol']))
            if not D(order['limit_price'])*D('.995') <= ask <= D(order['limit_price']):
                return False
            client='ag-m-'+hashlib.sha256(f"{signal_date}:{order['symbol']}".encode()).hexdigest()[:30]
            payload={'symbol':order['symbol'],'qty':order['qty'],'side':'buy','type':'limit',
                     'time_in_force':'gtc','order_class':'bracket','client_order_id':client,
                     'limit_price':order['limit_price'],'stop_loss':{'stop_price':order['stop_price']},
                     'take_profit':{'limit_price':order['target_price']}}
            thesis='Experimental 60/120-session ETF momentum allocation; 5% initial stop, full 2R target, 20-session maximum. No established benchmark outperformance.'
            if not self.ledger.reserve(payload,thesis):
                return False
            if self.ledger.get('halted',True):
                raise StateError('halted_before_post')
            try:
                response=self.broker.submit_bracket(payload)
                self.ledger.record(client,response)
            except Exception:
                self.halt('submission_requires_reconciliation')
                raise StateError('submission_requires_reconciliation') from None
            return True

    def time_exit(self,item):
        with process_lock(self.root/'state/execution.lock',timeout=20):
            if self.ledger.get('halted',True):
                raise StateError('halted')
            if not self.ledger.reserve_close(item['client_id'],item['symbol']):
                return
            for leg in item['snapshot']['legs']:
                current=self.broker.order_by_id(leg['id'])
                if current['status'] not in TERMINAL:
                    try:
                        self.broker.cancel(leg['id'])
                    except BrokerError:
                        if self.broker.order_by_id(leg['id'])['status'] not in TERMINAL:
                            raise
            deadline=time.monotonic()+10
            while any(o['symbol']==item['symbol'] for o in self.broker.open_orders()):
                if time.monotonic()>deadline:
                    raise StateError('cancel_not_confirmed')
                time.sleep(.3)
            positions=self.broker.positions()
            if not any(p['symbol']==item['symbol'] for p in positions):
                raise StateError('exit_requires_reconciliation')
            result=self.broker.close_position(item['symbol'])
            self.ledger.record_close(item['client_id'],result)

    def cycle_experiment(self):
        cfg=self.settings()
        if self.ledger.get('halted',True):
            return {'status':'halted'}
        account,positions,orders,clock,marked=self.refresh(allow_partial_settlement=True)
        session=clock_time(clock)
        report={'status':'monitoring','at':utcnow().isoformat(),'cash':account['cash'],
                'equity':account['equity'],'cash_target_met':D(account['cash'])<=D('250'),
                'positions':[{'symbol':p['symbol'],'qty':p['qty'],'market_value':p['market_value']} for p in positions],
                'open_order_roots':len(orders),'strategy':cfg['strategy']}
        self.ledger.set('experimental_report',report)
        (self.root/'reports/momentum_paper_account.json').write_text(json.dumps(report,indent=2))
        if not clock['is_open']:
            return dict(report,status='market_closed')
        # Only unfilled parent entries expire; never cancel resting protective exits.
        pending=False
        for item in self.ledger.active_intents():
            o=item['snapshot']
            if D(o['filled_qty'])<D(o['qty']) and o['status'] not in TERMINAL:
                pending=True
                if (utcnow()-timestamp(item['created_at'])).total_seconds()>60:
                    self.broker.cancel(o['id'])
        if pending:
            return dict(report,status='awaiting_entry_fill')
        histories={s:self.history(s,session.date()) for s in cfg['symbols']}
        held={p['symbol'] for p in positions}
        for item in self.ledger.active_intents():
            o=item['snapshot']
            if item['symbol'] in held and o.get('filled_at'):
                entered=timestamp(o['filled_at']).date()
                if sum(b.date>=entered for b in histories[item['symbol']])+1>=20:
                    self.time_exit(item)
                    return dict(report,status='time_exit_submitted')
        if D(account['cash'])<=D('250') or session.hour<10 or (session.hour,session.minute)>=(15,30):
            return report
        eligible=rank(histories)
        # Five completed-session cooldown after any recorded exit for that symbol.
        last_exits={}
        for item in self.ledger.intents():
            for leg in item['snapshot']['legs']:
                if D(leg['filled_qty'])>0 and leg.get('filled_at'):
                    last_exits[item['symbol']]=max(last_exits.get(item['symbol'],date.min),timestamp(leg['filled_at']).date())
        for item in self.ledger.exits():
            if item['snapshot'] and item['snapshot'].get('filled_at'):
                last_exits[item['symbol']]=max(last_exits.get(item['symbol'],date.min),timestamp(item['snapshot']['filled_at']).date())
        eligible=[r for r in eligible if r[0] not in last_exits or sum(b.date>=last_exits[r[0]] for b in histories[r[0]])>=5]
        prices={}
        for symbol,score in eligible:
            if symbol in held:
                continue
            try:
                _,ask=quote_prices(self.broker.quote(symbol))
                prices[symbol]=str((ask*D('1.001')).quantize(D('.01'),rounding=ROUND_UP))
            except (StateError,BrokerError):
                continue
        proposals=plan(eligible,prices,account['equity'],account['cash'],tuple(held))
        # One mutation per fresh cycle: wait for confirmed fill/protection before next.
        for proposal in proposals:
            if self.submit_experiment(proposal,session.date()):
                return dict(report,status='submitted',symbol=proposal['symbol'],qty=proposal['qty'])
        return dict(report,status='no_executable_allocation')


def run(root,resume_drill=False):
    e=MomentumEngine(root)
    e.settings()
    with process_lock(Path(root)/'state/supervisor.lock'):
        with process_lock(Path(root)/'state/worker.lock'):
            if resume_drill:
                a,p,o,c,m=e.refresh()
                if (p or o or e.ledger.get('halt_code')!='operator_execution_drill_complete'
                        or e.ledger.get('experimental_drill_resumed') is True
                        or not (Path(root)/'state/postmortem_2026-09-08.md').exists()):
                    raise StateError('drill_resume_not_verified')
                e.ledger.event('experimental_mandate_resume',{'strategy':'diversified_momentum_paper_v1','qualified':False})
                e.ledger.set('experimental_drill_resumed',True)
                e.ledger.set('halted',False)
            if e.ledger.get('halted',True):
                raise StateError('halt_requires_resolution')
            # A stopped, previously flat watchdog may leave its next empty sweep
            # mid-phase. Reset that phase only after independently verified flat
            # reconciliation and while holding the watchdog's ownership lock.
            with process_lock(Path(root)/'state/watchdog.lock'):
                a,p,o,c,m=e.refresh()
                if not p and not o:
                    path=Path(root)/'state/breaker.json'
                    temp=path.with_suffix('.tmp')
                    temp.write_text(json.dumps({'halted':False,'flat_verified':True,
                        'closing_started':False,'uncertain_close':False,'liquidation_ids':[], 'exit_sides':{}}))
                    temp.replace(path)
            e.ledger.set('watchdog_heartbeat',None)
            flags={'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}
            log=(Path(root)/'logs/momentum_watchdog.log').open('a')
            guard=subprocess.Popen([sys.executable,'-m','alphagrid','--root',str(root),'watchdog'],stdout=log,stderr=log,**flags)
            try:
                for _ in range(20):
                    if guard.poll() is not None or e.ledger.get('halted',True):
                        raise StateError('watchdog_failed')
                    if e.ledger.get('watchdog_heartbeat'):
                        break
                    time.sleep(1)
                else:
                    raise StateError('watchdog_missing')
                while True:
                    if guard.poll() is not None:
                        raise StateError('watchdog_exited')
                    result=e.cycle_experiment()
                    print(json.dumps(result),flush=True)
                    if result['status']=='halted':
                        return
                    time.sleep(5 if result['status'] in ('submitted','awaiting_entry_fill') else 30)
            except BaseException:
                e.halt('experimental_worker_failure')
                if guard.poll() is not None:
                    subprocess.Popen([sys.executable,'-m','alphagrid','--root',str(root),'watchdog'],stdout=log,stderr=log,**flags)
                raise StateError('experimental_worker_failure') from None


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--resume-drill',action='store_true')
    args=parser.parse_args()
    run(args.root.resolve(),args.resume_drill)
