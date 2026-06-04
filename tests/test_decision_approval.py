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
    assert not da.VALID_ID.match("good\n")   # trailing newline must not match


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
