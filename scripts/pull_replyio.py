#!/usr/bin/env python3
"""
pull_replyio.py  --  Reply.io leg of the GTM dashboard refresh.

Secret:    REPLYIO_API_KEY   (GitHub repo secret; Reply.io Settings > API key)
Produces:  sequences[]  and the outreach half of the funnel (Track B pieces)
Endpoint:  https://api.reply.io/v3   (Reply.io v3 REST API), stdlib urllib
Auth:      Authorization: Bearer <REPLYIO_API_KEY>
Scope:     sequences:read (the API key already carries it)

------------------------------------------------------------------------------
ENDPOINT / FIELD CONFIG  --  read this if the numbers ever look wrong
------------------------------------------------------------------------------
Confirmed from Reply.io's official OpenAPI spec (the numbers below match
data.json exactly):

  * base URL         https://api.reply.io/v3
  * auth             static Bearer token (Authorization: Bearer <key>)
  * list sequences   GET  /v3/sequences               (returns id / name / status)
  * per-sequence stats:
        POST /v3/sequences/{id}/stats
        headers: Authorization: Bearer <key>, Content-Type: application/json
        body (REQUIRED -- the endpoint defaults to "lastWeek" if omitted, so we
        send "allTime" explicitly to match the dashboard's cumulative figures):
            {"filters": {"dateRangePreset": "allTime"}}

    NOTE: it is POST (not GET) and the path segment is `stats` (not
    `statistics`). Every previous GET probe against `.../statistics` returned
    HTTP 404 for exactly those two reasons.

  * per-sequence stats response SHAPE (top-level, camelCase, NO Success/Data
    wrapper):
        {
          "emailOverview": {
            "contacted", "delivered", "opened", "replied", "meetingsBooked", ...
          },
          "linkedInOverview": {
            "connectionsSent", "connectionsAccepted",
            "connectionsAcceptedPercentage", "messagesSent", "replied", ...
          }
        }
    e.g. sequence 1658117 -> linkedInOverview.connectionsSent 468,
         connectionsAccepted 139, connectionsAcceptedPercentage 29.7.

Everything the pipeline depends on is in the *_KEYS maps below, so a
non-developer's engineer can correct a field name in ONE place. Run
`python3 scripts/pull_replyio.py --check` after adding the secret to confirm.
------------------------------------------------------------------------------
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

REPLY_BASE = "https://api.reply.io/v3"

# The four sequences shown on the dashboard (same IDs as data.json). Override
# with a comma-separated REPLY_SEQUENCE_IDS env var if the set ever changes.
DEFAULT_SEQUENCE_IDS = ["1658117", "1676940", "1695697", "1695700"]

# Required request body for POST /v3/sequences/{id}/stats. The endpoint defaults
# to "lastWeek" when the body is omitted; "allTime" matches the dashboard's
# cumulative figures (and data.json).
STATS_BODY = {"filters": {"dateRangePreset": "allTime"}}

# linkedInOverview stat object (camelCase) -> normalised sequence field.
LINKEDIN_KEYS = {
    "connections_sent":             ["connectionsSent"],
    "connection_requests_accepted": ["connectionsAccepted"],
    "connection_rate":              ["connectionsAcceptedPercentage"],
    "messages_sent":                ["messagesSent"],
    "li_replied":                   ["replied"],
}

# emailOverview stat object (camelCase) -> normalised field (kept for
# completeness / future email sequences; the current LinkedIn-only sequences
# report zeros here).
EMAIL_KEYS = {
    "email_contacted":       ["contacted"],
    "email_delivered":       ["delivered"],
    "email_opened":          ["opened"],
    "email_replied":         ["replied"],
    "email_bounced":         ["bounced"],
    "email_meetings_booked": ["meetingsBooked"],
}

# Contact-state counts, read defensively from the GET /v3/sequences list items
# (case-insensitive). The statistics endpoint does NOT return these; the list
# endpoint is the source when it carries them. Absent -> 0.
COUNT_KEYS = {
    "total":    ["peopleCount", "contactsCount", "prospectsCount", "total", "totalCount"],
    "new":      ["new", "newCount", "notStarted", "notStartedCount"],
    "pending":  ["pending", "inProgress", "active", "activeCount", "inProgressCount"],
    "finished": ["finished", "finishedCount", "completed", "completedCount"],
}


def _headers():
    key = C.require_env("REPLYIO_API_KEY")
    # Bearer is the documented v3 scheme. The value is never logged.
    return {
        "Authorization": "Bearer {}".format(key),
        "Content-Type": "application/json",
    }


def _ci_get(d, keys):
    """Case-insensitive lookup of the first present, non-null key in a dict."""
    if not isinstance(d, dict):
        return None
    flat = {k.lower(): v for k, v in d.items()}
    for k in keys:
        v = flat.get(k.lower())
        if v is not None:
            return v
    return None


def _find_block(data, name):
    """Return the `name` sub-object (e.g. 'LinkedIn') case-insensitively."""
    if not isinstance(data, dict):
        return None
    for k, v in data.items():
        if k.lower() == name.lower() and isinstance(v, dict):
            return v
    return None


def list_sequences():
    """id -> {name, status, and any contact-state counts the list carries}."""
    out = {}
    skip = 0
    while True:
        url = "{}/sequences?top=100&skip={}".format(REPLY_BASE, skip)
        res = C.request_json(url, headers=_headers())
        if isinstance(res, list):
            items = res
        else:
            items = res.get("Items") or res.get("items") or res.get("data") or []
        for it in items:
            sid = str(it.get("Id") or it.get("id"))
            meta = {
                "name": it.get("Name") or it.get("name") or "",
                "status": (it.get("Status") or it.get("status") or "").lower() or "active",
            }
            for field, keys in COUNT_KEYS.items():
                meta[field] = _ci_get(it, keys)
            out[sid] = meta
        has_more = res.get("HasMore") or res.get("hasMore") if isinstance(res, dict) else False
        if not has_more or not items:
            break
        skip += 100
    return out


def sequence_stats(sid):
    """Return a normalised stats dict for one sequence.

    POST /v3/sequences/{id}/stats with the required allTime filter body. The
    response is top-level camelCase with `linkedInOverview` / `emailOverview`
    blocks (no Success/Data wrapper).
    """
    url = "{}/sequences/{}/stats".format(REPLY_BASE, sid)
    try:
        res = C.request_json(
            url, method="POST", headers=_headers(), body=STATS_BODY
        )
    except Exception as e:  # noqa: BLE001
        C.log("  stats fetch failed for {}: {}".format(sid, e))
        return {}
    li = _find_block(res, "linkedInOverview")
    em = _find_block(res, "emailOverview")
    if li is None and em is None:
        C.log("  stats response for {} had no linkedInOverview/emailOverview block".format(sid))
        return {}
    norm = {}
    for field, keys in LINKEDIN_KEYS.items():
        norm[field] = _ci_get(li, keys)
    for field, keys in EMAIL_KEYS.items():
        norm[field] = _ci_get(em, keys)
    return norm


def _num(v, default=0):
    try:
        if v is None:
            return default
        return int(round(float(v)))
    except (ValueError, TypeError):
        return default


def _rate(v):
    try:
        return round(float(v), 2) if v is not None else None
    except (ValueError, TypeError):
        return None


def fetch():
    ids = [s.strip() for s in os.environ.get(
        "REPLY_SEQUENCE_IDS", ",".join(DEFAULT_SEQUENCE_IDS)).split(",") if s.strip()]
    catalog = list_sequences()

    sequences = []
    enrolled = invited = accepted = 0
    for sid in ids:
        meta = catalog.get(sid, {})
        st = sequence_stats(sid)
        total = _num(meta.get("total"))
        new = _num(meta.get("new"))
        pending = _num(meta.get("pending"))
        # If the list did not break out state buckets, treat the sequence as
        # drained (matches current data.json where new=pending=0). FLAGGED.
        finished = _num(meta.get("finished")) or max(0, total - new - pending)
        c_sent = _num(st.get("connections_sent"))
        c_acc = _num(st.get("connection_requests_accepted"))
        rate = _rate(st.get("connection_rate"))
        seq = {
            "id": sid,
            "name": meta.get("name", ""),
            "status": meta.get("status", "active"),
            "total": total,
            "new": new,
            "pending": pending,
            "finished": finished,
            "connected": 0,  # kept for schema parity with data.json
            "connections_sent": c_sent,
            "connection_requests_accepted": c_acc,
            "connection_rate": rate,
            "daily_send_rate": 0.0,  # front-end derives runway from `new`
            "runway_days": None,
        }
        sequences.append(seq)
        enrolled += total
        invited += c_sent
        accepted += c_acc

    funnel_reply = {
        # Track B (outreach conversion) pieces, counting only contacts in the
        # active outreach sequences:
        "enrolled": enrolled,                     # sum of sequence totals
        "invited": invited,                       # sum of LinkedIn invites sent
        "connected_via_outreach": accepted,       # sum of invites accepted
        # funnel_ext anchors (2.0 launch funnel). Same numbers, named to match
        # the data.json funnel_ext contract so the assembler is a straight read:
        "invites_sent": invited,                  # Reply.io connectionsSent, summed
        "connections_accepted": accepted,         # Reply.io connectionsAccepted, summed
    }
    return {"sequences": sequences, "funnel_reply": funnel_reply}


def check(data):
    seqs = data.get("sequences", [])
    if not seqs:
        return False
    # Healthy if at least one sequence returned a non-zero total or invite count.
    return any((s["total"] or s["connections_sent"]) for s in seqs)


if __name__ == "__main__":
    C.run_puller("reply_io", fetch, check)
