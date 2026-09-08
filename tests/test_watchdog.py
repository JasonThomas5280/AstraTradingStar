from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from alphagrid import service


@pytest.fixture
def rig(tmp_path, monkeypatch):
    state = {'halted': False, 'initialized': True}
    ledger = Mock()
    ledger.get.side_effect = lambda key, default=None: state.get(key, default)
    ledger.set.side_effect = lambda key, value: state.__setitem__(key, value)
    ledger.active_intents.return_value = []
    ledger.exits.return_value = []
    engine = Mock()
    engine.ledger = ledger
    engine.halt.side_effect = lambda code: state.__setitem__('halted', True)
    engine.broker.clock.return_value = {'timestamp': service.utcnow().isoformat(), 'is_open': True}
    monkeypatch.setattr(service, 'Engine', lambda root: engine)
    monkeypatch.setattr(service, 'health', lambda account: None)
    monkeypatch.setattr(service, 'breach_reason', lambda *marked: None)
    ledger.mark_equity.return_value = (100, 100, 100, 0)
    breaker = Mock()
    breaker.trip.return_value = True
    monkeypatch.setattr(service, 'CircuitBreaker', lambda path: breaker)
    locks = []
    @contextmanager
    def lock(path, timeout=0):
        locks.append((path.name, timeout))
        yield
    monkeypatch.setattr(service, 'process_lock', lock)
    return tmp_path, engine, breaker, state, locks


def intent(engine, filled='10', stops=True):
    engine.ledger.active_intents.return_value = [{'client_id': 'abc', 'symbol': 'SPY',
        'created_at': (service.utcnow()-timedelta(seconds=20)).isoformat()}]
    engine.broker.order.return_value = {'qty': '10', 'filled_qty': filled,
        'legs': [{'symbol': 'SPY', 'side': 'sell', 'type': 'stop', 'status': 'new',
                  'qty': '10', 'filled_qty': '0'}] if stops else []}


def test_latched_halt_does_not_need_account_endpoint(rig):
    root, engine, breaker, state, locks = rig
    state['halted'] = True
    engine.broker.account.side_effect = RuntimeError('offline')
    service.watchdog(root, once=True)
    engine.broker.account.assert_not_called()
    breaker.trip.assert_called_once()
    assert ('execution.lock', 20) in locks
    assert state['watchdog_flat_verified'] is True


def test_account_failure_flattens_same_cycle(rig):
    root, engine, breaker, state, _ = rig
    engine.broker.account.side_effect = RuntimeError('offline')
    service.watchdog(root, once=True)
    assert state['halted']
    breaker.trip.assert_called_once()


@pytest.mark.parametrize('filled,stops', [('5', True), ('10', False)])
def test_unprotected_fill_flattens_same_cycle(rig, filled, stops):
    root, engine, breaker, state, _ = rig
    intent(engine, filled, stops)
    service.watchdog(root, once=True)
    assert state['halted']
    breaker.trip.assert_called_once()


def test_protected_fill_renews_readiness(rig):
    root, engine, breaker, state, _ = rig
    intent(engine)
    service.watchdog(root, once=True)
    breaker.trip.assert_not_called()
    assert state['watchdog_heartbeat']
    assert engine.broker.order.call_args.kwargs['timeout'] <= 2


def test_unknown_old_intent_halts(rig):
    root, engine, breaker, state, _ = rig
    intent(engine)
    engine.broker.order.return_value = None
    service.watchdog(root, once=True)
    assert state['halted']
    breaker.trip.assert_called_once()


def test_sweep_deadline_halts(rig, monkeypatch):
    root, engine, breaker, state, _ = rig
    intent(engine)
    ticks = iter([0, 0, 9])
    monkeypatch.setattr(service.time, 'monotonic', lambda: next(ticks))
    service.watchdog(root, once=True)
    assert state['halted']
    breaker.trip.assert_called_once()


def test_flat_watchdog_keeps_monitoring(rig, monkeypatch):
    root, engine, breaker, state, _ = rig
    state['halted'] = True
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt
    monkeypatch.setattr(service.time, 'sleep', sleep)
    with pytest.raises(KeyboardInterrupt):
        service.watchdog(root)
    assert breaker.trip.call_count == 2


def test_supervisor_preserves_guard_after_trader_exit(rig, monkeypatch):
    root, engine, breaker, state, _ = rig
    monkeypatch.setattr(service, 'config', lambda root: {'trading_enabled': True})
    monkeypatch.setattr(service, 'validate_evidence', lambda *args: [])
    guard, trader = Mock(), Mock()
    guard.poll.return_value = None
    trader.poll.return_value = 1
    def launch(args, **kw):
        if args[-1] == 'watchdog':
            state['watchdog_heartbeat'] = service.utcnow().isoformat()
            return guard
        return trader
    monkeypatch.setattr(service.subprocess, 'Popen', launch)
    assert service.supervise(root) == 1
    guard.terminate.assert_not_called()
    guard.kill.assert_not_called()
    assert state['halted']

@pytest.mark.parametrize('age,snapshot,should_halt', [
    (5, None, False),
    (31, None, True),
    (5, {'side': 'sell', 'symbol': 'SPY', 'filled_qty': '0', 'status': 'new'}, False),
    (31, {'side': 'sell', 'symbol': 'SPY', 'filled_qty': '0', 'status': 'new'}, True),
    (31, {'side': 'sell', 'symbol': 'SPY', 'filled_qty': '10', 'status': 'filled'}, False),
])
def test_time_exit_grace_and_completion(rig, age, snapshot, should_halt):
    root, engine, breaker, state, _ = rig
    intent(engine, stops=False)
    engine.ledger.exits.return_value = [{'parent_id': 'abc', 'symbol': 'SPY',
        'created_at': (service.utcnow()-timedelta(seconds=age)).isoformat(), 'snapshot': snapshot}]
    service.watchdog(root, once=True)
    assert state['halted'] is should_halt
    assert breaker.trip.called is should_halt


def test_supervisor_startup_exception_latches_halt(rig, monkeypatch):
    root, engine, breaker, state, _ = rig
    monkeypatch.setattr(service, 'config', lambda root: {'trading_enabled': True})
    monkeypatch.setattr(service, 'validate_evidence', lambda *args: [])
    guard = Mock()
    guard.poll.return_value = 1
    monkeypatch.setattr(service.subprocess, 'Popen', lambda *a, **kw: guard)
    assert service.supervise(root) == 1
    assert state['halted']
    assert state['watchdog_heartbeat'] is None


@pytest.mark.parametrize('age,expected_halt', [(5,False),(16,True)])
def test_partial_fill_has_bounded_settlement_window(rig,age,expected_halt):
    root,engine,breaker,state,_=rig
    intent(engine,filled='1',stops=False)
    engine.ledger.active_intents.return_value[0]['created_at']=(service.utcnow()-timedelta(seconds=age)).isoformat()
    service.watchdog(root,once=True)
    assert state['halted'] is expected_halt
