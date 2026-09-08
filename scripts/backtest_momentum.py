"""Chronological multi-asset paper-strategy research with frozen parameters."""
import hashlib
import json
from pathlib import Path
from alphagrid.research.backtest import load_csv
from alphagrid.strategies.momentum import rank, plan

root = Path(__file__).resolve().parents[1]
cfg = json.loads((root/'config/paper_experiment.json').read_text())
data = {s:load_csv(root/'data'/f'momentum_{s}.csv') for s in cfg['symbols']+['SPY']}
dates = [b.date for b in data['SPY']]
assert all([b.date for b in bars] == dates for bars in data.values())

def window(start, end):
    cash, peak, dd = 2500., 2500., 0.
    held, cooldown, trades, curve = {}, {}, [], []
    halted = None
    for i in range(start, end):
        beginning = cash+sum(p['qty']*data[s][i-1].close for s,p in held.items())
        histories = {s:bars[:i] for s,bars in data.items() if s != 'SPY'}
        rankings = [r for r in rank(histories) if cooldown.get(r[0], -1) <= i]
        # Actual open plus two bps adverse slippage; modeled 1bp transaction cost.
        prices = {s:str(bars[i].open*1.0002) for s,bars in data.items()}
        if not halted:
            entries = plan(rankings, prices, str(beginning), str(cash), tuple(held), cash_target='152')
            for p in entries:
                s, qty, entry = p['symbol'], int(p['qty']), float(p['limit_price'])
                cash -= qty*entry*1.0001
                held[s] = {'qty':qty,'entry':entry,'stop':float(p['stop_price']),
                           'target':float(p['target_price']),'i':i}
        for s,p in list(held.items()):
            bar, reason, exit_price = data[s][i], None, None
            if bar.low <= p['stop']:
                reason, exit_price = 'stop', min(bar.open,p['stop'])*.9998
            elif bar.high > p['target']:
                reason, exit_price = 'target', p['target']*.9998
            elif i-p['i'] >= 19 or i == end-1:
                reason, exit_price = 'time_or_window_exit', bar.close*.9998
            if reason:
                cash += p['qty']*exit_price*.9999
                trades.append({'symbol':s,'entry_date':str(dates[p['i']]),'exit_date':str(dates[i]),
                               'pnl':p['qty']*(exit_price*.9999-p['entry']*1.0001),'reason':reason})
                del held[s]
                cooldown[s] = i+5
        equity = cash+sum(p['qty']*data[s][i].close for s,p in held.items())
        peak = max(peak,equity)
        dd = max(dd,1-equity/peak)
        if not halted and (equity <= beginning*.975 or equity <= peak*.9):
            halted = str(dates[i])
            for s,p in list(held.items()):
                price = data[s][i].close*.9998
                cash += p['qty']*price*.9999
                trades.append({'symbol':s,'entry_date':str(dates[p['i']]),'exit_date':str(dates[i]),
                               'pnl':p['qty']*(price*.9999-p['entry']*1.0001),'reason':'risk_halt'})
                del held[s]
            equity = cash
            dd = max(dd,1-equity/peak)
        curve.append({'date':str(dates[i]),'equity':equity,'cash':cash})
    return {'start':str(dates[start]),'end':str(dates[end-1]),'sessions':end-start,
            'ending_equity':equity,'return':equity/2500-1,'max_close_drawdown':dd,
            'trades':len(trades),'win_rate':sum(t['pnl']>0 for t in trades)/len(trades) if trades else None,
            'expectancy':sum(t['pnl'] for t in trades)/len(trades) if trades else None,
            'spy_price_return':data['SPY'][end-1].close*.9998*.9999/(data['SPY'][start].open*1.0002*1.0001)-1,
            'halted_on':halted,'cash_target_session_fraction':sum(p['cash']<=250 for p in curve)/len(curve),
            'trade_details':trades,'equity_curve':curve}

report = {'strategy':cfg['strategy'],'config_sha256':hashlib.sha256((root/'config/paper_experiment.json').read_bytes()).hexdigest(),
          'data_sha256':{s:hashlib.sha256((root/'data'/f'momentum_{s}.csv').read_bytes()).hexdigest() for s in data},
          'development':window(121,len(dates)-252),'validation':window(len(dates)-252,len(dates)),
          'limitations':['Daily bars cannot establish intraday execution, stop slippage or watchdog behavior.',
                         'Split-adjusted price data omit dividends for strategy and benchmark; not total returns.',
                         'Fixed current ETF universe has selection bias; opening fills approximate marketable limits.',
                         'Risk halts evaluated at daily close only; live paper watchdog checks more frequently.',
                         'Validation is retrospective, not proof of future returns; no parameter optimization.',
                         'No reinvestment after risk halt. Cash target is suspended by safety exits.']}
path = root/'reports'/'momentum_backtest.json'
path.write_text(json.dumps(report,indent=2,allow_nan=False))
for name in ['development','validation']:
    print(json.dumps({name:{k:v for k,v in report[name].items() if k not in ('trade_details','equity_curve')}}))
