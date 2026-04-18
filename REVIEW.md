# Architecture Review — Automation Oracle

Scope: the active code path reachable from `check_and_run.sh`. Dated
backup files (`*_20YYMMDD.py`) and vendored submodules (`open-trading-api/`,
`koreainvestment-autotrade/`, `claude-trading-bot/`) are excluded.

Severity legend: **HIGH** = causes wrong-money or missed/duplicate trades;
**MEDIUM** = data corruption, missed signals, or silent failure;
**LOW** = hygiene, drift, or minor readability.

---

## check_and_run.sh

- **`check_and_run.sh:35,57-58,195-221,253-261,346`** — **HIGH** — `rm -f`
  on shared state files while bots may be in-flight. On DST edge or a
  missed cron cycle, a second invocation can delete state the bot is
  mid-write. Replace with advisory lock or require the bot to clear its
  own sentinel on exit.
- **`check_and_run.sh:55-68`** — **HIGH** — VB state reset fires at
  15:55 ET, but BTC VB is 24/7. If a crypto trade is mid-flight (e.g.,
  an exit order awaiting fill) the reset kills the tmux session and
  clears state between write and confirmation. Gate the reset on
  "no open position" instead of a wall-clock minute.
- **`check_and_run.sh:73,187,225,240`** — **HIGH** — Time gates use
  exact-minute equality (`eq 0`, `eq 16`). Cron grace is ~1 min; if cron
  fires at `:01` (ntp skew, load spike) the entire window is missed for
  the day. Widen to a 2–3 minute window or use a "run within window if
  sentinel missing" guard.
- **`check_and_run.sh:283-341`** — **HIGH** — 09:30 ET handler forks a
  detached subshell with 4 sequential `sleep 30`s plus a trailing
  `sleep 300`. If cron re-fires before the subshell exits, orphans
  accumulate and `portfolio_summary.py` runs concurrently with another
  summary run. Use per-bot sentinels and a single `wait`-style
  completion file instead of sleeps.
- **`check_and_run.sh:123-128`** — **HIGH** — `USA_SCRIPT="UsaStockAutoTrade.py"`
  points at the **legacy** repo-root script (337 lines, float money,
  config.yaml requires `APP_KEY` which is no longer there). The modern
  Quant40 result comes from `framework/live_engine.py`, not this file.
  Either delete the legacy script or point `USA_SCRIPT` at the framework
  runner (`scripts/run_strategy.py`).
- **`check_and_run.sh:4,77,231`** — **LOW** — `$SCRIPT_DIR` is quoted in
  tmux commands but session names (`$USA_TMUX_SESSION`, etc.) are not.
  Safe today because names are hand-coded, but a space-bearing rename
  would silently break session targeting.
- **`check_and_run.sh:170-177`** — **LOW** — Korea-time gate uses
  string/int comparisons of `current_hour` as `10#$HH` — fine, but the
  gate ignores market holidays (won't run on closed KR days). Acceptable
  today; note for later.
- **`check_and_run.sh:338-340`** — **MEDIUM** — `portfolio_summary.py`
  runs in the detached subshell without `set -o pipefail` or error
  trap. On failure, cron log shows nothing — only the stderr log
  records it, and the summary is skipped silently.

## portfolio_summary.py

- **`portfolio_summary.py:22`** — **MEDIUM** — `STALE_THRESHOLD_HOURS = 6`
  is too tight for weekly/monthly rebalancers (Korea ETF is monthly,
  Uranium is longer). Bots will show ⚠️ stale on any day they didn't
  run, which is most days.
- **`portfolio_summary.py:73-75`** — **MEDIUM** — `json.load(f)` reads
  bot result files without flocking. A bot writing its result mid-read
  (writer uses direct write, not atomic swap in some cases) yields a
  truncated parse that's silently swallowed and becomes "NO DATA".
- **`portfolio_summary.py:146,184-186,216`** — **HIGH** — `total_profit`
  accumulates KRW and USD as raw floats, then a mixed-currency "total
  profit %" is reported. Numerically meaningless. Track currencies
  separately or FX-convert to a single base.
- **`portfolio_summary.py:181-182`** — **MEDIUM** — Invested-capital
  formula `stock_value - profit if stock_value > profit else
  stock_value` is fragile and wrong when profit is negative (divisor
  becomes larger, underreports loss %). Use `sum(avg_price * qty)` as
  cost basis.
- **`portfolio_summary.py:189-191`** — **HIGH** — `seen_cash` takes
  whichever strategy's `cash_balance` loads first. With 4 US bots
  sharing one KIS account, the "first" one wins — but KRW bots also
  expose `cash_balance`, so the reported figure can be in the wrong
  currency entirely.
- **`portfolio_summary.py:89-96`** — **LOW** — `fromisoformat` then
  re-tagging `tzinfo=timezone.utc` when naive. Bot timestamps are
  written in KST/ET; assuming UTC silently shifts age by 9–14 hours.

## dashboard_server.py

- **`dashboard_server.py:214,553`** — **HIGH** — `build_dashboard_data()`
  runs on every `GET /api/data`. It rereads ~15 JSON files, walks
  `claude-trading-bot/reports/*/`, tails 4 log files, regex-parses 1.2k
  lines each, and appends to `equity_history.jsonl`. No cache. At even
  1 req/sec this saturates disk and corrupts the JSONL via interleaved
  appends.
- **`dashboard_server.py:163-189`** — **HIGH** — `_save_equity_snapshot`
  opens the JSONL in append mode with no lock. Concurrent requests
  produce interleaved writes and duplicate date entries (`if history[-1]
  == today` is racy because `history` is read fresh per call).
- **`dashboard_server.py:588`** — **HIGH** — Server binds to `0.0.0.0`
  with no authentication. Live portfolio values, tickers, P&L exposed
  to anyone on the host's network. Bind to `127.0.0.1` and put behind
  an SSH tunnel (or add a bearer token).
- **`dashboard_server.py:36-42`** — **MEDIUM** — Hand-rolled DST offset
  logic; easy to get wrong on DST-change days (the "second Sunday"
  arithmetic uses `(6 - mar1.weekday()) % 7 + 7` which is correct but
  non-obvious). `ZoneInfo("America/New_York")` is already imported in
  sibling files — use it.
- **`dashboard_server.py:62-67`** — **MEDIUM** — `_file_age_hours` uses
  `datetime.now().timestamp()` (naive local time) vs `os.path.getmtime`
  (epoch, timezone-agnostic). Correct today by coincidence; fragile if
  server TZ shifts.
- **`dashboard_server.py:88-90`** — **MEDIUM** — Log regex assumes a
  fixed `YYYY-MM-DD HH:MM:SS,ms - name - LEVEL - message` format. Any
  logger using a different Formatter produces zero events silently.
- **`dashboard_server.py:46-51, 54-59`** — **MEDIUM** — `_load_json` /
  `_load_yaml` swallow all exceptions and return `None`/`{}`. A partial
  write, wrong permissions, or malformed JSON all look identical to
  "file missing". Log the exception class at least.
- **`dashboard_server.py:131-138`** — **LOW** — Event filtering is a
  cascade of `if "X" in msg` string matches — any bot that changes log
  wording drops off the feed.
- **`dashboard_server.py:512-513`** — **HIGH** — Loads
  `strategy_results/realized_pl_2026.json` but the file is never
  written anywhere. The field is always `{}`. (This is the gap Phase 2
  closes.)
- **`dashboard_server.py:576-578`** — **LOW** — `log_message` silenced
  entirely — access logs are lost, so abuse detection is impossible.

## dashboard.html

- **`dashboard.html:38,330`** — **LOW (policy)** — `--purple: #7a68a8`
  defined and used (`.msg.t-session` border). The warm-earth palette
  forbids purple; fold into `--accent` (amber) or `--blue`.
- **`dashboard.html:30`** — **LOW (contrast)** — `--tx4: #333130` on a
  `#08080a` background is ~1.5:1 contrast. Note in review for a
  possible bump to `#A8A39B` (would meet WCAG AA for non-text).
- **Search across file** — **MEDIUM** — Multiple `el.innerHTML = ...`
  with API data interpolated as `${s.name}` / `${t.ticker}`. If any
  result JSON ever contains hostile content (e.g., scraped news
  headlines), this is XSS. Use `textContent` or a small escape helper.
- **Search across file** — **LOW** — No `Content-Security-Policy` meta.
  Adds defense-in-depth cheaply.
- **`dashboard.html` (profit render)** — **LOW** — `profit` rendered
  without null/NaN guard; a partially-written result file shows
  `$NaN`.

## HybridVBAutoTrade.py

- **`HybridVBAutoTrade.py:662-666`** — **MEDIUM** — `trade_history`
  append is missing a `currency` field, so a downstream aggregator
  can't know whether `pnl` is USD or KRW without re-looking up the
  market. (Phase 2 fixes this.)
- **`HybridVBAutoTrade.py:109-117`** — **LOW** — State schema init
  lives in 3 places (lines 109, 115, 116). Easy to drift; keep one
  canonical factory.
- **`HybridVBAutoTrade.py:640-653`** — **MEDIUM** — Sell path: if
  `place_order_fn` fails, the trade is skipped but the SELL intent is
  lost from logs as a trade event — `trades_executed` is not appended.
  Retrying next tick is fine; surfacing the partial state isn't.
- **`HybridVBAutoTrade.py:38-48`** — **LOW** — `--market` flag with no
  validation on missing/typo'd value (`argparse` choices would help).

## UraniumVBAutoTrade.py

- **`UraniumVBAutoTrade.py:401-405`** — **MEDIUM** — Same as Hybrid:
  trade_history append lacks `currency` (Uranium is always USD, but
  the aggregator shouldn't need to know that out-of-band).
- **`UraniumVBAutoTrade.py:109-110`** — **MEDIUM** — Allocation-override
  `datetime.fromisoformat(ts)` then `datetime.now(override_time.tzinfo
  or timezone.utc)` — if the override's timestamp is naive the
  comparison silently uses UTC, which is not the writer's tz.

## JDStrategyAutoTrade.py

- **`JDStrategyAutoTrade.py`** (state: `jd_strategy_state.json`) —
  **HIGH** — No trade-history list exists. Closed trades leave no
  per-trade record; only the current-holdings snapshot is persisted.
  Realized PnL cannot be reconstructed without log scraping. (Phase 2
  adds a `closed_trades` list.)
- **`JDStrategyAutoTrade.py:1009,1322,1382,1738`** — **MEDIUM** —
  Multiple `datetime.now()` calls without `tz=`. Server runs in UTC;
  result timestamps are written naive, so downstream stale checks
  (`portfolio_summary._check_stale`) misclassify them by ~9–14 hours.
- **`JDStrategyAutoTrade.py:1,50`** — **LOW** — Imports from
  `open-trading-api/examples_llm/...` (vendored examples, not an SDK
  boundary). Any upstream refactor breaks the bot.
- **`JDStrategyAutoTrade.py` (order path)** — **MEDIUM** — No retry on
  Discord webhook POST. A notification failure is silent; operators
  only learn of fills from the log file.

## KoreaETFMomentumAutoTrade.py

- **`KoreaETFMomentumAutoTrade.py`** (state:
  `korea_etf_momentum_state.json`) — **HIGH** — No trade history.
  Rebalance between silver-futures and raw-materials ETFs produces a
  realized PnL on each rotation; today that realized number is
  discarded. (Phase 2 adds a `closed_trades` list.)
- **`KoreaETFMomentumAutoTrade.py:883,921`** — **LOW** — Result JSON
  path computed fresh in a top-level function; duplication with
  `jd`/`hybrid` bots. Acceptable surface area but invites drift.
- **`KoreaETFMomentumAutoTrade.py:167-171`** — **LOW** — State corruption
  backups accumulate without cleanup; disk fills slowly over a year.

## UsaStockAutoTrade.py (repo root — the one `check_and_run.sh` launches)

- **`UsaStockAutoTrade.py:1-15`** — **HIGH** — Requires `APP_KEY`,
  `APP_SECRET`, `CANO`, `ACNT_PRDT_CD`, `DISCORD_WEBHOOK_URL` from
  `config.yaml`, but the current `config.yaml` does **not** have them
  (secrets were moved to `.env`). This script **cannot start**. It is
  the launched USA bot per `check_and_run.sh:124`, so the USA slot is
  effectively dead — the actual Quant40 result appears to come from the
  framework runner on the server. Clarify and remove the dead path.
- **`UsaStockAutoTrade.py:18-23`** — **MEDIUM** — `datetime.datetime.now()`
  (naive) in Discord message; timestamps off by server TZ offset.
- **`UsaStockAutoTrade.py:65`** — **MEDIUM** — Money as `float`; fine
  for this script's purpose but inconsistent with the rest of the
  codebase.
- **`UsaStockAutoTrade.py:336-338`** — **HIGH** — Outer `try/except
  Exception`: on any failure, sends Discord message and exits. Any
  transient network blip = full bot outage until next cron. No retry.

## upbit_vb_strategy_auto_trade.py

- **`upbit_vb_strategy_auto_trade.py`** (state:
  `upbit_vb_strategy_state.json`) — **HIGH** — Only aggregate stats
  (`total_trades`, `winning_trades`, `total_pnl_krw`) are persisted,
  no per-trade list. YTD filtering is impossible without log parsing.
  (Phase 2 adds a `closed_trades` list.)
- **`upbit_vb_strategy_auto_trade.py` (state save)** — **MEDIUM** —
  State written directly (no `os.replace`). A crash mid-write leaves
  truncated JSON; restart reads zero and forgets the held position.
- **`upbit_vb_strategy_auto_trade.py:150-154`** — **LOW** — Upbit keys
  loaded from config — today the config is externalized via `.env`,
  but if someone regresses this to `config.yaml`-only the keys leak on
  any repo clone.

## ClaudeTradingBotAutoTrade.py

- **`ClaudeTradingBotAutoTrade.py`** — **HIGH** — `trade_history` is
  initialized as an empty list but no SELL path appends to it. Same
  gap as JD. (Phase 2.)
- **`ClaudeTradingBotAutoTrade.py:111-132`** — **MEDIUM** — Allocation
  override stale check (>2h) silently defaults to 100% allocation when
  SecondLevelBot hasn't run. Intent is probably the opposite — a
  missing override should be *conservative*, not aggressive.
- **`ClaudeTradingBotAutoTrade.py:184-195`** — **LOW** — `fetch_price_data`
  catches all exceptions and returns `None`; a yfinance outage silently
  degrades to HOLD without alerting.

## ModifiedDualMomentumAutoTrade.py

- **`ModifiedDualMomentumAutoTrade.py`** — **HIGH** — `trade_history`
  initialized but not appended on sell. (Phase 2.)
- **`ModifiedDualMomentumAutoTrade.py` (monthly rebalance check)** —
  **LOW** — Uses month-string equality; skipped on a month where the
  server is down for >24h spanning month boundary.
- **`ModifiedDualMomentumAutoTrade.py` (state save)** — **LOW** — State
  write happens after in-memory position mutation. A crash between
  order placement and state save leaves broker state ahead of bot
  state. (Reconciliation on next start would fix this.)

## SecondLevelBot.py

- **`SecondLevelBot.py`** — **LOW** — Writes `allocation_override.json`
  and `second_level_state.json` without atomic swap. Concurrent reads
  from bots can see partial JSON; today benign because reads tolerate
  empty dict, but fragile.
- **`SecondLevelBot.py`** — **LOW** — `fromisoformat(ts.replace('Z',
  '+00:00'))` assumes trailing-Z ISO; older files (no Z) parse as
  naive and compare wrong.

## framework/

- **`framework/strategy.py` (save_state)** — **LOW** — Atomic swap is
  used here (good); worth extracting as a utility and calling from
  every bot's state write.
- **`framework/types.py` (Portfolio)** — **MEDIUM** — Money fields are
  `float`. Acceptable for display, not for accumulated position
  accounting; use `Decimal` or at least round at boundaries.
- **`framework/live_engine.py` (run())** — **MEDIUM** — Broad
  try/except around the whole run, logs and returns. If an order
  placement raises *after* the state save, the bot forgets there's an
  in-flight order. Add an explicit reconcile step at startup.
- **`framework/config/loader.py` (load_secrets)** — **LOW** — Warns on
  missing env vars and continues. A required secret missing at runtime
  fails with a cryptic 401 later. Exit at load time instead.

## discord_notifier.py

- **`discord_notifier.py`** — **MEDIUM** — Single POST attempt, no
  retry, no backoff. Discord rate limits (429) or transient 502s eat a
  trade notification. Add `tenacity`-style retry or a simple 3x
  exponential loop.
- **`discord_notifier.py`** — **LOW** — Webhook URL is an instance
  attribute; if it lands in a traceback (e.g., `repr(self)` in a log),
  it leaks.

## strategies/ (modern framework vs legacy drift)

- `strategies/quant40.py`, `strategies/jd_strategy.py`, etc., are the
  intended replacements for the root-level `*AutoTrade.py` scripts,
  but `check_and_run.sh` still launches the legacy versions. Two
  implementations of the same strategy can and will diverge. Either
  flip the orchestrator to the framework or delete the modern
  versions. — **LOW (out of scope, note for follow-up).**

---

## Top 5 architectural risks

1. **Launched USA bot cannot start.** `check_and_run.sh:124` points
   at `UsaStockAutoTrade.py` (legacy, requires config keys that no
   longer exist). The Quant40 result file is being written by
   something else entirely. This is a live discrepancy between the
   orchestrator and the codebase.
2. **Currency-mixed totals.** `portfolio_summary.py` sums KRW and USD
   as raw floats and reports a "total profit %". Any dashboard or
   Discord summary that surfaces this number is misleading.
3. **Dashboard server is unbounded work per request and publicly
   bound.** No cache, no auth, `0.0.0.0`. Anyone who can reach the
   host gets live holdings; a modest request rate can corrupt
   `equity_history.jsonl`.
4. **State-file resets race with in-flight bots.** `check_and_run.sh`
   deletes state on minute-boundaries even while BTC VB (24/7) may be
   mid-trade. A late cron fire or DST edge creates observable state
   loss.
5. **Realized PnL is invisible.** Most bots don't persist closed
   trades at all. The dashboard loads a file that no one writes. All
   backward-looking P&L today is unrealized-only — the number most
   operators want (YTD realized) is unavailable. (Phase 2 closes this.)
