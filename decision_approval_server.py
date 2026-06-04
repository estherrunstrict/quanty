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
