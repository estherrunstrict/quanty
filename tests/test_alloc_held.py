"""The allocation panel's Budget column is not the bot's holdings.

`current_krw` is the APPLIED BUDGET — the ceiling the allocator wrote to
config.yaml. The panel's caption used to read "what each bot actually holds is
the Current column", which was the opposite of the truth: on 2026-08-21 the
KIS-US bots each showed a W36.9M budget while holding about W19M, and a bot
picks up a newly applied budget only on its NEXT run, so its own card can show
the previous number for a whole day.

Both figures are legitimate. Showing one under the other's name was not, so the
panel now carries both and `allocation_held_krw` supplies the second.

Run: python3 -m pytest tests/test_alloc_held.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_dashboard_data as G  # noqa: E402

FX = 1393.0


def test_usd_is_converted_and_krw_is_not():
    held = G.allocation_held_krw([
        {"id": "quant40", "currency": "USD", "value": 13_795.38},
        {"id": "nmf2", "currency": "KRW", "value": 921_906.0},
    ], FX)
    assert held["quant40"] == round(13_795.38 * FX, 2)
    assert held["nmf2"] == 921_906.0, "a KRW card must not be multiplied by FX"


def test_multi_card_is_unpacked_into_its_two_allocator_rows():
    """Hybrid VB is ONE card with two legs but TWO rows in the allocator."""
    held = G.allocation_held_krw([{
        "id": "hybrid_vb", "currency": "MULTI",
        "kr": {"value": 14_901_645.0},
        "us": {"value": 4_637.58},
    }], FX)
    assert held["hybrid_vb_kr"] == 14_901_645.0
    assert held["hybrid_vb_us"] == round(4_637.58 * FX, 2)
    assert "hybrid_vb" not in held, "the combined card is not an allocator row"


def test_hands_on_sleeve_is_not_a_bot():
    assert G.allocation_held_krw(
        [{"id": "manual", "currency": "KRW", "value": 174_000_000}], FX) == {}


def test_survives_missing_and_malformed_cards():
    """A publish must not fail over a card that dropped a field."""
    held = G.allocation_held_krw(
        [None, "junk", {"id": "x"}, {"id": "y", "currency": "USD", "value": None}], FX)
    assert held == {"x": 0.0, "y": 0.0}
    assert G.allocation_held_krw(None, FX) == {}


def test_a_bot_with_no_card_gets_no_held_figure():
    """held_krw must be None, not 0 — 'unknown' and 'flat' are different claims."""
    held = G.allocation_held_krw([{"id": "quant40", "currency": "USD", "value": 1.0}], FX)
    assert held.get("event_bot") is None
