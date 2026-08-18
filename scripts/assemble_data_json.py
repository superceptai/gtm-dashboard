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
        # Coverage/completeness are best-effort in the puller: if this run
        # skipped them (wedged filter), carry forward the last good pivots.
        coverage = hd.get("coverage") or old.get("coverage")
        completeness = hd.get("completeness") or old.get("completeness")
    else:
        icp = old.get("icp", {})
        bands = old.get("bands", {})
        pipeline = old.get("pipeline", {})
        coverage = old.get("coverage")
        completeness = old.get("completeness")
        tb = old_funnel.get("track_b", {})
        old_ext = old.get("funnel_ext", {})
        fh = {
            "icp_contacts": old_ext.get("icp_contacts", old_funnel.get("icp_contacts", 0)),
            "connected": old_funnel.get("connected", 0),
            "enrolled": old_funnel.get("enrolled", 0),
            "intake": tb.get("intake", 0),
            "deal": tb.get("deal", 0),
            "first_connections_total": old_ext.get("first_connections_total", 0),
            "added_to_sequence": old_ext.get("added_to_sequence", 0),
            "no_li_profile_raw": old_ext.get("no_li_profile_raw", 0),
        }

    # ---- sequences (Reply.io leg) -----------------------------------------
    if ok("reply_io"):
        rd = data_of("reply_io")
        sequences = rd["sequences"]
        fr = rd["funnel_reply"]
    else:
        sequences = old.get("sequences", [])
        tb = old_funnel.get("track_b", {})
        old_ext_r = old.get("funnel_ext", {})
        # Carry forward the last good funnel_ext anchors (invites/accepted) rather
        # than recomputing from the older flat funnel fields, which measure a
        # different cohort and would corrupt the launch funnel on a Reply.io error.
        cf_invites = old_ext_r.get("invites_sent", old_funnel.get("invite_sent", 0))
        cf_accepted = old_ext_r.get("connections_accepted", tb.get("connected_via_outreach", 0))
        fr = {
            "enrolled": old_funnel.get("enrolled", 0),
            "invited": cf_invites,
            "connected_via_outreach": cf_accepted,
            "invites_sent": cf_invites,
            "connections_accepted": cf_accepted,
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
    # `enrolled` is sourced from the HubSpot leg (reply_sequence_name is the
    # canonical enrolment ledger); `invited` and `connected_via_outreach` stay
    # on the Reply.io leg.
    icp_contacts = fh.get("icp_contacts", 0)
    connected = fh.get("connected", 0)
    enrolled = fh.get("enrolled", 0)
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

    # ---- funnel_ext (2.0 launch funnel, real top steps) -------------------
    # Anchors are clean single counts (HubSpot leg) + Reply.io sums, verified
    # live against the signed-off mockup. `already_connected` and the email-only
    # remainder are decomposed arithmetically from those real anchors:
    #   already_connected = first_connections_total - connections_accepted
    #   no_li_profile     = (icp_contacts - invites_sent) - already_connected
    # i.e. of the ICP contacts NOT sent a LinkedIn invite, how many were already
    # 1st-degree connections vs reachable via email only. This mirrors the
    # funnel explainer and uses only defensible real numbers (see note below).
    ext_icp = fh.get("icp_contacts", 0) or icp_contacts
    ext_invites = fr.get("invites_sent", fr.get("invited", 0))
    ext_accepted = fr.get("connections_accepted", fr.get("connected_via_outreach", 0))
    ext_first = fh.get("first_connections_total", 0)
    ext_added = fh.get("added_to_sequence", 0)
    ext_already = max(0, ext_first - ext_accepted)
    ext_email_only = max(0, (ext_icp - ext_invites) - ext_already)

    # ---- uninvited_breakdown: WHY the non-connected, non-invited ICP contacts
    # were not sent a launch connection invite. Real reasons, not a remainder.
    #   already_connected     = 1st connections (no invite needed)
    #   routed_other_sequence = not connected, enrolled in a non-launch sequence
    #                           (email or a different LinkedIn campaign)
    #   connect_invite_queued = not connected, in a launch connect sequence but
    #                           the invite is not yet sent (LinkedIn daily limits)
    #   not_yet_enrolled      = not connected, not in any sequence yet
    # By construction routed + queued + not_enrolled == not_connected - invited_pending
    # == icp - invites_sent - already_connected (the ~1,489). Asserted below.
    if ok("hubspot_icp"):
        invited_pending = max(0, ext_invites - ext_accepted)
        nc_connect = fh.get("uninvited_nc_connect", 0)
        connect_queued = max(0, nc_connect - invited_pending)
        uninvited_breakdown = {
            "already_connected": ext_already,
            "routed_other_sequence": fh.get("uninvited_nc_other", 0),
            "connect_invite_queued": connect_queued,
            "not_yet_enrolled": fh.get("uninvited_nc_none", 0),
        }
        reasons_sum = (uninvited_breakdown["routed_other_sequence"]
                       + uninvited_breakdown["connect_invite_queued"]
                       + uninvited_breakdown["not_yet_enrolled"])
        expected = max(0, ext_icp - ext_invites - ext_already)
        if reasons_sum != expected:
            C.log("WARNING: uninvited_breakdown reasons sum {} != expected "
                  "remainder {} (icp {} - invites {} - already_connected {}). "
                  "Rendering will use the reasons' own sum.".format(
                      reasons_sum, expected, ext_icp, ext_invites, ext_already))
        else:
            C.log("uninvited_breakdown OK: {} already connected + {} reasons = "
                  "{} not invited".format(ext_already, reasons_sum,
                                          ext_already + reasons_sum))
    else:
        # HubSpot down: carry forward the last good breakdown verbatim.
        uninvited_breakdown = (old.get("funnel_ext", {}) or {}).get(
            "uninvited_breakdown", {"already_connected": ext_already})

    funnel_ext = {
        "icp_contacts": ext_icp,
        "invites_sent": ext_invites,
        "connections_accepted": ext_accepted,
        "first_connections_total": ext_first,
        "already_connected": ext_already,
        "no_li_profile": ext_email_only,
        "added_to_sequence": ext_added,
        # Reconstructed weekly cumulative 1st-connections curve (HubSpot leg);
        # carry the last good series forward when the leg skipped/failed it.
        "first_connections_weekly": (fh.get("first_connections_weekly")
                                     or old.get("funnel_ext", {}).get("first_connections_weekly", [])),
        "uninvited_breakdown": uninvited_breakdown,
        # transparency: the literal "no hs_linkedin_url" count. Near-zero live,
        # so the explainer uses the real reason breakdown above instead.
        "no_li_profile_raw": fh.get("no_li_profile_raw", 0),
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
        "funnel_ext": funnel_ext,
        "pipeline": pipeline,
        "ga4": ga4,
    }
    # Coverage/completeness are optional (present once the HubSpot bulk pull has
    # run at least once). Only emit when we have them so an early run does not
    # write empty objects the renderer would mistake for real zeros.
    if coverage:
        data["coverage"] = coverage
    if completeness:
        data["completeness"] = completeness

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
        # Additive evolution is allowed: NEW top-level keys (e.g. funnel_ext,
        # coverage, completeness) must be able to flow into history without
        # tripping the guard. We only refuse to append when a previously-present
        # key has gone MISSING -- that signals a real regression / bad run, and
        # appending it would corrupt the trend series downstream.
        missing = expected - got
        if missing:
            C.log("!" * 70)
            C.log("WARNING: history row is missing previously-present keys -- "
                  "SKIPPING append to avoid corrupting history.jsonl.")
            C.log("  missing: {}".format(sorted(missing)))
            C.log("  (new keys this run: {})".format(sorted(got - expected)))
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
