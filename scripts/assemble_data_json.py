#!/usr/bin/env python3
"""
assemble_data_json.py  --  combine the four puller legs into data.json and
append one row to history.jsonl, in the EXACT existing schema.

Reads:   scripts/_refresh_tmp/leg_*.json  (from the four pullers)
         data.json        (current, for carry-forward + follower deltas)
         history.jsonl    (for follower deltas + row-shape validation)
         config/manual_overrides.json  (funnel targets)

Writes:  data.json        (rewritten each run)
         history.jsonl     (append-only; today's row updated in place if present)

Rules honoured:
  * Any leg that failed carries forward its previous section values from the
    current data.json and is marked status="error", so one bad source never
    blanks the whole dashboard.
  * history.jsonl is append-only. The new row's `data` keys are validated
    against the last existing row; on mismatch the append is SKIPPED with a loud
    warning rather than corrupting the file.
  * If a row for today's date already exists it is updated in place (no dup).
"""

import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_JSON = os.path.join(ROOT, "data.json")
HISTORY = os.path.join(ROOT, "history.jsonl")
OVERRIDES = os.path.join(ROOT, "config", "manual_overrides.json")
SCHEMA_VERSION = 1

LEGS = ["hubspot_icp", "reply_io", "ga4", "linkedin"]


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def load_leg(leg):
    return load_json(C.leg_path(leg), {"leg": leg, "status": "error",
                                        "last_success": None, "data": {}})


def load_history():
    rows = []
    try:
        with open(HISTORY) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except FileNotFoundError:
        pass
    return rows


def pct(part, whole):
    return round(part / whole * 100.0, 1) if whole else 0.0


def follower_delta(current, history, days):
    """current followers minus the value from ~`days` ago (most recent history
    row on/before that cutoff). Returns None if no suitable prior row."""
    if current is None:
        return None
    cutoff = (datetime.now(C.AEST) - timedelta(days=days)).date()
    best = None
    for row in history:
        ts = row.get("snapshot_timestamp", "")
        try:
            d = datetime.fromisoformat(ts).date()
        except ValueError:
            continue
        if d <= cutoff:
            if best is None or d > best[0]:
                prev = (((row.get("data") or {}).get("linkedin") or {})
                        .get("followers_current"))
                if isinstance(prev, (int, float)):
                    best = (d, prev)
    if best is None:
        return None
    return int(current - best[1])


def main():
    legs = {leg: load_leg(leg) for leg in LEGS}
    old = load_json(DATA_JSON, {})
    old_status = old.get("source_status", {})
    old_funnel = old.get("funnel", {})
    history = load_history()
    overrides = load_json(OVERRIDES, {})

    def ok(leg):
        return legs[leg].get("status") == "ok"

    def data_of(leg):
        return legs[leg].get("data") or {}

    # ---- source_status (carry forward last_success on error) --------------
    source_status = {}
    for leg in LEGS:
        if ok(leg):
            source_status[leg] = {
                "status": "fresh",
                "last_success": legs[leg].get("last_success") or C.now_aest_iso(),
            }
        else:
            source_status[leg] = {
                "status": "error",
                "last_success": (old_status.get(leg) or {}).get("last_success"),
            }

    # ---- icp / bands / pipeline (HubSpot leg) -----------------------------
    if ok("hubspot_icp"):
        hd = data_of("hubspot_icp")
        icp = hd["icp"]
        bands = hd["bands"]
        pipeline = hd["pipeline"]
        fh = hd["funnel_hubspot"]
    else:
        icp = old.get("icp", {})
        bands = old.get("bands", {})
        pipeline = old.get("pipeline", {})
        tb = old_funnel.get("track_b", {})
        fh = {
            "icp_contacts": old_funnel.get("icp_contacts", 0),
            "connected": old_funnel.get("connected", 0),
            "intake": tb.get("intake", 0),
            "deal": tb.get("deal", 0),
        }

    # ---- sequences (Reply.io leg) -----------------------------------------
    if ok("reply_io"):
        rd = data_of("reply_io")
        sequences = rd["sequences"]
        fr = rd["funnel_reply"]
    else:
        sequences = old.get("sequences", [])
        tb = old_funnel.get("track_b", {})
        fr = {
            "enrolled": old_funnel.get("enrolled", 0),
            "invited": old_funnel.get("invite_sent", 0),
            "connected_via_outreach": tb.get("connected_via_outreach", 0),
        }

    # ---- ga4 leg ----------------------------------------------------------
    ga4 = data_of("ga4").get("ga4") if ok("ga4") else old.get("ga4", {})

    # ---- linkedin leg (+ deltas from history) -----------------------------
    if ok("linkedin"):
        followers = data_of("linkedin")["linkedin"]["followers_current"]
        li_source = data_of("linkedin")["linkedin"].get("source", "connectsafely")
    else:
        followers = (old.get("linkedin") or {}).get("followers_current")
        li_source = (old.get("linkedin") or {}).get("source", "carry_forward")
    linkedin = {
        "followers_current": followers,
        "followers_24h_delta": follower_delta(followers, history, 1),
        "followers_7d_delta": follower_delta(followers, history, 7),
        "followers_30d_delta": follower_delta(followers, history, 30),
        "status": "fresh" if ok("linkedin") else "error",
        "source": li_source,
    }

    # ---- funnel (merge Track A HubSpot + Track B Reply/deals) -------------
    icp_contacts = fh.get("icp_contacts", 0)
    connected = fh.get("connected", 0)
    enrolled = fr.get("enrolled", 0)
    invite_sent = fr.get("invited", 0)
    outreach_connected = fr.get("connected_via_outreach", 0)
    intake = fh.get("intake", 0)
    deal = fh.get("deal", 0)
    pre_existing = max(0, connected - outreach_connected)
    targets = overrides.get("funnel_targets", {}) or {}

    funnel = {
        # existing keys (preserved for continuity with history + index.html)
        "icp_contacts": icp_contacts,
        "enrolled": enrolled,
        "enrolled_pct_of_icp": pct(enrolled, icp_contacts),
        "invite_sent": invite_sent,
        "invite_sent_pct_of_enrolled": pct(invite_sent, enrolled),
        "connected": connected,
        "connected_pct_of_invited": pct(connected, invite_sent),
        # NEW: two-track split (flagged for reconciliation vs funnel spec)
        "track_a": {
            "label": "Coverage (ICP contacts -> connected, incl. pre-existing)",
            "icp_contacts": icp_contacts,
            "connected": connected,
            "connected_pct_of_icp": pct(connected, icp_contacts),
            "pre_existing_connected": pre_existing,
            "outreach_connected": outreach_connected,
        },
        "track_b": {
            "label": "Outreach conversion (enrolled -> invited -> connected -> intake -> deal)",
            "enrolled": enrolled,
            "invited": invite_sent,
            "invited_pct_of_enrolled": pct(invite_sent, enrolled),
            "connected_via_outreach": outreach_connected,
            "connected_pct_of_invited": pct(outreach_connected, invite_sent),
            "intake": intake,
            "intake_pct_of_connected": pct(intake, outreach_connected),
            "deal": deal,
        },
        "targets": {
            "connected": targets.get("connected"),
            "intake": targets.get("intake"),
        },
    }

    # ---- feed label -------------------------------------------------------
    def tag(leg, live, err="ERR"):
        return live if ok(leg) else err
    li_tag = "LINKEDIN_LIVE_CONNECTSAFELY" if (ok("linkedin") and li_source ==
             "connectsafely") else ("LINKEDIN_MANUAL" if ok("linkedin") else "LINKEDIN_ERR")
    feed_label = "refresh: {}, {}, {}, {}".format(
        tag("hubspot_icp", "HS_ICP_LIVE", "HS_ICP_ERR"),
        tag("reply_io", "REPLY_LIVE", "REPLY_ERR"),
        tag("ga4", "GA4_LIVE", "GA4_ERR"),
        li_tag,
    )

    now = C.now_aest_iso()
    as_of = datetime.now(C.AEST).strftime("%-d %b %Y")

    data = {
        "schema_version": SCHEMA_VERSION,
        "last_updated": now,
        "as_of_label": as_of,
        "feed_label": feed_label,
        "source_status": source_status,
        "icp": icp,
        "bands": bands,
        "sequences": sequences,
        "linkedin": linkedin,
        "funnel": funnel,
        "pipeline": pipeline,
        "ga4": ga4,
    }

    with open(DATA_JSON, "w") as f:
        json.dump(data, f, indent=2)
    C.log("wrote data.json ({})".format(feed_label))

    append_history(data, now, history)


def append_history(data, now, history):
    """Append one row to history.jsonl, or update today's row in place. Validate
    top-level `data` keys against the last existing row first."""
    new_row = {
        "snapshot_timestamp": now,
        "schema_version": SCHEMA_VERSION,
        "data": data,
    }

    if history:
        expected = set((history[-1].get("data") or {}).keys())
        got = set(data.keys())
        if expected and got != expected:
            C.log("!" * 70)
            C.log("WARNING: history row shape mismatch -- SKIPPING append to avoid "
                  "corrupting history.jsonl.")
            C.log("  missing: {}".format(sorted(expected - got)))
            C.log("  extra:   {}".format(sorted(got - expected)))
            C.log("!" * 70)
            return

    today = datetime.fromisoformat(now).date().isoformat()

    def row_date(row):
        try:
            return datetime.fromisoformat(row.get("snapshot_timestamp", "")).date().isoformat()
        except ValueError:
            return None

    idx_today = None
    for i, row in enumerate(history):
        if row_date(row) == today:
            idx_today = i

    if idx_today is not None:
        # Update today's row in place (rewrite the whole file, rows unchanged
        # except today's) -- avoids a duplicate row for the same date.
        history[idx_today] = new_row
        with open(HISTORY, "w") as f:
            for row in history:
                f.write(json.dumps(row) + "\n")
        C.log("updated today's history row in place ({})".format(today))
    else:
        # Pure append -- never rewrites existing rows. Guard against the
        # existing file not ending in a newline (the current last row does not),
        # which would otherwise glue two JSON objects onto one physical line.
        need_nl = False
        if os.path.exists(HISTORY) and os.path.getsize(HISTORY) > 0:
            with open(HISTORY, "rb") as f:
                f.seek(-1, os.SEEK_END)
                need_nl = f.read(1) != b"\n"
        with open(HISTORY, "a") as f:
            if need_nl:
                f.write("\n")
            f.write(json.dumps(new_row) + "\n")
        C.log("appended new history row ({})".format(today))


if __name__ == "__main__":
    main()
