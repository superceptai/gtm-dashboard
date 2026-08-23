#!/usr/bin/env python3
"""
diag_industry_distribution.py  --  READ-ONLY industry histogram for the ICP.

Purpose
-------
Diagnostic for the dashboard's industry bucketing (Part A of the demo-ready
pass). The coverage "by industry" cut collapses every HubSpot `industry` enum
value that is NOT in pull_hubspot.INDUSTRY_MAP into a single "Other" bucket.
When "Other" is ~half the ICP the cut is close to useless because half the
picture is hidden inside one column. This script pulls the same ANZ 3+ ICP
company universe that pull_hubspot.py uses and prints the raw `industry` enum
distribution, so we can decide which large unmapped industries to promote into
their own named buckets.

It is READ-ONLY: it makes only HubSpot Search reads, writes nothing, commits
nothing, and never touches data.json / history.jsonl.

How to run
----------
    export HUBSPOT_TOKEN=<read-only HubSpot private-app token>
    python3 scripts/diag_industry_distribution.py

Output
------
A histogram sorted by count desc, one row per raw industry enum value with
columns:  raw_value  count  pct_of_icp  already_mapped(Y/N)
followed by a footer: total ICP accounts, count currently in "Other", and
"Other" as a pct of the ICP (i.e. how much of the picture is hidden today).

`already_mapped` and the "Other" total are computed against the LIVE
pull_hubspot.INDUSTRY_MAP, so after the Step A2 expansion the newly promoted
industries read Y and the reported "Other" pct drops accordingly.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pull_hubspot as P  # noqa: E402  (reuse auth, hs_search_all, filters, map)


def main():
    # Same ICP universe as pull_hubspot.build_coverage_and_completeness():
    # ANZ companies whose seller band is one of the six ICP bands.
    filters = [
        {"propertyName": "no_sellers", "operator": "IN", "values": P.BAND_VALUES},
        {"propertyName": "country", "operator": "IN", "values": P.ANZ},
    ]
    companies = P.hs_search_all(
        "companies", filters, ["industry", "no_sellers", "country"])

    total = len(companies)
    counts = {}
    for co in companies:
        raw = (co.get("properties", {}) or {}).get("industry")
        raw = (str(raw).strip() if raw else "") or "(blank)"
        counts[raw] = counts.get(raw, 0) + 1

    rows = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)

    def is_mapped(raw):
        return raw in P.INDUSTRY_MAP

    def pct(n):
        return (n / total * 100.0) if total else 0.0

    print("ICP industry distribution  (ANZ; seller bands {})".format(
        ", ".join(P.BAND_VALUES)))
    print("source: HubSpot company `industry` enum -- read-only diagnostic\n")
    print("{:<46} {:>7} {:>11} {:>14}".format(
        "raw_value", "count", "pct_of_icp", "already_mapped"))
    print("-" * 80)
    for raw, n in rows:
        print("{:<46} {:>7} {:>10.2f}% {:>14}".format(
            raw[:46], n, pct(n), "Y" if is_mapped(raw) else "N"))

    other = sum(n for raw, n in rows if not is_mapped(raw))
    print("-" * 80)
    print("total ICP accounts    : {}".format(total))
    print('in "Other" (unmapped) : {}  ({:.2f}% of ICP)'.format(other, pct(other)))


if __name__ == "__main__":
    main()
