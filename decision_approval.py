#!/usr/bin/env python3
"""Decision-approval pure core: read/transition a decision memo's status.

Safety-critical and side-effect-free so it can be exhaustively unit-tested.
Only ever transitions `proposed -> approved|rejected`; refuses everything else.
"""
import re

VALID_ACTIONS = {"approve": "approved", "reject": "rejected"}
VALID_ID = re.compile(r"^[a-z0-9][a-z0-9-]*\Z")
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
