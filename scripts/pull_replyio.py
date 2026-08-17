#!/usr/bin/env python3
"""
pull_replyio.py  --  Reply.io leg of the GTM dashboard refresh.

Secret:    REPLYIO_API_KEY   (GitHub repo secret; Reply.io Settings > API key)
Produces:  sequences[]  and the outreach half of the funnel (Track B pieces)
Endpoint:  https://api.reply.io/v3   (Reply.io v3 REST API), stdlib urllib
Auth:      Authorization: Bearer <REPLYIO_API_KEY>

------------------------------------------------------------------------------
ENDPOINT / FIELD CONFIG  --  read this if the numbers ever look wrong
------------------------------------------------------------------------------
Confirmed against the live Reply.io connector (the numbers below match data.json
exactly) at build time:

  * base URL         https://api.reply.io/v3
  * auth             static Bearer token (Authorization: Bearer <key>)
  * list sequences   GET /v3/sequences        (returns id / name / status)
  * per-sequence stats response SHAPE (PascalCase, wrapped in Success/Data):
        {
          "Success": true,
          "Data": {
            "SequenceId": 1658117,
            "Email":    { "Contacted", "Delivered", "Opened", "Replied",
                          "Bounced", "MeetingsBooked", ...+"...Percentage" },
            "LinkedIn": { "ConnectionsSent", "ConnectionsAccepted",
                          "ConnectionsAcceptedPercentage", "MessagesSent",
                          "Replied", ...+"...Percentage" }
          }
        }
    e.g. sequence 1658117 -> LinkedIn.ConnectionsSent 468,
         ConnectionsAccepted 139, ConnectionsAcceptedPercentage 29.7.

The exact REST *path* of the statistics endpoint could NOT be re-confirmed from
the live docs at build time: docs.reply.io / apidocs.reply.io / api.reply.io are
all blocked by the CI network egress policy (HTTP 403 from the proxy), so the
docs page for the "Statistics" section is unreadable here. Rather than hardcode a
single unverified path, the puller probes the candidate paths in SEQ_STATS_PATHS
below (each tried in order) and ONLY accepts a response that matches the
confirmed shape above (a `Data` object carrying an `Email`/`LinkedIn` block). A
path that 404s, or that 200s with a different body, is rejected and the next
candidate is tried -- so a wrong path can never be silently mis-parsed. When the
real path is confirmed, move it to the top of SEQ_STATS_PATHS (or drop the rest).

Everything the pipeline depends on is in SEQ_STATS_PATHS / the *_KEYS maps below,
so a non-developer's engineer can correct a path or a field name in ONE place.
Run `python3 scripts/pull_replyio.py --check` after adding the secret to confirm.
------------------------------------------------------------------------------
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

REPLY_BASE = "https://api.reply.io/v3"
REPLY_ROOT = REPLY_BASE.rsplit("/v3", 1)[0]  # https://api.reply.io

# The four sequences shown on the dashboard (same IDs as data.json). Override
# with a comma-separated REPLY_SEQUENCE_IDS env var if the set ever changes.
DEFAULT_SEQUENCE_IDS = ["1658117", "1676940", "1695697", "1695700"]

# Candidate per-sequence statistics paths, tried in order until one returns the
# confirmed Email/LinkedIn shape. The two paths the previous build guessed
# (/v3/sequences/{id}/statistics and /v3/statistics/sequences/{id}) both returned
# HTTP 404, so they are intentionally NOT retried here. These candidates target
# the dedicated "Statistics" API section (singular/plural, path- and
# query-parameter forms). {id} is substituted with the numeric sequence id.
SEQ_STATS_PATHS = [
    "/v3/statistics/sequence/{id}",
    "/v3/statistics/sequence?sequenceId={id}",
    "/v3/statistics/sequences?sequenceId={id}",
    "/v3/statistics/sequence?sequenceIds={id}",
    "/v3/reports/sequence/{id}",
    "/v3/reports/sequences/{id}",
]

# LinkedIn stat object (PascalCase) -> normalised sequence field.
LINKEDIN_KEYS = {
    "connections_sent":             ["ConnectionsSent"],
    "connection_requests_accepted": ["ConnectionsAccepted"],
    "connection_rate":              ["ConnectionsAcceptedPercentage"],
    "messages_sent":                ["MessagesSent"],
    "li_replied":                   ["Replied"],
}

# Email stat object (PascalCase) -> normalised field (kept for completeness /
# future email sequences; the current LinkedIn-only sequences report zeros here).
EMAIL_KEYS = {
    "email_contacted":       ["Contacted"],
    "email_delivered":       ["Delivered"],
    "email_opened":          ["Opened"],
    "email_replied":         ["Replied"],
    "email_bounced":         ["Bounced"],
    "email_meetings_booked": ["MeetingsBooked"],
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
    return {"Authorization": "Bearer {}".format(key)}


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
    """Return a normalised stats dict for one sequence, trying candidate paths.

    Only a response matching the confirmed shape (a Data object carrying an
    Email or LinkedIn block) is accepted; anything else is skipped.
    """
    last_err = None
    for tmpl in SEQ_STATS_PATHS:
        url = REPLY_ROOT + tmpl.format(id=sid)
        try:
            res = C.request_json(url, headers=_headers())
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
        data = res.get("Data") if isinstance(res, dict) and "Data" in res else res
        li = _find_block(data, "LinkedIn")
        em = _find_block(data, "Email")
        if li is None and em is None:
            # Wrong shape for this path (e.g. a 200 that is not the stats object).
            continue
        norm = {}
        for field, keys in LINKEDIN_KEYS.items():
            norm[field] = _ci_get(li, keys)
        for field, keys in EMAIL_KEYS.items():
            norm[field] = _ci_get(em, keys)
        norm["_stats_path"] = tmpl
        return norm
    if last_err:
        C.log("  stats fetch failed for {}: {}".format(sid, last_err))
    else:
        C.log("  no candidate statistics path returned the expected shape for {}".format(sid))
    return {}


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
    used_path = None
    for sid in ids:
        meta = catalog.get(sid, {})
        st = sequence_stats(sid)
        used_path = used_path or st.get("_stats_path")
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

    if used_path:
        C.log("  statistics endpoint in use: {}".format(used_path))

    funnel_reply = {
        # Track B (outreach conversion) pieces, counting only contacts in the
        # active outreach sequences:
        "enrolled": enrolled,                     # sum of sequence totals
        "invited": invited,                       # sum of LinkedIn invites sent
        "connected_via_outreach": accepted,       # sum of invites accepted
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
