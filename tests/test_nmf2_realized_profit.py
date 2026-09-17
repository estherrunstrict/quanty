"""Realized P/L is based on attributed fills, independent of budget resets."""
import copy
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import generate_dashboard_data as G
from nmf2_accounting import attach_realized, replay_realized


def order(oid, side, qty, price, fee=0, day='2026-09-01'):
    return {'orderId': oid, 'symbol': '005930', 'side': side,
            'orderedAt': day + 'T09:05:00',
            'execution': {'filledQuantity': qty, 'averageFilledPrice': price,
                          'commission': fee, 'tax': 0}}


def sample():
    led = {'created': '2026-07-27', 'budget_krw': 10000, 'cash_krw': 9476,
           'positions': {'005930': {'qty': 6, 'avg_price': 100}},
           'attempted': {'nmf2-buy-B': 'buy', 'nmf2-sale-S': 'sale'},
           'synced_qty': {'nmf2-buy-B': 10, 'nmf2-sale-S': 4}}
    orders = [order('buy', 'BUY', 10, 100, 10), order('sale', 'SELL', 4, 120, 4)]
    return led, orders


def card(led, orders, qty=6):
    report = replay_realized(led, orders)['2026']
    enriched = dict(led, dashboard_realized=dict(report, source='attributed_broker_fills', year='2026'))
    marks = [{'symbol': '005930', 'qty': qty, 'currency': 'KRW', 'value_native': qty * 110}] if qty else []
    return G.build_nmf2_card(enriched, marks)


def test_actual_sales_and_fees_flow_to_card():
    led, orders = sample()
    before = copy.deepcopy(led)
    result = card(led, orders)
    assert result['realized_profit_ytd'] == 66
    assert result['realized_trades'] == 1
    assert result['unrealized_profit'] == 60
    assert result['total_pl_ytd'] == 126
    assert result['profit_rate_ytd_pct'] == 1.26
    assert result['value'] == 660
    assert led == before


@pytest.mark.parametrize('cash,budget', [(0, 1), (999999, 10000), (200, 999999)])
def test_arbitrary_cash_and_budget_resets_do_not_change_profit(cash, budget):
    led, orders = sample()
    led.update(cash_krw=cash, budget_krw=budget)
    assert card(led, orders)['realized_profit_ytd'] == 66


@pytest.mark.parametrize('qty', [0, 3])
def test_stale_marks_do_not_change_realized(qty):
    led, orders = sample()
    assert card(led, orders, qty)['realized_profit_ytd'] == 66


def test_external_sale_keeps_liquidated_card_visible():
    led, orders = sample()
    led['external_fills'] = [{'at': '2026-09-17', 'symbol': '005930', 'qty': 6, 'price': 80, 'fee': 6}]
    led['positions'] = {}
    result = card(led, orders, 0)
    assert result['value'] == 0
    assert result['total_pl_ytd'] == result['realized_profit_ytd'] == -60
    assert result['realized_trades'] == 2


def test_manual_orders_and_duplicate_api_rows_are_not_counted():
    led, orders = sample()
    assert replay_realized(led, orders + orders + [order('manual', 'SELL', 900, 1000)])['2026']['realized_profit_ytd'] == 66


def test_prior_year_buy_retains_basis_but_not_prior_year_fees():
    led, orders = sample()
    orders[0]['orderedAt'] = '2025-12-01'
    report = replay_realized(led, orders)
    assert report['2025']['realized_profit_ytd'] == -10
    assert report['2026']['realized_profit_ytd'] == 76


@pytest.mark.parametrize('problem', ['missing', 'quantity', 'position'])
def test_incomplete_history_is_rejected(problem):
    led, orders = sample()
    if problem == 'missing':
        orders.pop()
    elif problem == 'quantity':
        orders[1]['execution']['filledQuantity'] = 3
    else:
        led['positions']['005930']['qty'] = 7
    with pytest.raises(ValueError):
        replay_realized(led, orders)


def test_cache_is_keyed_to_fills_and_queries_have_explicit_windows(tmp_path):
    led, orders = sample()
    class Client:
        calls = []
        def list_orders_all(self, **kw):
            self.calls.append(kw)
            return orders
    client = Client()
    cache = tmp_path / 'report.json'
    result = attach_realized(led, '/unused', cache, client, date(2026, 9, 17))
    assert result['dashboard_realized']['realized_profit_ytd'] == 66
    assert client.calls == [{'from_date': '2026-07-27', 'to_date': '2026-08-25'},
                            {'from_date': '2026-08-26', 'to_date': '2026-09-17'}]
    led.update(cash_krw=0, budget_krw=1)
    attach_realized(led, '/unused', cache, client, date(2026, 9, 17))
    assert len(client.calls) == 2
    led['positions']['005930']['qty'] = 7
    failed = attach_realized(led, '/unused', cache, client, date(2026, 9, 17))
    assert failed['dashboard_realized']['source'] == 'unavailable'
    assert 'realized_profit_ytd' not in failed['dashboard_realized']


def test_missing_report_is_unknown_not_zero_or_cash_residual():
    led, _ = sample()
    assert G.build_nmf2_card(led, [])['realized_profit_ytd'] is None
    assert G.build_nmf2_card({}, []) is None
