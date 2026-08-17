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
Reply.io's v3 docs (docs.reply.io / apidocs.reply.io) are not reachable from
the CI network, so the exact statistics path could not be re-confirmed against
live docs at build time. What IS confirmed:
  * base URL           https://api.reply.io/v3
  * auth               static Bearer token (Authorization: Bearer <key>)
  * list sequences     GET /v3/sequences
  * the LinkedIn stat fields returned per sequence:
        ConnectionsSent, ConnectionsAccepted, ConnectionsAcceptedPercentage
    (verified live: seq 1658117 -> 468 sent / 139 accepted / 29.7 %, and seq
     1695700 -> 341 / 77 / 22.58 %, both matching data.json exactly).

Everything the pipeline depends on is in SEQ_STATS_PATHS / FIELD_MAP below, so a
non-developer's engineer can correct a path in ONE place. The puller tries each
candidate stats path until one returns usable data, and normalises field names
case-insensitively, so small doc differences do not break the run. Run
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

# Candidate per-sequence statistics paths, tried in order until one works.
SEQ_STATS_PATHS = [
    "/v3/sequences/{id}/statistics",
    "/v3/sequences/{id}",
    "/v3/statistics/sequences/{id}",
]

# Normalised field name -> list of accepted source keys (case-insensitive).
FIELD_MAP = {
    "total":                    ["peopleCount", "total", "contactsCount", "prospectsCount"],
    "new":                      ["new", "newCount", "notStarted"],
    "pending":                  ["pending", "inProgress", "active", "activeCount"],
    "finished":                 ["finished", "finishedCount", "completed"],
    "connections_sent":         ["connectionsSent", "linkedInConnectionsSent"],
    "connections_accepted":     ["connectionsAccepted", "linkedInConnectionsAccepted"],
    "connection_rate":          ["connectionsAcceptedPercentage", "connectionRate"],
    "daily_send_rate":          ["dailySendRate", "sendRatePerDay"],
}


def _headers():
    key = C.require_env("REPLYIO_API_KEY")
    # Bearer is the documented v3 scheme; X-Api-Key is sent too for tolerance
    # with older Reply.io endpoints. Neither value is ever logged.
    return {"Authorization": "Bearer {}".format(key), "X-Api-Key": key}


def _ci_get(d, keys):
    """Case-insensitive lookup of the first present key. Descends into nested
    'linkedIn'/'stats' objects that Reply.io sometimes wraps counts in."""
    if not isinstance(d, dict):
        return None
    flat = {}
    for k, v in d.items():
        flat[k.lower()] = v
        if isinstance(v, dict):
            for k2, v2 in v.items():
                flat.setdefault(k2.lower(), v2)
    for k in keys:
        if k.lower() in flat and flat[k.lower()] is not None:
            return flat[k.lower()]
    return None


def list_sequences():
    """id -> {name, status} for every sequence the key can see."""
    out = {}
    skip = 0
    while True:
        url = "{}/sequences?top=100&skip={}".format(REPLY_BASE, skip)
        res = C.request_json(url, headers=_headers())
        items = res.get("Items") or res.get("items") or res.get("data") or []
        if isinstance(res, list):
            items = res
        for it in items:
            sid = str(it.get("Id") or it.get("id"))
            out[sid] = {
                "name": it.get("Name") or it.get("name") or "",
                "status": (it.get("Status") or it.get("status") or "").lower() or "active",
            }
        has_more = res.get("HasMore") or res.get("hasMore")
        if not has_more or not items:
            break
        skip += 100
    return out


def sequence_stats(sid):
    """Return a normalised stats dict for one sequence, trying candidate paths."""
    last_err = None
    for tmpl in SEQ_STATS_PATHS:
        url = REPLY_BASE.rsplit("/v3", 1)[0] + tmpl.format(id=sid)
        try:
            res = C.request_json(url, headers=_headers())
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
        data = res.get("Data") if isinstance(res, dict) and "Data" in res else res
        norm = {}
        for field, keys in FIELD_MAP.items():
            norm[field] = _ci_get(data, keys)
        # Consider the path usable if it produced at least the connection counts.
        if norm.get("connections_sent") is not None or norm.get("total") is not None:
            return norm
    if last_err:
        C.log("  stats fetch failed for {}: {}".format(sid, last_err))
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
    for sid in ids:
        meta = catalog.get(sid, {})
        st = sequence_stats(sid)
        total = _num(st.get("total"))
        new = _num(st.get("new"))
        pending = _num(st.get("pending"))
        # If the API did not break out state buckets, treat the sequence as
        # drained (matches current data.json where new=pending=0). FLAGGED.
        finished = _num(st.get("finished")) or max(0, total - new - pending)
        c_sent = _num(st.get("connections_sent"))
        c_acc = _num(st.get("connections_accepted"))
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
            "daily_send_rate": _rate(st.get("daily_send_rate")) or 0.0,
            "runway_days": None,  # front-end derives runway from `new`
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
