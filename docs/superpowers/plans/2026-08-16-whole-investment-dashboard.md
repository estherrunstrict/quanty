# Whole-Investment Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Tasks are ordered by dependency; do NOT reorder. Each task ends with acceptance criteria — do not proceed until they pass.

**Goal:** The public dashboard (https://estherrunstrict.github.io/quanty/) must show Jae's *whole* investment status, not just the bot sleeves:

1. The "AI Market Intelligence" bot (`claude_bot`) is **one of the bots** — it must appear in the comparison graph and in total investing status exactly like every other bot (today it is missing from the chart's hardcoded series list).
2. A **Hands-on / Manual** sleeve — KIS holdings Jae bought manually that belong to no bot — must be computed, shown as its own card, and counted in totals.
3. The **Toss Securities account** (NMF2 sleeve + any manual Toss holdings) must be included via a read-only snapshot.
4. A redesigned hero: **TOTAL INVESTMENTS = KIS + Upbit + Toss**, with a Bots / Hands-on / Cash split.

**Architecture:** All changes are **reporting-side only**. `dashboard_server.py` (operational API, systemd `quanty-dashboard-api`) and `quanty-dashboard/generate_dashboard_data.py` (public generator, cron 2×/day) are extended; live bots are untouched. Toss data enters via a Mac-side read-only snapshot scp'd to the server (Toss IP-allowlist stays Mac-only). Manual-sleeve history is persisted to a new reporting-side JSONL.

**Tech stack:** Python 3 stdlib + existing helpers, vanilla JS, pytest, scp/launchd.

---

## ⛔ Guardrails (read before every task)

- **NEVER edit:** any `*AutoTrade*.py`, `check_and_run.sh`, `SecondLevelBot.py`, remote crontab, any live `*_state.json`, `config.yaml`'s `capital_management` budgets. (Quanty paper/live isolation rule.)
- Toss code is **read-only**: token + accounts + holdings + cash endpoints only. No order code, ever.
- `quanty-dashboard/` local scratch copy is **NOT a git repo** — deploy via scp, commit on the server (same convention as the 2026-06-03 approval-buttons plan). `automation_oracle/` changes are committed locally on a feature branch and pushed to the `mine` remote.
- The public `docs/index.html` and `automation_oracle/dashboard.html` are **dual-edited copies** — every front-end change in this plan must be applied to BOTH (Task 7 reduces this risk but does not remove it).
- After every deploy: run `python3 quanty-dashboard/dashboard_healthcheck.py` on the server and confirm `OK`.
- Secrets: Toss credentials live only in a gitignored `.env` on the Mac. Never scp them, never commit them.

## Repos, paths & file map

Server (production):
- Bots + API: `/home/ubuntu/koreainvestment-autotrade/` (= local `automation_oracle/`)
- Public generator + Pages repo: `/home/ubuntu/quanty-dashboard/` → published `docs/`
- SSH: `ssh -i ~/.ssh/oci_rsa ubuntu@193.123.246.52`

Local (scratch = `/Users/jaelee/.gemini/antigravity/scratch/quanty/`):

| Action | File |
|---|---|
| Modify | `automation_oracle/dashboard.html` — chart series, hero, manual card |
| Modify | `automation_oracle/dashboard_server.py` — card rename, `accounts`/`totals` plumbing |
| Modify | `automation_oracle/scripts/aggregate_realized_pnl.py` — claude ticker attribution |
| Create | `automation_oracle/framework/brokers/toss_readonly.py` — vendored read-only client |
| Create | `automation_oracle/scripts/toss_snapshot.py` — Mac-side snapshot writer |
| Create | `automation_oracle/scripts/launchd/com.quanty.toss-snapshot.plist` — Mac schedule (Jae arms it) |
| Create | `automation_oracle/tests/test_dashboard/test_manual_sleeve.py` |
| Create | `automation_oracle/tests/test_dashboard/test_toss_snapshot.py` |
| Modify | `quanty-dashboard/generate_dashboard_data.py` — manual sleeve, toss ingest, totals |
| Modify | `quanty-dashboard/dashboard_healthcheck.py` — content checks |
| Modify | `quanty-dashboard/docs/decision.html` — (optional) hero totals |
| Modify (server copy) | `quanty-dashboard/docs/index.html` — same patch as `dashboard.html` |
| Modify | `automation_oracle/docs/runbooks/dashboard-pipeline.md` — runbook updates |

New data files (server, all reporting-side):
- `strategy_results/toss_snapshot.json` (written from Mac via scp)
- `strategy_results/manual_sleeve_history.jsonl` (written by generator)

---

## Task 0: Server-side verification (read-only, no changes)

**Files:** none. Output: a `## Task 0 findings` note at the top of your working branch's PR/commit message.

- [ ] **Step 1: Confirm `claude_bot` has equity snapshots**
```bash
ssh -i ~/.ssh/oci_rsa ubuntu@193.123.246.52 \
  "tail -3 /home/ubuntu/koreainvestment-autotrade/strategy_results/equity_history.jsonl | python3 -c 'import sys,json; [print(sorted(json.loads(l).keys())) for l in sys.stdin]'"
```
Expect `claude_bot` in the key list (it goes through the same `_save_equity_snapshot` loop, `dashboard_server.py:255-330`). If absent, STOP and report — Task 1 assumptions change.

- [ ] **Step 2: Capture the AI bot's live watchlist tickers**
```bash
ssh -i ~/.ssh/oci_rsa ubuntu@193.123.246.52 \
  "sed -n '65,85p' /home/ubuntu/koreainvestment-autotrade/ClaudeTradingBotAutoTrade.py"
```
Record the exact ticker list (expected: EQIX, DLR, AMT, CCI, MSFT, GOOGL, NVDA, …). Needed in Task 2. **Read-only — do not edit this file.**

- [ ] **Step 3: Baseline snapshot** — save current published JSON for before/after diff:
```bash
curl -s https://estherrunstrict.github.io/quanty/data/dashboard_data.json > /tmp/dashboard_baseline.json
```

- [ ] **Step 4: Confirm cron + push_dashboard.sh** (`crontab -l` on server; note the two publish times: Mon–Fri 16:00 KST, Tue–Sat 06:30 KST).

**Acceptance:** all four findings recorded; no server file modified.

---

## Task 1: Put the AI bot in the comparison graph (the missing line)

**Root cause:** `buildComparisonChart()` hardcodes 7 series and omits `claude_bot` — `automation_oracle/dashboard.html:773-782`.

**Files:** `automation_oracle/dashboard.html` + server's `quanty-dashboard/docs/index.html` (pull live copy first, patch identically).

- [ ] **Step 1:** Pull the live Pages copy so you patch what's actually served:
```bash
scp -i ~/.ssh/oci_rsa ubuntu@193.123.246.52:/home/ubuntu/quanty-dashboard/docs/index.html \
  /Users/jaelee/.gemini/antigravity/scratch/quanty/quanty-dashboard/docs/index.html
```

- [ ] **Step 2:** In BOTH files, add to `seriesMeta` (after `dual_momentum`, before the hybrid legs):
```js
    claude_bot:    { name: 'Claude Trading Bot',      stratId: 'claude_bot',    color: '#5a9a8a' },
```
Also update the comment above the function: "all 7 bots" → "all 8 bots".

- [ ] **Step 3:** Check the card renderer's special case at `dashboard.html:~1211` (`s.id === 'claude_bot'`): keep the intelligence tiles (regime/breadth/VCP), but make sure the card ALSO shows the standard money row (value, Total P/L, win rate) like other bots. If it already does, no change.

- [ ] **Step 4: Deploy** both copies:
```bash
scp -i ~/.ssh/oci_rsa automation_oracle/dashboard.html ubuntu@193.123.246.52:/home/ubuntu/koreainvestment-autotrade/dashboard.html
scp -i ~/.ssh/oci_rsa quanty-dashboard/docs/index.html ubuntu@193.123.246.52:/home/ubuntu/quanty-dashboard/docs/index.html
ssh -i ~/.ssh/oci_rsa ubuntu@193.123.246.52 "cd /home/ubuntu/quanty-dashboard && git add docs/index.html && git commit -m 'chart: add claude_bot series' && git push"
```

**Acceptance:** after the next publish (or manual `push_dashboard.sh` run), the public comparison chart legend lists **Claude Trading Bot** (8 entries + 2 hybrid legs), with a plotted line or "insufficient data" if < 2 snapshots exist. Healthcheck `OK`.

---

## Task 2: Honest naming + realized-P/L ticker attribution

**Files:** `automation_oracle/dashboard_server.py`, `automation_oracle/scripts/aggregate_realized_pnl.py`.

- [ ] **Step 1: Rename the card** at `dashboard_server.py:812`: `"name": "AI Market Intelligence"` → `"name": "Claude Trading Bot"`. Keep `id: "claude_bot"` and every `extra` field unchanged (the id is load-bearing: equity keys, REALIZED_KEY_BY_ID, front-end special case).

- [ ] **Step 2: Add claude-exclusive tickers to `TICKER_TO_STRATEGY`** (`aggregate_realized_pnl.py:69`). Using the Task 0 Step 2 list, add every ticker **not already claimed by another bot**:
```python
    # Claude Trading Bot (AI-curated watchlist — exclusive names only;
    # NVDA/MSFT/GOOGL stay with JD_STRATEGY, see collision note below)
    'EQIX': 'CLAUDE_AI_BOT', 'DLR': 'CLAUDE_AI_BOT', 'AMT': 'CLAUDE_AI_BOT',
    'CCI': 'CLAUDE_AI_BOT',  # …extend with the actual Task-0 list
```
Do **NOT** remap NVDA/MSFT/GOOGL (shared with JD): JD stays canonical, same convention as the 144600/132030 HybridVB-KR note already in the file.

- [ ] **Step 3: Understand + verify the behavior change.** Adding CLAUDE_AI_BOT values to the map puts it in `kis_bot_keys` (`aggregate_realized_pnl.py:556`), so the KIS pass now **resets and rebuilds** its realized P/L from broker records instead of `claude_trading_bot_state.json`. That is more authoritative but must be checked:
```bash
ssh -i ~/.ssh/oci_rsa ubuntu@193.123.246.52 "cd /home/ubuntu/koreainvestment-autotrade && ~/myenv/bin/python3 scripts/aggregate_realized_pnl.py --year 2026 --dry-run" # use the script's actual flags; if no dry-run flag exists, run it and diff strategy_results/realized_pl_2026.json against a saved copy
```
Compare CLAUDE_AI_BOT and JD_STRATEGY realized_ytd before/after. Expected: CLAUDE_AI_BOT moves to `source: kis_*`; JD unchanged (collision tickers were already JD's). If JD's number changes materially (> ₩100k), STOP and report before deploying.

- [ ] **Step 4: Document the known collision** in the file header: claude-bot trades in NVDA/MSFT/GOOGL are booked to JD_STRATEGY; per-bot precision for those three names is a known limitation (fixing it needs order-id-level attribution — out of scope).

- [ ] **Step 5: Deploy** `dashboard_server.py` + `aggregate_realized_pnl.py` via scp, then restart the API using the existing script: `bash automation_oracle/deploy_dashboard.sh` (it verifies a fresh restart). Confirm `curl -s localhost:8077/api/data | python3 -m json.tool | grep -A2 claude` on the server shows the new name.

**Acceptance:** card renamed on both dashboards; `realized_pl_2026.json` has a sane CLAUDE_AI_BOT row; unattributed-ticker error count in the aggregator log decreases; JD realized P/L unchanged (±₩100k).

---

## Task 3: Hands-on / Manual sleeve (KIS holdings that belong to no bot)

**Files:** `quanty-dashboard/generate_dashboard_data.py` (+ its `query_account_total.py` if holdings listing is missing), new test `automation_oracle/tests/test_dashboard/test_manual_sleeve.py`.

**Definition (implement exactly this):**
- Inputs: (a) full KIS account holdings — domestic and overseas — as `{ticker: {qty, value_native, currency, name}}`; (b) every bot card's holdings from `strategies[]` as `{ticker: qty}` summed across bots.
- For each account ticker: `manual_qty = account_qty − bots_qty`. If `manual_qty > 0`, the manual sleeve gets that ticker at `manual_qty × current_price` (reuse the mark-to-market prices already fetched by `mark_to_market_strategies`, `generate_dashboard_data.py:266-326`).
- Value in KRW using the generator's FX rate. Cash is NOT part of the manual sleeve (it's a separate `totals.cash_krw`).

- [ ] **Step 1:** Add `get_all_holdings()` to `query_account_total.py` if not present (KIS domestic `inquire-balance` + overseas balance, both already used elsewhere in the pipeline — copy the auth/session pattern from `get_account_totals`). Include the memory-noted accuracy fields (`prvs_rcdl_excc_amt`, `ustl_sll_amt_smtl`) only for totals, not per-holding.

- [ ] **Step 2:** In `generate_dashboard_data.py`, after `mark_to_market_strategies`, add `build_manual_sleeve(all_holdings, strategies, fx_rate) -> dict`. **Pure function, no I/O** — testable locally with fixtures. Emit a strategy-shaped card appended to `strategies`:
```json
{"id": "manual", "name": "Hands-on / Manual", "currency": "KRW",
 "value": 0.0, "holdings": [{"ticker": "...", "name": "...", "qty": 0, "value_krw": 0.0}],
 "extra": {"note": "KIS holdings not attributed to any bot", "ticker_count": 0}}
```

- [ ] **Step 3: Persist manual history** — append `{date, value_krw}` to `strategy_results/manual_sleeve_history.jsonl` (atomic write via `.tmp` + `os.replace`, same as `dashboard_server.py:343-349`; one row per publish date, replace same-date row). Load it into `equity_series["manual"]` as `[[date, value], …]` so the chart can plot it.

- [ ] **Step 4: Unit tests** (`test_manual_sleeve.py`): (1) bot-owned qty fully subtracts; (2) partial overlap (account 100 NVDA, bots 60 → manual 40); (3) zero/negative diff excluded; (4) overseas ticker converts at FX; (5) empty account holdings → empty sleeve, no crash. Run: `python3 -m pytest tests/test_dashboard/ -q`.

- [ ] **Step 5: Front-end** (both HTML copies): render the `manual` card in the bot grid (grey accent `#8a8a8a`, no win-rate row) and add `manual: { name: 'Hands-on / Manual', stratId: 'manual', color: '#8a8a8a' }` to `seriesMeta`.

- [ ] **Step 6: Deploy** generator + `query_account_total.py` to `/home/ubuntu/quanty-dashboard/`, both HTMLs as in Task 1, commit on server, trigger a manual publish (`bash push_dashboard.sh`), verify.

**Acceptance:** published `dashboard_data.json` contains the `manual` card with plausible holdings (spot-check one known manual position with Jae's data); pytest green; totals task (5) will reconcile it.

---

## Task 4: Toss account snapshot (Mac-side, read-only)

**Files:** `automation_oracle/framework/brokers/toss_readonly.py`, `automation_oracle/scripts/toss_snapshot.py`, `automation_oracle/scripts/launchd/com.quanty.toss-snapshot.plist`, test.

- [ ] **Step 1:** Vendor the existing read-only client: copy `/Users/jaelee/.gemini/antigravity/scratch/quanty/brokers/toss_client.py` → `automation_oracle/framework/brokers/toss_readonly.py` **unchanged except** module docstring noting the vendoring source/date. It is already order-free by design; keep it that way.

- [ ] **Step 2:** `scripts/toss_snapshot.py` — runs **on the Mac** (Toss IP allowlist covers the Mac's IP only; the server would 403):
  - Read `TOSS_CLIENT_ID/SECRET/ACCOUNT_SEQ` from env / gitignored `.env`.
  - Fetch accounts, holdings, cash. Build:
```json
{"as_of": "<ISO8601+09:00>", "account_seq": "1", "total_krw": 0.0, "cash_krw": 0.0,
 "holdings": [{"symbol": "...", "name": "...", "qty": 0.0, "value_krw": 0.0}],
 "source": "mac-launchd"}
```
  - Write to a local temp file, then `scp -i ~/.ssh/oci_rsa <tmp> ubuntu@193.123.246.52:/home/ubuntu/koreainvestment-autotrade/strategy_results/toss_snapshot.json`.
  - Toss quirks (from the client's docstring/memory): currency param required; use `symbol` not ticker; account-scoped calls need `X-Tossinvest-Account`.
  - Exit non-zero on any failure; NEVER write a partial snapshot.

- [ ] **Step 3:** launchd plist (label `com.quanty.toss-snapshot`, daily 06:00 KST + 15:50 KST, `StandardErrorPath` to `~/Library/Logs/quanty-toss-snapshot.log`). **Do not `launchctl load` it — Jae arms launchd himself** (house convention from the NMF2 bot).

- [ ] **Step 4:** Generator ingest in `generate_dashboard_data.py`: read `strategy_results/toss_snapshot.json`; compute `stale = (now − as_of) > 30h`; emit into `accounts.toss` (schema in Task 5). Stale or missing → `{"total_krw": 0, "stale": true, "as_of": null}` and the hero shows a grey "Toss: no data" tile — never crash, never silently omit the key.

- [ ] **Step 5:** Unit test with a fixture snapshot file: fresh vs stale vs missing.

**Acceptance:** running `python3 scripts/toss_snapshot.py` on the Mac (Jae supplies `.env`) lands a valid JSON on the server; generator emits `accounts.toss` in all three states; nothing Toss-related runs on the server itself.

---

## Task 5: `accounts` + `totals` contract and the Whole-Investment hero

**Files:** `quanty-dashboard/generate_dashboard_data.py`, both HTML copies, optionally `docs/decision.html`.

- [ ] **Step 1:** Extend the generator output (top-level keys, additive — never remove existing keys):
```json
"accounts": {
  "kis":   {"total_krw": 0.0, "cash_krw": 0.0, "as_of": "..."},
  "upbit": {"total_krw": 0.0, "as_of": "..."},
  "toss":  {"total_krw": 0.0, "cash_krw": 0.0, "as_of": "...", "stale": false}
},
"totals": {
  "investments_krw": 0.0,
  "bots_krw": 0.0, "manual_krw": 0.0, "cash_krw": 0.0,
  "split_pct": {"bots": 0.0, "manual": 0.0, "cash": 0.0},
  "reconciliation_gap_krw": 0.0
}
```
  - `investments_krw = kis + upbit + toss` totals.
  - `bots_krw` = Σ bot card values (KRW-converted) **excluding** `manual`; `manual_krw` from Task 3; `cash_krw` = Σ account cash.
  - `reconciliation_gap_krw = investments_krw − (bots_krw + manual_krw + cash_krw)`. If `|gap| / investments > 1%`, log a warning line into the generator output and set a `"reconciliation_warning": true` flag in `totals`.
  - KIS values reuse the existing resilient totals path (`get_account_totals_resilient`); Upbit reuses the existing state-file equity.

- [ ] **Step 2: Hero redesign** (both HTML copies): replace the current single portfolio strip with:
  - Row A: **TOTAL INVESTMENTS** big number (`totals.investments_krw`, ≈USD) + overall P/L (keep the existing deposit-based P/L% for continuity).
  - Row B: three account tiles — KIS / Upbit / Toss (value + as-of; grey + "stale" badge when `toss.stale`).
  - Row C: horizontal split bar — Bots % / Hands-on % / Cash % from `totals.split_pct`, with a small ⚠ icon when `reconciliation_warning`.
  - Keep every existing section below (bot grid incl. `claude_bot` + `manual`, comparison chart, aggregate chart, feed).
  - Old clients: all new keys are additive, so the old HTML keeps working during rollout. Deploy JSON first, HTML second.

- [ ] **Step 3 (optional, small):** `decision.html` hero "Total Portfolio" number → read `totals.investments_krw` when present, else fall back to current field.

- [ ] **Step 4: Deploy + manual publish + eyeball** on the live URL (desktop + phone width).

**Acceptance:** hero shows KIS+Upbit+Toss total; split bar sums to 100±1%; `reconciliation_gap_krw` under 1% (if not, investigate before closing the task — usually a bot card double-counting cash); nothing previously on the page disappeared.

---

## Task 6: Healthcheck content checks

**Files:** `quanty-dashboard/dashboard_healthcheck.py`.

- [ ] **Step 1:** After the existing freshness check, add content assertions on the local `docs/data/dashboard_data.json`:
  - `equity_series` contains a `claude_bot` key.
  - `strategies` contains ids `claude_bot` and `manual`.
  - `accounts.toss.as_of` fresher than 30h **only when** `accounts.toss.stale == false` (a truthful stale flag is OK, a lying fresh flag is not).
  - `totals.reconciliation_warning` is not `true`.
  Failures → same debounced Discord path as DOWN/STALE, new status string `CONTENT`.
- [ ] **Step 2:** Push-success check: `git -C /home/ubuntu/quanty-dashboard log origin/main..main --oneline` non-empty for > 2h → alert `PUSH_STUCK` (catches the frozen-public-site failure mode).
- [ ] **Step 3:** Unit-test `classify_status` extensions with fixture JSONs; deploy via scp.

**Acceptance:** healthcheck cron stays green on the healthy system; hand-corrupting a copy of the JSON in a test flags `CONTENT`.

---

## Task 7: Hygiene — version-control the orchestrator + runbook

**Files:** `quanty-dashboard/push_dashboard.sh` (pull from server), `automation_oracle/docs/runbooks/dashboard-pipeline.md`.

- [ ] **Step 1:** `scp` the server's `/home/ubuntu/quanty-dashboard/push_dashboard.sh` down, commit it on the server repo (`git add push_dashboard.sh`) so the publish orchestrator is finally versioned. Do not modify its logic in this task.
- [ ] **Step 2:** Update the runbook: new data files (`toss_snapshot.json`, `manual_sleeve_history.jsonl`), the Mac launchd job + how Jae arms it, new healthcheck statuses (`CONTENT`, `PUSH_STUCK`), the accounts/totals JSON contract, and the dual-edit HTML warning.
- [ ] **Step 3:** Commit `automation_oracle` changes on the feature branch, push to `mine`.

**Acceptance:** `git -C /home/ubuntu/quanty-dashboard status` clean with push_dashboard.sh tracked; runbook covers every new moving part.

---

## Rollout order & rollback

Deploy strictly in task order (0→7). Each task is independently rollback-able: scp the previous file version back (server keeps `.bak` copies — make one before every overwrite: `cp X X.bak.$(date +%Y%m%d)`). JSON contract changes are additive, so the old front-end never breaks against the new JSON.

## Out of scope (tracked separately — from the 2026-08-16 dashboard review)

- Publishing SecondLevelBot's cycle gauge / allocation multiplier.
- Regime guidance text + memo bodies on the public site.
- Markov engine persistence/scheduling.
- korea-etf-momentum 518% MDD artifact in `equity_history.jsonl` (empty-holdings dropout poisoning history).
- The 3 overdue decision memos.
