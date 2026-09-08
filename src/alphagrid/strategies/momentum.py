"""Frozen whole-share paper allocation experiment; no claim of optimality."""
from decimal import Decimal as D, ROUND_DOWN, ROUND_UP
from .trend_pullback import validate_bars
from ..risk.numbers import number


def rank(histories):
    result = []
    for symbol, bars in histories.items():
        validate_bars(bars)
        if len(bars) < 121:
            continue
        adv = sum(b.close*b.volume for b in bars[-20:])/20
        if adv < 50_000_000 or bars[-1].close < 5:
            continue
        score = .5*(bars[-1].close/bars[-61].close-1) + .5*(bars[-1].close/bars[-121].close-1)
        result.append((symbol, score))
    return sorted(result, key=lambda row: (-row[1], row[0]))


def plan(ranked, prices, equity, cash, held=(), cash_target='150'):
    """Budget in limit-price dollars. No leverage, additions or fractional orders."""
    equity, cash = number(equity, positive=True), number(cash)
    target = number(cash_target)
    if cash < 0 or target < 0 or len(set(held)) != len(held) or len(held) > 12:
        raise ValueError('invalid portfolio')
    budget = max(D(0), min(cash-target, equity))
    selected = []
    for symbol, score in ranked:
        if symbol in held or symbol not in prices or symbol in selected:
            continue
        price = number(prices[symbol], positive=True)
        if price < 5 or price > equity*D('.14'):
            continue
        selected.append(symbol)
        if len(selected) >= 12-len(held):
            break
    if len(held) == 12 or not selected:
        return []
    quantities = {s: 0 for s in selected}
    limits = {s: number(prices[s], positive=True).quantize(D('.01'), rounding=ROUND_UP) for s in selected}
    # First spread capital, then use whole-share residuals in momentum order.
    equal = budget/len(selected)
    for s in selected:
        quantities[s] = int(min(equal, equity*D('.14'))/limits[s])
    spent = sum(limits[s]*quantities[s] for s in selected)
    while True:
        changed = False
        for s in selected:
            if spent+limits[s] <= budget and (quantities[s]+1)*limits[s] <= equity*D('.14'):
                quantities[s] += 1
                spent += limits[s]
                changed = True
        if not changed:
            break
    output = []
    for s in selected:
        qty, entry = quantities[s], limits[s]
        if not qty:
            continue
        stop = (entry*D('.95')).quantize(D('.01'), rounding=ROUND_DOWN)
        if qty*(entry-stop) > equity*D('.0075'):
            raise ValueError('position_stop_risk')
        output.append({'symbol':s, 'qty':str(qty), 'limit_price':str(entry),
                       'stop_price':str(stop), 'target_price':str(entry+2*(entry-stop))})
    assert sum(D(p['qty'])*D(p['limit_price']) for p in output) <= budget
    return output
