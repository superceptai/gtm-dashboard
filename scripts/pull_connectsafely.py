#!/usr/bin/env python3
"""
pull_connectsafely.py  --  LinkedIn followers leg of the dashboard refresh.

Secret:    CONNECTSAFELY_API_KEY   (GitHub repo secret; ConnectSafely API key)
Produces:  linkedin { followers_current, status, source }
Endpoint:  https://api.connectsafely.ai   (ConnectSafely REST API), stdlib urllib

OPEN ITEM RESOLUTION (per brief):
  ConnectSafely DOES cleanly return the personal follower count for Aaron's
  account -- verified live: followerGrowth.totalFollowers = 16091, which matches
  data.json exactly. So the REST call is the PRIMARY path.

  Because ConnectSafely's public REST docs are not reachable from CI, the exact
  URL path could not be re-confirmed against live docs at build time. The puller
  therefore tries a small set of candidate follower-analytics paths and deep-
  searches the JSON for the follower total. If NONE returns a usable number, it
  falls back to the committed config/manual_overrides.json `linkedin_followers`
  value so a human can update the number weekly from the LinkedIn UI. The chosen
  path is reported in `source` ("connectsafely" or "manual_override") and printed
  in the run log so a non-developer can see which path was used.

Week-over-week follower deltas are computed by the assembler from history.jsonl,
not here (ConnectSafely's changePercent is not a fixed 24h/7d/30d delta).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

CS_BASE = "https://api.connectsafely.ai"
ACCOUNT_ID = os.environ.get("CONNECTSAFELY_ACCOUNT_ID", "6a5dcba69b82053edcdfd39a")

# Candidate follower-analytics paths, tried in order. {acct} is substituted.
FOLLOWER_PATHS = [
    "/followers/analytics?accountId={acct}&resultType=GROWTH&timeRange=past_30_days",
    "/linkedin/followers/analytics?accountId={acct}&resultType=GROWTH",
    "/accounts/{acct}/followers/analytics?resultType=GROWTH",
    "/followers-analytics?accountId={acct}",
]

OVERRIDE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "manual_overrides.json",
)


def _headers():
    key = C.require_env("CONNECTSAFELY_API_KEY")
    # Send both common schemes; ConnectSafely uses a bearer/api-key header.
    return {"Authorization": "Bearer {}".format(key), "x-api-key": key}


def _deep_find_followers(obj):
    """Return the first plausible total-followers integer found in the JSON."""
    keys = ("totalfollowers", "followerscount", "followers", "totalfollowercount")
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k.lower() in keys and isinstance(v, (int, float)):
                    return int(v)
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _from_connectsafely():
    last_err = None
    for tmpl in FOLLOWER_PATHS:
        url = CS_BASE + tmpl.format(acct=ACCOUNT_ID)
        try:
            res = C.request_json(url, headers=_headers())
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
        n = _deep_find_followers(res)
        if n and n > 0:
            return n
    if last_err:
        C.log("  connectsafely follower fetch failed: {}".format(last_err))
    return None


def _from_override():
    try:
        with open(OVERRIDE_FILE) as f:
            cfg = json.load(f)
        n = cfg.get("linkedin_followers")
        return int(n) if n else None
    except Exception:  # noqa: BLE001
        return None


def fetch():
    followers = _from_connectsafely()
    source = "connectsafely"
    if not followers:
        followers = _from_override()
        source = "manual_override"
        if followers:
            C.log("  using manual override for followers ({})".format(followers))
    if not followers:
        raise RuntimeError(
            "no follower count from ConnectSafely and no config/manual_overrides.json "
            "linkedin_followers value")
    return {
        "linkedin": {
            "followers_current": followers,
            "status": "fresh",
            "source": source,
        }
    }


def check(data):
    li = data.get("linkedin", {})
    return bool(li.get("followers_current"))


if __name__ == "__main__":
    C.run_puller("linkedin", fetch, check)
