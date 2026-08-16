#!/usr/bin/env python3
"""Dashboard pipeline healthcheck.

Run by cron every 15 min. Alerts Discord ONLY on status transitions
(OK->DOWN, OK->STALE, ... and recovery back to OK) so it never spams. Turns the
silent-freeze failure mode (dead API -> cron no-ops -> frozen dashboard) into a
visible alert.

Statuses, in priority order:
  DOWN       - the operational API (localhost:8077) is unreachable.
  STALE      - published data is older than the last scheduled push.
  PUSH_STUCK - data regenerated and committed, but `git push` has been failing
               for > 2h, so the PUBLIC site is frozen while the server looks
               healthy. (The failure mode a freshness-only check cannot see.)
  CONTENT    - the JSON is fresh and published, but its *content* broke the
               whole-investment contract (a bot vanished from the chart, the
               Toss account lies about being fresh, totals do not reconcile).
  OK         - everything above passes.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API_URL = "http://localhost:8077/api/data"
DATA_FILE = os.path.join(HERE, "docs", "data", "dashboard_data.json")
STATE_FILE = os.path.join(HERE, ".healthcheck_state.json")

KST = timezone(timedelta(hours=9))

# --- Content contract (see docs/runbooks/dashboard-pipeline.md) --------------
# Every bot must be plottable in the comparison chart; claude_bot was silently
# missing for months, which is exactly what this check exists to catch.
CONTENT_REQUIRED_EQUITY_KEYS = ("claude_bot",)
# The whole-investment view is meaningless without the AI bot and the
# hands-on/manual sleeve.
CONTENT_REQUIRED_STRATEGY_IDS = ("claude_bot", "manual")
CONTENT_REQUIRED_ACCOUNTS = ("kis", "upbit", "toss")
# The Toss snapshot is pushed from the Mac twice a day; > 30h means the launchd
# job died. A *truthful* stale flag is acceptable (the hero greys the tile out);
# a fresh flag over an ancient timestamp is not.
TOSS_MAX_AGE_HOURS = 30
# How long unpushed commits may sit before the public site counts as frozen.
PUSH_STUCK_HOURS = 2

# Scheduled dashboard pushes, in KST, mirroring the crontab:
#   0 16 * * 1-5  -> Mon-Fri 16:00 (after KR close)
#   30 6 * * 2-6  -> Tue-Sat 06:30 (after US close)
# Python's weekday(): Mon=0 .. Sun=6.
PUSH_SCHEDULE = (
    (16, 0, frozenset({0, 1, 2, 3, 4})),   # Mon-Fri 16:00 KST
    (6, 30, frozenset({1, 2, 3, 4, 5})),   # Tue-Sat 06:30 KST
)
# Minutes a scheduled push is allowed to take (generate + push) before its
# absence counts as a miss. The generator finishes in seconds; 30 min is slack.
GRACE_MINUTES = 30


def _parse_updated_at(s):
    """Parse 'YYYY-MM-DD HH:MM KST' -> aware datetime (KST = UTC+9)."""
    if not s:
        return None
    try:
        base = s.replace(" KST", "")
        dt = datetime.strptime(base, "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=KST)
    except (ValueError, TypeError):
        return None


def last_expected_push(now, grace_minutes=GRACE_MINUTES):
    """Most recent scheduled push (KST) that should already have completed.

    Returns an aware KST datetime, or None if no push has come due in the
    lookback window. Markets are closed on weekends, so there is simply no
    scheduled push between Sat 06:30 and Mon 16:00 -- the dashboard is *meant*
    to hold Friday's close over the weekend, and that must NOT read as stale.
    """
    cutoff = now.astimezone(KST) - timedelta(minutes=grace_minutes)
    best = None
    for d in range(0, 9):
        day = (cutoff - timedelta(days=d)).date()
        for hour, minute, weekdays in PUSH_SCHEDULE:
            if day.weekday() in weekdays:
                cand = datetime(day.year, day.month, day.day, hour, minute, tzinfo=KST)
                if cand <= cutoff and (best is None or cand > best):
                    best = cand
    return best


def _parse_iso(s):
    """Parse an ISO-8601 timestamp -> aware datetime. Naive input is read as KST."""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=KST)


def content_issues(data, now, toss_max_age_hours=TOSS_MAX_AGE_HOURS):
    """Return a list of content-contract violations (empty == healthy). Pure.

    Freshness says the pipeline *ran*; this says it produced something honest.
    """
    if not isinstance(data, dict):
        return ["dashboard_data.json missing or unreadable"]

    issues = []

    equity = data.get("equity_series")
    equity = equity if isinstance(equity, dict) else {}
    for key in CONTENT_REQUIRED_EQUITY_KEYS:
        if key not in equity:
            issues.append("equity_series missing '{}'".format(key))

    strategies = data.get("strategies")
    strategies = strategies if isinstance(strategies, list) else []
    strat_ids = {s.get("id") for s in strategies if isinstance(s, dict)}
    for sid in CONTENT_REQUIRED_STRATEGY_IDS:
        if sid not in strat_ids:
            issues.append("strategies missing id '{}'".format(sid))

    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        issues.append("top-level 'accounts' missing")
    else:
        for name in CONTENT_REQUIRED_ACCOUNTS:
            if not isinstance(accounts.get(name), dict):
                issues.append("accounts.{} missing".format(name))
        toss = accounts.get("toss")
        # Only a *claim of freshness* is auditable. When stale=true the
        # generator moves the real timestamp to last_seen_at and nulls as_of,
        # which is honest reporting, not a fault.
        if isinstance(toss, dict) and not toss.get("stale", False):
            as_of = _parse_iso(toss.get("as_of"))
            if as_of is None:
                issues.append("accounts.toss claims fresh but as_of is missing/unparseable")
            else:
                age_h = (now - as_of).total_seconds() / 3600.0
                if age_h > toss_max_age_hours:
                    issues.append(
                        "accounts.toss claims fresh but as_of is {:.1f}h old".format(age_h))

    totals = data.get("totals")
    if not isinstance(totals, dict):
        issues.append("top-level 'totals' missing")
    elif totals.get("reconciliation_warning") is True:
        issues.append("totals.reconciliation_warning is true (gap {} KRW)".format(
            totals.get("reconciliation_gap_krw")))

    return issues


def push_stuck(unpushed_commit_epochs, now, max_age_hours=PUSH_STUCK_HOURS):
    """True when the OLDEST unpushed commit has been sitting too long. Pure.

    Commits land locally on every generate; the public Pages site only moves on
    a successful `git push`. A push that has been failing for hours leaves the
    server perfectly healthy and the public site frozen.
    """
    epochs = [e for e in (unpushed_commit_epochs or []) if isinstance(e, (int, float))]
    if not epochs:
        return False
    return (now.timestamp() - min(epochs)) / 3600.0 > max_age_hours


def classify_status(api_ok, updated_at, now, grace_minutes=GRACE_MINUTES,
                    data=None, push_is_stuck=False):
    """Return 'DOWN' | 'STALE' | 'PUSH_STUCK' | 'CONTENT' | 'OK'. Pure.

    Schedule-aware: data is STALE only when it is older than the most recent
    push that should already have run. A flat 'N hours old' threshold raised
    false weekend alarms (Sat 06:30 -> Mon 16:00 is ~57h with no push due).

    `data` and `push_is_stuck` are optional so the pre-existing three-status
    call signature keeps working unchanged.
    """
    if not api_ok:
        return "DOWN"
    ts = _parse_updated_at(updated_at)
    if ts is None:
        return "STALE"
    expected = last_expected_push(now, grace_minutes)
    if expected is not None and ts < expected:
        return "STALE"
    # A frozen public site outranks a content nit: nobody can see the content.
    if push_is_stuck:
        return "PUSH_STUCK"
    if data is not None and content_issues(data, now):
        return "CONTENT"
    return "OK"


def should_alert(prev, curr):
    """Alert only when status changes (incl. recovery to OK)."""
    return prev != curr


def alert_message(status, updated_at, detail=None):
    detail_txt = ""
    if detail:
        detail_txt = "\n" + "\n".join("- {}".format(d) for d in detail)
    if status == "DOWN":
        return "\U0001F534 Quanty dashboard API is DOWN (localhost:8077 unreachable). Data is not updating."
    if status == "STALE":
        return "\U0001F7E0 Quanty dashboard data is STALE (last update {}). Pipeline may be frozen.".format(updated_at)
    if status == "PUSH_STUCK":
        return ("\U0001F7E0 Quanty dashboard commits are NOT reaching GitHub Pages "
                "(unpushed for > {}h). The public site is frozen while the server "
                "looks healthy.{}").format(PUSH_STUCK_HOURS, detail_txt)
    if status == "CONTENT":
        return ("\U0001F7E1 Quanty dashboard data is fresh but its CONTENT broke the "
                "whole-investment contract (last update {}).{}").format(updated_at, detail_txt)
    return "\U0001F7E2 Quanty dashboard recovered -- data is fresh again (last update {}).".format(updated_at)


def _api_ok():
    try:
        with urllib.request.urlopen(API_URL, timeout=15) as r:
            if r.status != 200:
                return False
            json.loads(r.read())
            return True
    except Exception:
        return False


def _read_data():
    """Published dashboard_data.json as a dict, or None if unreadable."""
    try:
        with open(DATA_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _unpushed_commit_times(repo_dir=HERE):
    """Author epochs of commits on main that origin/main does not have.

    Returns [] on any git error (no remote ref, detached head, git missing) --
    an unknown push state must never masquerade as an alert.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", repo_dir, "log", "origin/main..main", "--format=%ct"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    out = proc.stdout.decode("utf-8", "replace")
    return [int(tok) for tok in out.split() if tok.isdigit()]


def _load_prev_status():
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("status", "OK")
    except Exception:
        return "OK"


def _save_status(status):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"status": status}, f)
    os.replace(tmp, STATE_FILE)


def _send_discord(message):
    try:
        import requests
        import notify_discord
        url = notify_discord.get_webhook_url()
        if not url:
            print("healthcheck: no Discord webhook url", file=sys.stderr)
            return
        requests.post(url, json={"content": message}, timeout=10)
    except Exception as e:
        print("healthcheck: discord send failed: {}".format(e), file=sys.stderr)


def main():
    now = datetime.now(timezone.utc)
    data = _read_data()
    updated_at = (data or {}).get("updated_at")
    unpushed = _unpushed_commit_times()
    stuck = push_stuck(unpushed, now)
    status = classify_status(_api_ok(), updated_at, now, data=data, push_is_stuck=stuck)

    detail = None
    if status == "CONTENT":
        detail = content_issues(data, now)
    elif status == "PUSH_STUCK":
        detail = ["{} commit(s) waiting, oldest {:.1f}h old".format(
            len(unpushed), (now.timestamp() - min(unpushed)) / 3600.0)]

    prev = _load_prev_status()
    if should_alert(prev, status):
        _send_discord(alert_message(status, updated_at, detail))
    _save_status(status)
    print("[{}] healthcheck status={} (prev={}){}".format(
        now.isoformat(), status, prev,
        " detail=" + "; ".join(detail) if detail else ""))


if __name__ == "__main__":
    main()
