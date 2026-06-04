# Decision Approval Buttons Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Jae approve/reject open decision memos with a button on the public dashboard, which flips the memo's `status: proposed → approved|rejected` (records the decision only — never executes it).

**Architecture:** Buttons on the public GitHub Pages Decision Center → Cloudflare **quick tunnel** (HTTPS, outbound, ephemeral URL) → token-gated `decision_approval_server.py` on `127.0.0.1:8078` that edits the memo, regenerates `decisions.json`, and pushes it. A safety-critical pure core (frontmatter transition) is unit-tested; the HTTP/tunnel/git layers wrap it.

**Tech Stack:** Python 3 (stdlib `http.server`), pytest, bash, cloudflared, systemd, vanilla JS `fetch`.

**Spec:** `docs/superpowers/specs/2026-06-03-decision-approval-buttons-design.md`

---

## Repos, paths & file map

Server layout (production):
- Decision memos: `/home/ubuntu/quanty-wiki/wiki/decisions/<id>.md`
- Generator: `/home/ubuntu/quanty-wiki/tools/generate_decisions_json.py`
- Dashboard repo (GitHub Pages): `/home/ubuntu/quanty-dashboard/` → published `docs/`
- Generator env (from `push_dashboard.sh`): `QUANTY_WIKI_DIR=/home/ubuntu/quanty-wiki`, `QUANTY_AUTOMATION_DIR=/home/ubuntu/koreainvestment-autotrade`, `QUANTY_DECISIONS_OUT=/home/ubuntu/quanty-dashboard/docs/data/decisions.json`

Files to create/modify — work in the **local scratch copy** `quanty-dashboard/` (created earlier; NOT a git repo — deploy via scp, commit on the server):
- Create: `quanty-dashboard/decision_approval.py` — pure core (frontmatter transition)
- Create: `quanty-dashboard/tests/test_decision_approval.py`
- Create: `quanty-dashboard/decision_approval_server.py` — HTTP server (auth, routes, publish)
- Create: `quanty-dashboard/start_approval_tunnel.sh` — cloudflared wrapper + URL publisher
- Create: `quanty-dashboard/quanty-decision-approval.service` — systemd unit (server)
- Create: `quanty-dashboard/quanty-approval-tunnel.service` — systemd unit (tunnel)
- Modify: `quanty-dashboard/docs/decision.html` — Approve/Reject buttons + JS (pull from server first)
- Create: `automation_oracle/docs/runbooks/decision-approval.md` — runbook (committed in automation_oracle)

> The pure core and server import nothing server-only at module top, so they unit-test locally. The generator call + git push are isolated functions, skipped in tests via `APPROVAL_SKIP_PUBLISH=1`.

---

## Task 0: Refresh the local scratch copy of decision.html

**Files:** none (setup)

- [ ] **Step 1: Pull the live served file**

Run:
```bash
cd /Users/jaelee/.gemini/antigravity/scratch/quanty/quanty-dashboard
mkdir -p docs/data
scp -i ~/.ssh/oci_rsa ubuntu@193.123.246.52:/home/ubuntu/quanty-dashboard/docs/decision.html ./docs/decision.html
```
Expected: `docs/decision.html` exists locally.

- [ ] **Step 2: Confirm it contains the open_decisions render block**

Run: `grep -n "open_decisions" docs/decision.html`
Expected: a line like `decEl.innerHTML = dec.open_decisions` (the block this plan edits in Task 5). If absent, STOP and report — the served file differs from expectations.

---

## Task 1: Pure frontmatter-transition core (TDD)

The safety-critical logic, isolated and fully unit-tested: read a memo's status, and transition `proposed → approved|rejected` (refusing anything else).

**Files:**
- Create: `quanty-dashboard/decision_approval.py`
- Test: `quanty-dashboard/tests/test_decision_approval.py`

- [ ] **Step 1: Write the failing tests**

Create `quanty-dashboard/tests/test_decision_approval.py`:
```python
"""Unit tests for the decision-approval pure core."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision_approval as da

PROPOSED = """---
type: decision
title: "Test memo"
status: proposed
decision_type: kill
target: [[risk-parity]]
created: 2026-05-25
updated: 2026-05-25
---

# Body stays untouched.
"""


def test_current_status_reads_proposed():
    assert da.current_status(PROPOSED) == "proposed"


def test_transition_approve():
    out, new = da.transition_frontmatter(PROPOSED, "approve", "2026-06-03T10:00:00Z", "2026-06-03")
    assert new == "approved"
    assert "status: approved" in out
    assert "approved_by: jae" in out
    assert "approved_at: 2026-06-03T10:00:00Z" in out
    assert "updated: 2026-06-03" in out
    assert "# Body stays untouched." in out          # body preserved
    assert "status: proposed" not in out


def test_transition_reject():
    out, new = da.transition_frontmatter(PROPOSED, "reject", "2026-06-03T10:00:00Z", "2026-06-03")
    assert new == "rejected"
    assert "status: rejected" in out


def test_transition_refuses_non_proposed():
    approved = PROPOSED.replace("status: proposed", "status: approved")
    try:
        da.transition_frontmatter(approved, "approve", "t", "d")
        assert False, "should have raised"
    except da.NotProposed:
        pass


def test_invalid_action_rejected():
    try:
        da.transition_frontmatter(PROPOSED, "delete", "t", "d")
        assert False
    except ValueError:
        pass


def test_id_regex():
    assert da.VALID_ID.match("2026-05-23-modified-dual-momentum-etf-nav-monitor")
    assert not da.VALID_ID.match("../etc/passwd")
    assert not da.VALID_ID.match("a b")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd quanty-dashboard && python3 -m pytest tests/test_decision_approval.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_approval'`.

- [ ] **Step 3: Write the core module**

Create `quanty-dashboard/decision_approval.py`:
```python
#!/usr/bin/env python3
"""Decision-approval pure core: read/transition a decision memo's status.

Safety-critical and side-effect-free so it can be exhaustively unit-tested.
Only ever transitions `proposed -> approved|rejected`; refuses everything else.
"""
import re

VALID_ACTIONS = {"approve": "approved", "reject": "rejected"}
VALID_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_STATUS_RE = re.compile(r"^status:\s*(\w+)\s*$", re.MULTILINE)
_UPDATED_RE = re.compile(r"^updated:\s*.*$", re.MULTILINE)


class NotProposed(Exception):
    """Raised when a memo is not in `proposed` state (already decided)."""


def current_status(content):
    """Return the frontmatter `status:` value, or None if absent."""
    m = _STATUS_RE.search(content)
    return m.group(1) if m else None


def transition_frontmatter(content, action, now_iso, today):
    """Return (new_content, new_status). Refuses non-proposed memos / bad actions.

    Sets status, bumps `updated`, and records `approved_by`/`approved_at`.
    The body (everything after frontmatter) is untouched.
    """
    if action not in VALID_ACTIONS:
        raise ValueError("invalid action: {}".format(action))
    if current_status(content) != "proposed":
        raise NotProposed("memo is not proposed")
    new_status = VALID_ACTIONS[action]

    out = _STATUS_RE.sub("status: {}".format(new_status), content, count=1)
    if _UPDATED_RE.search(out):
        out = _UPDATED_RE.sub("updated: {}".format(today), out, count=1)
    # Record who/when just after the status line.
    out = out.replace(
        "status: {}".format(new_status),
        "status: {}\napproved_by: jae\napproved_at: {}".format(new_status, now_iso),
        1,
    )
    return out, new_status
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd quanty-dashboard && python3 -m pytest tests/test_decision_approval.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit (local scratch is not a git repo — skip git; controller commits on server later)**

No git here. Just confirm the two files exist:
Run: `ls quanty-dashboard/decision_approval.py quanty-dashboard/tests/test_decision_approval.py`
Expected: both listed.

---

## Task 2: The approval HTTP server

Wraps the core with auth, CORS, rate-limit, memo resolution, regenerate, push, audit.

**Files:**
- Create: `quanty-dashboard/decision_approval_server.py`
- Test: extend `quanty-dashboard/tests/test_decision_approval.py`

- [ ] **Step 1: Write the failing integration tests** (append to `tests/test_decision_approval.py`)

```python
# ── server integration (publish skipped via env) ──
import json, os, threading, urllib.request, urllib.error, importlib


def _start_server(tmp_path, token):
    decisions = tmp_path / "wiki" / "decisions"
    decisions.mkdir(parents=True)
    (decisions / "test-memo.md").write_text(PROPOSED)
    tok = tmp_path / "token"; tok.write_text(token)
    os.environ["APPROVAL_TOKEN_FILE"] = str(tok)
    os.environ["APPROVAL_DECISIONS_DIR"] = str(decisions)
    os.environ["APPROVAL_SKIP_PUBLISH"] = "1"
    os.environ["APPROVAL_ORIGIN"] = "https://estherrunstrict.github.io"
    srv = importlib.import_module("decision_approval_server")
    importlib.reload(srv)
    httpd = srv.build_server(("127.0.0.1", 0))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, decisions


def _post(port, body, token):
    req = urllib.request.Request(
        "http://127.0.0.1:{}/approve".format(port),
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def test_server_rejects_bad_token(tmp_path):
    httpd, _ = _start_server(tmp_path, "good")
    port = httpd.server_address[1]
    code, _ = _post(port, {"id": "test-memo", "action": "approve"}, "wrong")
    assert code == 401
    httpd.shutdown()


def test_server_approves_proposed(tmp_path):
    httpd, decisions = _start_server(tmp_path, "good")
    port = httpd.server_address[1]
    code, data = _post(port, {"id": "test-memo", "action": "approve"}, "good")
    assert code == 200 and data["new_status"] == "approved"
    assert "status: approved" in (decisions / "test-memo.md").read_text()
    httpd.shutdown()


def test_server_409_when_already_decided(tmp_path):
    httpd, decisions = _start_server(tmp_path, "good")
    port = httpd.server_address[1]
    _post(port, {"id": "test-memo", "action": "approve"}, "good")
    code, _ = _post(port, {"id": "test-memo", "action": "reject"}, "good")
    assert code == 409
    httpd.shutdown()


def test_server_404_unknown_and_400_bad_id(tmp_path):
    httpd, _ = _start_server(tmp_path, "good")
    port = httpd.server_address[1]
    assert _post(port, {"id": "nope", "action": "approve"}, "good")[0] == 404
    assert _post(port, {"id": "../x", "action": "approve"}, "good")[0] == 400
    httpd.shutdown()
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd quanty-dashboard && python3 -m pytest tests/test_decision_approval.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_approval_server'`.

- [ ] **Step 3: Write the server**

Create `quanty-dashboard/decision_approval_server.py`:
```python
#!/usr/bin/env python3
"""Token-gated HTTP endpoint to approve/reject decision memos.

Binds 127.0.0.1 only (reached via cloudflared). POST /approve {id, action}.
Flips a *proposed* memo to approved|rejected, regenerates decisions.json, and
pushes it. Never executes the decision. Env:
  APPROVAL_TOKEN_FILE   file holding the bearer token
  APPROVAL_DECISIONS_DIR  dir of <id>.md memos
  APPROVAL_ORIGIN       allowed CORS origin
  APPROVAL_SKIP_PUBLISH  "1" to skip regenerate+push (tests)
  QUANTY_WIKI_DIR / QUANTY_AUTOMATION_DIR / QUANTY_DECISIONS_OUT  for the generator
"""
import http.server
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import decision_approval as core

PORT = 8078
DASH_DIR = "/home/ubuntu/quanty-dashboard"
WIKI_DIR = os.environ.get("QUANTY_WIKI_DIR", "/home/ubuntu/quanty-wiki")
GEN = os.path.join(WIKI_DIR, "tools", "generate_decisions_json.py")
AUDIT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "approval_audit.log")

_RATE = {"window": 0, "count": 0}
_RATE_MAX = 10  # per 60s


def _origin():
    return os.environ.get("APPROVAL_ORIGIN", "https://estherrunstrict.github.io")


def _decisions_dir():
    return os.environ.get("APPROVAL_DECISIONS_DIR", os.path.join(WIKI_DIR, "wiki", "decisions"))


def _token():
    try:
        with open(os.environ["APPROVAL_TOKEN_FILE"]) as f:
            return f.read().strip()
    except Exception:
        return None


def _audit(entry):
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _rate_ok():
    now = int(time.time())
    if now // 60 != _RATE["window"]:
        _RATE["window"] = now // 60
        _RATE["count"] = 0
    _RATE["count"] += 1
    return _RATE["count"] <= _RATE_MAX


def _publish():
    if os.environ.get("APPROVAL_SKIP_PUBLISH") == "1":
        return
    subprocess.run([sys.executable, GEN], cwd=WIKI_DIR, check=True,
                   timeout=60, env={**os.environ})
    out = os.environ.get("QUANTY_DECISIONS_OUT", os.path.join(DASH_DIR, "docs/data/decisions.json"))
    rel = os.path.relpath(out, DASH_DIR)
    for args in (["add", rel], ["commit", "-m", "chore: decision approved/rejected via dashboard", "-q"], ["push", "-q"]):
        subprocess.run(["git", "-C", DASH_DIR] + args, check=False, timeout=60)


def apply_and_publish(memo_id, action):
    """Return new_status. Raises core.NotProposed / FileNotFoundError / ValueError."""
    if not core.VALID_ID.match(memo_id):
        raise ValueError("bad id")
    path = os.path.join(_decisions_dir(), memo_id + ".md")
    with open(path) as f:                       # FileNotFoundError -> 404
        content = f.read()
    now = datetime.now(timezone.utc)
    new_content, new_status = core.transition_frontmatter(
        content, action, now.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%d"))
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(new_content)
    os.replace(tmp, path)
    _publish()
    return new_status


class Handler(http.server.BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", _origin())
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path != "/approve":
            return self._json(404, {"error": "not found"})
        token = _token()
        auth = self.headers.get("Authorization", "")
        sent = auth[7:] if auth.startswith("Bearer ") else ""
        import hmac
        ok = bool(token) and hmac.compare_digest(sent, token)
        ip = self.client_address[0]
        if not ok:
            _audit({"ts": time.time(), "ip": ip, "token_ok": False})
            return self._json(401, {"error": "unauthorized"})
        if not _rate_ok():
            return self._json(429, {"error": "rate limited"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json(400, {"error": "bad body"})
        memo_id, action = body.get("id", ""), body.get("action", "")
        if action not in core.VALID_ACTIONS:
            return self._json(400, {"error": "bad action"})
        try:
            new_status = apply_and_publish(memo_id, action)
        except ValueError:
            return self._json(400, {"error": "bad id"})
        except FileNotFoundError:
            return self._json(404, {"error": "unknown memo"})
        except core.NotProposed:
            return self._json(409, {"error": "already decided"})
        except Exception as e:
            _audit({"ts": time.time(), "ip": ip, "id": memo_id, "action": action, "error": str(e)})
            return self._json(500, {"error": "publish failed"})
        _audit({"ts": time.time(), "ip": ip, "id": memo_id, "action": action,
                "status": new_status, "token_ok": True})
        return self._json(200, {"ok": True, "id": memo_id, "new_status": new_status})

    def log_message(self, *a):
        pass


def build_server(addr=("127.0.0.1", PORT)):
    return http.server.ThreadingHTTPServer(addr, Handler)


if __name__ == "__main__":
    build_server().serve_forever()
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd quanty-dashboard && python3 -m pytest tests/test_decision_approval.py -q`
Expected: PASS (10 passed — 6 core + 4 server).

- [ ] **Step 5: Confirm files exist** (no git locally)

Run: `python3 -c "import ast; ast.parse(open('quanty-dashboard/decision_approval_server.py').read()); print('parse OK')"`
Expected: `parse OK`.

---

## Task 3: systemd unit for the approval server

**Files:**
- Create: `quanty-dashboard/quanty-decision-approval.service`

- [ ] **Step 1: Create the unit**

Create `quanty-dashboard/quanty-decision-approval.service`:
```ini
[Unit]
Description=Quanty decision-approval endpoint (decision_approval_server.py)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/quanty-dashboard
Environment=APPROVAL_TOKEN_FILE=/home/ubuntu/.quanty_approval_token
Environment=QUANTY_WIKI_DIR=/home/ubuntu/quanty-wiki
Environment=QUANTY_AUTOMATION_DIR=/home/ubuntu/koreainvestment-autotrade
Environment=QUANTY_DECISIONS_OUT=/home/ubuntu/quanty-dashboard/docs/data/decisions.json
ExecStart=/home/ubuntu/myenv/bin/python3 decision_approval_server.py
Restart=always
RestartSec=5
StandardOutput=append:/home/ubuntu/quanty-dashboard/decision_approval.out
StandardError=append:/home/ubuntu/quanty-dashboard/decision_approval.out

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Validate (best-effort on macOS)**

Run: `systemd-analyze verify quanty-dashboard/quanty-decision-approval.service 2>&1 || echo "verify on server"`
Expected: no errors or the note.

---

## Task 4: cloudflared quick-tunnel wrapper + URL publisher

**Files:**
- Create: `quanty-dashboard/start_approval_tunnel.sh`
- Create: `quanty-dashboard/quanty-approval-tunnel.service`

- [ ] **Step 1: Create the wrapper**

Create `quanty-dashboard/start_approval_tunnel.sh`:
```bash
#!/usr/bin/env bash
# Run a Cloudflare quick tunnel to the local approval endpoint and publish the
# ephemeral URL to docs/data/approve_endpoint.json (committed+pushed to Pages).
set -uo pipefail
DASH=/home/ubuntu/quanty-dashboard
ENDPOINT_FILE="$DASH/docs/data/approve_endpoint.json"
last=""
cloudflared tunnel --url http://localhost:8078 2>&1 | while IFS= read -r line; do
  echo "$line"
  url=$(printf '%s' "$line" | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)
  if [ -n "$url" ] && [ "$url" != "$last" ]; then
    last="$url"
    printf '{"url": "%s", "updated_at": "%s"}\n' "$url" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$ENDPOINT_FILE"
    git -C "$DASH" add docs/data/approve_endpoint.json >/dev/null 2>&1 \
      && git -C "$DASH" commit -m "chore: publish approval tunnel url" -q >/dev/null 2>&1 \
      && git -C "$DASH" push -q >/dev/null 2>&1 || true
    echo "[tunnel] published $url"
  fi
done
```

- [ ] **Step 2: Lint**

Run: `cd quanty-dashboard && chmod +x start_approval_tunnel.sh && bash -n start_approval_tunnel.sh && echo "syntax OK"`
Expected: `syntax OK`.

- [ ] **Step 3: Create the tunnel systemd unit**

Create `quanty-dashboard/quanty-approval-tunnel.service`:
```ini
[Unit]
Description=Quanty approval Cloudflare quick tunnel
After=network-online.target quanty-decision-approval.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/quanty-dashboard
ExecStart=/bin/bash /home/ubuntu/quanty-dashboard/start_approval_tunnel.sh
Restart=always
RestartSec=10
StandardOutput=append:/home/ubuntu/quanty-dashboard/approval_tunnel.out
StandardError=append:/home/ubuntu/quanty-dashboard/approval_tunnel.out

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Validate**

Run: `systemd-analyze verify quanty-dashboard/quanty-approval-tunnel.service 2>&1 || echo "verify on server"`
Expected: no errors or the note.

---

## Task 5: Frontend — Approve/Reject buttons in `decision.html`

**Files:**
- Modify: `quanty-dashboard/docs/decision.html`

- [ ] **Step 1: Add buttons to the open_decisions render**

Find this block (the `dec.open_decisions.map(...)` render; the `.rhs` line):
```javascript
          <div class="rhs">${memoLink(m.wiki_path)}</div>
```
Replace it with (adds an actions cell with the two buttons):
```javascript
          <div class="rhs">
            ${memoLink(m.wiki_path)}
            <div class="actions">
              <button class="btn-approve" onclick="actOnDecision('${m.id}','approve',this.closest('.memo'))">Approve</button>
              <button class="btn-reject" onclick="actOnDecision('${m.id}','reject',this.closest('.memo'))">Reject</button>
            </div>
          </div>
```

- [ ] **Step 2: Add the handler + minimal styles**

Immediately before the closing `</script>` tag (find the LAST `</script>` in the file), insert:
```javascript
async function actOnDecision(id, action, rowEl) {
  if (!confirm(action.toUpperCase() + " this decision?\n\n" + id)) return;
  let token = localStorage.getItem('quanty_approval_token');
  if (!token) { token = prompt('Approval token:'); if (!token) return; localStorage.setItem('quanty_approval_token', token); }
  let base;
  try {
    const ep = await fetch('data/approve_endpoint.json', { cache: 'no-store' }).then(r => r.json());
    base = (ep.url || '').replace(/\/$/, '');
    if (!base) throw new Error('no url');
  } catch (e) { alert('Approval endpoint unavailable (tunnel may be restarting). Try again shortly.'); return; }
  try {
    const res = await fetch(base + '/approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
      body: JSON.stringify({ id, action })
    });
    if (res.status === 401) { localStorage.removeItem('quanty_approval_token'); alert('Token rejected — re-enter next time.'); return; }
    if (res.status === 409) { alert('Already decided — refresh the page.'); return; }
    if (!res.ok) { alert('Failed (' + res.status + '). Tunnel may be restarting; try again shortly.'); return; }
    const data = await res.json();
    const a = rowEl.querySelector('.actions');
    if (a) a.innerHTML = '<span class="decided-' + data.new_status + '">' + data.new_status + '</span>';
    rowEl.style.opacity = '0.6';
  } catch (e) { alert('Network error — tunnel may be restarting. Try again shortly.'); }
}
```

- [ ] **Step 3: Add CSS** — immediately before the closing `</style>` tag (find the first `</style>`), insert:
```css
.actions { display: inline-flex; gap: 6px; margin-left: 8px; }
.actions button { cursor: pointer; border: 1px solid var(--border, #ccc); border-radius: 4px; padding: 2px 8px; font-size: 0.75rem; }
.btn-approve { color: #1a7f37; } .btn-reject { color: #b42318; }
.decided-approved { color: #1a7f37; font-weight: 600; } .decided-rejected { color: #b42318; font-weight: 600; }
```

- [ ] **Step 4: Verify the HTML still parses / buttons wired**

Run:
```bash
cd quanty-dashboard
grep -c "actOnDecision" docs/decision.html   # expect 3 (2 buttons + 1 function def)
python3 -c "import html.parser,sys
class P(html.parser.HTMLParser):
    pass
P().feed(open('docs/decision.html').read()); print('html parse OK')"
```
Expected: `3` then `html parse OK`.

---

## Task 6: Runbook + deployment + activation

**Files:**
- Create: `automation_oracle/docs/runbooks/decision-approval.md`

- [ ] **Step 1: Write the runbook**

Create `automation_oracle/docs/runbooks/decision-approval.md`:
````markdown
# Runbook — Decision approval buttons

## Components
- `decision_approval_server.py` — systemd `quanty-decision-approval`, 127.0.0.1:8078, token-gated.
- `start_approval_tunnel.sh` — systemd `quanty-approval-tunnel`, Cloudflare quick tunnel → publishes `docs/data/approve_endpoint.json`.
- Buttons in `docs/decision.html`.

## One-time install (sudo)
```bash
# cloudflared (pick the arch matching `uname -m`: x86_64→amd64, aarch64→arm64)
ARCH=$([ "$(uname -m)" = aarch64 ] && echo arm64 || echo amd64)
curl -L -o /tmp/cf.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH.deb
sudo dpkg -i /tmp/cf.deb && cloudflared --version

# token (keep it secret; you'll paste it into the dashboard once)
openssl rand -hex 32 | tee /home/ubuntu/.quanty_approval_token
chmod 600 /home/ubuntu/.quanty_approval_token

# services
sudo cp /home/ubuntu/quanty-dashboard/quanty-decision-approval.service /etc/systemd/system/
sudo cp /home/ubuntu/quanty-dashboard/quanty-approval-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now quanty-decision-approval
sudo systemctl enable --now quanty-approval-tunnel
sudo systemctl status quanty-decision-approval quanty-approval-tunnel --no-pager | head -20
```

## Verify
```bash
# endpoint published?
sleep 8; cat /home/ubuntu/quanty-dashboard/docs/data/approve_endpoint.json
# local endpoint up?
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8078/approve   # 401 (no token) = good
```
Then open the public dashboard, click Approve on a test memo, paste the token, confirm the row flips and `decisions.json` updates after the push.

## Caveats
- Quick-tunnel URL is **ephemeral** (changes on restart, ~1 min Pages-lag to republish) and best-effort.
- Upgrade to a stable URL later: named tunnel + a Cloudflare domain — only `start_approval_tunnel.sh` changes.
- Audit log: `/home/ubuntu/quanty-dashboard/approval_audit.log`.
````

- [ ] **Step 2: Commit the runbook (automation_oracle IS a git repo)**

```bash
cd automation_oracle
git add docs/runbooks/decision-approval.md
git commit -m "docs(approval): decision-approval buttons runbook"
```

- [ ] **Step 3: Deploy server-side files (Jae authorizes the prod push)**

```bash
cd /Users/jaelee/.gemini/antigravity/scratch/quanty
scp -i ~/.ssh/oci_rsa quanty-dashboard/{decision_approval.py,decision_approval_server.py,start_approval_tunnel.sh,quanty-decision-approval.service,quanty-approval-tunnel.service,docs/decision.html} ubuntu@193.123.246.52:/home/ubuntu/quanty-dashboard/
scp -i ~/.ssh/oci_rsa -r quanty-dashboard/tests/test_decision_approval.py ubuntu@193.123.246.52:/home/ubuntu/quanty-dashboard/tests/
```
Expected: files transferred. (`docs/decision.html` lands at the repo's docs/, which is on GitHub Pages.)

- [ ] **Step 4: Commit server-side + run one-time install (Jae, sudo)**

On the server: `cd /home/ubuntu/quanty-dashboard && git add decision_approval.py decision_approval_server.py start_approval_tunnel.sh quanty-*.service docs/decision.html tests/test_decision_approval.py && git commit -m "feat: decision approval buttons" && git push` (push publishes the new decision.html to Pages). Then run the runbook "One-time install" block.

- [ ] **Step 5: Verify end-to-end**

Run the runbook "Verify" block. Confirm: `approve_endpoint.json` has a `trycloudflare.com` URL; `curl POST` with no token returns `401`; clicking Approve on a real proposed memo flips it (check the memo file's `status:` and that `decisions.json` regenerated). Then **set the memo back to `proposed`** if it was only a test, or pick a genuinely-decided memo.

---

## Notes for the implementer
- The local `quanty-dashboard/` scratch dir is **not** a git repo — never run git there; deploy via scp and commit on the server (Task 6).
- The approval server must never execute a decision — it only flips status. Keep the action whitelist (`approve`/`reject`) and the `proposed`-only guard intact.
- Deploy order: ship `decision_approval.py` with `decision_approval_server.py` (the server imports it).
