#!/usr/bin/env python3
"""Generate dashboard_data.json from live dashboard_server API + KIS account query."""

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(DASHBOARD_DIR, "docs", "data")
API_URL = "http://localhost:8077/api/data"
TRADING_DIR = "/home/ubuntu/koreainvestment-autotrade"
DEPOSITS_FILE = os.path.join(DASHBOARD_DIR, "deposits.json")

sys.path.insert(0, DASHBOARD_DIR)
sys.path.insert(0, os.path.join(TRADING_DIR, "open-trading-api", "examples_llm"))
sys.path.insert(0, TRADING_DIR)  # for dashboard_equity (shared with dashboard_server)
from dashboard_equity import et_today, pin_equity_endpoints

# Toss Securities snapshot: written on Jae's Mac by
# automation_oracle/scripts/toss_snapshot.py and scp'd here. Nothing on this
# server talks to Toss (the Open API IP allowlist covers the Mac only), so this
# file is the account's only representation and it can legitimately go stale.
TOSS_SNAPSHOT_FILE = os.path.join(TRADING_DIR, "strategy_results", "toss_snapshot.json")
# What matters is the snapshot's age AT BUILD TIME, not how old it gets sitting
# on disk between cycles. The Mac job runs 06:00 and 15:50 and the publish runs
# 06:30 and 16:15, so the generator should never see a snapshot older than ~30
# minutes; a snapshot older than one cycle (>=14h) has missed its run outright.
#
# The history is a story of this number being too generous. 30h passed a
# snapshot that had missed an entire day (2026-08-19, 25.5h). 20h then passed
# 2026-08-20's 19.1h snapshot — yesterday's close, published as if current,
# hands-on still listing a stock that had already been sold. 6h is the first
# value that catches a missed cycle: far above the 30-minute normal case, far
# below the 14h a skipped run costs, and still tolerant of a late catch-up run
# that at least landed the same day.
#
# Tightening is safe because an aged snapshot is KEPT and labelled, never
# zeroed: `usable` stays true, the rows still feed the hands-on sleeve and
# NMF2's marks, and only the `stale` flag changes. See load_toss_account.
TOSS_MAX_AGE_HOURS = float(os.environ.get("QUANTY_TOSS_MAX_AGE_HOURS", "6"))

# NMF2 (신마법공식 2.0) sleeve — a real automated bot trading a ~W1M slice of the
# Toss account. Its ledger is the ONLY record of which Toss positions are the
# bot's; without it every Toss share looks hands-on. Read STRICTLY read-only:
# this file is the bot's own live state and the dashboard must never write it.
# We read it here (rather than shipping it in the Mac-side Toss snapshot)
# because the ledger already lives on THIS host next to the generator, so a
# direct read needs no new transport, no Mac job, and no snapshot schema bump.
NMF2_LEDGER_FILE = "/home/ubuntu/toss-nmf2-bot/toss_nmf2_bot/state/ledger.json"
# Hands-on / Manual and NMF2 sleeve history (reporting-side only — no bot reads
# these; they live beside the other reporting artefacts, never in the bot's dir).
MANUAL_HISTORY_FILE = os.path.join(TRADING_DIR, "strategy_results", "manual_sleeve_history.jsonl")
NMF2_HISTORY_FILE = os.path.join(TRADING_DIR, "strategy_results", "nmf2_history.jsonl")
# |investments - (bots + manual + cash)| above this share of investments flips
# totals.reconciliation_warning. The split is built by partitioning the account
# rows themselves, so a non-zero gap means an INPUT is missing (KIS query failed,
# a bot claims more shares than the account holds), not a rounding drift.
RECONCILIATION_WARN_PCT = 1.0
# accounts.upbit is replayed from the BTC VB bot's state file — no live exchange
# call happens anywhere in this generator — so it goes stale on AGE, not on
# value. The bot trades at the US close and rewrites state only when its monitor
# window closes, which routinely leaves last_run a day or two behind at publish
# time; three days is the first age that normal operation cannot explain.
UPBIT_STALE_DAYS = float(os.environ.get("QUANTY_UPBIT_STALE_DAYS", "3"))

# ── Bot staleness ────────────────────────────────────────────────────────
# How long each bot is NORMALLY silent between runs, in hours.
#
# A flat threshold cannot express this. Every stock bot fires ONCE per
# session and then sits quiet until the next one: the US bots run at the
# open (22:30 KST) and are honestly 21h "old" by dinner time, every single
# day, while working perfectly. The page used `stale_hours > 6`, which lit
# up seven of the eight bots at once on 2026-08-20 while all of them were
# live. A warning that is always on is furniture — you stop reading it, and
# then it cannot tell you about the one bot that really did die.
#
# The question worth asking is not "how long since it ran" but "has it
# missed its OWN next run".
BOT_CADENCE_HOURS = {
    "btc_vb":        24,   # 16:00 ET, 365 days — BTC trades weekends too
    "korea_etf":     24,   # KR open, weekdays
    "hybrid_vb":     24,   # KR and US legs, weekdays
    "quant40":       24,   # US open, weekdays
    "jd_strategy":   24,
    "dual_momentum": 24,
    "claude_bot":    24,
    "nmf2":          24,   # daily sync 09:05/15:40; the rebalance is monthly
}
BOT_CADENCE_DEFAULT = 24
# Flag only once a bot is half a cycle PAST its next run — a late start or a
# slow session must not raise an alarm.
BOT_STALE_MULTIPLE = float(os.environ.get("QUANTY_BOT_STALE_MULTIPLE", "1.5"))
# Everything except BTC trades on market days only, so weekend silence is
# not staleness. Friday 22:30 -> Monday 20:00 is 69 wall-clock hours and
# exactly ONE missed session; counting the weekend would flag the whole
# fleet every Monday.
BOT_TRADES_WEEKENDS = {"btc_vb"}

# ── Bot identity colours ─────────────────────────────────────────────────
# ONE palette, used everywhere a bot is named: cards, comparison chart,
# allocation table, activity feed, and the Discord reports. A colour that means
# "quant40" in one place and nothing in another is not an identity.
#
# Chosen under three constraints, all verified numerically rather than by eye:
#   1. every pair >= 20 deltaE apart (closest is 23.9) — distinguishable;
#   2. every colour >= 22 deltaE from the SEMANTIC colours (closest is 22.6):
#      profit green #3a8a5a, loss red #a84848, accent amber #c9a24d. Without
#      this a rust-tinted bot reads as "this one is losing". The old chart-only
#      palette failed exactly here — korea_etf WAS the profit green and
#      jd_strategy WAS the loss red;
#   3. >= 3:1 contrast on the card surface #0e0e11.
#
# Bot colour is for IDENTITY chrome only — rails, dots, chart lines, borders.
# Numbers stay red/green. That structural split is what keeps the two readable
# side by side.
BOT_COLORS = {
    "btc_vb":        "#c29a80",   # tan
    "hybrid_vb":     "#bcc251",   # yellow-olive
    "claude_bot":    "#5dc29f",   # mint
    "korea_etf":     "#408799",   # cyan
    "quant40":       "#517ec2",   # blue
    "dual_momentum": "#6851c2",   # indigo
    "nmf2":          "#b880c2",   # purple
    "jd_strategy":   "#99406d",   # wine
    "manual":        "#8a8a8a",   # grey — the hands-on sleeve is not a bot
}

# Discord cannot colour a line. Inside a code fence only ANSI's 8 colours work
# and emoji break the monospace alignment; outside one, only emoji render. So a
# bot's Discord identity is a coloured square, placed on the header line where
# nothing is being aligned. There are exactly nine squares and nine entities,
# assigned to minimise total distance from the real hex — they are IDENTITY
# markers, not colour reproduction, so one or two are only roughly on hue.
BOT_CHIPS = {
    "btc_vb": "🟧", "hybrid_vb": "🟨", "claude_bot": "🟩", "korea_etf": "⬛",
    "quant40": "🟦", "dual_momentum": "🟪", "nmf2": "🟫", "jd_strategy": "🟥",
    "manual": "⬜",
}
BOT_COLOR_FALLBACK = "#6a6a6a"

# Feed entries carry a logger name, not a bot id. Matched longest-first so
# "HybridVB_KR" cannot be swallowed by a shorter pattern.
FEED_SOURCE_BOTS = {
    "hybridvb_kr": "hybrid_vb", "hybridvb_us": "hybrid_vb",
    "hybridvb": "hybrid_vb", "vb_strategy": "btc_vb",
    "korea_etf": "korea_etf", "koreaetf": "korea_etf",
    "quant40": "quant40", "jd_strategy": "jd_strategy",
    "dual_momentum": "dual_momentum", "claude": "claude_bot", "nmf2": "nmf2",
}


def bot_color(bot_id):
    return BOT_COLORS.get(bot_id, BOT_COLOR_FALLBACK)


def feed_bot_id(source):
    """Logger name -> bot id, or None. Longest pattern wins."""
    s = str(source or "").strip().lower()
    if not s:
        return None
    for pat in sorted(FEED_SOURCE_BOTS, key=len, reverse=True):
        if pat in s:
            return FEED_SOURCE_BOTS[pat]
    return None


def annotate_feed_bots(feed):
    """Tag each feed event with the bot that produced it. Mutates in place.

    The feed's left rail used to encode the EVENT TYPE (buy green, sell red),
    which duplicated what the tag already said and told you nothing about which
    bot was talking. The rail now carries bot identity and the tag keeps the
    buy/sell colour, so one glance answers both questions.
    """
    for e in feed or []:
        if isinstance(e, dict) and not e.get("bot"):
            b = feed_bot_id(e.get("source"))
            if b:
                e["bot"] = b
    return feed


# ── Asset-management layer ───────────────────────────────────────────────
# The allocator decides how much capital each bot runs; the bots only READ
# their budget. That inversion is the whole governance model and it was
# invisible here — the dashboard showed what the bots DID with the money and
# never showed who decided the amount, so an X of 80% or a bot dropped from
# the roster could only be found by reading a cron log.
ALLOC_STATE_DIR = os.environ.get(
    "QUANTY_ALLOC_STATE", "/home/ubuntu/asset-mgmt/asset_mgmt/state")
# The allocator runs 08:20 daily. Past a day and a half it has missed a run.
ALLOC_MAX_AGE_HOURS = float(os.environ.get("QUANTY_ALLOC_MAX_AGE_HOURS", "36"))
# The allocator splits hybrid_vb into its two legs and carries bots the
# dashboard has no card for. Names for the ones the grid cannot label.
ALLOC_NAMES = {
    "quant40": "US Quant40", "jd_strategy": "JD Strategy",
    "dual_momentum": "Dual Momentum", "claude_bot": "Claude Trading Bot",
    "hybrid_vb_kr": "Hybrid VB (KR)", "hybrid_vb_us": "Hybrid VB (US)",
    "nmf2": "NMF2", "btc_vb": "BTC Volatility Breakout",
    "korea_etf": "Korea ETF Momentum", "event_bot": "Event Bot",
    "usvb": "US VB",
}


def weekday_age_hours(age_hours, now, skip_weekends=True):
    """`age_hours` with whole Saturday/Sunday hours removed. PURE.

    Returns the age unchanged when the bot trades weekends, and never
    returns less than zero. None in, None out — an unknown age must stay
    unknown rather than collapse to "fresh".
    """
    if age_hours is None:
        return None
    if not skip_weekends:
        return float(age_hours)
    start = now - timedelta(hours=float(age_hours))
    closed, cur = 0.0, start
    while cur < now:
        midnight = (cur + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        nxt = min(now, midnight)
        if cur.weekday() >= 5:                 # 5=Sat, 6=Sun
            closed += (nxt - cur).total_seconds() / 3600.0
        cur = nxt
    return max(0.0, float(age_hours) - closed)


def account_name_map(totals):
    """ticker -> company name, taken from the BROKER's own labels.

    KIS returns a display name with every holding (`prdt_name` for KR,
    `ovrs_item_name` for US) and query_account_total already keeps it, but the
    bots' own result files store tickers only. So the cards rendered raw codes:
    069500, 132030, 364690, GLTR. Nobody reads 364690 and thinks "KODEX
    Innovation Tech Active" — and the two sleeves that DID show names (NMF2 and
    hands-on, which come from the Toss snapshot) proved how much easier the
    cards are to scan that way.

    Using the broker's labels rather than a hand-kept table means the names
    cannot drift from the account, and a newly bought ticker is named the day it
    appears.
    """
    out = {}
    for leg in ("kr", "us"):
        for h in ((totals or {}).get(leg) or {}).get("holdings") or []:
            t = str(h.get("ticker") or "").strip()
            n = str(h.get("name") or "").strip()
            if t and n and n != t:
                out[t] = n
    return out


def annotate_holding_names(strategies, names):
    """Fill `name` on holdings and open positions. Mutates in place.

    Never overwrites a name that is already there — the Toss-sourced sleeves
    carry Korean names the KIS account does not have. A ticker the broker cannot
    label keeps the code, which is the honest fallback.
    """
    if not names:
        return strategies
    for s in strategies or []:
        if not isinstance(s, dict):
            continue
        buckets = [s] + [s[leg] for leg in ("kr", "us")
                         if isinstance(s.get(leg), dict)]
        for b in buckets:
            for h in b.get("holdings") or []:
                if isinstance(h, dict) and not h.get("name"):
                    t = str(h.get("ticker") or h.get("symbol") or "").strip()
                    if names.get(t):
                        h["name"] = names[t]
        # hybrid_vb's open positions are keyed BY ticker, so the name has to go
        # inside the value for the row renderer to reach it.
        ops = s.get("open_positions")
        if isinstance(ops, dict):
            for positions in ops.values():
                if not isinstance(positions, dict):
                    continue
                for ticker, pos in positions.items():
                    if isinstance(pos, dict) and not pos.get("name"):
                        if names.get(str(ticker).strip()):
                            pos["name"] = names[str(ticker).strip()]
    return strategies


def annotate_capital_fields(strategies):
    """Fill budget / cost basis / profit rate for a bot that owns its account.

    btc_vb is the one bot the allocator leaves out — Upbit is not an investable
    account under the capital policy — so it carries no allocated budget and
    simply runs on whatever the exchange holds. dashboard_server computes
    profit_rate_ytd_pct as total_pl / BUDGET and returns None when the budget is
    zero, so a bot with a real W363k YTD P/L reported Budget Cap "Unlimited",
    Invested "—" and Profit % "—": three blanks for a bot that is fully invested.

    For a bot that owns its whole account the account balance IS the budget, and
    `value` already IS that balance (the card is built from the Upbit total).
    Deployed cost is then value minus unrealized gain — equal to value when the
    bot is sitting in cash, slightly below it while a position is in profit.

    Scoped by `budget falsy AND value > 0`, so it cannot touch a bot that is
    genuinely unfunded: korea_etf is paper at value 0 and stays untouched.
    """
    for s in strategies or []:
        if not isinstance(s, dict) or s.get("id") == "manual":
            continue
        val = float(s.get("value") or 0)
        if not float(s.get("budget") or 0) and val > 0:
            s["budget"] = round(val, 2)
            s["budget_basis"] = "account"
            if not float(s.get("cost_basis") or 0):
                unreal = float(s.get("unrealized_profit") or 0)
                s["cost_basis"] = round(max(0.0, val - unreal), 2)
                s["cost_basis_basis"] = "derived"

        if s.get("profit_rate_ytd_pct") is not None:
            s.setdefault("profit_rate_basis", "budget")
            continue
        pl = s.get("total_pl_ytd")
        if pl is None:
            continue
        # Prefer the budget denominator so this bot's return is comparable with
        # every other bot's; fall back to deployed cost only if there is still
        # no budget, and LABEL it, because the two are not the same question.
        budget = float(s.get("budget") or 0)
        if budget > 0:
            s["profit_rate_ytd_pct"] = round(float(pl) / budget * 100, 2)
            s["profit_rate_basis"] = "budget"
            continue
        basis = float(s.get("cost_basis") or 0)
        if basis > 0:
            s["profit_rate_ytd_pct"] = round(float(pl) / basis * 100, 2)
            s["profit_rate_basis"] = "deployed"
    return strategies


def annotate_bot_staleness(strategies, now):
    """Add `is_stale` + `stale_weekday_hours` to each bot card. PURE-ish.

    `is_stale` is None when the age is unknown — the front end must be able
    to tell "no answer" from "fine", because those call for different
    behaviour and only one of them is safe to ignore.

    The hands-on sleeve is skipped: it has no run schedule at all, and its
    freshness is the Toss snapshot's, reported on accounts.toss.
    """
    for s in strategies or []:
        if not isinstance(s, dict) or s.get("id") == "manual":
            continue
        sid = s.get("id")
        raw = s.get("stale_hours")
        eff = weekday_age_hours(
            raw, now, skip_weekends=sid not in BOT_TRADES_WEEKENDS)
        s["stale_weekday_hours"] = None if eff is None else round(eff, 2)
        if eff is None:
            s["is_stale"] = None
            continue
        limit = BOT_CADENCE_HOURS.get(sid, BOT_CADENCE_DEFAULT) * BOT_STALE_MULTIPLE
        s["is_stale"] = eff > limit
        s["stale_limit_hours"] = round(limit, 2)
    return strategies


def _days_since(stamp, now):
    """Whole+fractional days between a bot's `last_run` and now, or None.

    `last_run` arrives in whatever shape the bot wrote — "2026-08-17",
    "2026-08-17 22:30 KST", an ISO timestamp — so an unparseable value must
    return None (unknown), never 0 (fresh). Reporting "fresh" on a stamp we
    could not read is the same silent lie the age check is here to remove.
    """
    if not stamp:
        return None
    text = str(stamp).strip().replace("KST", "").replace("T", " ").strip()
    when = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            when = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if when is None:
        # Every shape above leads with the date; an ISO stamp carrying
        # microseconds matches none of them but still yields a usable day.
        try:
            when = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return round((now.replace(tzinfo=None) - when).total_seconds() / 86400.0, 2)


def load_toss_account(path=None, now=None, max_age_hours=TOSS_MAX_AGE_HOURS, fx_rate=None):
    """Read the Mac-side Toss snapshot into the `accounts.toss` contract.

    Returns ALWAYS — missing file, unreadable JSON, bad schema and stale data all
    resolve to a zeroed, honestly-flagged dict rather than an exception or a
    missing key. A publish must never fail because the Mac was asleep, and the
    hero must never quietly drop the Toss sleeve from the total: a zero with
    stale=true is a tile the front-end greys out, a missing key is a silent lie.

    `as_of` is deliberately None whenever stale=true (the healthcheck asserts
    "as_of fresher than max_age only when stale is false"); the snapshot's own
    timestamp is preserved under `last_seen_at` for the UI.

    When `fx_rate` is given and the snapshot carries native currency buckets, the
    USD sleeve is re-converted at the dashboard's own KIS rate so the Toss tile
    can't disagree with the KIS tile over FX. Otherwise the snapshot's own
    fallback rate stands.
    """
    def _empty(note, last_seen=None):
        # usable=False means "there is no position to show", which is a
        # different thing from "the position we have is old" — see the aged
        # branch below. Callers gate the holdings rows on `usable`, not `stale`.
        return {"total_krw": 0.0, "cash_krw": 0.0, "holdings_krw": 0.0,
                "as_of": None, "stale": True, "usable": False, "note": note,
                "last_seen_at": last_seen}

    path = path or TOSS_SNAPSHOT_FILE
    if not os.path.exists(path):
        return _empty("no snapshot at {} (Mac job never ran?)".format(path))
    try:
        with open(path) as f:
            snap = json.load(f)
    except Exception as e:
        return _empty("unreadable snapshot: {}".format(e))
    if not isinstance(snap, dict):
        return _empty("snapshot is not an object")

    raw_as_of = snap.get("as_of")
    try:
        stamp = datetime.fromisoformat(str(raw_as_of).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return _empty("unparseable as_of {!r}".format(raw_as_of))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=KST)          # snapshots are written in KST
    now = now or datetime.now(KST)
    age_h = (now - stamp).total_seconds() / 3600.0
    if age_h < -1:
        # A stamp from the future means a broken clock somewhere, which makes
        # the numbers themselves untrustworthy — unlike mere age.
        return _empty("as_of {} is {:.1f}h in the future — clock skew".format(
            raw_as_of, -age_h), last_seen=raw_as_of)

    native = snap.get("native") or {}
    holdings_krw = float(snap.get("holdings_krw") or 0)
    cash_krw = float(snap.get("cash_krw") or 0)
    if fx_rate and fx_rate > 0 and native:
        # Re-price the USD sleeve at the dashboard's rate.
        holdings_krw = float(native.get("holdings_krw") or 0) + float(native.get("holdings_usd") or 0) * fx_rate
        cash_krw = float(native.get("cash_krw") or 0) + float(native.get("cash_usd") or 0) * fx_rate
    total_krw = holdings_krw + cash_krw
    # An OLD snapshot is flagged but KEPT. Zeroing it (what this used to do)
    # deleted ~W190M from the hero the moment the Mac missed a run — on
    # 2026-08-17 the hands-on sleeve fell from W193M to W12.6M and the split
    # went with it, which reads as "Jae sold everything", not "the laptop was
    # off". The last known position is the honest thing to show; `stale` and
    # `last_seen_at` are how the tile says so. Only a snapshot we cannot read at
    # all (above) has nothing to show and is zeroed.
    aged = age_h > max_age_hours
    out = {
        "total_krw": round(total_krw, 2),
        "cash_krw": round(cash_krw, 2),
        "holdings_krw": round(holdings_krw, 2),
        # Same contract as the other sleeves: as_of is None whenever stale is
        # true, and the real stamp moves to last_seen_at.
        "as_of": None if aged else raw_as_of,
        "stale": aged,
        "usable": True,
        "age_hours": round(age_h, 2),
        "holdings_count": len(snap.get("holdings") or []),
        "source": snap.get("source") or "unknown",
    }
    if aged:
        out["last_seen_at"] = raw_as_of
        out["note"] = ("snapshot {:.1f}h old (limit {}h) — Mac job has not run; "
                       "showing the last known position".format(age_h, max_age_hours))
    return out


def get_portfolio(totals=None):
    """Query KIS API for real account totals + Upbit.

    `totals` may be passed in to avoid a second KIS round-trip; if None,
    we call get_account_totals() ourselves.
    """
    try:
        if totals is None:
            from query_account_total import get_account_totals
            totals = get_account_totals()

        kr = totals["kr"]
        us = totals["us"]
        krw_cash = totals["krw_cash"]                                # prvs_rcdl_excc_amt — settled cash incl. today's KR pending
        usd_cash = totals["usd_cash"]                                # USD cash in USD
        unsettled_us_sell_krw = totals.get("unsettled_us_sell_krw", 0)  # US T+3 pending in KRW

        # Exchange rate from KIS
        exchange_rate = 1380
        try:
            import kis_auth as ka
            import inquire_psamount
            ka.auth(svr="prod")
            acct = ka.getTREnv()
            df = inquire_psamount.inquire_psamount(
                cano=acct.my_acct, acnt_prdt_cd=acct.my_prod,
                ovrs_excg_cd="NASD", item_cd="AAPL",
                ovrs_ord_unpr="0", env_dv="real"
            )
            if df is not None and not df.empty:
                rate = float(df.iloc[0].get("exrt", 0))
                if rate > 0:
                    exchange_rate = rate
        except Exception:
            pass

        # Upbit (BTC) — separate account
        upbit_equity = 0
        vb_state_file = os.path.join(TRADING_DIR, "upbit_vb_strategy_state.json")
        if os.path.exists(vb_state_file):
            with open(vb_state_file) as f:
                vb = json.load(f)
            eq = vb.get("equity_history", [])
            if eq:
                upbit_equity = eq[-1][1]

        # Total KIS assets = KR stocks + KRW cash (incl. pending KR settlements)
        #                  + US stocks (KRW) + USD cash (KRW)
        #                  + unsettled US sales (KRW, already converted by KIS)
        # The two "settlement-in-flight" terms (KR pending via prvs_rcdl_excc_amt,
        # US pending via ustl_sll_amt_smtl) were both being dropped — proceeds
        # had left stock_value but hadn't landed in cash yet — so the dashboard
        # was understating total assets by the day's settling-out trades.
        kis_total = (kr["stock_value"] + krw_cash
                     + us["stock_value"] * exchange_rate
                     + usd_cash * exchange_rate
                     + unsettled_us_sell_krw)

        total_value = kis_total + upbit_equity

        # Original deposit
        original_deposit = 75714217  # fallback
        if os.path.exists(DEPOSITS_FILE):
            with open(DEPOSITS_FILE) as f:
                dep = json.load(f)
                original_deposit = dep.get("kis_total_original_krw", 65726902) + dep.get("upbit_original_krw", 9987315)

        total_profit = total_value - original_deposit
        total_pct = (total_profit / original_deposit * 100) if original_deposit > 0 else 0

        return {
            "total_value_krw": round(total_value),
            "original_deposit_krw": original_deposit,
            "total_profit_krw": round(total_profit),
            "total_profit_pct": round(total_pct, 2),
            "cash_krw": round(krw_cash),
            "cash_usd": round(usd_cash, 2),
            "unsettled_us_sell_krw": round(unsettled_us_sell_krw),
            "upbit_krw": round(upbit_equity),
            "exchange_rate": exchange_rate,
        }
    except Exception as e:
        print("  Portfolio query failed: {}".format(e))
        import traceback
        traceback.print_exc()
        return {}


def get_account_totals_resilient(retries=4, delay=2.0, fetch=None, sleep=None):
    """get_account_totals(), retried when the KR holdings come back empty.

    The KIS balance query intermittently returns an empty page with no
    exception (the same dropout that zeroes korea_etf and the other KR-funded
    bots). The KR account in this portfolio is never legitimately empty, so an
    empty kr.holdings list is a transient miss — re-query before publishing so
    recovery, mark-to-market, and the price index all see the real positions.
    Returns the first response with non-empty KR holdings, else the last
    response after `retries` attempts.
    """
    import time as _t
    if sleep is None:
        sleep = _t.sleep
    if fetch is None:
        from query_account_total import get_account_totals
        fetch = get_account_totals
    totals = None
    for attempt in range(retries):
        totals = fetch()
        kr = ((totals or {}).get("kr") or {}).get("holdings") or []
        if kr:
            if attempt:
                print("  KR account recovered after {} empty attempt(s)".format(attempt))
            return totals
        if attempt < retries - 1:
            sleep(delay)
    print("  WARNING: KR account holdings empty after {} attempts (KR bots may show 0)".format(retries))
    return totals


def recover_missing_bot_holdings(api_data, totals):
    """Restore a bot's position from the authoritative account balance when its
    own result file reported zero holdings.

    Korea ETF Momentum scopes its KIS balance query to a single ticker
    (`get_holdings([state_target])`) and that call intermittently returns an
    empty page with NO exception — see the bot log flickering
    "Current holding: 139220 (1263 shares)" -> "None (0 shares)" -> back again
    on days with no rebalance and an unchanged target_qty. On a dropout the bot
    writes holdings=[] / total_profit=0, which zeroes the dashboard card even
    though the position is live.

    The account-level totals query (`get_account_totals`) is a SEPARATE,
    full-account call made at publish time; when it sees the bot's target
    ticker we recover the position from it. 139220 (TIGER 200 Construction) is
    KEM-exclusive (not in any other bot's universe), so the full account row is
    attributable to this bot. Guarded so we never recover a ticker another bot
    already claims (avoids double-counting), and only fires when the bot itself
    reported nothing. mark_to_market_strategies runs afterward and recomputes
    profit / total_pl_ytd / profit_rate from the restored holdings.
    """
    if not totals:
        return 0
    kr_acct = {h.get("ticker"): h for h in (totals.get("kr") or {}).get("holdings", []) or []
               if (h.get("quantity") or 0) > 0}
    # Tickers other bots already report holding — don't poach these.
    claimed = set()
    for s in api_data.get("strategies", []) or []:
        if s.get("id") == "korea_etf":
            continue
        for leg in (s, s.get("kr") or {}, s.get("us") or {}):
            for h in (leg.get("holdings") or []):
                if (h.get("quantity") or 0) > 0:
                    claimed.add(h.get("ticker"))

    recovered = 0
    for s in api_data.get("strategies", []) or []:
        if s.get("id") != "korea_etf":
            continue
        if any((h.get("quantity") or 0) > 0 for h in (s.get("holdings") or [])):
            continue  # bot reported a live position; nothing to recover
        tkr = (s.get("extra") or {}).get("target_ticker")
        acct = kr_acct.get(tkr)
        if not tkr or tkr == "CASH" or not acct or tkr in claimed:
            continue
        qty = acct["quantity"]
        val = float(acct.get("value") or 0)
        profit = float(acct.get("profit") or 0)
        cur = val / qty if qty else 0
        avg = (val - profit) / qty if qty else 0
        s["holdings"] = [{
            "ticker": tkr,
            "quantity": qty,
            "value": round(val, 2),
            "avg_price": round(avg, 4),
            "current_price": round(cur, 2),
            "profit": round(profit, 2),
            "profit_rate": acct.get("profit_rate", 0),
            "currency": "KRW",
            "recovered_from_account": True,
        }]
        s["value"] = round(val, 2)
        s["cost_basis"] = round(val - profit, 2)
        s["profit"] = round(profit, 2)
        recovered += 1
    return recovered


def reconcile_underreported_bot_holdings(api_data, totals):
    """Top a bot's holding up to the quantity the ACCOUNT reports.

    A bot writes its result file within a second of placing its order — before
    the KIS balance reflects the fill — so the file carries the PRE-trade
    quantity while the account already carries the post-trade one. On
    2026-08-17 quant40 bought 6 SPY at 22:30:08 and wrote holdings at
    22:30:09 (card said 7, account held 13); jd_strategy bought 19 NVDA at
    22:30:44 and wrote at 22:30:44 (card said 22, account held 41).

    Those 25 shares (W12.46M) belonged to no bucket at all: build_manual_sleeve
    deliberately refuses to call a bot-traded ticker "hands-on" (SPY/NVDA were
    once misfiled as Jae's own), and nothing else claimed them — so they sat in
    `totals.reconciliation_gap_krw` at 2.3% and the hero told him 2.3% of his
    money was unaccounted for when in fact his own bots had just bought it.

    Only a ticker claimed by EXACTLY ONE kis-account bot is topped up. When two
    cards report the same name the excess is genuinely ambiguous, and guessing
    an owner would put real money on the wrong card — that case stays in the
    gap, which is what the gap is for. The broker's blended `avg_price` is
    adopted along with the quantity, because the cost basis across the bot's
    old and new lots is the broker's number, not the bot's. Runs BEFORE
    mark_to_market_strategies, which then re-prices the topped-up quantity.
    """
    if not totals:
        return []

    acct = {}
    for leg in ("kr", "us"):
        for h in ((totals or {}).get(leg) or {}).get("holdings", []) or []:
            ticker = h.get("ticker")
            if ticker and float(h.get("quantity") or 0) > 0 and ticker not in acct:
                acct[ticker] = h                  # KR/US never collide; first wins

    # ticker -> [(card, row)], one entry per CARD. Within a card the same ticker
    # can appear twice (hybrid_vb's top-level `holdings` IS its US leg), so the
    # biggest row per card wins rather than counting as a second claimant.
    holders = {}
    for s in api_data.get("strategies", []) or []:
        if not isinstance(s, dict) or s.get("id") == "manual":
            continue
        if s.get("account") not in (None, "", "kis"):
            continue                              # NMF2 is Toss; its 6-digit
                                                  # tickers would match KIS names
        per = {}
        for leg in (s, s.get("kr") or {}, s.get("us") or {}):
            for h in (leg.get("holdings") or []):
                if not isinstance(h, dict):
                    continue
                ticker = h.get("ticker")
                qty = float(h.get("quantity") or h.get("qty") or 0)
                if not ticker or qty <= 0:
                    continue
                prev = per.get(ticker)
                if prev is None or qty > float(
                        prev.get("quantity") or prev.get("qty") or 0):
                    per[ticker] = h
        for ticker, h in per.items():
            holders.setdefault(ticker, []).append((s, h))

    topped = []
    for ticker, claimants in holders.items():
        if len(claimants) != 1:
            continue                              # ambiguous — leave in the gap
        s, h = claimants[0]
        row = acct.get(ticker)
        if not row:
            continue
        acct_qty = float(row.get("quantity") or 0)
        bot_qty = float(h.get("quantity") or h.get("qty") or 0)
        if acct_qty <= bot_qty + 1e-9:
            continue                              # nothing missing

        value = float(row.get("value") or 0)
        profit = float(row.get("profit") or 0)
        h["quantity"] = acct_qty
        if "qty" in h:
            h["qty"] = acct_qty
        h["avg_price"] = round((value - profit) / acct_qty, 6)
        h["value"] = round(value, 2)
        h["profit"] = round(profit, 2)
        h["bot_reported_qty"] = bot_qty
        h["topped_up_from_account"] = True
        topped.append((s.get("id"), ticker, bot_qty, acct_qty))

    # The card's own total must follow its holdings, or the tile shows the
    # pre-trade value beside the post-trade share count.
    for sid in {t[0] for t in topped}:
        for s in api_data.get("strategies", []) or []:
            if s.get("id") != sid or s.get("currency") == "MULTI":
                continue
            rows = s.get("holdings") or []
            s["value"] = round(sum(float(r.get("value") or 0) for r in rows), 2)
            s["profit"] = round(sum(float(r.get("profit") or 0) for r in rows), 2)
            s["cost_basis"] = round(s["value"] - s["profit"], 2)

    return topped


def _build_kis_price_index(totals):
    """ticker -> (current_price_native, currency) from KIS positions.

    KIS's `value` is mark-to-market (qty * current_price), so we recover
    the price as value/qty. This is the live price KIS used at the moment
    the inquire_balance call returned — typically <1s old.
    """
    idx = {}
    for h in (totals.get("kr") or {}).get("holdings", []) or []:
        qty = h.get("quantity") or 0
        val = h.get("value") or 0
        if qty > 0:
            idx[h["ticker"]] = (val / qty, "KRW")
    for h in (totals.get("us") or {}).get("holdings", []) or []:
        qty = h.get("quantity") or 0
        val = h.get("value") or 0
        if qty > 0:
            idx[h["ticker"]] = (val / qty, "USD")
    return idx


def _mark_holdings(holdings, price_idx, skipped):
    """Re-price each holding in-place using KIS current prices. Returns the
    bot's new unrealized P&L (sum of per-holding profits).

    Updates value, profit, current_price, AND profit_rate so the dashboard's
    per-holding row (price + %) shows the live mark, not the bot's morning
    snapshot. Without the profit_rate update the HTML renderer falls through
    to a stale value from the bot's result file — and when that stale value
    happens to be 0 (no movement at write time) the % is omitted entirely
    because `hld.profit_rate ? ...` treats 0 as falsy. See incident
    2026-05-03 (Hybrid VB KR holdings showed pl but no % for 091170).
    """
    new_unrealized = 0.0
    for h in holdings or []:
        ticker = h.get("ticker")
        qty = h.get("quantity") or 0
        if not ticker or qty <= 0 or ticker not in price_idx:
            if ticker:
                skipped.add(ticker)
            new_unrealized += h.get("profit") or 0
            continue
        cur_price, _ = price_idx[ticker]
        h["value"] = round(cur_price * qty, 2)
        avg = h.get("avg_price")
        if avg is not None and avg > 0:
            h["profit"] = round((cur_price - avg) * qty, 2)
            h["profit_rate"] = round((cur_price - avg) / avg * 100, 2)
        h["current_price"] = cur_price
        h["live_price"] = cur_price
        h["mark_to_market"] = True
        new_unrealized += h.get("profit") or 0
    return new_unrealized


def mark_to_market_strategies(api_data, totals):
    """Mutate api_data so each bot's holdings reflect current KIS prices.

    Recomputes unrealized_profit / profit / total_pl_ytd from the updated
    holdings. Realized fields (realized_profit_ytd) are NOT touched —
    those are YTD-from-2026-01-01 by design and come from the aggregator.

    btc_vb is skipped because dashboard_server already does live
    mark-to-market via pyupbit when a position is held.
    """
    price_idx = _build_kis_price_index(totals)
    if not price_idx:
        return {"marked": 0, "skipped": [], "note": "no KIS prices"}

    marked = 0
    skipped = set()

    for s in api_data.get("strategies", []) or []:
        sid = s.get("id")
        if sid == "btc_vb":
            continue
        if sid == "hybrid_vb":
            for leg in ("kr", "us"):
                ld = s.get(leg) or {}
                if not ld.get("holdings"):
                    continue
                before = sum((h.get("profit") or 0) for h in ld["holdings"])
                new_un = _mark_holdings(ld["holdings"], price_idx, skipped)
                marked += sum(1 for h in ld["holdings"] if h.get("mark_to_market"))
                ld["unrealized_profit"] = round(new_un, 2)
                ld["profit"] = ld["unrealized_profit"]
                real = ld.get("realized_profit_ytd") or 0
                ld["total_pl_ytd"] = round(real + new_un, 2)
                ld["unrealized_drift_at_mtm"] = round(new_un - before, 2)
                # Re-sum value and re-derive the rate from the marked holdings.
                # _mark_holdings refreshes each holding's value/profit but the
                # leg-level value and profit_rate are otherwise left at their
                # stale pre-mark figures — which makes value-cost != profit and
                # flips KR Profit % positive against a negative Total P/L.
                ld["value"] = round(sum((h.get("value") or 0) for h in ld["holdings"]), 2)
                leg_budget = s.get("budget_kr") if leg == "kr" else s.get("budget_us")
                if leg_budget and leg_budget > 0:
                    ld["profit_rate_ytd_pct"] = round(ld["total_pl_ytd"] / leg_budget * 100, 2)
                s[leg] = ld
            continue

        if not s.get("holdings"):
            continue
        before = sum((h.get("profit") or 0) for h in s["holdings"])
        new_un = _mark_holdings(s["holdings"], price_idx, skipped)
        marked += sum(1 for h in s["holdings"] if h.get("mark_to_market"))
        s["unrealized_profit"] = round(new_un, 2)
        s["profit"] = s["unrealized_profit"]
        # Re-sum the card total from the marked holdings, for the same reason
        # the hybrid legs below do: _mark_holdings refreshes each row but leaves
        # the card-level `value` at its pre-mark figure, so the tile showed a
        # value that no longer matched the rows under it (quant40: card $5,433
        # against holdings worth $5,384).
        s["value"] = round(sum((h.get("value") or 0) for h in s["holdings"]), 2)
        s["cost_basis"] = round(s["value"] - new_un, 2)
        real = s.get("realized_profit_ytd") or 0
        s["total_pl_ytd"] = round(real + new_un, 2)
        budget = s.get("budget") or 0
        if budget and budget > 0:
            s["profit_rate_ytd_pct"] = round(s["total_pl_ytd"] / budget * 100, 2)
        s["unrealized_drift_at_mtm"] = round(new_un - before, 2)

    return {"marked": marked, "skipped": sorted(skipped)}


# ═════════════════════════════════════════════════════════════════════════════
# Hands-on / Manual sleeve  +  accounts / totals contract
#
# Jae runs most of his money by hand, in front of the bots. Everything below
# exists so the published dashboard answers "what is my WHOLE investment
# status", not just "what did the bots do". The split is built by PARTITIONING
# the broker account rows — every share is either bot-claimed or hands-on, and
# every won is either in a position or in cash — so bots + manual + cash is the
# account total by construction, and `reconciliation_gap_krw` is a real alarm
# rather than a rounding readout.
#
# All the builders below are pure functions over already-fetched payloads so
# they unit-test with fixtures (tests/test_dashboard/test_manual_sleeve.py).
# ═════════════════════════════════════════════════════════════════════════════

def kis_holdings_map(totals):
    """{ticker: {qty, value_native, currency, name, profit}} for the WHOLE KIS account.

    PURE — takes the already-fetched get_account_totals() payload. KIS's `value`
    is mark-to-market (qty x current price), so slicing a position by quantity
    is a straight pro-rate of both value and profit.
    """
    out = {}
    for leg, cur in (("kr", "KRW"), ("us", "USD")):
        for h in ((totals or {}).get(leg) or {}).get("holdings", []) or []:
            ticker = h.get("ticker")
            qty = float(h.get("quantity") or 0)
            if not ticker or qty <= 0 or ticker in out:
                continue                      # KR/US tickers never collide; first wins
            out[ticker] = {
                "qty": qty,
                "value_native": float(h.get("value") or 0),
                "currency": cur,
                "name": h.get("name") or ticker,
                "profit": float(h.get("profit") or 0),
            }
    return out


def collect_bot_claims(strategies, sources=("holdings", "open_positions")):
    """{ticker: qty} claimed by the bots, read off the dashboard's own cards. PURE.

    Three claim sources per card, because a bot's *reported* holdings can drop
    out while the position is live — the same intermittent KIS balance-page miss
    that zeroes korea_etf. On 2026-08-16 Hybrid VB's KR leg reported no holdings
    at all while it actually held 091170/132030/144600 (~W9.35M):
      1. card `holdings`
      2. `kr` / `us` leg holdings (hybrid_vb)
      3. `open_positions` — the bot's own position ledger, already published by
         dashboard_server; authoritative exactly when 1 and 2 have dropped out.
    Without source 3 that W9.35M is reported to Jae as HIS hands-on money.

    Within one card the sources overlap (hybrid's top-level `holdings` IS its US
    leg), so we take the MAX per ticker rather than summing. Across cards we sum
    — and the caller caps the sum at the account quantity, which absorbs two
    cards claiming the same name (korea_etf's account-recovered 132030 vs
    hybrid_vb's live open position in it).

    `sources` narrows which of the three count, so the caller can measure the
    dropout itself: claims(all) - claims(("holdings",)) is exactly the value of
    live bot positions their own cards forgot to report.

    Cards carrying an explicit non-KIS `account` (NMF2's "toss") are skipped: the
    result is matched against the KIS holdings map, and Toss and KIS share
    Korea's 6-digit ticker space, so an unguarded match would hand the bot KIS
    shares it does not own — inflating `bots_krw` and opening a reconciliation
    gap. Those cards are attributed against their own account instead.
    """
    claims = {}
    for s in strategies or []:
        if not isinstance(s, dict) or s.get("id") == "manual":
            continue
        if s.get("account") not in (None, "", "kis"):
            continue
        per = {}

        def _take_holdings(rows):
            for h in rows or []:
                if not isinstance(h, dict):
                    continue
                ticker = h.get("ticker")
                qty = float(h.get("quantity") or h.get("qty") or 0)
                if ticker and qty > 0:
                    per[ticker] = max(per.get(ticker, 0.0), qty)

        def _take_positions(book):
            for ticker, pos in (book or {}).items():
                if not isinstance(pos, dict):
                    continue
                if "shares" in pos or "quantity" in pos:
                    qty = float(pos.get("shares") or pos.get("quantity") or 0)
                    if ticker and qty > 0:
                        per[ticker] = max(per.get(ticker, 0.0), qty)
                else:
                    _take_positions(pos)          # {"kr": {...}, "us": {...}}

        if "holdings" in sources:
            _take_holdings(s.get("holdings"))
            for leg in ("kr", "us"):
                leg_data = s.get(leg)
                if isinstance(leg_data, dict):
                    _take_holdings(leg_data.get("holdings"))
        if "open_positions" in sources and isinstance(s.get("open_positions"), dict):
            _take_positions(s["open_positions"])

        for ticker, qty in per.items():
            claims[ticker] = claims.get(ticker, 0.0) + qty
    return claims


def _attributed_krw(kis_holdings, claims, fx):
    """KRW value of the account shares `claims` covers (capped at what is held)."""
    total = 0.0
    for ticker, acct in (kis_holdings or {}).items():
        qty = float(acct.get("qty") or 0)
        claimed = min(float(claims.get(ticker, 0.0)), qty)
        if qty <= 0 or claimed <= 0:
            continue
        value_native = float(acct.get("value_native") or 0) * (claimed / qty)
        total += value_native if acct.get("currency") == "KRW" else value_native * fx
    return total


def load_nmf2_ledger(path=None):
    """The NMF2 bot's ledger, READ-ONLY. Returns {} on any problem, never raises.

    Degrading to {} is deliberate: a publish must not fail — and must not shift
    money between sleeves — because the bot rotated its state file mid-read. An
    empty ledger yields no nmf2 card, and every Toss share falls back to the
    hands-on sleeve exactly as it did before this bot had a card.
    """
    try:
        with open(path or NMF2_LEDGER_FILE) as f:
            led = json.load(f)
    except Exception:
        return {}
    if not isinstance(led, dict) or not isinstance(led.get("positions"), dict):
        return {}
    return led


def build_nmf2_card(ledger, toss_holdings, fx_rate=None):
    """The NMF2 bot as a first-class strategy card. PURE — no I/O.

    The ledger knows WHAT the bot owns (qty + avg_price); it does not know what
    those shares are worth today. The Mac-side Toss snapshot does. So the card is
    the intersection: for each ledger symbol, claim `min(ledger_qty, account_qty)`
    shares and value them by pro-rating the account's own mark.

    Two rules keep `totals.reconciliation_gap_krw` at zero:
      1. Never value more shares than the account reports — the same pro-rate the
         KIS sleeve uses, so the bot and the hands-on sleeve split one number.
      2. A ledger symbol absent from the snapshot contributes ZERO, not
         qty x avg_price. Cost basis is not market value, and money the account
         does not report cannot be added to a total built from account rows. Such
         symbols are named in `extra.unmatched_symbols` rather than swallowed.

    Ledger cash is reported for context but NOT added to `value` — Toss cash is
    already whole inside `totals.cash_krw`, and counting it here would book it
    twice.
    """
    positions = (ledger or {}).get("positions") or {}
    if not positions:
        return None
    fx = float(fx_rate or 0)
    by_symbol = {}
    for h in toss_holdings or []:
        if isinstance(h, dict):
            sym = h.get("symbol") or h.get("ticker")
            if sym and sym not in by_symbol:
                by_symbol[sym] = h

    rows, unmatched = [], []
    value_krw = cost_krw = 0.0
    for symbol, pos in sorted(positions.items()):
        if not isinstance(pos, dict):
            continue
        led_qty = float(pos.get("qty") or 0)
        if led_qty <= 0:
            continue
        acct = by_symbol.get(symbol)
        acct_qty = float((acct or {}).get("qty") or (acct or {}).get("quantity") or 0)
        if not acct or acct_qty <= 0:
            unmatched.append(symbol)
            continue
        claimed = min(led_qty, acct_qty)
        share = claimed / acct_qty
        cur = acct.get("currency") or "KRW"
        acct_native = float(acct.get("value_native") or acct.get("value_krw") or 0)
        native = acct_native * share
        krw = native if cur == "KRW" else (native * fx if fx > 0
                                           else float(acct.get("value_krw") or 0) * share)
        cost = claimed * float(pos.get("avg_price") or 0)
        value_krw += krw
        cost_krw += cost
        rows.append({
            "ticker": symbol,
            "name": acct.get("name") or symbol,
            "qty": round(claimed, 6),
            "quantity": round(claimed, 6),
            "currency": cur,
            "avg_price": round(float(pos.get("avg_price") or 0), 4),
            "current_price": round(native / claimed, 4) if claimed else 0.0,
            "value": round(native, 2),
            "value_krw": round(krw, 2),
            "profit": round(krw - cost, 2),
            "profit_rate": round((krw - cost) / cost * 100, 2) if cost > 0 else 0.0,
            "source": "toss",
            "ledger_qty": round(led_qty, 6),
        })

    rows.sort(key=lambda r: r.get("value_krw", 0), reverse=True)
    unrealized = value_krw - cost_krw
    budget = float((ledger or {}).get("budget_krw") or 0)
    return {
        "id": "nmf2",
        "name": "NMF2 (신마법공식 2.0)",
        "currency": "KRW",
        "mode": "live",
        # Marks this card as trading a NON-KIS account. collect_bot_claims reads
        # it to keep these KRX codes away from the KIS holdings map (Toss and KIS
        # share Korea's 6-digit ticker space, so an unguarded match would credit
        # the bot with KIS shares it does not own and open a reconciliation gap).
        "account": "toss",
        "value": round(value_krw, 2),
        "cost_basis": round(cost_krw, 2),
        "budget": round(budget, 2),
        "unrealized_profit": round(unrealized, 2),
        # No realized figure: the ledger keeps positions, not a closed-trade
        # book, and this sleeve has not sold since inception. 0.0 is the honest
        # value today, and total_pl_ytd stays equal to unrealized because of it.
        "realized_profit_ytd": 0.0,
        "realized_trades": 0,
        "total_pl_ytd": round(unrealized, 2),
        "profit_rate_ytd_pct": round(unrealized / cost_krw * 100, 2) if cost_krw > 0 else 0.0,
        "holdings": rows,
        "last_run": (ledger or {}).get("updated"),
        "extra": {
            "note": "신마법공식 2.0 + 계절성 — real-money KRW sleeve inside the Toss account, "
                    "run from Jae's Mac. Positions come from the bot's ledger; prices from the Toss snapshot.",
            "ticker_count": len(rows),
            "ledger_position_count": len(positions),
            "cash_krw": round(float((ledger or {}).get("cash_krw") or 0), 2),
            "budget_krw": round(budget, 2),
            "deployed_pct": round(cost_krw / budget * 100, 2) if budget > 0 else 0.0,
            "unmatched_symbols": unmatched,
            "ledger_updated": (ledger or {}).get("updated"),
            "caveat": "Value is marked from the Toss snapshot, so it is only as fresh as that "
                      "snapshot. Ledger cash is shown but excluded from `value` — Toss cash is "
                      "counted once, in totals.cash_krw.",
        },
    }


def build_manual_sleeve(all_holdings, strategies, fx_rate, toss_holdings=None,
                        toss_claims=None):
    """The Hands-on / Manual sleeve, as a strategy-shaped card. PURE — no I/O.

    manual_qty = account_qty - SUM(bot-claimed qty), per ticker, valued at the
    mark the account itself reports and converted to KRW at the dashboard's FX
    rate. Zero and negative diffs are dropped (a bot claiming more shares than
    the account holds is a bot-side bug, never negative hands-on money).

    Cash is deliberately NOT in the sleeve — it is totals.cash_krw.

    `toss_holdings` (rows from the Mac-side Toss snapshot) get the SAME
    subtraction, via `toss_claims` ({symbol: qty} owned by Toss-account bots —
    today that is NMF2's ledger). Whatever the bots do not claim is hands-on.
    Including Toss here is what makes Task 5's reconciliation meaningful:
    otherwise ~W187M of real money belongs to no bucket at all and the gap check
    fires on every publish. And because bot and hands-on shares are carved out of
    the same account row, the two can never both count the same share.
    """
    fx = float(fx_rate or 0)
    claims = collect_bot_claims(strategies)
    rows = []
    kis_krw = 0.0
    kis_unrealized_krw = 0.0

    # ── KIS contributes NOTHING to this sleeve. ──────────────────────────────
    # KIS is the bots' account; Jae's own buying happens in Toss. So a KIS row
    # that no card claims is a bot whose result file dropped it — never money he
    # bought by hand. Deriving "his" from "what the bots forgot to mention" made
    # the sleeve hostage to every bot reporting perfectly, and it misfired every
    # time it mattered:
    #   2026-08-17  SPY 6 + NVDA 19 (W12.6M) — result files written one second
    #               after the fill, before KIS showed it
    #   2026-08-19  069500/305720/364690 (W4.4M) — hybrid_vb_kr published
    #               holdings:[] and open_positions:null while holding all three
    # Both were his bots' positions, shown to him as his own hand-picked stock.
    # The unclaimed remainder is recorded below for bot-ledger triage and is
    # counted under bots in build_totals, so no won goes missing.
    kis_unclaimed_krw = 0.0
    kis_unclaimed = {}
    for ticker, acct in sorted((all_holdings or {}).items()):
        qty = float(acct.get("qty") or 0)
        if qty <= 0:
            continue
        unclaimed = qty - min(float(claims.get(ticker, 0.0)), qty)
        if unclaimed <= 1e-9:
            continue
        cur_r = acct.get("currency") or "KRW"
        v = float(acct.get("value_native") or 0) * (unclaimed / qty)
        kis_unclaimed_krw += v if cur_r == "KRW" else v * fx
        kis_unclaimed[ticker] = round(unclaimed, 6)

    toss_krw = 0.0
    toss_rows = 0
    toss_bot_claimed_krw = 0.0
    toss_cost_krw = 0.0
    toss_pl_krw = 0.0
    for h in toss_holdings or []:
        if not isinstance(h, dict):
            continue
        acct_qty = float(h.get("qty") or h.get("quantity") or 0)
        cur = h.get("currency") or "KRW"
        acct_native = float(h.get("value_native") or h.get("value_krw") or 0)
        # Re-price at the DASHBOARD's FX rate, not the snapshot's own fallback
        # rate. accounts.toss.total_krw is already re-priced that way; leaving
        # the sleeve on the snapshot's rate opens a reconciliation gap the size
        # of the FX drift (W286k at a 3-won difference on a $98k US sleeve).
        if fx > 0:
            acct_krw = acct_native if cur == "KRW" else acct_native * fx
        else:
            acct_krw = float(h.get("value_krw") or acct_native)
        if acct_qty <= 0 or acct_krw <= 0:
            continue
        ticker = h.get("symbol") or h.get("ticker") or "?"
        claimed = min(float((toss_claims or {}).get(ticker, 0.0)), acct_qty)
        manual_qty = acct_qty - claimed
        toss_bot_claimed_krw += acct_krw * (claimed / acct_qty)
        if manual_qty <= 1e-9:
            continue
        share = manual_qty / acct_qty
        value_native = acct_native * share
        value_krw = acct_krw * share
        # 매입원가·평가손익도 같은 비율로 안분한다.
        #
        # 원화 원가는 스냅샷이 계산해 준 cost_krw 를 **그대로** 쓴다. 미국 종목은
        # 매입 시점 환율로 환산돼 있어서, 여기서 cost_native 에 오늘 환율을 곱하면
        # 환율 효과가 지워진다 — 알파벳A 가 앱에서 -3.5% 인데 +1.0% 로 보이던 원인.
        cost_native = float(h.get("cost_native") or 0) * share
        cost_krw_h = float(h.get("cost_krw") or 0) * share
        if not cost_krw_h and cost_native:          # 구버전 스냅샷 폴백
            cost_krw_h = cost_native * (1.0 if cur == "KRW" else (fx if fx > 0 else 0.0))
        # 원화 손익 = 원화 평가 - 원화 원가 (달러 손익을 환산하는 것이 아니다).
        pl_krw_h = value_krw - cost_krw_h
        pl_native = float(h.get("pl_native") or 0) * share
        toss_krw += value_krw
        toss_cost_krw += cost_krw_h
        toss_pl_krw += pl_krw_h
        toss_rows += 1
        rows.append({
            "ticker": ticker,
            "name": h.get("name") or ticker,
            "qty": round(manual_qty, 6),
            "quantity": round(manual_qty, 6),
            "currency": cur,
            "current_price": round(value_native / manual_qty, 4) if manual_qty else 0.0,
            "value": round(value_native, 2),
            "value_krw": round(value_krw, 2),
            "cost_krw": round(cost_krw_h, 2),
            "pl_krw": round(pl_krw_h, 2),
            "profit": round(pl_native, 2),
            # 수익률도 원화 기준 — 토스 앱이 보여주는 것과 같은 기준이다.
            "profit_rate": (round(pl_krw_h / cost_krw_h * 100, 2)
                            if cost_krw_h else None),
            "source": "toss",
            "bot_claimed_qty": round(claimed, 6),
        })

    rows.sort(key=lambda r: r.get("value_krw", 0), reverse=True)
    total_krw = kis_krw + toss_krw
    # 이제 토스 스냅샷이 매입원가를 담아 오므로 이 슬리브도 실제 손익을 낼 수 있다.
    # KIS 분은 원가를 평가-손익으로 역산한다(보유데이터가 손익을 준다).
    kis_cost_krw = kis_krw - kis_unrealized_krw
    manual_cost_krw = kis_cost_krw + toss_cost_krw
    manual_pl_krw = kis_unrealized_krw + toss_pl_krw
    manual_rate = (manual_pl_krw / manual_cost_krw * 100) if manual_cost_krw else None
    return {
        "id": "manual",
        "name": "Hands-on / Manual",
        "currency": "KRW",
        "mode": "manual",
        "value": round(total_krw, 2),
        "holdings": rows,
        "cost_basis": round(manual_cost_krw, 2),
        "total_pl_ytd": round(manual_pl_krw, 2),
        "profit_rate_ytd_pct": (round(manual_rate, 2) if manual_rate is not None
                                else None),
        # unrealized_profit / realized_profit_ytd 는 여전히 **일부러 비운다**.
        # 히어로 스트립과 get_portfolio() 가 이 두 키를 전 전략에 걸쳐 합산해
        # "Bot P/L YTD" 헤드라인을 만드는데, 수동 슬리브를 거기 섞으면 '봇 손익'이라는
        # 라벨이 거짓이 된다. 카드에 보여줄 값은 total_pl_ytd 로 따로 낸다.
        "extra": {
            "note": "Broker positions attributed to no bot: KIS and Toss holdings minus every bot's claimed shares.",
            "ticker_count": len(rows),
            # KIS is bot territory by definition, so these are always 0 now.
            # Kept as keys so anything reading the old shape still parses.
            "kis_krw": round(kis_krw, 2),
            "kis_ticker_count": sum(1 for r in rows if r.get("source") == "kis"),
            "kis_unrealized_krw": round(kis_unrealized_krw, 2),
            # KIS 주식 중 어떤 봇 카드도 주장하지 않은 몫. 수동이 아니라 봇 것이며
            # (build_totals 에서 봇으로 계상), 값이 크면 그 봇의 결과파일이 포지션을
            # 빠뜨렸다는 신호다 — 여기 남겨 두는 이유가 그것이다.
            "kis_unclaimed_krw": round(kis_unclaimed_krw, 2),
            "kis_unclaimed": kis_unclaimed,
            "toss_krw": round(toss_krw, 2),
            "toss_ticker_count": toss_rows,
            # What the Toss-account bots (NMF2) took out of this sleeve. Kept
            # here so the carve-out is auditable from the published JSON alone.
            "toss_bot_claimed_krw": round(toss_bot_claimed_krw, 2),
            "toss_cost_krw": round(toss_cost_krw, 2),
            "toss_pl_krw": round(toss_pl_krw, 2),
            "caveat": "The NMF2 sleeve inside Toss is now its own bot card and is excluded here. "
                      "P/L uses the broker's own purchase amounts (Toss purchaseAmount / KIS profit), "
                      "pro-rated by the share no bot claims.",
        },
    }


CMA_PRODUCT_CODE = "21"     # 계좌번호는 코드에 두지 않는다 — 위탁계좌와 cano가 같다


def get_cma_krw():
    """KIS CMA 잔액(원). 실패하면 0 — 조회 실패가 발행을 막으면 안 된다.

    CMA는 위탁계좌가 아니라서 inquire_balance / inquire_psbl_order 로는 안 잡힌다
    (각각 "위탁계좌인 경우만 조회가능합니다", "해당계좌 정보가 없습니다"). 맞는 API는
    투자계좌자산현황(inquire_account_balance)이고, 종합계좌번호는 위탁계좌와 같으며
    계좌상품코드만 21로 다르다.
    """
    try:
        sys.path.insert(0, os.path.join(TRADING_DIR, "open-trading-api", "examples_llm",
                                        "domestic_stock", "inquire_account_balance"))
        import kis_auth as ka
        import inquire_account_balance as iab
        ka.auth(svr="prod")
        acct = ka.getTREnv()
        _d1, d2 = iab.inquire_account_balance(cano=acct.my_acct,
                                              acnt_prdt_cd=CMA_PRODUCT_CODE)
        if d2 is None or getattr(d2, "empty", True):
            return 0.0
        row = d2.iloc[0]
        for f in ("tot_asst_amt", "tot_dncl_amt", "dncl_amt"):
            if f in row.index:
                try:
                    v = float(row[f])
                except (TypeError, ValueError):
                    continue
                if v > 0:
                    return v
        return 0.0
    except Exception as e:  # noqa: BLE001
        print("  WARNING: CMA lookup failed: {}".format(e))
        return 0.0


# The allocator's turnover band. Unlike the ramp this cannot be derived from a
# proposal — a bot held by the band shows target == current, which tells you the
# move was under the band but not what the band IS. Mirrored from
# asset_mgmt/allocate.py TURNOVER_BAND; change one, change both.
ALLOC_TURNOVER_BAND = float(os.environ.get("QUANTY_ALLOC_BAND", "0.20"))
ALLOC_RAMP_FALLBACK = 1.35


def proposal_ramp_max(allocations):
    """The allocator's RAMP_MAX, read back out of its own proposal.

    A ramped bot has target == current * RAMP_MAX exactly, in native units, so
    the ratio IS the constant — no need to hard-code it here and have it drift
    the first time someone tunes AM_RAMP_MAX. Falls back only when nothing was
    ramped, in which case nothing is being capped and the value barely matters.
    """
    for p in (allocations or {}).values():
        if not isinstance(p, dict) or not p.get("ramped"):
            continue
        cur = float(p.get("current_budget") or 0)
        tgt = float(p.get("target_budget") or 0)
        if cur > 0 and tgt > cur:
            return tgt / cur
    return ALLOC_RAMP_FALLBACK


def _live_target(cur_krw, per_bot_krw, ramp_max, band=None):
    """Re-run the allocator's per-bot rule against the LIVE 1/N share.

    The target the allocator wrote is a function of the residual IT saw at
    08:20. Sell part of the hands-on sleeve at lunchtime and the residual grows,
    the 1/N share on the panel grows with it — but the Target column stayed
    frozen on the morning's number, so the whole point of reducing hands-on
    (more capital for the fleet) was invisible until the next morning.

    Same two rules the allocator applies, in the same order:
      1. ramp   — no bot grows more than RAMP_MAX in one cycle;
      2. band   — a move under the turnover band is not worth making at all.

    Returns (target_krw, limited_by). This is a PROJECTION for the panel: it
    does not write any budget. What the bots actually hold is `current_krw`.
    """
    band = ALLOC_TURNOVER_BAND if band is None else band
    tgt = float(per_bot_krw or 0)
    if tgt <= 0:
        return 0.0, ""
    limited = ""
    if cur_krw > 0 and tgt > cur_krw * ramp_max:
        tgt, limited = cur_krw * ramp_max, "ramp"
    if cur_krw > 0 and abs(tgt - cur_krw) / cur_krw < band:
        tgt, limited = cur_krw, "band"
    return tgt, limited


def _runs_to_target(current_krw, uncapped_krw, target_krw):
    """Daily runs until a ramped bot reaches its 1/N share. None if unknowable.

    The ramp is multiplicative (target = current x factor each run), so the
    answer is a logarithm, not a division. Reported because "capped at 1.35x"
    alone does not say whether that means two days or two months.
    """
    try:
        cur, cap, tgt = float(current_krw), float(uncapped_krw), float(target_krw)
    except (TypeError, ValueError):
        return None
    if cur <= 0 or cap <= cur or tgt <= cur:
        return None
    factor = tgt / cur
    if factor <= 1.0:
        return None
    import math
    return int(math.ceil(math.log(cap / cur) / math.log(factor)))


def load_allocation(state_dir=None, now=None, fx_rate=None, live_totals=None):
    """The asset-management layer as a dashboard block. Never raises.

    Reads the newest `proposed_*.json` plus `last_applied.json` written by
    asset_mgmt/allocate.py + apply.py. Returns None when there is nothing to
    show — an absent allocator must not break a publish, and a half-read
    proposal is worse than no panel.

    Two numbers deserve care:
      * `current_budget` is in the bot's NATIVE currency (USD for the KIS-US
        bots) while `target_krw` is already KRW. Comparing them directly
        makes a $14.5k budget look like a rounding error next to a W27.7M
        target. Both are normalised to KRW here.
      * `applied` is not the same as `proposed`. The proposal file keeps
        status "proposed" even after apply.py has written the budgets, so
        freshness comes from last_applied.json, not from the proposal.
    """
    import glob

    state = state_dir or ALLOC_STATE_DIR
    try:
        files = sorted(glob.glob(os.path.join(state, "proposed_*.json")))
    except Exception:
        return None
    if not files:
        return None
    try:
        with open(files[-1]) as f:
            prop = json.load(f)
    except Exception as e:
        print("  WARNING: allocation proposal unreadable: {}".format(e))
        return None
    if not isinstance(prop, dict):
        return None

    applied = {}
    try:
        with open(os.path.join(state, "last_applied.json")) as f:
            applied = json.load(f) or {}
    except Exception:
        applied = {}

    now = now or datetime.now(KST).replace(tzinfo=None)
    stamp = applied.get("applied_at") or prop.get("proposed_at")
    age_h = None
    if stamp:
        try:
            when = datetime.fromisoformat(str(stamp))
            if when.tzinfo is not None:
                when = when.replace(tzinfo=None)
            age_h = (now - when).total_seconds() / 3600.0
        except (TypeError, ValueError):
            age_h = None

    # Everything money-shaped is re-priced into the DASHBOARD's frame — same
    # FX, same instant as the hero — so "Total capital" here and "Total
    # Investments" up there are the same number. Two different totals on one
    # page read as a bug no matter how well the footnote explains them.
    #
    # The arithmetic still closes because the whole chain is recomputed, not
    # rescaled: capital and hands-on come from the live totals, residual is
    # their difference, and pool/per-bot are derived from that residual using
    # the allocator's X and N. X and N are POLICY — they carry no currency and
    # need no conversion.
    #
    # Per-bot budgets are converted from their NATIVE amounts, never rescaled
    # from the allocator's KRW: the ramp factor is target/current in native
    # units and is therefore exactly 1.35 at any FX. Rescaling KRW figures
    # would smear rounding into that ratio and make the ramp look arbitrary.
    alloc_fx = float((prop.get("capital") or {}).get("fx") or 0) or float(fx_rate or 1400.0)
    fx = float(fx_rate or 0) or alloc_fx

    # Where the capital figure came from, so the hero and this panel can
    # disagree without either looking wrong: they are the same quantity
    # measured hours apart at different FX.
    basis, cap_as_of = "", None
    try:
        with open(os.path.join(state, "bots.json")) as f:
            _b = json.load(f) or {}
        basis = _b.get("capital_basis") or ""
        cap_as_of = _b.get("as_of") or _b.get("collected_at")
    except Exception:
        pass

    per_bot = float((prop.get("level2") or {}).get("per_bot_krw") or 0)
    targets = applied.get("targets") or {}
    rows = []
    for bid, p in sorted((prop.get("allocations") or {}).items()):
        if not isinstance(p, dict):
            continue
        # BOTH sides converted from their native amounts at the SAME rate.
        # Taking target_krw straight from the proposal (written at the
        # allocator's FX) while converting current at the dashboard's rate
        # smears the difference into their ratio — the ramp then reads x1.36
        # instead of the x1.35 the allocator actually applied.
        k = fx if (p.get("currency") == "USD") else 1.0
        cur_native = float(p.get("current_budget") or 0)
        tgt_native = float(p.get("target_budget") or 0)
        cur_krw = cur_native * k
        tgt_krw = tgt_native * k
        rows.append({
            "id": bid,
            "name": ALLOC_NAMES.get(bid, bid),
            "in_etf": bool(p.get("in_etf")),
            "account": p.get("account") or "",
            "why": p.get("why") or "",
            "currency": p.get("currency") or "KRW",
            "current_krw": round(cur_krw, 2),
            "target_krw": round(tgt_krw, 2),
            "drift_krw": round(tgt_krw - cur_krw, 2),
            "ramped": bool(p.get("ramped")),
            "held_by_band": bool(p.get("kept_by_turnover_band")),
            # What the bot WOULD get on pure 1/N, and what cut it back. Without
            # this the panel shows "per bot W40.3M" beside a W27.7M target and
            # simply looks broken — the ramp is the missing sentence.
            "uncapped_krw": round(per_bot, 2) if p.get("in_etf") else None,
            "limited_by": ("ramp" if p.get("ramped") else
                           "band" if p.get("kept_by_turnover_band") else ""),
            # Native ratio: FX cancels, so this is exactly the allocator's
            # RAMP_MAX no matter which rate the panel is priced at.
            "ramp_factor": (round(tgt_native / cur_native, 4)
                            if p.get("ramped") and cur_native > 0 else None),
            "runs_to_target": (_runs_to_target(cur_krw, per_bot, tgt_krw)
                               if p.get("ramped") else None),
            # Did apply.py actually write this target, or is it still only a
            # proposal? Governance says nothing moves without that step.
            "applied_krw": (round(float(targets[bid]), 2)
                            if bid in targets else None),
        })
    rows.sort(key=lambda r: (not r["in_etf"], -r["target_krw"]))

    lv1 = prop.get("level1") or {}
    lv2 = prop.get("level2") or {}

    # Live re-derivation. Falls back to the allocator's own figures when the
    # totals block is absent, so the panel still renders in a dry run.
    lt = live_totals or {}
    a_manual = float((prop.get("level0") or {}).get("manual_krw") or 0)
    a_resid = float((prop.get("level0") or {}).get("residual_krw") or 0)
    a_cap = float((prop.get("capital") or {}).get("total_krw") or 0)
    # `x or fallback` treats a legitimate ZERO as missing. A hands-on sleeve
    # of exactly 0 — the whole sleeve sold, which is precisely the case worth
    # getting right — silently reverted to the allocator's stale figure and
    # the residual collapsed back instead of jumping to the full capital.
    # Presence is tested with `is not None`, never truthiness.
    has_live = lt.get("investments_krw") is not None
    cap = float(lt["investments_krw"]) if has_live else a_cap
    man = float(lt["manual_krw"]) if lt.get("manual_krw") is not None else a_manual
    resid = max(0.0, cap - man) if has_live else a_resid
    X = float(lv1.get("X") or 0)
    N = int(lv2.get("N") or 0)
    # Hands-on is deducted INSIDE X, not before it (policy set 2026-08-21).
    #
    # The old chain was pool = X * (C - manual), which quietly broke the cash
    # floor: hands-on positions are exposure too, so total exposure came to
    # manual + X*(C-manual) — 86.4% of capital on a day X was 80%, leaving 13.6%
    # cash against a 20% floor, while `binding` reported "cash_floor".
    #
    #   exposure budget = X * C          <- the whole risk allowance
    #   fleet pool      = budget - manual <- what is left after the senior sleeve
    #
    # Total exposure is then exactly X*C and the floor holds. The cost: when X is
    # low the fleet can go to zero (X*C < manual) — on 2026-08-08..13 X sat at
    # X_MIN 10% and X*C was W60M against W177M hands-on. daily.py's MAX_STEP rail
    # holds a change that large for a human, so it cannot silently recall a fleet.
    exposure = cap * X
    pool = max(0.0, exposure - man)
    fleet_recalled = bool(N) and exposure <= man
    per_bot_live = pool / N if N else 0.0
    ramp_max = proposal_ramp_max(prop.get("allocations"))
    for r in rows:
        if not r["in_etf"]:
            continue
        r["uncapped_krw"] = round(per_bot_live, 2)
        # Re-derive the target from the LIVE residual so a hands-on reduction
        # reaches this column the moment it happens, instead of waiting for the
        # allocator's next 08:20 run.
        cur = float(r["current_krw"] or 0)
        tgt, limited = _live_target(cur, per_bot_live, ramp_max)
        r["target_krw"] = round(tgt, 2)
        r["drift_krw"] = round(tgt - cur, 2)
        r["limited_by"] = limited
        r["ramp_factor"] = round(tgt / cur, 4) if limited == "ramp" and cur > 0 else None
        r["runs_to_target"] = (_runs_to_target(cur, per_bot_live, tgt)
                               if limited == "ramp" else None)
        # True when the panel's projection has moved away from what the
        # allocator actually wrote — i.e. the sleeve changed since 08:20.
        r["target_is_live"] = True
    return {
        "as_of": stamp,
        "age_hours": None if age_h is None else round(age_h, 2),
        "stale": bool(age_h is not None and age_h > ALLOC_MAX_AGE_HOURS),
        "applied": bool(targets),
        "applied_at": applied.get("applied_at"),
        "policy_version": prop.get("policy_version") or "",
        "capital_krw": round(cap, 2),
        "capital_as_of": cap_as_of,
        "capital_basis": basis,
        "fx": fx,
        "repriced_live": bool(lt.get("investments_krw")),
        "ramp_max": round(ramp_max, 4),
        "turnover_band": ALLOC_TURNOVER_BAND,
        # What the allocator ACTUALLY used when it sized the bots. Kept so the
        # gap stays visible: on 2026-08-20 collect.py read a dashboard whose
        # Toss sleeve was a day stale, so hands-on came in W12.1M high and
        # every bot was sized off an understated residual.
        "allocator_frame": {
            "capital_krw": a_cap, "manual_krw": a_manual,
            "residual_krw": a_resid, "fx": alloc_fx,
        },
        # LEVEL 0 — the hands-on sleeve is senior to the bots: it is carved
        # out first and the fleet only ever sees the residual.
        "level0": {"manual_krw": round(man, 2), "residual_krw": round(resid, 2)},
        # LEVEL 1 — one system-wide exposure X, and WHICH constraint set it.
        # `binding` is the interesting field: kelly / vol cap / cash floor /
        # drawdown brake are different stories with the same number.
        "level1": {
            "X": float(lv1.get("X") or 0),
            "binding": lv1.get("binding") or "",
            "brake": float(lv1.get("brake") or 1.0),
            "kelly_half": lv1.get("kelly_half"),
            "x_vol_cap": lv1.get("x_vol_cap"),
            "mu_shrunk": lv1.get("mu_shrunk"),
            "sigma": lv1.get("sigma"),
            "fleet_dd": lv1.get("fleet_dd"),
            "n_days": lv1.get("n_days"),
        },
        # LEVEL 2 — equal weight, deliberately (DeMiguel-Garlappi-Uppal 1/N).
        "level2": {
            "N": N,
            "exposure_budget_krw": round(exposure, 2),
            "per_bot_krw": round(per_bot_live, 2),
            "fleet_pool_krw": round(pool, 2),
            "fleet_recalled": fleet_recalled,
            "roster": list(lv2.get("roster") or []),
        },
        "totals": prop.get("totals") or {},
        "bots": rows,
    }


def build_accounts(totals, portfolio, toss, strategies, now):
    """`accounts` block: KIS / Upbit / Toss, each honestly flagged. PURE.

    Never raises and never omits a key — a missing account is a zeroed tile with
    stale=true, because a silently dropped sleeve understates Jae's net worth
    without saying so.
    """
    fx = float((portfolio or {}).get("exchange_rate") or 0) or 1380.0
    stamp = now.strftime("%Y-%m-%d %H:%M KST")

    if totals:
        kr = totals.get("kr") or {}
        us = totals.get("us") or {}
        krw_cash = float(totals.get("krw_cash") or 0)
        usd_cash = float(totals.get("usd_cash") or 0)
        unsettled = float(totals.get("unsettled_us_sell_krw") or 0)
        kr_stock = float(kr.get("stock_value") or 0)
        us_stock_krw = float(us.get("stock_value") or 0) * fx
        # Settlement-in-flight (KR pending inside prvs_rcdl_excc_amt, US T+3 via
        # ustl_sll_amt_smtl) is money out of the positions and not yet in cash.
        # It is counted as cash so the split can't lose it.
        cash_krw = krw_cash + usd_cash * fx + unsettled
        kis = {
            "total_krw": round(kr_stock + us_stock_krw + cash_krw, 2),
            "cash_krw": round(cash_krw, 2),
            "stock_krw": round(kr_stock + us_stock_krw, 2),
            "as_of": stamp,
            "stale": False,
            "kr_stock_krw": round(kr_stock, 2),
            "us_stock_krw": round(us_stock_krw, 2),
            "krw_cash_krw": round(krw_cash, 2),
            "usd_cash_usd": round(usd_cash, 2),
            "unsettled_us_sell_krw": round(unsettled, 2),
            "holdings_count": len(kr.get("holdings") or []) + len(us.get("holdings") or []),
        }
    else:
        kis = {"total_krw": 0.0, "cash_krw": 0.0, "stock_krw": 0.0, "as_of": None,
               "stale": True, "note": "KIS account query failed at publish time"}

    btc = next((s for s in (strategies or []) if s.get("id") == "btc_vb"), {}) or {}
    upbit_total = float((portfolio or {}).get("upbit_krw") or btc.get("value") or 0)
    holding = bool(btc.get("is_holding"))
    # NOTHING HERE QUERIES UPBIT. The figure is the last equity point the BTC VB
    # bot wrote into its own state file, replayed at publish time. So staleness
    # has to be judged on the AGE of that write: the old rule (`upbit_total <= 0`)
    # could only ever fire on a zero, which means a bot that died — or an account
    # that was emptied — would republish the same frozen number every day and
    # never once be flagged. It reads identical to a healthy flat sleeve, because
    # a flat sleeve genuinely does repeat the same number (W9,737,967.20081266
    # sat unchanged from 08-12 to 08-17 while the bot was working perfectly).
    last_run = btc.get("last_run")
    age_days = _days_since(last_run, now)
    # The bot runs daily at the US close and only rewrites state once its monitor
    # window closes, so being a day or two behind is normal operation, not a
    # fault. Three days is the first age that cannot be explained that way.
    upbit_stale = (upbit_total <= 0) or (age_days is not None and age_days > UPBIT_STALE_DAYS)
    upbit = {
        "total_krw": round(upbit_total, 2),
        # Flat bot => the whole sleeve is idle KRW. Calling that "deployed by a
        # bot" would overstate how much of the portfolio is actually at work.
        "cash_krw": 0.0 if holding else round(upbit_total, 2),
        "position_krw": round(upbit_total, 2) if holding else 0.0,
        # Same contract the Toss sleeve uses: as_of is None whenever stale is
        # true (the healthcheck asserts freshness only against a non-stale
        # stamp) and the real stamp moves to last_seen_at for the tile.
        "as_of": None if upbit_stale else (last_run or stamp),
        "stale": upbit_stale,
        "is_holding": holding,
    }
    if upbit_stale:
        upbit["last_seen_at"] = last_run
        upbit["note"] = (
            "no live Upbit query — replayed from BTC VB bot state, last run {}"
            .format(last_run or "unknown"))
    if age_days is not None:
        upbit["age_days"] = age_days
    # The money is deliberately NOT zeroed when stale. Unlike a missing Toss
    # snapshot (no data at all), this is a real balance we simply cannot re-
    # confirm; dropping it would understate net worth by ~W9.7M without saying
    # so, which is the exact failure this module exists to prevent.

    return {"kis": kis, "upbit": upbit, "toss": toss or {}}


def build_totals(accounts, strategies, manual_card, kis_holdings, fx_rate):
    """`totals` block — the Whole-Investment hero's numbers. PURE.

    investments_krw = KIS + Upbit + Toss.
    bots / manual / cash partition the SAME money:
      bots   = every account share a bot claims, at the account's own mark
               (+ the Upbit sleeve when the BTC bot is actually in a position,
                + Toss-account bots' marked value — NMF2, whose shares the
                  hands-on sleeve gave up in the very same carve-out)
      manual = the hands-on sleeve (KIS and Toss leftovers)
      cash   = KIS cash + settlement-in-flight + Toss cash + idle Upbit
    so the gap is zero unless an input went missing. `bots_cards_krw` is the
    naive "sum the bot cards" figure kept alongside for diagnosis: the
    difference is exactly the value of live bot positions whose card dropped
    its holdings (W8.1M of Hybrid VB KR on 2026-08-16).
    """
    fx = float(fx_rate or 0)
    kis = accounts.get("kis") or {}
    upbit = accounts.get("upbit") or {}
    toss = accounts.get("toss") or {}

    cma = accounts.get("cma") or {}
    investments = (float(kis.get("total_krw") or 0)
                   + float(cma.get("total_krw") or 0)
                   + float(upbit.get("total_krw") or 0)
                   + float(toss.get("total_krw") or 0))

    # EVERY KIS share is bot money — that account is not where Jae buys by hand,
    # and the hands-on sleeve no longer takes anything from it. Summing the
    # account rather than the bots' claims is also what makes the split immune
    # to a card dropping its holdings: hybrid_vb_kr publishing holdings:[] used
    # to move W4.4M into "his" money, and now moves nothing at all.
    bots_kis_krw = sum(
        (float(a.get("value_native") or 0)
         if (a.get("currency") or "KRW") == "KRW"
         else float(a.get("value_native") or 0) * fx)
        for a in (kis_holdings or {}).values() if float(a.get("qty") or 0) > 0)
    reported_kis_krw = _attributed_krw(
        kis_holdings, collect_bot_claims(strategies, sources=("holdings",)), fx)
    # Bots trading a non-KIS account carry their own already-KRW marked value
    # (build_nmf2_card pro-rates it out of the Toss snapshot rows the hands-on
    # sleeve then skips), so they are added directly rather than re-attributed.
    upbit_bot_krw = float(upbit.get("position_krw") or 0)
    toss_bots_krw = sum(float(s.get("value") or 0) for s in (strategies or [])
                        if isinstance(s, dict) and s.get("account") == "toss")
    bots_krw = bots_kis_krw + upbit_bot_krw + toss_bots_krw
    manual_krw = float((manual_card or {}).get("value") or 0)
    # CMA는 전액 예수금이라 현금 버킷에 넣는다. 안 넣으면 investments 에는 잡히는데
    # bots/manual/cash 합에는 빠져서 히어로의 구성 막대가 총액과 어긋난다.
    cash_krw = (float(kis.get("cash_krw") or 0)
                + float(cma.get("cash_krw") or 0)
                + float(toss.get("cash_krw") or 0)
                + float(upbit.get("cash_krw") or 0))

    bots_cards_krw = 0.0
    for s in strategies or []:
        if s.get("id") == "manual":
            continue
        if s.get("currency") == "MULTI":
            for leg, cur in (("kr", "KRW"), ("us", "USD")):
                val = float(((s.get(leg) or {}).get("value")) or 0)
                bots_cards_krw += val if cur == "KRW" else val * fx
        else:
            val = float(s.get("value") or 0)
            bots_cards_krw += val if s.get("currency") == "KRW" else val * fx

    gap = investments - (bots_krw + manual_krw + cash_krw)
    gap_pct = abs(gap) / investments * 100 if investments > 0 else 0.0
    denom = bots_krw + manual_krw + cash_krw
    pct = (lambda v: round(v / denom * 100, 2)) if denom > 0 else (lambda v: 0.0)

    out = {
        "investments_krw": round(investments, 2),
        "bots_krw": round(bots_krw, 2),
        "manual_krw": round(manual_krw, 2),
        "cash_krw": round(cash_krw, 2),
        "split_pct": {"bots": pct(bots_krw), "manual": pct(manual_krw), "cash": pct(cash_krw)},
        "reconciliation_gap_krw": round(gap, 2),
        "reconciliation_gap_pct": round(gap_pct, 4),
        "reconciliation_warning": gap_pct > RECONCILIATION_WARN_PCT,
        "bots_cards_krw": round(bots_cards_krw, 2),
        # Value of live bot positions their OWN card no longer reports, rescued
        # from open_positions. Would otherwise be shown to Jae as his own money.
        "unreported_bot_positions_krw": round(bots_kis_krw - reported_kis_krw, 2),
        "bots_breakdown": {
            "kis_krw": round(bots_kis_krw, 2),
            "upbit_krw": round(upbit_bot_krw, 2),
            "toss_krw": round(toss_bots_krw, 2),
        },
        "manual_breakdown": {
            "kis_krw": round(float(((manual_card or {}).get("extra") or {}).get("kis_krw") or 0), 2),
            "toss_krw": round(float(((manual_card or {}).get("extra") or {}).get("toss_krw") or 0), 2),
        },
        "cash_breakdown": {
            "kis_krw": round(float(kis.get("cash_krw") or 0), 2),
            "toss_krw": round(float(toss.get("cash_krw") or 0), 2),
            "upbit_idle_krw": round(float(upbit.get("cash_krw") or 0), 2),
        },
        "fx_rate": fx,
    }
    return out


def load_manual_history(path=None):
    """Rows of {date, ...} from a date-keyed sleeve JSONL (never raises).

    Generic over `path`: serves both the manual sleeve and NMF2's own history.
    """
    rows = []
    try:
        with open(path or MANUAL_HISTORY_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("date"):
                    rows.append(row)
    except (IOError, OSError):
        return []
    rows.sort(key=lambda r: r["date"])
    return rows


def append_manual_history(row, path=None):
    """Upsert one row per date, atomically (.tmp + os.replace).

    Same durability contract as dashboard_server's equity snapshot: the reader
    (next publish, healthcheck) either sees the old file or the new one, never a
    half-written line. Publishing twice in a day replaces that day's row.
    """
    path = path or MANUAL_HISTORY_FILE
    rows = [r for r in load_manual_history(path) if r.get("date") != row.get("date")]
    rows.append(row)
    rows.sort(key=lambda r: r["date"])
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")
    os.replace(tmp, path)
    return rows


AGG_SKIP_IDS = ("manual",)          # 수동 슬리브는 봇 비교 차트의 대상이 아니다


def pin_aggregate_endpoint(aggregate, strategies, rate, today,
                           skip_ids=AGG_SKIP_IDS):
    """Pin the Portfolio Total line's endpoint to the sum of the live bot cards.

    dashboard_server already pins each BOT's series endpoint to its live Total
    P/L, but equity_series_aggregate is rebuilt straight from the snapshot file
    and never pinned. Two things go wrong because of that:

      1. A lagged snapshot write leaves the total line on yesterday's number
         while the bars around it show today's — on 2026-08-16 the total line
         drew W2,038,699 next to bars summing to W2,914,311.
      2. The aggregate is built in dashboard_server, BEFORE this file splices in
         the Toss bots, so it can never include nmf2/usvb/event_bot at all. That
         error grows with every Toss bot added.

    Recomputing the endpoint from the finished `strategies` list fixes both.
    Only the endpoint moves; earlier points are history and stay untouched.
    """
    if not aggregate:
        return

    def _to_krw(amount, currency):
        return float(amount or 0) if currency == "KRW" else float(amount or 0) * rate

    tot = {"realized": 0.0, "unrealized": 0.0, "total": 0.0}

    def _add(src, cur):
        tot["realized"] += _to_krw(src.get("realized_profit_ytd"), cur)
        tot["unrealized"] += _to_krw(src.get("unrealized_profit"), cur)
        tot["total"] += _to_krw(src.get("total_pl_ytd"), cur)

    for s in strategies:
        sid = s.get("id", "")
        if sid in skip_ids:
            continue
        if sid == "hybrid_vb":              # legs carry different currencies
            for leg, cur in (("kr", "KRW"), ("us", "USD")):
                _add(s.get(leg) or {}, cur)
            continue
        _add(s, s.get("currency", "KRW"))

    for key in ("realized", "unrealized", "total"):
        series = aggregate.get(key)
        if series is None:
            continue
        val = round(tot[key], 2)
        if series and series[-1][0] == today:
            series[-1][1] = val
        else:
            series.append([today, val])
    return tot


EQUITY_HISTORY_JSONL = os.path.join(TRADING_DIR, "strategy_results",
                                    "equity_history.jsonl")


def write_equity_snapshot(strategies, fx, today, path=EQUITY_HISTORY_JSONL):
    """Write today's per-bot P/L row from the FINAL, marked-to-market strategies.

    dashboard_server also writes this file, but it only sees what each bot wrote
    into strategy_results/*.json — numbers that can be days old on a weekend and
    are never marked to market. It also cannot see the Toss bots at all. The
    15-minute healthcheck hits that same code path, so the last write of any
    given day was a stale one, and that is the value that froze into history.

    This row is tagged `_src: "generator"`; dashboard_server refuses to overwrite
    a today-row carrying that tag. Readers skip non-dict values, so the tag is
    invisible to the chart builders.

    Non-fatal by contract: history is a nice-to-have, publishing is not.
    """
    rows = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except FileNotFoundError:
        pass
    except Exception as e:
        print("  WARNING: equity history unreadable, not rewriting: {}".format(e))
        return None

    def _to_krw(amount, currency):
        return float(amount or 0) if currency == "KRW" else float(amount or 0) * fx

    def _cell(realized, unrealized, currency):
        return {"realized_krw": round(_to_krw(realized, currency), 2),
                "unrealized_krw": round(_to_krw(unrealized, currency), 2),
                "total_pl_krw": round(_to_krw((realized or 0) + (unrealized or 0),
                                              currency), 2),
                "native": currency}

    snap = {"date": today, "_src": "generator"}
    for s in strategies:
        sid = s.get("id", "")
        if sid in AGG_SKIP_IDS:          # 수동 슬리브는 가치 카드지 P/L 카드가 아니다
            continue
        if sid == "hybrid_vb":
            for leg, cur in (("kr", "KRW"), ("us", "USD")):
                L = s.get(leg) or {}
                u = L.get("unrealized_profit")
                u = u if u is not None else L.get("profit")
                if u is None and L.get("realized_profit_ytd") is None:
                    continue
                snap["hybrid_vb_" + leg] = _cell(L.get("realized_profit_ytd"), u, cur)
            continue
        u = s.get("unrealized_profit")
        u = u if u is not None else s.get("profit")
        if u is None and s.get("realized_profit_ytd") is None:
            continue                     # P/L을 보고하지 않는 봇 — 0으로 날조하지 않는다
        snap[sid] = _cell(s.get("realized_profit_ytd"), u,
                          s.get("currency", "KRW"))

    if len(snap) <= 2:                   # date + _src 뿐이면 쓸 게 없다
        return None

    other = [e for e in rows if e.get("date") != today]
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as fh:
            for e in other:
                fh.write(json.dumps(e, default=str) + "\n")
            fh.write(json.dumps(snap, default=str) + "\n")
        os.replace(tmp, path)
    except Exception as e:
        print("  WARNING: could not write equity history: {}".format(e))
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    return snap


def main(dry_run=False):
    """Build and publish dashboard_data.json.

    dry_run writes to /tmp and skips the manual-history append, so the numbers
    can be eyeballed on the server before anything the public site reads is
    touched.
    """
    now = datetime.now(KST)
    print("[{}] Generating dashboard data{}...".format(
        now.strftime("%H:%M:%S KST"), " (DRY RUN)" if dry_run else ""))

    # Fetch live data from dashboard server API
    try:
        raw = urllib.request.urlopen(API_URL, timeout=30).read()
        api_data = json.loads(raw)
        print("  Loaded {} strategies from API".format(len(api_data.get("strategies", []))))
    except Exception as e:
        print("  ERROR: Could not fetch from {}: {}".format(API_URL, e))
        return

    # Pull KIS account positions ONCE — used both for the portfolio summary
    # and to mark-to-market each bot's holdings.
    totals = None
    try:
        totals = get_account_totals_resilient()
    except Exception as e:
        print("  WARNING: KIS totals query failed; falling back to bot-state values: {}".format(e))

    # Recover any bot position that its own result file dropped (intermittent
    # single-ticker balance-query dropout) from the authoritative account
    # balance, BEFORE mark-to-market re-prices it.
    if totals:
        rec = recover_missing_bot_holdings(api_data, totals)
        if rec:
            print("  Recovered {} bot position(s) from account balance".format(rec))

    # Same idea one step further: a bot that reported SOME of a position (result
    # file written a second after the fill) gets topped up to the account
    # quantity, so its shares land on its own card instead of the gap.
    if totals:
        topped = reconcile_underreported_bot_holdings(api_data, totals)
        for sid, ticker, was, now_qty in topped:
            print("  Topped up {} {}: {:g} -> {:g} shares (result file predates fill)"
                  .format(sid, ticker, was, now_qty))

    # Live mark-to-market: replace bot-saved holding values with current KIS
    # prices so the dashboard shows price moves up to publish time, not bot's
    # last-run snapshot. Realized P&L is untouched (YTD from 2026-01-01).
    if totals:
        mtm = mark_to_market_strategies(api_data, totals)
        if mtm.get("skipped"):
            print("  Mark-to-market: re-priced {} holdings; no KIS price for {}".format(
                mtm.get("marked", 0), ", ".join(mtm["skipped"])))
        else:
            print("  Mark-to-market: re-priced {} holdings".format(mtm.get("marked", 0)))
        api_data["mark_to_market_at"] = now.strftime("%Y-%m-%d %H:%M KST")

    # Get real account totals from KIS API (reuse the totals we already pulled)
    portfolio = get_portfolio(totals=totals)
    if portfolio:
        print("  Account total: W{:,.0f}".format(portfolio.get("total_value_krw", 0)))
    else:
        print("  WARNING: Portfolio query failed, using empty portfolio")

    # Override portfolio total_profit_krw to match the dashboard's Total P/L
    # (sum of per-bot unrealized + realized YTD). The raw account-level
    # total_value - original_deposit mixes in FX drift and top-ups, so
    # Discord was showing a number different from the dashboard strip.
    if portfolio:
        rate = portfolio.get("exchange_rate", 1380)
        unreal_krw = unreal_usd = 0.0
        real_krw = real_usd = 0.0
        for s in api_data.get("strategies", []):
            cur = s.get("currency")
            if cur == "KRW":
                unreal_krw += s.get("unrealized_profit") or s.get("profit") or 0
                real_krw   += s.get("realized_profit_ytd") or 0
            elif cur == "USD":
                unreal_usd += s.get("unrealized_profit") or s.get("profit") or 0
                real_usd   += s.get("realized_profit_ytd") or 0
            elif cur == "MULTI":
                kr = s.get("kr") or {}
                us = s.get("us") or {}
                unreal_krw += kr.get("unrealized_profit") or kr.get("profit") or 0
                unreal_usd += us.get("unrealized_profit") or us.get("profit") or 0
                real_krw   += kr.get("realized_profit_ytd") or 0
                real_usd   += us.get("realized_profit_ytd") or 0
        unreal_combined = unreal_krw + unreal_usd * rate
        real_combined   = real_krw   + real_usd   * rate
        total_pnl = unreal_combined + real_combined
        original = portfolio.get("original_deposit_krw") or 1
        portfolio["unrealized_krw"]    = round(unreal_combined)
        portfolio["realized_krw"]      = round(real_combined)
        portfolio["unrealized_native"] = {"krw": round(unreal_krw), "usd": round(unreal_usd, 2)}
        portfolio["realized_native"]   = {"krw": round(real_krw),   "usd": round(real_usd, 2)}
        portfolio["total_profit_krw"]  = round(total_pnl)
        portfolio["total_profit_pct"]  = round(total_pnl / original * 100, 2) if original > 0 else 0
        print("  Matched dashboard P/L: realized W{:+,.0f} + unrealized W{:+,.0f} = W{:+,.0f}".format(
            real_combined, unreal_combined, total_pnl))

    # Re-pin the comparison-chart endpoints to the marked-to-market cards so the
    # published chart can't disagree with the published bot cards.
    pin_equity_endpoints(
        api_data.get("equity_series") or {},
        api_data.get("strategies") or [],
        (portfolio or {}).get("exchange_rate", 1380),
        et_today(),
    )

    # Merge
    output = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M KST"),
        "portfolio": portfolio,
    }
    output.update(api_data)

    fx = (portfolio or {}).get("exchange_rate") or 1380

    # Toss Securities account (Mac-side read-only snapshot). setdefault so this
    # merges with whatever else populates `accounts`, in any order.
    toss = load_toss_account(fx_rate=fx)
    output.setdefault("accounts", {})["toss"] = toss
    if not toss.get("usable"):
        print("  Toss: ABSENT — {}".format(toss.get("note")))
    elif toss["stale"]:
        print("  Toss: STALE W{:,.0f} ({} holdings) — {}".format(
            toss["total_krw"], toss.get("holdings_count", 0), toss.get("note")))
    else:
        print("  Toss: W{:,.0f} ({} holdings, as of {})".format(
            toss["total_krw"], toss.get("holdings_count", 0), toss["as_of"]))

    # ── Hands-on / Manual sleeve + whole-investment totals ───────────────────
    # Everything below runs AFTER pin_equity_endpoints on purpose: the manual
    # card is not a bot, so it must not have its chart endpoint pinned to a
    # total_pl_ytd it does not have.
    toss_rows = []
    # Gated on `usable`, not `stale`: an aged snapshot still carries real rows,
    # and dropping them took the whole hands-on sleeve AND NMF2's marks down
    # with it whenever the Mac missed a single run.
    if toss.get("usable", not toss.get("stale")):
        try:
            with open(TOSS_SNAPSHOT_FILE) as f:
                toss_rows = json.load(f).get("holdings") or []
        except Exception as e:
            print("  WARNING: Toss holdings unreadable for the manual sleeve: {}".format(e))

    kis_holdings = kis_holdings_map(totals)
    strategies = output.get("strategies") or []

    # NMF2 first: it claims its ledger's shares out of the Toss rows so the
    # hands-on sleeve below can only ever see what is left. One carve-out, two
    # sleeves — which is why the reconciliation gap cannot move.
    nmf2 = build_nmf2_card(load_nmf2_ledger(), toss_rows, fx)
    toss_claims = {r["ticker"]: r["qty"] for r in (nmf2 or {}).get("holdings", [])}
    if nmf2:
        nx = nmf2["extra"]
        print("  NMF2: W{:,.0f} ({}/{} ledger positions marked, cost W{:,.0f}, "
              "P/L W{:+,.0f}, budget W{:,.0f})".format(
                  nmf2["value"], nx["ticker_count"], nx["ledger_position_count"],
                  nmf2["cost_basis"], nmf2["total_pl_ytd"], nmf2["budget"]))
        if nx["unmatched_symbols"]:
            print("  NOTE: NMF2 ledger symbols absent from the Toss snapshot "
                  "(valued at 0, NOT counted): {}".format(", ".join(nx["unmatched_symbols"])))
    else:
        print("  NMF2: no ledger positions readable — no card; "
              "its Toss shares stay in the hands-on sleeve")

    manual = build_manual_sleeve(kis_holdings, strategies, fx, toss_rows, toss_claims)
    strategies = [s for s in strategies if s.get("id") not in ("manual", "nmf2")]
    if nmf2:
        strategies.append(nmf2)
    strategies.append(manual)                     # hands-on always last in the grid
    # Staleness last, once every card exists — nmf2 and the hands-on sleeve
    # are assembled here rather than by the API, so an earlier pass would
    # silently skip them.
    annotate_bot_staleness(strategies, datetime.now(KST).replace(tzinfo=None))
    annotate_capital_fields(strategies)
    _names = account_name_map(totals)
    annotate_holding_names(strategies, _names)
    print("  Holding names: {} tickers labelled from the broker".format(len(_names)))
    output["strategies"] = strategies
    _stale = [s["id"] for s in strategies if s.get("is_stale")]
    print("  Bot staleness: {}".format(
        "all fresh" if not _stale else "STALE -> " + ", ".join(_stale)))
    mx = manual["extra"]
    print("  Hands-on / Manual: W{:,.0f} ({} tickers — KIS W{:,.0f} / {}, Toss W{:,.0f} / {})".format(
        manual["value"], mx["ticker_count"], mx["kis_krw"], mx["kis_ticker_count"],
        mx["toss_krw"], mx["toss_ticker_count"]))

    # Manual history: one row per publish date, atomically upserted. The series
    # is a VALUE series (the Toss half has no cost basis, so a P/L series would
    # be half-covered); the comparison chart is a P/L chart and deliberately
    # does NOT plot it — the manual card's own sparkline does.
    hist_row = {"date": et_today(), "value_krw": manual["value"],
                "kis_krw": mx["kis_krw"], "toss_krw": mx["toss_krw"],
                "ticker_count": mx["ticker_count"],
                "toss_stale": bool(toss.get("stale"))}
    if dry_run:
        history = [r for r in load_manual_history() if r.get("date") != hist_row["date"]] + [hist_row]
    else:
        try:
            history = append_manual_history(hist_row)
        except Exception as e:
            print("  WARNING: could not persist manual sleeve history: {}".format(e))
            history = [hist_row]
    eq = output.setdefault("equity_series", {})
    eq["manual"] = [[r["date"], r.get("value_krw", 0)] for r in history]

    # NMF2's comparison-chart series. It is built here rather than by
    # pin_equity_endpoints because that helper only extends series that already
    # exist, and dashboard_server has never snapshotted this bot — the Toss
    # sleeve is invisible to the KIS-side equity loop. The chart is a Total P/L
    # chart, so this series is P/L (W66k scale), not the W915k value: dropping a
    # value series into a P/L chart would dwarf every other bot's line.
    if nmf2:
        nmf2_row = {"date": et_today(), "total_pl_krw": nmf2["total_pl_ytd"],
                    "value_krw": nmf2["value"], "cost_krw": nmf2["cost_basis"],
                    "ticker_count": nmf2["extra"]["ticker_count"],
                    "toss_stale": bool(toss.get("stale"))}
        if dry_run:
            nmf2_hist = [r for r in load_manual_history(NMF2_HISTORY_FILE)
                         if r.get("date") != nmf2_row["date"]] + [nmf2_row]
        else:
            try:
                nmf2_hist = append_manual_history(nmf2_row, NMF2_HISTORY_FILE)
            except Exception as e:
                print("  WARNING: could not persist NMF2 history: {}".format(e))
                nmf2_hist = [nmf2_row]
        eq["nmf2"] = [[r["date"], r.get("total_pl_krw", 0)] for r in nmf2_hist]
        if len(eq["nmf2"]) < 2:
            print("  NOTE: NMF2 has {} history point(s) — the comparison chart shows "
                  "'insufficient data' until the next publish".format(len(eq["nmf2"])))

    # The Portfolio Total line is the only series dashboard_server hands over
    # unpinned, and it is built before the Toss bots exist. Re-derive its
    # endpoint now that `strategies` is final, so the total line agrees with the
    # bars beside it and with the hero panel above it.
    _agg_tot = pin_aggregate_endpoint(
        output.get("equity_series_aggregate"), strategies, fx, et_today())
    if _agg_tot:
        print("  Portfolio Total pinned: W{:,.0f} (realized W{:,.0f} + unrealized W{:,.0f})".format(
            _agg_tot["total"], _agg_tot["realized"], _agg_tot["unrealized"]))

    # equity_history.jsonl에 남는 행도 이 값으로 맞춘다. 안 그러면 차트의 과거
    # 구간이 시가평가 전 값으로 굳고 토스 봇은 아예 빠진다 (끝점은 위 핀이
    # 가려주지만 어제 이전은 못 고친다).
    if not dry_run:
        _snap = write_equity_snapshot(strategies, fx, et_today())
        if _snap:
            print("  Equity history row written for {} ({} bots)".format(
                _snap["date"], len(_snap) - 2))
    else:
        print("  (dry run) equity history not written")

    # build_accounts re-emits the very `toss` dict the Toss ingest produced, so
    # this update adds kis/upbit without ever rewriting Task 4's contract.
    accounts = build_accounts(totals, portfolio, toss, strategies, now)
    output.setdefault("accounts", {}).update(accounts)
    # CMA는 위탁계좌와 별개라 위 KIS 조회에 안 들어온다. 총자산에 포함시키려면
    # 따로 불러야 한다 (0이면 계좌 자체를 만들지 않아 기존 표시가 그대로 유지된다).
    _cma = get_cma_krw()
    if _cma > 0:
        output.setdefault("accounts", {})["cma"] = {
            "total_krw": round(_cma, 2), "cash_krw": round(_cma, 2),
            "krw_cash_krw": round(_cma, 2), "stock_krw": 0.0,
            "label": "KIS CMA"}
        print("  CMA: W{:,.0f}".format(_cma))

    totals_block = build_totals(output["accounts"], strategies, manual, kis_holdings, fx)

    # Asset-management layer. Runs AFTER build_totals so it can price itself in
    # the same frame as the hero — the panel and the headline must not show two
    # different "total capital" figures. Non-blocking: the dashboard publishes
    # with or without it, because a missing allocator panel is a gap in
    # reporting while a failed publish is a gap in everything.
    alloc = load_allocation(now=datetime.now(KST).replace(tzinfo=None), fx_rate=fx,
                            live_totals=totals_block)
    if alloc:
        output["allocation"] = alloc
        print("  Allocation: X {:.0%} ({}) - 1/N {} x W{:,.0f}, {}{}".format(
            alloc["level1"]["X"], alloc["level1"]["binding"] or "-",
            alloc["level2"]["N"], alloc["level2"]["per_bot_krw"],
            "applied" if alloc["applied"] else "PROPOSED ONLY",
            " (STALE {:.0f}h)".format(alloc["age_hours"])
            if alloc["stale"] else ""))
    else:
        print("  Allocation: no state readable at {}".format(ALLOC_STATE_DIR))

    output["totals"] = totals_block

    # Palette travels WITH the data so the page cannot drift from the
    # generator and the Discord reports.
    output["bot_colors"] = {k: {"hex": v, "chip": BOT_CHIPS.get(k, "")}
                            for k, v in BOT_COLORS.items()}
    annotate_feed_bots(output.get("feed"))

    print("  Accounts: KIS W{:,.0f} + Upbit W{:,.0f} + Toss W{:,.0f}".format(
        accounts["kis"]["total_krw"], accounts["upbit"]["total_krw"],
        (accounts.get("toss") or {}).get("total_krw", 0)))
    print("  TOTAL INVESTMENTS W{:,.0f} = bots W{:,.0f} ({}%) + hands-on W{:,.0f} ({}%) + cash W{:,.0f} ({}%)".format(
        totals_block["investments_krw"], totals_block["bots_krw"], totals_block["split_pct"]["bots"],
        totals_block["manual_krw"], totals_block["split_pct"]["manual"],
        totals_block["cash_krw"], totals_block["split_pct"]["cash"]))
    if totals_block["unreported_bot_positions_krw"]:
        print("  NOTE: W{:+,.0f} of live bot positions are missing from their own cards "
              "(rescued via open_positions, so NOT counted as hands-on)".format(
                  totals_block["unreported_bot_positions_krw"]))
    if totals_block["reconciliation_warning"]:
        print("  WARNING: reconciliation gap W{:+,.0f} ({:.2f}% of investments) exceeds {}%".format(
            totals_block["reconciliation_gap_krw"], totals_block["reconciliation_gap_pct"],
            RECONCILIATION_WARN_PCT))
    else:
        print("  Reconciliation gap W{:+,.0f} ({:.3f}%) — OK".format(
            totals_block["reconciliation_gap_krw"], totals_block["reconciliation_gap_pct"]))

    out_dir = "/tmp/dashboard_dryrun" if dry_run else DATA_DIR
    os.makedirs(out_dir, exist_ok=True)

    data_file = os.path.join(out_dir, "dashboard_data.json")
    with open(data_file, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print("  Wrote {}".format(data_file))

    eq_series = output.get("equity_series", {})
    if eq_series:
        eq_file = os.path.join(out_dir, "equity_history.json")
        with open(eq_file, "w") as f:
            json.dump(eq_series, f, indent=2)
        print("  Wrote {}".format(eq_file))

    print("[{}] Done.".format(datetime.now(KST).strftime("%H:%M:%S KST")))


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
