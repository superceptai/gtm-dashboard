#!/usr/bin/env python3
"""
pull_ga4.py  --  GA4 (via Windsor.ai) leg of the GTM dashboard refresh.

Secret:    WINDSOR_API_KEY   (GitHub repo secret; Windsor.ai API key)
Produces:  ga4 { sessions, users, engaged_pct, key events + conversion rates, daily[] }
Endpoint:  https://connectors.windsor.ai/googleanalytics4   (Windsor.ai free tier)
Auth:      api_key query parameter (Windsor.ai convention; never logged)

GA4 property/account: 536587504 (Windsor account "Supercept").
Confirmed Windsor field IDs (from get_fields): date, sessions, totalusers,
engaged_sessions, engagement_rate, event_name, event_count, conversions.

Custom key events tracked (GA4 event names):
    intake_call_booked      -> intake
    application_qualified   -> qualified
    application_disqualified-> disqualified
    purchase                -> purchase
Each with a conversion rate = event_count / sessions over the window.

The Windsor REST surface returns JSON as {"data":[ {field: value}, ... ]}. Two
reads are made: one at date granularity for the traffic series, one at
event_name granularity for the key-event counts (mixing those dimensions in one
call would change the row grain).
"""

import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

WINDSOR_BASE = "https://connectors.windsor.ai/googleanalytics4"
GA4_ACCOUNT = "536587504"
DATE_PRESET = "last_7d"

KEY_EVENTS = {
    "intake": "intake_call_booked",
    "qualified": "application_qualified",
    "disqualified": "application_disqualified",
    "purchase": "purchase",
}


def _url(fields):
    params = {
        "api_key": C.require_env("WINDSOR_API_KEY"),
        "connector": "googleanalytics4",
        "accounts": GA4_ACCOUNT,
        "date_preset": DATE_PRESET,
        "fields": ",".join(fields),
        "_renderer": "json",
    }
    return WINDSOR_BASE + "?" + urllib.parse.urlencode(params)


def _rows(payload):
    if isinstance(payload, dict):
        return payload.get("data") or payload.get("Data") or []
    if isinstance(payload, list):
        return payload
    return []


def _f(v, default=0.0):
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def fetch():
    # --- traffic series (date grain) ---------------------------------------
    traffic = _rows(C.request_json(_url(
        ["date", "sessions", "totalusers", "engaged_sessions", "engagement_rate"])))
    by_date = {}
    sessions = users = engaged = 0.0
    for r in traffic:
        d = str(r.get("date") or "")[:10]
        s = _f(r.get("sessions"))
        by_date[d] = by_date.get(d, 0.0) + s
        sessions += s
        users += _f(r.get("totalusers"))
        engaged += _f(r.get("engaged_sessions"))

    daily = [{"date": d, "sessions": int(round(v))} for d, v in sorted(by_date.items())]
    sessions_i = int(round(sessions))
    users_i = int(round(users))
    engaged_pct = round(engaged / sessions * 100.0, 1) if sessions else 0.0

    # --- key events (event_name grain) -------------------------------------
    events = _rows(C.request_json(_url(["event_name", "event_count", "conversions"])))
    counts = {}
    for r in events:
        name = str(r.get("event_name") or "")
        counts[name] = counts.get(name, 0.0) + _f(r.get("event_count"))

    ga4 = {
        "status": "live",
        "setup_note": "GA4 property {}, last 7 days (incl. today)".format(GA4_ACCOUNT),
        "sessions": sessions_i,
        "users": users_i,
        "engaged_pct": engaged_pct,
        "daily": daily,
    }
    for out_key, ev_name in KEY_EVENTS.items():
        n = int(round(counts.get(ev_name, 0.0)))
        ga4[out_key] = n
        ga4[out_key + "_cvr"] = round(n / sessions * 100.0, 1) if sessions else 0.0

    return {"ga4": ga4}


def check(data):
    ga4 = data.get("ga4", {})
    # Loud-fail only if we got no rows at all (sessions is None); zero sessions
    # is a legitimate quiet day, not an error.
    return "sessions" in ga4 and ga4["sessions"] is not None


if __name__ == "__main__":
    C.run_puller("ga4", fetch, check)
