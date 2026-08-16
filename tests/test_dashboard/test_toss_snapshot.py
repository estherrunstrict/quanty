"""Tests for the read-only Toss account snapshot (Task 4 of the whole-investment
dashboard plan).

Two halves:
  * scripts/toss_snapshot.py     — the Mac-side builder/validator/writer
  * generate_dashboard_data.load_toss_account — the server-side ingest, in its
    three states: fresh / stale / missing.

All fixtures are synthetic. Real position data never lands in the repo, but the
response SHAPES below are copied from a live 2026-08-16 read-only call, including
the quirk that `marketValue.amount.krw` and `.usd` are two separate currency
buckets rather than the same portfolio quoted twice.
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

import toss_snapshot as ts

KST = timezone(timedelta(hours=9))


# --------------------------------------------------------------------------- #
# locate the (non-git) quanty-dashboard scratch copy for the ingest half
# --------------------------------------------------------------------------- #
def _load_generator():
    """Import quanty-dashboard/generate_dashboard_data.py, or skip.

    quanty-dashboard is a sibling scratch directory, not part of this repo and
    not a git repo itself (deployed by scp). Walk up until we find it so the
    test works from a checkout, a worktree, or the server.
    """
    for parent in [REPO_ROOT] + list(REPO_ROOT.parents):
        cand = parent / "quanty-dashboard" / "generate_dashboard_data.py"
        if cand.exists():
            sys.path.insert(0, str(cand.parent))
            sys.path.insert(0, str(REPO_ROOT))       # dashboard_equity lives here
            spec = importlib.util.spec_from_file_location("generate_dashboard_data", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    pytest.skip("quanty-dashboard/generate_dashboard_data.py not found next to this repo")


@pytest.fixture(scope="module")
def gen():
    return _load_generator()


# --------------------------------------------------------------------------- #
# synthetic Toss API responses
# --------------------------------------------------------------------------- #
def _holding(symbol, name, currency, qty, amount, country):
    return {
        "symbol": symbol, "name": name, "marketCountry": country,
        "currency": currency, "quantity": str(qty), "lastPrice": "1",
        "averagePurchasePrice": "1",
        "marketValue": {"purchaseAmount": str(amount), "amount": str(amount),
                        "amountAfterCost": str(amount)},
        "profitLoss": {"amount": "0", "rate": "0"},
    }


def holdings_resp(items=None):
    items = [_holding("005930", "삼성전자", "KRW", 10, 700000, "KR"),
             _holding("NKE", "나이키", "USD", 2.5, 100.0, "US")] if items is None else items

    def _amt(i):                       # items may carry junk on purpose
        return ts._num(i.get("marketValue", {}).get("amount"))

    krw = sum(_amt(i) for i in items if i.get("currency") == "KRW")
    usd = sum(_amt(i) for i in items if i.get("currency") == "USD")
    return {"result": {
        # NOTE: separate buckets, not the same total in two currencies.
        "marketValue": {"amount": {"krw": str(krw), "usd": str(usd)}},
        "items": items,
    }}


def cash_resp(currency, amount):
    return {"result": {"currency": currency, "cashBuyingPower": str(amount)}}


AS_OF = "2026-08-16T21:00:00+09:00"


def build(**kw):
    kw.setdefault("holdings_resp", holdings_resp())
    kw.setdefault("cash_krw_resp", cash_resp("KRW", 5000))
    kw.setdefault("cash_usd_resp", cash_resp("USD", 10))
    kw.setdefault("account_seq", "1")
    kw.setdefault("as_of", AS_OF)
    kw.setdefault("fx_rate", 1400.0)
    kw.setdefault("fx_source", "explicit")
    return ts.build_snapshot(**kw)


# --------------------------------------------------------------------------- #
# build_snapshot
# --------------------------------------------------------------------------- #
def test_build_converts_usd_sleeve_and_sums_total():
    snap = build()
    assert snap["native"] == {"holdings_krw": 700000.0, "holdings_usd": 100.0,
                              "cash_krw": 5000.0, "cash_usd": 10.0}
    assert snap["holdings_krw"] == 700000 + 100 * 1400          # 840,000
    assert snap["cash_krw"] == 5000 + 10 * 1400                 # 19,000
    assert snap["total_krw"] == snap["holdings_krw"] + snap["cash_krw"]
    assert snap["account_seq"] == "1"
    assert snap["source"] == "mac-launchd"
    assert snap["fx_rate"] == 1400.0


def test_build_per_holding_values():
    by_symbol = {h["symbol"]: h for h in build()["holdings"]}
    assert by_symbol["005930"]["value_krw"] == 700000.0         # KRW passes through
    assert by_symbol["NKE"]["value_krw"] == 140000.0            # 100 USD * 1400
    assert by_symbol["NKE"]["qty"] == 2.5                       # fractional shares survive
    assert by_symbol["NKE"]["currency"] == "USD"
    assert by_symbol["NKE"]["value_native"] == 100.0


def test_build_krw_only_account_needs_no_fx():
    snap = build(holdings_resp=holdings_resp([_holding("005930", "삼성전자", "KRW", 10, 700000, "KR")]),
                 cash_usd_resp=cash_resp("USD", 0), fx_rate=None, fx_source="unavailable")
    assert snap["total_krw"] == 705000.0
    assert snap["fx_rate"] is None and snap["fx_source"] == "unused"


def test_build_refuses_to_drop_usd_when_fx_missing():
    """The dangerous silent failure: a KRW total that quietly omits US stocks."""
    with pytest.raises(ts.SnapshotError, match="no FX rate"):
        build(fx_rate=None, fx_source="unavailable")


def test_build_refuses_usd_cash_without_fx():
    with pytest.raises(ts.SnapshotError, match="no FX rate"):
        build(holdings_resp=holdings_resp([_holding("005930", "s", "KRW", 1, 1000, "KR")]),
              cash_usd_resp=cash_resp("USD", 21.2), fx_rate=None, fx_source="unavailable")


@pytest.mark.parametrize("resp, match", [
    ({"result": {"marketValue": {}}}, "no `items`"),
    ({"result": {"items": "nope"}}, "expected list"),
    ({"result": {"items": [{"currency": "KRW", "quantity": "1"}]}}, "without a symbol"),
    ({"result": {"items": [dict(_holding("X", "x", "KRW", 1, 1, "KR"), currency="JPY")]}},
     "unhandled holding currency"),
])
def test_build_rejects_malformed_holdings(resp, match):
    with pytest.raises(ts.SnapshotError, match=match):
        build(holdings_resp=resp)


def test_build_tolerates_empty_account():
    snap = build(holdings_resp=holdings_resp([]), cash_usd_resp=cash_resp("USD", 0))
    assert snap["holdings"] == [] and snap["holdings_krw"] == 0.0
    assert snap["total_krw"] == 5000.0


def test_build_tolerates_missing_and_junk_numbers():
    item = _holding("005930", "삼성전자", "KRW", 10, 700000, "KR")
    item["marketValue"]["amount"] = None
    item["quantity"] = ""
    snap = build(holdings_resp=holdings_resp([item]), cash_usd_resp=cash_resp("USD", 0))
    assert snap["holdings"][0]["value_krw"] == 0.0 and snap["holdings"][0]["qty"] == 0.0


def test_validate_catches_inconsistent_total():
    snap = build()
    snap["total_krw"] = snap["total_krw"] + 100
    with pytest.raises(ts.SnapshotError, match="total_krw"):
        ts.validate_snapshot(snap)


def test_validate_catches_negative_holdings_but_allows_negative_cash():
    snap = build()
    snap["cash_krw"] = -1000.0
    snap["total_krw"] = round(snap["holdings_krw"] + snap["cash_krw"], 2)
    ts.validate_snapshot(snap)                      # margin/settlement, not an error
    snap["holdings_krw"] = -1.0
    snap["total_krw"] = round(snap["holdings_krw"] + snap["cash_krw"], 2)
    with pytest.raises(ts.SnapshotError, match="negative"):
        ts.validate_snapshot(snap)


def test_validate_catches_bad_as_of():
    snap = build()
    snap["as_of"] = "yesterday"
    with pytest.raises(ts.SnapshotError, match="ISO-8601"):
        ts.validate_snapshot(snap)


# --------------------------------------------------------------------------- #
# write_atomic / upload
# --------------------------------------------------------------------------- #
def test_write_atomic_roundtrips_and_leaves_no_tmp(tmp_path):
    out = tmp_path / "sub" / "toss_snapshot.json"
    ts.write_atomic(build(), str(out))
    assert json.loads(out.read_text(encoding="utf-8"))["total_krw"] == 859000.0
    assert [p.name for p in out.parent.iterdir()] == ["toss_snapshot.json"]


def test_write_atomic_refuses_an_invalid_snapshot(tmp_path):
    out = tmp_path / "toss_snapshot.json"
    bad = build()
    bad["total_krw"] = 0.0                          # no longer holdings + cash
    with pytest.raises(ts.SnapshotError):
        ts.write_atomic(bad, str(out))
    assert not out.exists()                         # nothing partial on disk
    assert list(tmp_path.iterdir()) == []


def test_upload_raises_on_scp_failure(tmp_path):
    class P:
        returncode, stderr, stdout = 255, "Permission denied (publickey).", ""
    with pytest.raises(ts.SnapshotError, match="scp failed"):
        ts.upload(str(tmp_path / "x.json"), runner=lambda *a, **k: P())


def test_upload_targets_the_reporting_side_path_only(tmp_path):
    seen = {}

    class P:
        returncode, stderr, stdout = 0, "", ""

    def runner(cmd, **kw):
        seen["cmd"] = cmd
        return P()

    ts.upload(str(tmp_path / "x.json"), runner=runner)
    target = seen["cmd"][-1]
    assert target.endswith("/strategy_results/toss_snapshot.json")
    # never near live bot state / ledgers
    assert "_state.json" not in target and "ledger" not in target


# --------------------------------------------------------------------------- #
# read-only contract of the vendored client
# --------------------------------------------------------------------------- #
def test_vendored_client_exposes_no_order_endpoints():
    from framework.brokers import toss_readonly

    public = {n for n in dir(toss_readonly.TossClient) if not n.startswith("_")}
    assert public == {"accounts", "holdings", "buying_power", "sellable_quantity"}
    src = (REPO_ROOT / "framework" / "brokers" / "toss_readonly.py").read_text(encoding="utf-8")
    for banned in ("/orders", "place_order", "cancel_order", "def order"):
        assert banned not in src, "order code leaked into the read-only client: " + banned


# --------------------------------------------------------------------------- #
# generator ingest: fresh / stale / missing
# --------------------------------------------------------------------------- #
def _write_snapshot(tmp_path, as_of, **over):
    snap = build(as_of=as_of)
    snap.update(over)
    p = tmp_path / "toss_snapshot.json"
    p.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_ingest_fresh(gen, tmp_path):
    now = datetime(2026, 8, 16, 22, 0, tzinfo=KST)
    path = _write_snapshot(tmp_path, AS_OF)
    acct = gen.load_toss_account(path=path, now=now)
    assert acct["stale"] is False
    assert acct["as_of"] == AS_OF
    assert acct["total_krw"] == 859000.0
    assert acct["cash_krw"] == 19000.0
    assert acct["holdings_count"] == 2
    assert acct["age_hours"] == 1.0


def test_ingest_stale_zeroes_and_nulls_as_of(gen, tmp_path):
    now = datetime(2026, 8, 18, 12, 0, tzinfo=KST)     # ~39h later
    path = _write_snapshot(tmp_path, AS_OF)
    acct = gen.load_toss_account(path=path, now=now)
    assert acct["stale"] is True
    assert acct["as_of"] is None                        # healthcheck contract
    assert acct["last_seen_at"] == AS_OF                # but still shown to humans
    assert acct["total_krw"] == 0.0 and acct["cash_krw"] == 0.0


def test_ingest_freshness_boundary(gen, tmp_path):
    path = _write_snapshot(tmp_path, AS_OF)
    base = datetime.fromisoformat(AS_OF)
    assert gen.load_toss_account(path=path, now=base + timedelta(hours=29.9))["stale"] is False
    assert gen.load_toss_account(path=path, now=base + timedelta(hours=30.1))["stale"] is True


def test_ingest_missing_file(gen, tmp_path):
    acct = gen.load_toss_account(path=str(tmp_path / "nope.json"),
                                 now=datetime(2026, 8, 16, 22, 0, tzinfo=KST))
    assert acct["stale"] is True and acct["as_of"] is None
    assert acct["total_krw"] == 0.0
    assert "no snapshot" in acct["note"]
    # the key must exist even with no data — a missing key would drop Toss from
    # the hero silently instead of showing a grey "no data" tile.
    assert set(acct) >= {"total_krw", "cash_krw", "as_of", "stale"}


@pytest.mark.parametrize("body, match", [
    ("{not json", "unreadable"),
    ("[1, 2, 3]", "not an object"),
    ('{"as_of": "sometime"}', "unparseable as_of"),
    ('{"total_krw": 1}', "unparseable as_of"),
])
def test_ingest_malformed_snapshot_never_crashes(gen, tmp_path, body, match):
    p = tmp_path / "toss_snapshot.json"
    p.write_text(body, encoding="utf-8")
    acct = gen.load_toss_account(path=str(p), now=datetime(2026, 8, 16, 22, 0, tzinfo=KST))
    assert acct["stale"] is True and acct["total_krw"] == 0.0
    assert match in acct["note"]


def test_ingest_future_timestamp_is_treated_as_broken(gen, tmp_path):
    path = _write_snapshot(tmp_path, AS_OF)
    acct = gen.load_toss_account(path=path, now=datetime(2026, 8, 16, 12, 0, tzinfo=KST))
    assert acct["stale"] is True


def test_ingest_reconverts_usd_at_the_dashboard_fx_rate(gen, tmp_path):
    """The snapshot's own FX is a fallback; the KIS rate keeps the tiles agreeing."""
    now = datetime(2026, 8, 16, 22, 0, tzinfo=KST)
    path = _write_snapshot(tmp_path, AS_OF)           # built at fx 1400
    acct = gen.load_toss_account(path=path, now=now, fx_rate=1300.0)
    assert acct["holdings_krw"] == 700000 + 100 * 1300
    assert acct["cash_krw"] == 5000 + 10 * 1300
    assert acct["total_krw"] == 848000.0


def test_ingest_without_native_block_falls_back_to_snapshot_totals(gen, tmp_path):
    now = datetime(2026, 8, 16, 22, 0, tzinfo=KST)
    path = _write_snapshot(tmp_path, AS_OF, native=None)
    acct = gen.load_toss_account(path=path, now=now, fx_rate=1300.0)
    assert acct["total_krw"] == 859000.0              # snapshot's own conversion stands


def test_ingest_accepts_naive_and_zulu_timestamps(gen, tmp_path):
    now = datetime(2026, 8, 16, 22, 0, tzinfo=KST)
    naive = gen.load_toss_account(path=_write_snapshot(tmp_path, "2026-08-16T21:00:00"), now=now)
    assert naive["stale"] is False                    # naive is read as KST
    zulu = gen.load_toss_account(path=_write_snapshot(tmp_path, "2026-08-16T12:00:00Z"), now=now)
    assert zulu["stale"] is False                     # 12:00Z == 21:00 KST
