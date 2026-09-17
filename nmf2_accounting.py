"""Read-only NMF2 realized P/L from attributed fills, never allocation balances."""
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def replay_realized(ledger, orders):
    """Replay synced orders and explicitly attributed external sales by year.

    Like the bot ledger, cost excludes fees; fees are expensed when paid.
    Reject incomplete history rather than infer profit from cash/budget resets.
    """
    rows = {str(r["orderId"]): r for r in orders}
    events = []
    for coid, qty in (ledger.get("synced_qty") or {}).items():
        if not coid.startswith("nmf2-") or float(qty) <= 0:
            continue
        row = rows.get(str((ledger.get("attempted") or {}).get(coid)))
        if row is None:
            raise ValueError("Missing filled order: " + coid)
        ex = row.get("execution") or {}
        filled = float(ex.get("filledQuantity") or 0)
        price = float(ex.get("averageFilledPrice") or 0)
        if filled != float(qty) or price <= 0:
            raise ValueError("Broker/ledger fill mismatch: " + coid)
        # Missing fees use the same estimates as the bot; explicit zero is real.
        fee = (float(ex.get("commission") or 0) + float(ex.get("tax") or 0)
               if ex.get("commission") is not None and ex.get("tax") is not None
               else filled * price * (0.00015 if row["side"] == "BUY" else 0.002))
        events.append((ex.get("filledAt") or row["orderedAt"], row["symbol"],
                       row["side"], filled, price, fee))
    for fill in ledger.get("external_fills") or []:
        events.append((fill["at"], fill["symbol"], "SELL", float(fill["qty"]),
                       float(fill["price"]), float(fill["fee"])))
    book, years = {}, {}
    for stamp, symbol, side, qty, price, fee in sorted(events):
        year = stamp[:4]
        result = years.setdefault(year, {"realized_profit_ytd": 0.0, "realized_trades": 0})
        owned, cost = book.get(symbol, (0.0, 0.0))
        result["realized_profit_ytd"] -= fee
        if side == "BUY":
            book[symbol] = (owned + qty, cost + qty * price)
        elif side == "SELL" and 0 < qty <= owned:
            basis = cost * qty / owned
            result["realized_profit_ytd"] += qty * price - basis
            result["realized_trades"] += 1
            book[symbol] = (owned - qty, cost - basis)
        else:
            raise ValueError("Invalid sale or side for " + symbol)
    expected = ledger.get("positions") or {}
    for symbol in set(book) | set(expected):
        if book.get(symbol, (0, 0))[0] != float(expected.get(symbol, {}).get("qty") or 0):
            raise ValueError("Replayed position mismatch: " + symbol)
    for result in years.values():
        result["realized_profit_ytd"] = round(result["realized_profit_ytd"], 2)
    return years


def attach_realized(ledger, ledger_path, cache_path, client=None, today=None):
    """Attach report to a copy; never write to bot state or place orders.

    Cache only verified results, keyed to fills and positions. Budget/cash resets
    cannot change the result. Explicit <=30-day queries avoid broker defaults
    silently omitting the more recent trades.
    """
    if not ledger:
        return ledger
    today = today or datetime.now(ZoneInfo("Asia/Seoul")).date()
    year = str(today.year)
    key = hashlib.sha256(json.dumps({k: ledger.get(k) for k in
        ("created", "positions", "attempted", "synced_qty", "external_fills")},
        sort_keys=True).encode()).hexdigest()
    try:
        try:
            cached = json.loads(Path(cache_path).read_text())
        except (OSError, ValueError):
            cached = {}
        if cached.get("key") == key:
            years = cached["years"]
        else:
            if client is None:
                sys.path.insert(0, str(Path(ledger_path).parents[2]))
                from toss_nmf2_bot.broker import OrderClient
                client = OrderClient(account_seq="1")
            start = date.fromisoformat(ledger["created"][:10])
            orders = []
            while start <= today:
                end = min(today, start + timedelta(days=29))
                orders.extend(client.list_orders_all(from_date=start.isoformat(), to_date=end.isoformat()))
                start = end + timedelta(days=1)
            years = replay_realized(ledger, orders)
            target = Path(cache_path)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps({"key": key, "years": years}))
            os.replace(tmp, target)
        report = dict(years.get(year, {"realized_profit_ytd": 0.0, "realized_trades": 0}),
                      source="attributed_broker_fills", year=year)
    except Exception as exc:
        print("  NMF2 realized P/L unavailable:", type(exc).__name__, str(exc)[:160])
        report = {"source": "unavailable", "year": year,
                  "error": "Unable to verify complete NMF2 fill history."}
    return dict(ledger, dashboard_realized=report)
