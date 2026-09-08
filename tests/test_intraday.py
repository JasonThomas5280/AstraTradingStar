from datetime import datetime,timedelta,timezone
import pytest
from alphagrid.strategies.intraday import evaluate
from alphagrid.execution.broker import PaperBroker,BrokerError
from alphagrid.intraday_scan import common_equity


def sample():
    start=datetime(2026,9,8,13,30,tzinfo=timezone.utc)
    raw=[{'t':(start+timedelta(minutes=5*i)).isoformat(),'o':10.1,'h':10.2,'l':10.,'c':10.1,'v':200000} for i in range(8)]
    raw[-1].update(o=10.15,h=10.5,l=10.1,c=10.4,v=400000)
    return raw,start+timedelta(minutes=40,seconds=5)


def test_confirmed_breakout_has_defined_three_r_exit():
    raw,now=sample();r=evaluate(raw,8,now)
    assert r['eligible']
    from decimal import Decimal as D
    assert D(r['target'])-D(r['entry_limit'])==3*(D(r['entry_limit'])-D(r['stop']))


def test_incomplete_bar_cannot_trigger_entry():
    raw,now=sample()
    assert not evaluate(raw,8,now-timedelta(minutes=1))['eligible']


def test_future_bar_does_not_change_present_signal():
    raw,now=sample();expected=evaluate(raw,8,now)
    raw.append(dict(raw[-1],t=(now+timedelta(minutes=5)).isoformat(),c=100,h=100))
    assert evaluate(raw,8,now)==expected


@pytest.mark.parametrize('change',[{'v':1000},{'c':10.1},{'l':5,'o':10.1}])
def test_rejects_weak_volume_or_no_breakout_or_oversized_stop(change):
    raw,now=sample()
    if 'l' in change: raw[-2].update(change)
    else: raw[-1].update(change)
    assert not evaluate(raw,8,now)['eligible']


def test_stale_data_and_small_daily_gain_rejected():
    raw,now=sample()
    assert evaluate(raw,8,now+timedelta(minutes=3))['reason']=='stale_completed_bar'
    assert not evaluate(raw,10,now)['eligible']


@pytest.mark.parametrize('name',['Example Warrant','Example Rights','Example Units','Example ETF','Example Preferred Stock'])
def test_non_common_instruments_excluded(name):
    assert not common_equity({'name':name,'tradable':True,'status':'active','class':'us_equity','exchange':'NASDAQ'})


def test_bar_pagination_and_mover_validation(monkeypatch):
    b=PaperBroker();calls=[]
    pages=iter([{'bars':{'ABC':[{'t':'one'}]},'next_page_token':'next'}, {'bars':{'ABC':[{'t':'two'}]},'next_page_token':None}])
    def request(method,path,**kwargs):
        calls.append((method,path,dict(kwargs['query'])))
        return next(pages)
    monkeypatch.setattr(b,'_request',request)
    assert len(b.intraday_bars('ABC','2026-08-01','2026-09-01'))==2
    assert calls[-1][2]['page_token']=='next'
    assert all(c[0]=='GET' and c[2]['timeframe']=='5Min' for c in calls)
    with pytest.raises(BrokerError):b.movers(51)


def test_delayed_positive_signal_cannot_become_executable(tmp_path,monkeypatch):
    from alphagrid import intraday_scan as scanner
    from alphagrid.strategies.intraday import ET
    from unittest.mock import Mock
    raw,end=sample();now=end+timedelta(minutes=16)
    monkeypatch.setattr(scanner,'utcnow',lambda:now)
    monkeypatch.setattr(scanner,'clock_time',lambda c:now.astimezone(ET))
    b=Mock();b.clock.return_value={'is_open':True}
    b.movers.return_value={'last_updated':now.isoformat(),'gainers':[{'symbol':'ABC','percent_change':30,'price':10.4}]}
    b.asset.return_value={'name':'Example Common Stock','class':'us_equity','exchange':'NASDAQ','tradable':True,'status':'active'}
    b.intraday_bars.side_effect=[BrokerError('http_error',status=403),raw]
    b.bars.return_value=[{'t':'2026-09-04T04:00:00Z','c':8}]
    b.quote.return_value={'t':now.isoformat(),'bp':10.4,'ap':10.41}
    result=scanner.scan(tmp_path,b)
    row=result['candidates'][0]
    assert row['historical_signal'] is True
    assert row['eligible'] is False
    assert row['reason']=='realtime_sip_access_required'
    assert result['orders_submitted']==0 and result['execution_enabled'] is False
    b.submit_bracket.assert_not_called()
