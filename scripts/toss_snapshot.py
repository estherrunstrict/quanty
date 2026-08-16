#!/usr/bin/env python3
"""Toss Securities account snapshot — READ-ONLY, Mac-side.

Why the Mac: the Toss Open API enforces an IP allowlist that covers Jae's Mac
only. The trading server (193.123.246.52) would get 403 on every call, so the
server never talks to Toss. Instead this script runs on the Mac (launchd,
see scripts/launchd/com.quanty.toss-snapshot.plist), writes one small JSON, and
scp's it to the server where the public-dashboard generator ingests it.

READ-ONLY: it uses framework/brokers/toss_readonly.py, which has no order code.
This script never places, cancels or modifies anything. It only reads
accounts / holdings / buying-power.

Output schema (strategy_results/toss_snapshot.json):
    {
      "schema_version": 1,
      "as_of": "2026-08-16T21:00:00+09:00",
      "account_seq": "1",
      "total_krw": 0.0,            # holdings_krw + cash_krw
      "holdings_krw": 0.0,         # market value of positions, KRW
      "cash_krw": 0.0,             # KRW cash + USD cash * fx
      "fx_rate": 1380.0,           # KRW per USD used for the conversion (None if unused)
      "fx_source": "yfinance:KRW=X",
      "native": {"holdings_krw": 0.0, "holdings_usd": 0.0,
                 "cash_krw": 0.0,     "cash_usd": 0.0},
      "holdings": [{"symbol": "005930", "name": "삼성전자", "qty": 10.0,
                    "currency": "KRW", "value_native": 0.0, "value_krw": 0.0,
                    "market_country": "KR"}],
      "source": "mac-launchd"
    }

`native` is emitted on purpose: the server-side generator re-converts the USD
bucket with the same KIS exchange rate the rest of the dashboard uses, so the
Toss tile can never disagree with the KIS tile over FX. The `fx_rate` here is
only a fallback for consumers with no better rate.

Toss API quirks handled here (learned the hard way, keep them documented):
  * error bodies are GZIPPED — the client already decompresses them;
  * account-scoped calls need the `X-Tossinvest-Account` header;
  * /buying-power REQUIRES a `currency` query param (KRW|USD) or it 400s;
  * the endpoint uses `symbol`, not `ticker`;
  * holdings' top-level `marketValue.amount.krw` / `.usd` are two SEPARATE
    currency buckets (KR positions / US positions), NOT the same portfolio
    quoted twice. Adding them without FX would be nonsense; converting the
    KRW bucket would double-count. Verified against the live account 2026-08-16.

Credentials: TOSS_CLIENT_ID / TOSS_CLIENT_SECRET / TOSS_ACCOUNT_SEQ, read from
the environment or a gitignored .env (auto-located, see _locate_dotenv). They
are NEVER written to the snapshot, logged, or shipped to the server.

Failure policy: any error exits non-zero and NO file is written or uploaded.
A stale snapshot is a state the generator handles gracefully; a truncated or
half-converted one is not.

Usage:
    python3 scripts/toss_snapshot.py                 # fetch, write, scp to server
    python3 scripts/toss_snapshot.py --no-upload     # local dry run
    python3 scripts/toss_snapshot.py --out /tmp/x.json --no-upload
    python3 scripts/toss_snapshot.py --fx 1380       # pin FX (skips yfinance)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

KST = timezone(timedelta(hours=9))
SCHEMA_VERSION = 1

REMOTE_HOST = "ubuntu@193.123.246.52"
REMOTE_PATH = "/home/ubuntu/koreainvestment-autotrade/strategy_results/toss_snapshot.json"
SSH_KEY = os.path.expanduser("~/.ssh/oci_rsa")
DEFAULT_OUT = os.path.expanduser("~/.quanty/toss_snapshot.json")


class SnapshotError(RuntimeError):
    """Anything that must abort the run without writing a snapshot."""


# --------------------------------------------------------------------------- #
# credentials plumbing
# --------------------------------------------------------------------------- #
def _locate_dotenv(explicit: str | None = None) -> str | None:
    """Point the vendored client at the real gitignored .env.

    The vendored client looks for a .env next to itself (framework/brokers/) and
    in the cwd — neither holds the credentials. Rather than move secrets around,
    we set TOSS_DOTENV to the first .env we can find, walking up from this repo.
    An already-set TOSS_DOTENV always wins (explicit operator intent).
    """
    if os.environ.get("TOSS_DOTENV"):
        return os.environ["TOSS_DOTENV"]
    candidates = []
    if explicit:
        candidates.append(explicit)
    for parent in [REPO_ROOT] + list(REPO_ROOT.parents):
        candidates.append(str(parent / "brokers" / ".env"))
    candidates += [
        os.path.expanduser("~/.quanty/toss.env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            os.environ["TOSS_DOTENV"] = path
            return path
    return None


# --------------------------------------------------------------------------- #
# parsing helpers — Toss returns every number as a string
# --------------------------------------------------------------------------- #
def _num(value, default=0.0) -> float:
    """'148.668614' / 148 / None / '' -> float. Never raises."""
    if value is None or value == "":
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _result(payload):
    """Unwrap the {"result": ...} envelope, tolerating a bare payload."""
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload


# --------------------------------------------------------------------------- #
# FX
# --------------------------------------------------------------------------- #
def resolve_fx_rate(explicit=None):
    """KRW-per-USD, as (rate, source). Returns (None, 'unavailable') on failure.

    Order: explicit flag > TOSS_SNAPSHOT_FX env > yfinance. A missing rate is
    only fatal when the account actually holds USD positions or USD cash —
    build_snapshot() enforces that.
    """
    if explicit is not None:
        rate = _num(explicit, 0.0)
        if rate > 0:
            return rate, "explicit"
    env = os.environ.get("TOSS_SNAPSHOT_FX")
    if env:
        rate = _num(env, 0.0)
        if rate > 0:
            return rate, "env:TOSS_SNAPSHOT_FX"
    try:
        import yfinance as yf
        for symbol in ("KRW=X", "USDKRW=X"):
            try:
                hist = yf.Ticker(symbol).history(period="5d")
                if hist is not None and not hist.empty:
                    rate = float(hist["Close"].dropna().iloc[-1])
                    if 500 < rate < 5000:          # sanity band for KRW/USD
                        return rate, "yfinance:{}".format(symbol)
            except Exception:
                continue
    except Exception:
        pass
    return None, "unavailable"


# --------------------------------------------------------------------------- #
# pure snapshot builder (unit-tested; no network, no clock, no disk)
# --------------------------------------------------------------------------- #
def build_snapshot(holdings_resp, cash_krw_resp, cash_usd_resp, account_seq,
                   as_of, fx_rate=None, fx_source="unavailable",
                   source="mac-launchd"):
    """Toss API responses -> the snapshot dict. Raises SnapshotError if unusable.

    `as_of` is an ISO-8601 string (KST). Everything is recomputed from the
    per-item rows rather than trusting the response's aggregate block, so a
    partial `items` list can never masquerade as a full portfolio: the totals
    always describe exactly the holdings we emit.
    """
    res = _result(holdings_resp) or {}
    items = res.get("items")
    if items is None:
        raise SnapshotError("holdings response has no `items` list: {}".format(
            str(res)[:200]))
    if not isinstance(items, list):
        raise SnapshotError("holdings `items` is {}, expected list".format(type(items).__name__))

    holdings = []
    native_krw = 0.0
    native_usd = 0.0
    for item in items:
        if not isinstance(item, dict):
            raise SnapshotError("holdings item is {}, expected object".format(type(item).__name__))
        symbol = item.get("symbol")
        if not symbol:
            raise SnapshotError("holdings item without a symbol: {}".format(str(item)[:200]))
        currency = (item.get("currency") or "KRW").upper()
        qty = _num(item.get("quantity"))
        value_native = _num((item.get("marketValue") or {}).get("amount"))
        if currency == "KRW":
            native_krw += value_native
        elif currency == "USD":
            native_usd += value_native
        else:
            raise SnapshotError("unhandled holding currency {!r} on {}".format(currency, symbol))
        holdings.append({
            "symbol": str(symbol),
            "name": item.get("name") or str(symbol),
            "qty": qty,
            "currency": currency,
            "value_native": round(value_native, 6),
            "value_krw": 0.0,                       # filled once FX is known
            "market_country": item.get("marketCountry") or "",
        })

    cash_krw_native = _num((_result(cash_krw_resp) or {}).get("cashBuyingPower"))
    cash_usd_native = _num((_result(cash_usd_resp) or {}).get("cashBuyingPower")) if cash_usd_resp else 0.0

    needs_fx = (native_usd != 0.0) or (cash_usd_native != 0.0)
    rate = _num(fx_rate, 0.0)
    if needs_fx and rate <= 0:
        raise SnapshotError(
            "account holds USD (positions {:.2f} / cash {:.2f}) but no FX rate is "
            "available (source={}). Pass --fx or set TOSS_SNAPSHOT_FX; refusing to "
            "write a KRW total that silently drops the USD sleeve.".format(
                native_usd, cash_usd_native, fx_source))

    for h in holdings:
        h["value_krw"] = round(h["value_native"] * rate if h["currency"] == "USD"
                               else h["value_native"], 2)

    holdings_krw = round(native_krw + native_usd * rate, 2)
    cash_krw = round(cash_krw_native + cash_usd_native * rate, 2)
    total_krw = round(holdings_krw + cash_krw, 2)

    snap = {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of,
        "account_seq": str(account_seq) if account_seq is not None else None,
        "total_krw": total_krw,
        "holdings_krw": holdings_krw,
        "cash_krw": cash_krw,
        "fx_rate": rate if rate > 0 else None,
        "fx_source": fx_source if rate > 0 else "unused",
        "native": {
            "holdings_krw": round(native_krw, 2),
            "holdings_usd": round(native_usd, 6),
            "cash_krw": round(cash_krw_native, 2),
            "cash_usd": round(cash_usd_native, 6),
        },
        "holdings": holdings,
        "source": source,
    }
    validate_snapshot(snap)
    return snap


def validate_snapshot(snap):
    """Last gate before anything touches the disk. Raises SnapshotError."""
    for key in ("as_of", "total_krw", "cash_krw", "holdings_krw", "holdings", "source"):
        if key not in snap:
            raise SnapshotError("snapshot missing required key {!r}".format(key))
    try:
        # `.replace` because Python < 3.11's fromisoformat rejects a "Z" suffix,
        # and this must validate identically wherever it runs (launchd uses the
        # system /usr/bin/python3, which is 3.9 on this Mac).
        datetime.fromisoformat(str(snap["as_of"]).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise SnapshotError("as_of is not ISO-8601: {!r}".format(snap.get("as_of")))
    for key in ("total_krw", "cash_krw", "holdings_krw"):
        v = snap[key]
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            raise SnapshotError("{} is not a finite number: {!r}".format(key, v))
    # Cash may legitimately go negative (margin / settlement in flight); a
    # negative position value or account total cannot, and means we mis-parsed.
    for key in ("total_krw", "holdings_krw"):
        if snap[key] < 0:
            raise SnapshotError("{} is negative ({}) — refusing to publish".format(key, snap[key]))
    if not isinstance(snap["holdings"], list):
        raise SnapshotError("holdings is not a list")
    for h in snap["holdings"]:
        for key in ("symbol", "qty", "value_krw", "currency"):
            if key not in h:
                raise SnapshotError("holding missing {!r}: {}".format(key, str(h)[:120]))
        if not math.isfinite(_num(h["value_krw"], float("nan"))):
            raise SnapshotError("holding {} has a non-finite value_krw".format(h["symbol"]))
    expected = round(snap["holdings_krw"] + snap["cash_krw"], 2)
    if abs(expected - snap["total_krw"]) > 1.0:
        raise SnapshotError(
            "total_krw {} != holdings_krw + cash_krw {}".format(snap["total_krw"], expected))
    return True


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def write_atomic(snap, path):
    """Serialize first, then tmp-file + os.replace, so readers never see a
    half-written or half-serialized snapshot."""
    blob = json.dumps(snap, ensure_ascii=False, indent=2)
    validate_snapshot(json.loads(blob))       # round-trip guard
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def upload(local_path, host=REMOTE_HOST, remote_path=REMOTE_PATH, key=SSH_KEY,
           runner=None):
    """scp the snapshot to the server. Raises SnapshotError on failure."""
    runner = runner or subprocess.run
    cmd = ["scp", "-i", key, "-o", "ConnectTimeout=20", "-o", "BatchMode=yes",
           local_path, "{}:{}".format(host, remote_path)]
    proc = runner(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise SnapshotError("scp failed ({}): {}".format(
            proc.returncode, (proc.stderr or "").strip()[:400]))
    return remote_path


# --------------------------------------------------------------------------- #
# live fetch
# --------------------------------------------------------------------------- #
def fetch_live(account_seq=None):
    """(holdings, cash_krw, cash_usd, account_seq) from the live read-only API."""
    from framework.brokers.toss_readonly import TossClient, _find_account_seqs

    client = TossClient(account_seq=account_seq)
    seq = account_seq or client.account_seq
    if not seq:
        found = _find_account_seqs(client.accounts())
        if not found:
            raise SnapshotError("no accountSeq available — set TOSS_ACCOUNT_SEQ or pass --account")
        seq = found[0]
    return (client.holdings(seq),
            client.buying_power(seq, currency="KRW"),
            client.buying_power(seq, currency="USD"),
            seq)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", default=DEFAULT_OUT, help="local snapshot path")
    p.add_argument("--no-upload", action="store_true", help="write locally, do not scp")
    p.add_argument("--account", default=None, help="accountSeq override")
    p.add_argument("--dotenv", default=None, help="explicit path to the credentials .env")
    p.add_argument("--fx", default=None, help="pin the KRW-per-USD rate (skips yfinance)")
    p.add_argument("--source", default="mac-launchd", help="provenance tag in the JSON")
    p.add_argument("--remote-path", default=REMOTE_PATH)
    p.add_argument("--ssh-key", default=SSH_KEY)
    args = p.parse_args(argv)

    dotenv = _locate_dotenv(args.dotenv)
    print("[toss-snapshot] credentials: {}".format(dotenv or "environment only"))

    try:
        holdings, cash_krw, cash_usd, seq = fetch_live(args.account)
        rate, fx_source = resolve_fx_rate(args.fx)
        snap = build_snapshot(
            holdings, cash_krw, cash_usd, seq,
            as_of=datetime.now(KST).isoformat(timespec="seconds"),
            fx_rate=rate, fx_source=fx_source, source=args.source,
        )
    except SnapshotError as e:
        print("[toss-snapshot] ABORT: {}".format(e), file=sys.stderr)
        return 1
    except Exception as e:                                    # auth / API / network
        print("[toss-snapshot] ABORT: {}: {}".format(type(e).__name__, e), file=sys.stderr)
        return 1

    try:
        path = write_atomic(snap, args.out)
    except Exception as e:
        print("[toss-snapshot] ABORT: could not write {}: {}".format(args.out, e), file=sys.stderr)
        return 1
    print("[toss-snapshot] {} holdings, total W{:,.0f} (cash W{:,.0f}, fx {}) -> {}".format(
        len(snap["holdings"]), snap["total_krw"], snap["cash_krw"],
        snap["fx_rate"] or "n/a", path))

    if args.no_upload:
        print("[toss-snapshot] --no-upload: server copy NOT updated")
        return 0
    try:
        upload(path, remote_path=args.remote_path, key=args.ssh_key)
    except Exception as e:
        print("[toss-snapshot] ABORT: {}".format(e), file=sys.stderr)
        return 1
    print("[toss-snapshot] uploaded -> {}:{}".format(REMOTE_HOST, args.remote_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
