#!/usr/bin/env python3
"""Toss Securities Open API — READ-ONLY client (OAuth2 client-credentials).

VENDORED COPY — do not diverge casually.
    source : /Users/jaelee/.gemini/antigravity/scratch/quanty/brokers/toss_client.py
    vendored: 2026-08-16, for the whole-investment dashboard (Toss account tile).
    change : this docstring only. The code below is byte-identical to the source.
Why vendored: `scripts/toss_snapshot.py` ships inside automation_oracle and is
scp'd/launchd'd on the Mac; it must not depend on a sibling scratch directory.
Re-sync by copying the source over this file and re-applying this docstring.

READ-ONLY BY CONTRACT. This module exposes token + accounts + holdings +
buying-power + sellable-quantity and NOTHING else. Never add an order/cancel
endpoint here — orders live in a deliberately separate, human-gated module, and
never inside automation_oracle/*AutoTrade*.py (Quanty paper/live isolation rule).

Credentials are read from the environment (or a gitignored .env), NEVER hardcoded:
    TOSS_CLIENT_ID       your Open API client_id
    TOSS_CLIENT_SECRET   your Open API client_secret   (keep secret; .env is gitignored)
    TOSS_ACCOUNT_SEQ     (optional) default accountSeq for account-scoped calls
Get them in Toss Securities WTS -> 설정 -> Open API. Register your calling host's
public IP there too, or every call returns 403.

Auth model (from developers.tossinvest.com):
    POST https://openapi.tossinvest.com/oauth2/token
        Content-Type: application/x-www-form-urlencoded
        grant_type=client_credentials & client_id=... & client_secret=...
    -> {"access_token": "...", "expires_in": N, ...}
    Then: Authorization: Bearer {access_token}
    Account-scoped calls also need:  X-Tossinvest-Account: {accountSeq}

Usage:
    python -m framework.brokers.toss_readonly            # token + accounts + holdings/cash
    python -m framework.brokers.toss_readonly accounts
    python -m framework.brokers.toss_readonly holdings [--account SEQ]
    python -m framework.brokers.toss_readonly cash      [--account SEQ]
    python -m framework.brokers.toss_readonly sellable TICKER [--account SEQ]

Note: `_load_dotenv()` looks next to THIS file and in the cwd. The vendored copy
lives in framework/brokers/ where no .env exists, so callers should point
TOSS_DOTENV at the real one — scripts/toss_snapshot.py does that for you.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

BASE = "https://openapi.tossinvest.com"
TOKEN_URL = f"{BASE}/oauth2/token"
_TOKEN_SAFETY_MARGIN_S = 60      # refresh a bit before real expiry
_DEFAULT_TOKEN_TTL_S = 1800      # fallback if the response omits expires_in


class TossAuthError(RuntimeError):
    """Auth/credential problem (missing creds, 401, or IP-allowlist 403)."""


def read_err_body(e) -> str:
    """HTTPError body -> readable text (Toss gzips error bodies)."""
    raw = e.read()
    if raw[:2] == b"\x1f\x8b":
        import gzip
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    return raw.decode("utf-8", "replace")[:400]


class TossAPIError(RuntimeError):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {str(body)[:400]}")


# --------------------------------------------------------------------------- #
# .env loading (fills only keys not already in the real environment)
# --------------------------------------------------------------------------- #
def _load_dotenv() -> None:
    candidates = []
    if os.environ.get("TOSS_DOTENV"):
        candidates.append(os.environ["TOSS_DOTENV"])
    here = os.path.dirname(os.path.abspath(__file__))
    candidates += [os.path.join(here, ".env"), os.path.join(os.getcwd(), ".env")]
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    os.environ.setdefault(k, v)  # real env always wins
        except OSError:
            continue
        break  # first found wins


class TossClient:
    def __init__(self, client_id=None, client_secret=None, account_seq=None, timeout=15):
        _load_dotenv()
        self.client_id = client_id or os.environ.get("TOSS_CLIENT_ID")
        self._client_secret = client_secret or os.environ.get("TOSS_CLIENT_SECRET")
        # Treat an empty TOSS_ACCOUNT_SEQ= as absent (else a blank header is sent).
        self.account_seq = (account_seq or os.environ.get("TOSS_ACCOUNT_SEQ")) or None
        self.timeout = timeout
        self._access_token = None
        self._token_expiry = 0.0
        if not self.client_id or not self._client_secret:
            raise TossAuthError(
                "Missing TOSS_CLIENT_ID / TOSS_CLIENT_SECRET. Put them in the environment "
                "or a gitignored brokers/.env (see brokers/.env.example)."
            )

    # ---- token -----------------------------------------------------------
    def _fetch_token(self) -> None:
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self._client_secret,
        }).encode("utf-8")
        req = urllib.request.Request(
            TOKEN_URL, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = read_err_body(e)
            if e.code in (400, 401):
                raise TossAuthError(f"token rejected ({e.code}) — check client_id/secret. {detail}")
            if e.code == 403:
                raise TossAuthError(f"token 403 — is this host's IP allowlisted in Open API settings? {detail}")
            raise TossAPIError(e.code, detail)
        except urllib.error.URLError as e:
            raise TossAPIError(0, f"network error: {e.reason}")
        tok = payload.get("access_token")
        if not tok:
            raise TossAuthError(f"no access_token in response: {str(payload)[:200]}")
        ttl = int(payload.get("expires_in") or _DEFAULT_TOKEN_TTL_S)
        self._access_token = tok
        self._token_expiry = time.time() + max(0, ttl - _TOKEN_SAFETY_MARGIN_S)

    def _token(self) -> str:
        if not self._access_token or time.time() >= self._token_expiry:
            self._fetch_token()
        return self._access_token

    # ---- request core (read-only GET) ------------------------------------
    def _get(self, path, params=None, account_seq=None, _retried=False):
        url = f"{BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        seq = account_seq or self.account_seq
        if seq:  # skip a blank/None account (avoids sending an empty header)
            headers["X-Tossinvest-Account"] = str(seq)
        req = urllib.request.Request(url, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = read_err_body(e)
            if e.code == 401 and not _retried:
                self._access_token = None  # force refresh once
                return self._get(path, params, account_seq, _retried=True)
            if e.code == 403:
                raise TossAuthError(
                    f"403 on {path} — IP not allowlisted, or this account/scope isn't permitted. {detail}")
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "1") or "1")
                time.sleep(min(wait, 5))
                if not _retried:
                    return self._get(path, params, account_seq, _retried=True)
            raise TossAPIError(e.code, detail)
        except urllib.error.URLError as e:
            raise TossAPIError(0, f"network error: {e.reason}")

    # ---- read-only endpoints --------------------------------------------
    def accounts(self):
        """GET /api/v1/accounts — the user's account list (contains accountSeq)."""
        return self._get("/api/v1/accounts")

    def holdings(self, account_seq: Optional[str] = None):
        """GET /api/v1/holdings — current stock holdings for the account."""
        return self._get("/api/v1/holdings", account_seq=account_seq)

    def buying_power(self, account_seq: Optional[str] = None, currency: str = "KRW"):
        """GET /api/v1/buying-power — available cash for purchases.

        Requires a `currency` (KRW | USD) query param — the API returns
        400 invalid-request field=currency without it.
        """
        return self._get("/api/v1/buying-power", params={"currency": currency},
                         account_seq=account_seq)

    def sellable_quantity(self, symbol: str, account_seq: Optional[str] = None):
        """GET /api/v1/sellable-quantity — sellable shares of `symbol` (Toss uses `symbol`)."""
        return self._get("/api/v1/sellable-quantity", params={"symbol": symbol},
                         account_seq=account_seq)


# --------------------------------------------------------------------------- #
# Best-effort accountSeq extraction (response wrapper shape unknown until seen)
# --------------------------------------------------------------------------- #
def _find_account_seqs(accounts_response) -> list:
    seqs = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("accountSeq", "account_seq") and isinstance(v, (str, int)):
                    seqs.append(str(v))
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(accounts_response)
    return list(dict.fromkeys(seqs))


def _pp(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(description="Toss Securities read-only API client")
    p.add_argument("cmd", nargs="?", default="whoami",
                   choices=["whoami", "accounts", "holdings", "cash", "sellable"])
    p.add_argument("ticker", nargs="?", default=None)
    p.add_argument("--account", default=None, help="accountSeq override")
    p.add_argument("--currency", default="KRW", help="KRW|USD (for cash)")
    args = p.parse_args(argv)

    try:
        c = TossClient(account_seq=args.account)
    except TossAuthError as e:
        print(f"[toss] AUTH SETUP: {e}")
        return 2

    try:
        if args.cmd in ("whoami", "accounts"):
            c._token()  # exercises the token exchange
            tok_prefix = (c._access_token or "")[:6]
            print(f"[toss] token OK (starts {tok_prefix}…). Fetching accounts…")
            acc = c.accounts()
            _pp(acc)
            found = _find_account_seqs(acc)
            if found:
                print(f"[toss] accountSeq(s): {found}  "
                      f"(set TOSS_ACCOUNT_SEQ or pass --account for holdings/cash)")
            if args.cmd == "whoami":
                seq = args.account or c.account_seq or (found[0] if found else None)
                if seq:
                    print(f"\n[toss] holdings (account {seq}):"); _pp(c.holdings(seq))
                    for cur in ("KRW", "USD"):
                        print(f"\n[toss] buying-power {cur} (account {seq}):")
                        _pp(c.buying_power(seq, currency=cur))
                else:
                    print("[toss] no accountSeq yet — copy one above into TOSS_ACCOUNT_SEQ.")
        elif args.cmd == "holdings":
            _pp(c.holdings(args.account))
        elif args.cmd == "cash":
            _pp(c.buying_power(args.account, currency=args.currency))
        elif args.cmd == "sellable":
            if not args.ticker:
                print("[toss] usage: sellable TICKER [--account SEQ]"); return 2
            _pp(c.sellable_quantity(args.ticker, args.account))
    except TossAuthError as e:
        print(f"[toss] AUTH ERROR: {e}")
        return 2
    except TossAPIError as e:
        print(f"[toss] API ERROR: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
