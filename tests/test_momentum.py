from decimal import Decimal as D
import pytest
from alphagrid.strategies.momentum import plan

def test_whole_share_allocation_preserves_cash_and_position_limits():
    ranking=[(f'S{i}',15-i) for i in range(15)]
    prices={s:str(15+i*11) for i,(s,_) in enumerate(ranking)}
    orders=plan(ranking,prices,'2499.99','2499.99')
    spent=sum(D(o['qty'])*D(o['limit_price']) for o in orders)
    assert D('150') <= D('2499.99')-spent <= D('250')
    assert len(orders)<=12
    for o in orders:
        assert D(o['qty'])==int(o['qty'])
        assert D(o['qty'])*D(o['limit_price'])<=D('2499.99')*D('.14')
        assert D(o['qty'])*(D(o['limit_price'])-D(o['stop_price']))<=D('2499.99')*D('.0075')

def test_never_add_to_existing_or_exceed_cash():
    assert plan([('A',1)],{'A':'100'},'2500','200',('A',))==[]
    assert plan([('A',1)],{'A':'100'},'2500','100')==[]
    assert plan([('A',1)],{'A':'400'},'2500','2500')==[]

@pytest.mark.parametrize('price',['NaN','Infinity','0','-1'])
def test_invalid_quotes_fail_closed(price):
    with pytest.raises(ValueError):
        plan([('A',1)],{'A':price},'2500','2500')

def test_full_portfolio_does_not_open_thirteenth_name():
    assert plan([('Z',1)],{'Z':'10'},'2500','1000',tuple(str(i) for i in range(12)))==[]

def test_execution_rechecks_limits_and_existing_position():
    from alphagrid.paper_experiment import entry_checks
    from alphagrid.ledger import StateError
    order={'symbol':'A','qty':'2','limit_price':'100','stop_price':'95'}
    account={'equity':'2500','cash':'2500'}
    entry_checks(order,account,[])
    for changed in ({'qty':'4'},{'stop_price':'80'},{'qty':'0.5'},{'limit_price':'NaN'}):
        with pytest.raises((StateError,ArithmeticError)):
            entry_checks(dict(order,**changed),account,[])
    with pytest.raises(StateError):
        entry_checks(order,account,[{'symbol':'A'}])
    with pytest.raises(StateError):
        entry_checks(order,dict(account,cash='200'),[])

def test_experiment_records_before_post_and_never_retries(tmp_path,monkeypatch):
    from alphagrid.paper_experiment import MomentumEngine
    from alphagrid.ledger import StateError,utcnow
    e=MomentumEngine(tmp_path)
    e.ledger.set('halted',False)
    e.ledger.set('watchdog_heartbeat',utcnow().isoformat())
    monkeypatch.setattr(e,'settings',lambda: {})
    # Existing protective orders must not be mistaken for pending entries.
    monkeypatch.setattr(e,'refresh',lambda: ({'equity':'2500','cash':'2000'},[{'symbol':'B'}],[{'id':'protective'}],{'is_open':True},None))
    monkeypatch.setattr(e.broker,'asset',lambda s: {'tradable':True,'status':'active','exchange':'ARCA'})
    monkeypatch.setattr(e.broker,'quote',lambda s: {'t':utcnow().isoformat(),'bp':'99.98','ap':'100'})
    calls=[]
    def post(payload):
        calls.append(payload)
        assert e.ledger.intents()[0]['snapshot'] is None
        raise TimeoutError()
    monkeypatch.setattr(e.broker,'submit_bracket',post)
    p={'symbol':'A','qty':'2','limit_price':'100','stop_price':'95','target_price':'110'}
    with pytest.raises(StateError,match='submission_requires_reconciliation'):
        e.submit_experiment(p,'2026-09-08')
    assert len(calls)==1
    assert e.ledger.get('halted') is True

def test_held_stop_requires_fully_filled_parent_and_active_profit_leg():
    from alphagrid.service import protective_stops
    stop={'type':'stop','status':'held','side':'sell'}
    profit={'type':'limit','status':'new','side':'sell'}
    order={'status':'filled','qty':'2','filled_qty':'2','legs':[stop,profit]}
    assert protective_stops(order)==[stop]
    assert protective_stops(dict(order,filled_qty='1'))==[]
    assert protective_stops(dict(order,status='pending_new',filled_qty='0'))==[]
    assert protective_stops(dict(order,legs=[stop,dict(profit,status='held')]))==[]
    assert protective_stops(dict(order,legs=[dict(stop,status='canceled'),profit]))==[]
