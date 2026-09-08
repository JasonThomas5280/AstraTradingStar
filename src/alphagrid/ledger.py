"""Durable order intents, append-only events, and account reconciliation.

Only explicit flat-account initialization creates a cash baseline. Broker fills
reconstruct expected cash/positions; external trades, deposits, fees or dividends
that cannot be explained by the ledger stop entries for operator reconciliation.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import sqlite3

from .risk.numbers import number


class StateError(RuntimeError):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise StateError("timestamp_without_timezone")
    return result.astimezone(timezone.utc)


def canonical_order(order):
    """Persist only fields used in reconciliation, never arbitrary broker text."""
    if not isinstance(order, dict) or not order.get("id"):
        raise StateError("invalid_order")
    fields = ("id", "client_order_id", "symbol", "side", "type", "status", "qty",
              "filled_qty", "filled_avg_price", "limit_price", "stop_price", "filled_at")
    result = {key: order.get(key) for key in fields}
    result["legs"] = [canonical_order(leg) for leg in order.get("legs") or []]
    return result


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS intents(
                    client_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
                    payload TEXT NOT NULL, thesis TEXT NOT NULL,
                    created_at TEXT NOT NULL, snapshot TEXT);
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS exits(
                    parent_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
                    created_at TEXT NOT NULL, snapshot TEXT);
                CREATE TRIGGER IF NOT EXISTS immutable_events_update
                    BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'append-only'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_events_delete
                    BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connect() as db:
            self._set(db, key, value)

    @staticmethod
    def _set(db, key, value):
        db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))

    @staticmethod
    def _event(db, kind, data):
        db.execute("INSERT INTO events(at,kind,data) VALUES(?,?,?)",
                   (utcnow().isoformat(), kind, json.dumps(data)))

    def event(self, kind, data):
        with self.connect() as db:
            self._event(db, kind, data)

    def initialize(self, account, positions, orders, session_date):
        if positions != [] or orders != []:
            raise StateError("initialization_requires_flat_account")
        equity = number(account["equity"], positive=True)
        cash = number(account["cash"])
        if not account.get("id") or abs(equity - cash) > D("0.01"):
            raise StateError("invalid_flat_baseline")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM meta WHERE key='initialized'").fetchone():
                raise StateError("already_initialized")
            values = {"initialized": True, "account_hash": hashlib.sha256(account["id"].encode()).hexdigest(),
                      "baseline_cash": str(cash), "peak_equity": str(equity),
                      "session_date": session_date, "start_equity": str(equity),
                      "worst_daily_loss": "0", "halted": False}
            for key, value in values.items():
                self._set(db, key, value)
            self._event(db, "initialized_flat", {"equity": str(equity), "session": session_date})

    def halt(self, code):
        # Call sites supply fixed reason codes, never raw exception messages.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._set(db, "halted", True)
            self._set(db, "halt_code", code)
            self._set(db, "halted_at", utcnow().isoformat())
            self._event(db, "halt", {"code": code})

    def reserve(self, payload, thesis):
        """An intent is committed BEFORE POST. An uncertain intent is never retried."""
        client_id = payload["client_order_id"]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM meta WHERE key='halted'").fetchone()
            if row is None or json.loads(row[0]) is not False:
                raise StateError("halted")
            if db.execute("SELECT 1 FROM intents WHERE client_id=?", (client_id,)).fetchone():
                return False
            db.execute("INSERT INTO intents VALUES(?,?,?,?,?,NULL)",
                       (client_id, payload["symbol"], json.dumps(payload), thesis, utcnow().isoformat()))
            self._event(db, "reserved", {"client_id": client_id, "payload": payload, "thesis": thesis})
        return True

    def record(self, client_id, order):
        order = canonical_order(order)
        with self.connect() as db:
            row = db.execute("SELECT payload,snapshot FROM intents WHERE client_id=?", (client_id,)).fetchone()
            if row is None:
                raise StateError("unknown_intent")
            payload = json.loads(row[0])
            if order["client_order_id"] != client_id or order["symbol"] != payload["symbol"] or order["side"] != "buy":
                raise StateError("order_identity_mismatch")
            if number(order["qty"], positive=True) != number(payload["qty"], positive=True):
                raise StateError("order_quantity_mismatch")
            if row[1] and number(json.loads(row[1])["filled_qty"]) > number(order["filled_qty"]):
                raise StateError("fill_regression")
            old_sold = sum(number(leg["filled_qty"]) for leg in json.loads(row[1])["legs"]) if row[1] else D(0)
            sold = sum(number(leg["filled_qty"]) for leg in order["legs"])
            if sold < old_sold:
                raise StateError("exit_fill_regression")
            if sold > old_sold:
                self._set(db, "last_exit_at", utcnow().isoformat())
            encoded = json.dumps(order, sort_keys=True)
            if row[1] != encoded:
                db.execute("UPDATE intents SET snapshot=? WHERE client_id=?", (encoded, client_id))
                self._event(db, "broker_update", {"client_id": client_id, "order": order})

    def intents(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM intents ORDER BY created_at,client_id").fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"]),
                 "snapshot": json.loads(row["snapshot"]) if row["snapshot"] else None} for row in rows]

    def reconcile(self, account, positions, orders):
        if self.get("initialized") is not True:
            raise StateError("uninitialized")
        if hashlib.sha256(str(account.get("id", "")).encode()).hexdigest() != self.get("account_hash"):
            raise StateError("wrong_account")
        expected, cash, known = {}, number(self.get("baseline_cash")), set()
        for item in self.intents():
            order = item["snapshot"]
            if order is None:
                raise StateError("ambiguous_submission")
            known.add(order["id"])
            symbol = item["symbol"]
            bought = number(order["filled_qty"])
            if bought > number(order["qty"], positive=True):
                raise StateError("overfill")
            cash -= bought * (number(order["filled_avg_price"], positive=True) if bought else 0)
            remaining = bought
            for leg in order["legs"]:
                if leg["side"] != "sell" or leg["symbol"] != symbol:
                    raise StateError("invalid_exit_leg")
                known.add(leg["id"])
                sold = number(leg["filled_qty"])
                remaining -= sold
                cash += sold * (number(leg["filled_avg_price"], positive=True) if sold else 0)
            if remaining < 0:
                raise StateError("exit_overfill")
            if remaining:
                expected[symbol] = expected.get(symbol, D(0)) + remaining
        for exit_item in self.exits():
            order = exit_item["snapshot"]
            if order is None:
                raise StateError("ambiguous_close")
            known.add(order["id"])
            sold = number(order["filled_qty"])
            symbol = exit_item["symbol"]
            expected[symbol] = expected.get(symbol, D(0)) - sold
            cash += sold * (number(order["filled_avg_price"], positive=True) if sold else 0)
            if expected[symbol] < 0:
                raise StateError("exit_overfill")
            if expected[symbol] == 0:
                del expected[symbol]
        observed = {}
        for position in positions:
            if position.get("side") != "long" or position.get("asset_class") != "us_equity":
                raise StateError("unsupported_position")
            symbol = position["symbol"]
            if symbol in observed:
                raise StateError("duplicate_position")
            observed[symbol] = number(position["qty"], positive=True)
        if observed != expected:
            raise StateError("position_mismatch")
        if abs(number(account["cash"]) - cash) > D("0.02"):
            raise StateError("cash_mismatch")
        def check_order(order):
            if order.get("id") not in known:
                raise StateError("external_order")
            for leg in order.get("legs") or []:
                check_order(leg)
        for order in orders:
            check_order(order)
        self.set("last_reconciled", utcnow().isoformat())
        return expected

    def active_intents(self):
        closed = {e["parent_id"]: number(e["snapshot"]["filled_qty"]) for e in self.exits() if e["snapshot"]}
        result = []
        for item in self.intents():
            order = item["snapshot"]
            if order is None or order["status"] not in {"filled", "canceled", "expired", "rejected", "replaced"}:
                result.append(item)
            elif number(order["filled_qty"]) > sum(number(leg["filled_qty"]) for leg in order["legs"]) + closed.get(item["client_id"], D(0)):
                result.append(item)
        return result

    def exits(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM exits ORDER BY created_at").fetchall()
        return [{**dict(row), "snapshot": json.loads(row["snapshot"]) if row["snapshot"] else None} for row in rows]

    def reserve_close(self, parent_id, symbol):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM exits WHERE parent_id=?", (parent_id,)).fetchone():
                return False
            if not db.execute("SELECT 1 FROM intents WHERE client_id=? AND symbol=?", (parent_id, symbol)).fetchone():
                raise StateError("unknown_close_parent")
            db.execute("INSERT INTO exits VALUES(?,?,?,NULL)", (parent_id, symbol, utcnow().isoformat()))
            self._event(db, "close_reserved", {"parent_id": parent_id, "symbol": symbol})
        return True

    def record_close(self, parent_id, order):
        order = canonical_order(order)
        with self.connect() as db:
            row = db.execute("SELECT symbol,snapshot FROM exits WHERE parent_id=?", (parent_id,)).fetchone()
            if not row or order["symbol"] != row[0] or order["side"] != "sell":
                raise StateError("close_identity_mismatch")
            old = json.loads(row[1]) if row[1] else None
            if old and (old["id"] != order["id"] or number(old["filled_qty"]) > number(order["filled_qty"])):
                raise StateError("close_regression")
            if number(order["filled_qty"]) > (number(old["filled_qty"]) if old else 0):
                self._set(db, "last_exit_at", utcnow().isoformat())
            encoded = json.dumps(order, sort_keys=True)
            if row[1] != encoded:
                db.execute("UPDATE exits SET snapshot=? WHERE parent_id=?", (encoded, parent_id))
                self._event(db, "close_update", {"parent_id": parent_id, "order": order})

    def mark_equity(self, account, session_date):
        equity = number(account["equity"], positive=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            values = {row[0]: json.loads(row[1]) for row in db.execute("SELECT key,value FROM meta")}
            if values.get("initialized") is not True:
                raise StateError("uninitialized")
            start = number(values["start_equity"], positive=True)
            worst = number(values["worst_daily_loss"])
            if values["session_date"] != session_date:
                start = number(account["last_equity"], positive=True)
                worst = D(0)
            worst = max(worst, D(0), (start - equity) / start)
            peak = max(number(values["peak_equity"], positive=True), equity, start)
            for key, value in {"session_date": session_date, "start_equity": str(start),
                               "worst_daily_loss": str(worst), "peak_equity": str(peak),
                               "last_equity": str(equity)}.items():
                self._set(db, key, value)
        return equity, start, peak, worst
