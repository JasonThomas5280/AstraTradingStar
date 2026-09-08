from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
import json
from unittest.mock import Mock
import pytest
from alphagrid import intraday_execution as module
from alphagrid.intraday_execution import IntradayEngine,order_for,authorized
from alphagrid.ledger import StateError
from alphagrid.strategies.intraday import ET


@pytest.fixture
def engine(tmp_path,monkeypatch):
    (tmp_path/'config').mkdir()
    (tmp_path/'config/intraday_execution.json').write_text(json.dumps({'mode':'paper','enabled':True,
        'strategy':'intraday_iex_news_5m_v1','max_intraday_names':3,'rotate_etf_on_signal':True}))
    now=datetime(2026,9,8,14,10,5,tzinfo=timezone.utc)
    monkeypatch.setattr(module,'utcnow',lambda:now)
    monkeypatch.setattr(module,'clock_time',lambda c:now.astimezone(ET))
    # Broker refresh is covered by the shared reconciliation tests; this fixture
    # isolates submission routing after successful reconciliation.
    e=IntradayEngine(tmp_path,Mock());e.ledger=Mock()
    e.ledger.get.side_effect=lambda key,default=None: now.isoformat() if key=='watchdog_heartbeat' else False
    e.ledger.active_intents.return_value=[];e.ledger.intents.return_value=[]
    e.ledger.reserve.return_value=True
    e.refresh=Mock(return_value=({'equity':'2500','cash':'300'},[],[],{'is_open':True},None))
    e.broker.asset.return_value={'name':'Example Common Stock','class':'us_equity','exchange':'NASDAQ','tradable':True,'status':'active'}
    start=now.replace(hour=13,minute=30,second=0)
    raw=[{'t':(start+timedelta(minutes=5*i)).isoformat(),'o':10.1,'h':10.2,'l':10.,'c':10.1,'v':20000} for i in range(8)]
    raw[-1].update(o=10.15,h=10.5,l=10.1,c=10.4,v=40000)
    e.broker.intraday_bars.return_value=raw;e.broker.bars.return_value=[{'c':8}]
    e.broker.quote.return_value={'t':now.isoformat(),'bp':10.43,'ap':10.45}
    return e


def test_qualified_signal_submits_durable_paper_bracket(engine):
    result=engine.try_candidate('ABC')
    assert result['status']=='intraday_submitted'
    payload=engine.broker.submit_bracket.call_args.args[0]
    assert payload['client_order_id'].startswith('ag-i-')
    assert payload['order_class']=='bracket'
    assert D(payload['stop_loss']['stop_price'])<D(payload['limit_price'])<D(payload['take_profit']['limit_price'])
    assert D(payload['qty'])*D(payload['limit_price'])<=200
    engine.ledger.reserve.assert_called_once()
    engine.ledger.record.assert_called_once()


@pytest.mark.parametrize('failure',['wide','stale','weak_signal','duplicate','halted','pending'])
def test_non_executable_signal_cannot_submit(engine,failure):
    if failure=='wide':engine.broker.quote.return_value['bp']=9
    if failure=='stale':engine.broker.quote.return_value['t']='2026-09-08T13:00:00Z'
    if failure=='weak_signal':engine.broker.intraday_bars.return_value[-1]['c']=10.1
    if failure=='duplicate':
        import hashlib
        client='ag-i-'+hashlib.sha256(b'2026-09-08:ABC').hexdigest()[:30]
        engine.ledger.intents.return_value=[{'client_id':client}]
    if failure=='halted':engine.ledger.get.side_effect=lambda k,d=None:True
    if failure=='pending':engine.ledger.active_intents.return_value=[{'snapshot':{'status':'new'}}]
    assert engine.try_candidate('ABC') is None
    engine.broker.submit_bracket.assert_not_called()


def test_ambiguous_submission_is_halted_never_retried(engine):
    engine.broker.submit_bracket.side_effect=TimeoutError
    engine.halt=Mock()
    with pytest.raises(StateError,match='submission_requires_reconciliation'):
        engine.try_candidate('ABC')
    engine.broker.submit_bracket.assert_called_once()
    engine.halt.assert_called_once()


def test_full_portfolio_rotates_once_without_simultaneous_buy(engine):
    positions=[{'symbol':f'ETF{i}','market_value':str(100+i)} for i in range(12)]
    a,_,o,c,m=engine.refresh.return_value
    engine.refresh.return_value=(a,positions,o,c,m)
    engine.ledger.active_intents.return_value=[{'client_id':f'ag-m-{i}','symbol':f'ETF{i}','snapshot':{'status':'filled'}} for i in range(12)]
    engine.time_exit=Mock()
    result=engine.try_candidate('ABC')
    assert result['status']=='intraday_rotation_exit_submitted'
    assert result['symbol']=='ETF0'
    engine.broker.submit_bracket.assert_not_called()
    engine.time_exit.assert_called_once()
    engine.ledger.set.assert_called_once()


@pytest.mark.parametrize('late',[False,True])
def test_intraday_time_exit_without_new_signal(engine,late):
    now=module.utcnow();session=now.astimezone(timezone(timedelta(hours=-4)))
    if late:session=session.replace(hour=15,minute=50)
    item={'client_id':'ag-i-old','symbol':'ABC','snapshot':{'filled_at':(now-timedelta(minutes=1 if late else 46)).isoformat()}}
    engine.ledger.active_intents.return_value=[item];engine.time_exit=Mock()
    result=engine.intraday_step({'cash':'200'},[{'symbol':'ABC'}],session)
    assert result['status']=='intraday_time_exit_submitted'
    engine.time_exit.assert_called_once_with(item)


def test_missing_authorization_cannot_start(tmp_path):
    (tmp_path/'config').mkdir();(tmp_path/'config/intraday_execution.json').write_text('{"mode":"live"}')
    with pytest.raises(StateError):authorized(tmp_path)
