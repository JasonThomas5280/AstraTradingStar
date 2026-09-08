from dataclasses import dataclass
from decimal import Decimal
from .numbers import number


@dataclass(frozen=True)
class Stop:
    side: str
    price: Decimal

    def __post_init__(self):
        if self.side not in ("long", "short"):
            raise ValueError("invalid side")
        object.__setattr__(self, "price", number(self.price, positive=True))

    def tighten(self, price):
        price = number(price, positive=True)
        if (self.side == "long" and price < self.price) or (
                self.side == "short" and price > self.price):
            raise ValueError("stop widening is forbidden")
        return Stop(self.side, price)

    def triggered(self, market):
        market = number(market, positive=True)
        return market <= self.price if self.side == "long" else market >= self.price
