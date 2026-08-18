#!/usr/bin/env python3
"""
pull_hubspot.py  --  HubSpot leg of the GTM dashboard refresh.

Secret:      HUBSPOT_TOKEN  (read-only Private App token; GitHub repo secret)
Produces:    icp, bands, pipeline, and the HubSpot half of the funnel
Endpoint:    https://api.hubapi.com  (CRM v3 Search + Pipelines), stdlib urllib
Auth:        Authorization: Bearer <HUBSPOT_TOKEN>

------------------------------------------------------------------------------
HOW THE NUMBERS ARE DEFINED  (verified against data.json on 2026-08-16/17)
------------------------------------------------------------------------------
ICP companies  = COMPANY where:
    no_sellers          IN {3, 4, 5-9, 10-19, 20-49, 50+}   (property "No. Sellers")
    country             IN {Australia, New Zealand}
  HubSpot split via company property `hubspot_technoligies` = true
  (note: that internal name is misspelled in HubSpot; it is the correct field).
  Verified: combined = 4711, hubspot = 1877  -> matches data.json exactly.

  Seller bands map to data.json bands 1:1:
    borderline_3_4  = bands[3] + bands[4]
    core_5_19       = bands[5-9] + bands[10-19]
    high_value_20p  = bands[20-49] + bands[50+]   (hv_20_49 / hv_50p split out)

CEO / CRO contacts = CONTACT where:
    hs_persona = persona_2 (CEO/founder)  ->  "CEO contacts"
    hs_persona = persona_1 (revenue leader) -> "CRO contacts"
    company_country     IN {Australia, New Zealand}   (mirrored company field)
    company_no__sellers IN the six bands              (mirrored company field)
  HubSpot split via contact property `hubspot_technologies` = true
    (note: the CONTACT field is spelled correctly, unlike the COMPANY one).
  First-degree ("_1st") = same + linkedin_connected = true.
  Verified: ceo_contacts + cro_contacts = 4961 = funnel.icp_contacts, and
            ceo_1st + cro_1st = 695 = funnel.connected.  Exact match.
  NOTE (flagged): the persona-based CEO/CRO split is APPROXIMATE — the existing
  producer itself labels it CONTACT_SPLIT_APPROX. Live counts drift day to day.

FUNNEL (HubSpot half): Track A coverage.
    icp_contacts = ceo_contacts + cro_contacts   (all ANZ ICP CEO+CRO)
    connected    = ceo_1st + cro_1st             (first-degree, incl. pre-existing)
  Track B (outreach) enrolled/invited/connected-via-outreach come from the
  Reply.io leg; intake/deal come from the beta pipeline below.

PIPELINE = beta pipeline 1663910348, ZTEST deals excluded, counted per stage
  live from HubSpot (stage labels + order pulled from the Pipelines API).
"""

import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

HS_BASE = "https://api.hubapi.com"
BETA_PIPELINE_ID = "1663910348"

# Seller bands, in the exact order used by data.json/bands.labels.
BAND_VALUES = ["3", "4", "5-9", "10-19", "20-49", "50+"]
BAND_LABELS = ["3 sellers", "4 sellers", "5-9 sellers",
               "10-19 sellers", "20-49 sellers", "50+ sellers"]
ANZ = ["Australia", "New Zealand"]

PERSONA_CEO = "persona_2"   # "Visionary founder or CEO"
PERSONA_CRO = "persona_1"   # "Revenue leader" (Connor the CRO)

# The four LinkedIn connect-only launch sequences (Reply.io IDs 1658117,
# 1676940, 1695697, 1695700). `reply_sequence_name` on HubSpot contacts carries
# the Reply.io sequence name; the IN match is case-insensitive and verified live
# to return the full connect-enrolled set. These are the ONLY sequences that
# send the tracked launch connection invites, so they define "invited via the
# launch campaign" at the contact level (used for the uninvited breakdown).
CONNECT_SEQUENCE_NAMES = [
    "LI connect only - ANZ 2nd Degree Connection CEOs at 3+ Rep Companies Using HubSpot",
    "LI Connect Only - ANZ 2nd Degree Connection CROs at 3+ Rep Companies Using HubSpot",
    "LI connect only - ANZ 2nd Degree Connection CEOs at 3+ Rep Companies - Non-HubSpot",
    "LI Connect Only - ANZ 2nd Degree Connection CROs at 3+ Rep Companies - Non-HubSpot",
]

# ---------------------------------------------------------------------------
# Coverage / completeness pivot configuration (feeds the 2.0 dashboard).
# Column orders match the mockup EXACTLY so the renderer stays a straight read.
# ---------------------------------------------------------------------------
# CRM dimension = company property `crm_detected` (BuiltWith detection). The
# mockup collapses it to four visible buckets + All.
CRM_COLUMNS = ["HubSpot", "Salesforce", "Pipedrive", "Other", "All"]

# Industry dimension = company `industry` (UPPER_SNAKE_CASE enum). The mockup
# shows five named buckets + Other + All; everything not named maps to Other.
INDUSTRY_MAP = {
    "INFORMATION_TECHNOLOGY_AND_SERVICES": "IT & Services",
    "COMPUTER_SOFTWARE": "Software",
    "STAFFING_AND_RECRUITING": "Staffing",
    "MARKETING_AND_ADVERTISING": "Marketing",
    "WHOLESALE": "Wholesale",
}
INDUSTRY_COLUMNS = ["IT & Services", "Software", "Staffing",
                    "Marketing", "Wholesale", "Other", "All"]

# Seller-band dimension = company `no_sellers` (already one of BAND_VALUES).
BAND_COLUMNS = BAND_VALUES + ["All"]

# The seven coverage rows, in render order. Raw counts are stored; the renderer
# computes the percentages (coverage rows as % of that column's accounts,
# 1st-degree rows as % of their parent row) exactly as the mockup does.
COVERAGE_ROWS = ["accounts", "have_ceo", "ceo_1st", "have_sl",
                 "sl_1st", "have_either", "either_1st"]

# Completeness fields, in the mockup's column order.
COMPLETENESS_FIELDS = ["seller", "employees", "revenue", "year_founded", "crm"]


def _headers():
    return {"Authorization": "Bearer {}".format(C.require_env("HUBSPOT_TOKEN"))}


def hs_count(object_type, filters):
    """Return the total number of records matching a single AND filter group."""
    url = "{}/crm/v3/objects/{}/search".format(HS_BASE, object_type)
    body = {
        "filterGroups": [{"filters": filters}],
        "properties": ["hs_object_id"],
        "limit": 1,
    }
    res = C.request_json(url, method="POST", headers=_headers(), body=body)
    return int(res.get("total", 0))


def hs_search_all(object_type, filters, properties):
    """Return ALL records matching a single AND filter group, following the
    Search API `after` cursor. Used for the bulk company/contact pulls that feed
    the coverage + completeness pivots (one pull, all pivots derived in Python --
    per the brief, avoids many round-trips).

    NOTE (brief guardrail): HubSpot filtered reads can occasionally wedge and
    return the full object set regardless of filters. The caller sanity-checks
    the company total against the known ANZ 3+ base (~4,711) before trusting the
    breakdown; if it is wildly off, the coverage build is skipped and the leg
    carries forward the previous pivots rather than publishing a bad breakdown.
    """
    url = "{}/crm/v3/objects/{}/search".format(HS_BASE, object_type)
    out = []
    after = None
    pages = 0
    while True:
        body = {
            "filterGroups": [{"filters": filters}],
            "properties": properties,
            "limit": 200,
        }
        if after:
            body["after"] = after
        res = C.request_json(url, method="POST", headers=_headers(), body=body)
        out.extend(res.get("results", []))
        after = (res.get("paging", {}) or {}).get("next", {}).get("after")
        pages += 1
        if not after or pages > 200:   # 200*200 = 40k hard ceiling, never hit
            break
    return out


def band_filter(band, extra=None):
    f = [
        {"propertyName": "no_sellers", "operator": "EQ", "value": band},
        {"propertyName": "country", "operator": "IN", "values": ANZ},
    ]
    if extra:
        f += extra
    return f


def pct(part, whole):
    if not whole:
        return 0.0
    return round(part / whole * 100.0, 1)


def build_icp_and_bands():
    # --- company seller bands, combined + hubspot; non_hubspot = combined-hubspot
    combined, hubspot, non_hubspot = [], [], []
    for band in BAND_VALUES:
        c = hs_count("companies", band_filter(band))
        h = hs_count("companies", band_filter(
            band, [{"propertyName": "hubspot_technoligies", "operator": "EQ", "value": "true"}]))
        combined.append(c)
        hubspot.append(h)
        non_hubspot.append(max(0, c - h))

    def icp_block(bands, include_high_value_pct):
        total = sum(bands)
        borderline = bands[0] + bands[1]           # 3, 4
        core = bands[2] + bands[3]                  # 5-9, 10-19
        hv2049 = bands[4]                           # 20-49
        hv50 = bands[5]                             # 50+
        high_value = hv2049 + hv50
        block = {
            "total": total,
            "core_5_19": core,
            "core_pct": pct(core, total),
            "borderline_3_4": borderline,
            "borderline_pct": pct(borderline, total),
            "high_value_20p": high_value,
        }
        if include_high_value_pct:
            block["high_value_pct"] = pct(high_value, total)
        else:
            block["hv_20_49"] = hv2049
            block["hv_50p"] = hv50
        return block

    icp = {
        "combined": icp_block(combined, include_high_value_pct=True),
        "hubspot": icp_block(hubspot, include_high_value_pct=False),
        "non_hubspot": icp_block(non_hubspot, include_high_value_pct=False),
    }

    # --- CEO / CRO contact counts (combined + hubspot split + first-degree)
    def contact_counts(persona):
        base = [
            {"propertyName": "hs_persona", "operator": "EQ", "value": persona},
            {"propertyName": "company_country", "operator": "IN", "values": ANZ},
            {"propertyName": "company_no__sellers", "operator": "IN", "values": BAND_VALUES},
        ]
        hub = [{"propertyName": "hubspot_technologies", "operator": "EQ", "value": "true"}]
        first = [{"propertyName": "linkedin_connected", "operator": "EQ", "value": "true"}]
        c_total = hs_count("contacts", base)
        h_total = hs_count("contacts", base + hub)
        c_first = hs_count("contacts", base + first)
        h_first = hs_count("contacts", base + hub + first)
        return {
            "combined": (c_total, c_first),
            "hubspot": (h_total, h_first),
            "non_hubspot": (max(0, c_total - h_total), max(0, c_first - h_first)),
        }

    ceo = contact_counts(PERSONA_CEO)
    cro = contact_counts(PERSONA_CRO)

    for split in ("combined", "hubspot", "non_hubspot"):
        ceo_total, ceo_first = ceo[split]
        cro_total, cro_first = cro[split]
        icp[split]["ceo_contacts"] = ceo_total
        icp[split]["ceo_1st"] = ceo_first
        icp[split]["ceo_1st_pct"] = pct(ceo_first, ceo_total)
        icp[split]["cro_contacts"] = cro_total
        icp[split]["cro_1st"] = cro_first
        icp[split]["cro_1st_pct"] = pct(cro_first, cro_total)

    bands = {"labels": BAND_LABELS, "hubspot": hubspot, "non_hubspot": non_hubspot}

    # Track A funnel pieces (coverage), all HubSpot.
    icp_contacts = icp["combined"]["ceo_contacts"] + icp["combined"]["cro_contacts"]
    connected = icp["combined"]["ceo_1st"] + icp["combined"]["cro_1st"]
    # Track B `enrolled`: HubSpot is the canonical enrolment ledger. Reply.io's
    # API exposes no per-sequence contact totals, so `enrolled` is sourced here
    # as the ICP CEO/CRO contacts (same set as icp_contacts) that carry a
    # Reply sequence name.
    enrolled = enrolled_in_reply()
    funnel_hubspot = {
        "icp_contacts": icp_contacts,
        "connected": connected,
        "enrolled": enrolled,
    }
    return icp, bands, funnel_hubspot


def enrolled_in_reply():
    """Count ICP CEO/CRO contacts that have been enrolled in a Reply.io sequence.

    Same ICP universe as icp_contacts (hs_persona in {CEO, CRO}, ANZ company,
    seller bands) with the added requirement that `reply_sequence_name` is set.
    HAS_PROPERTY matches any contact where the property has a value."""
    total = 0
    for persona in (PERSONA_CEO, PERSONA_CRO):
        filters = [
            {"propertyName": "hs_persona", "operator": "EQ", "value": persona},
            {"propertyName": "company_country", "operator": "IN", "values": ANZ},
            {"propertyName": "company_no__sellers", "operator": "IN", "values": BAND_VALUES},
            {"propertyName": "reply_sequence_name", "operator": "HAS_PROPERTY"},
        ]
        total += hs_count("contacts", filters)
    return total


def build_pipeline():
    """Live beta-pipeline stage counts, ZTEST excluded, plus intake/deal totals
    for funnel Track B."""
    # Pipeline stage definitions (id -> label, displayOrder)
    stages_meta = []
    try:
        url = "{}/crm/v3/pipelines/deals/{}".format(HS_BASE, BETA_PIPELINE_ID)
        res = C.request_json(url, headers=_headers())
        for s in res.get("stages", []):
            stages_meta.append({
                "id": str(s.get("id")),
                "label": s.get("label", str(s.get("id"))),
                "order": s.get("displayOrder", 0),
                "closed_won": bool(s.get("metadata", {}).get("isClosed") == "true"
                                   and s.get("metadata", {}).get("probability") == "1.0"),
            })
        stages_meta.sort(key=lambda x: x["order"])
    except Exception as e:  # noqa: BLE001
        C.log("  pipeline stage fetch failed: {}".format(e))

    # Fetch all deals in the pipeline (paginated), exclude ZTEST by name.
    stage_counts = {}
    total_deals = 0
    after = None
    while True:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "pipeline", "operator": "EQ", "value": BETA_PIPELINE_ID}
            ]}],
            "properties": ["dealname", "dealstage"],
            "limit": 100,
        }
        if after:
            body["after"] = after
        res = C.request_json("{}/crm/v3/objects/deals/search".format(HS_BASE),
                             method="POST", headers=_headers(), body=body)
        for d in res.get("results", []):
            name = (d.get("properties", {}).get("dealname") or "")
            if "ztest" in name.lower():
                continue  # ZTEST records excluded from count
            stage = str(d.get("properties", {}).get("dealstage") or "")
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
            total_deals += 1
        paging = res.get("paging", {}).get("next", {})
        after = paging.get("after")
        if not after:
            break

    # Build ordered stages[] output.
    stages_out = []
    order_by_id = {}
    for s in stages_meta:
        order_by_id[s["id"]] = s["order"]
        stages_out.append({
            "key": s["id"],
            "label": s["label"],
            "deals": stage_counts.get(s["id"], 0),
        })

    # Track B intake / deal thresholds, resolved by label (fallback to known IDs).
    def order_of(label_substr, fallback_id):
        for s in stages_meta:
            if label_substr.lower() in s["label"].lower():
                return s["order"]
        return order_by_id.get(fallback_id)

    intake_order = order_of("Intake Call Booked", "2792549846")
    won_order = order_of("Accepted To Beta", "2792549850")
    intake = 0
    deal = 0
    if intake_order is not None:
        for sid, cnt in stage_counts.items():
            o = order_by_id.get(sid)
            if o is not None and o >= intake_order:
                intake += cnt
            if won_order is not None and o is not None and o >= won_order:
                deal += cnt

    if total_deals == 0:
        # Clean empty state (matches existing data.json behaviour).
        pipeline = {
            "stages": [],
            "note": "No deals in pipeline {} (Beta Pilot) as of last query. "
                    "ZTEST deals excluded from count.".format(BETA_PIPELINE_ID),
        }
    else:
        pipeline = {
            "stages": stages_out,
            "note": "HubSpot pipeline {} (Beta Pilot). ZTEST records excluded. "
                    "{} live deal(s).".format(BETA_PIPELINE_ID, total_deals),
        }
    return pipeline, {"intake": intake, "deal": deal, "total_deals": total_deals}


def icp_contact_base(extra=None):
    """The ICP CEO/SL contact universe: hs_persona in {CEO, SL}, ANZ company,
    3+ seller band. `extra` appends further AND filters."""
    f = [
        {"propertyName": "hs_persona", "operator": "IN",
         "values": [PERSONA_CEO, PERSONA_CRO]},
        {"propertyName": "company_country", "operator": "IN", "values": ANZ},
        {"propertyName": "company_no__sellers", "operator": "IN", "values": BAND_VALUES},
    ]
    if extra:
        f += extra
    return f


def build_funnel_ext_anchors():
    """The real, launch-funnel anchor counts, each a single clean count on the
    ICP CEO/SL contact base (NOT the persona-split sum, which is approximate --
    see CONTACT_SPLIT_APPROX). Verified live against the signed-off mockup:
        icp_contacts            4613
        first_connections_total 1285   (linkedin_connected = true)
        added_to_sequence       4428   (reply_sequence_name set)
        no_li_profile           small  (hs_linkedin_url NOT set -- see note)

    no_li_profile is the LITERAL "no hs_linkedin_url" count per the brief. Note:
    live it is near-zero (almost every ICP contact carries hs_linkedin_url), so
    the funnel explainer derives the email-only remainder arithmetically in the
    assembler instead of trusting this raw signal. It is still emitted for
    transparency.
    """
    first = [{"propertyName": "linkedin_connected", "operator": "EQ", "value": "true"}]
    in_connect = [{"propertyName": "reply_sequence_name", "operator": "IN",
                   "values": CONNECT_SEQUENCE_NAMES}]
    no_seq = [{"propertyName": "reply_sequence_name", "operator": "NOT_HAS_PROPERTY"}]

    icp_contacts = hs_count("contacts", icp_contact_base())
    first_connections_total = hs_count("contacts", icp_contact_base(first))
    added_to_sequence = hs_count("contacts", icp_contact_base(
        [{"propertyName": "reply_sequence_name", "operator": "HAS_PROPERTY"}]))
    no_li_profile = hs_count("contacts", icp_contact_base(
        [{"propertyName": "hs_linkedin_url", "operator": "NOT_HAS_PROPERTY"}]))

    # ---- uninvited breakdown pieces (why non-connected ICP contacts were not
    # sent a launch connection invite). Computed as NOT-connected counts per
    # reply_sequence_name bucket; NEQ on a nullable boolean would drop nulls, so
    # each bucket's not-connected count = bucket_total - bucket_connected.
    connect_total = hs_count("contacts", icp_contact_base(in_connect))
    connect_conn = hs_count("contacts", icp_contact_base(in_connect + first))
    none_total = hs_count("contacts", icp_contact_base(no_seq))
    none_conn = hs_count("contacts", icp_contact_base(no_seq + first))
    nc_connect = max(0, connect_total - connect_conn)   # not-connected, in a launch connect seq
    nc_none = max(0, none_total - none_conn)             # not-connected, no sequence yet
    not_connected = max(0, icp_contacts - first_connections_total)
    nc_other = max(0, not_connected - nc_connect - nc_none)  # not-connected, other/email seq

    return {
        "icp_contacts": icp_contacts,
        "first_connections_total": first_connections_total,
        "added_to_sequence": added_to_sequence,
        "no_li_profile_raw": no_li_profile,
        # uninvited breakdown inputs (see assembler for the final reason split):
        "uninvited_nc_connect": nc_connect,
        "uninvited_nc_none": nc_none,
        "uninvited_nc_other": nc_other,
    }


# ---- coverage + completeness (one bulk pull, all pivots derived) -----------

def _crm_bucket(props):
    v = (props.get("crm_detected") or "").strip()
    if v in ("HubSpot", "Salesforce", "Pipedrive"):
        return v
    return "Other"


def _industry_bucket(props):
    return INDUSTRY_MAP.get((props.get("industry") or "").strip(), "Other")


def _band_bucket(props):
    v = (props.get("no_sellers") or "").strip()
    return v if v in BAND_VALUES else None   # None -> excluded (sub-floor)


def _is_hubspot_company(props):
    return (props.get("hubspot_technoligies") or "").strip().lower() == "true"


def _present(v):
    return v is not None and str(v).strip() not in ("", "0")


def _num_positive(v):
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return False


def _crm_present(v):
    v = (v or "").strip()
    return bool(v) and v != "No CRM In Use"


def _company_rollup(contacts):
    """Group ICP CEO/SL contacts to their associated company. Returns
    company_id -> {ceo, ceo1, sl, sl1} booleans (the company-level 1st-degree
    rollups computed in the puller, per DECIDED in the brief)."""
    roll = {}
    for c in contacts:
        p = c.get("properties", {}) or {}
        cid = str(p.get("associatedcompanyid") or "").strip()
        if not cid:
            continue
        persona = (p.get("hs_persona") or "").strip()
        first = (p.get("linkedin_connected") or "").strip().lower() == "true"
        r = roll.setdefault(cid, {"ceo": False, "ceo1": False,
                                  "sl": False, "sl1": False})
        if persona == PERSONA_CEO:
            r["ceo"] = True
            if first:
                r["ceo1"] = True
        elif persona == PERSONA_CRO:
            r["sl"] = True
            if first:
                r["sl1"] = True
    return roll


def _build_coverage_pivot(companies, bucket_fn, columns, roll):
    counts = {rk: {c: 0 for c in columns} for rk in COVERAGE_ROWS}
    for co in companies:
        props = co.get("properties", {}) or {}
        col = bucket_fn(props)
        if col is None:
            continue
        r = roll.get(str(co.get("id")), {})
        ceo, ceo1 = r.get("ceo", False), r.get("ceo1", False)
        sl, sl1 = r.get("sl", False), r.get("sl1", False)
        either, either1 = (ceo or sl), (ceo1 or sl1)
        for tgt in (col, "All"):
            counts["accounts"][tgt] += 1
            if ceo:
                counts["have_ceo"][tgt] += 1
            if ceo1:
                counts["ceo_1st"][tgt] += 1
            if sl:
                counts["have_sl"][tgt] += 1
            if sl1:
                counts["sl_1st"][tgt] += 1
            if either:
                counts["have_either"][tgt] += 1
            if either1:
                counts["either_1st"][tgt] += 1
    rows = {rk: [counts[rk][c] for c in columns] for rk in COVERAGE_ROWS}
    return {"columns": columns, "rows": rows}


def _build_completeness_pivot(companies, bucket_fn, order, roll):
    def blank():
        return {"accounts": 0, "either": 0, "seller": 0, "employees": 0,
                "revenue": 0, "year_founded": 0, "crm": 0}

    agg = {b: blank() for b in order}
    allb = blank()
    for co in companies:
        props = co.get("properties", {}) or {}
        b = bucket_fn(props)
        if b is None or b not in agg:
            continue
        r = roll.get(str(co.get("id")), {})
        has_either = r.get("ceo", False) or r.get("sl", False)
        for tgt in (agg[b], allb):
            tgt["accounts"] += 1
            if _present(props.get("no_sellers")):
                tgt["seller"] += 1
            if _num_positive(props.get("numberofemployees")):
                tgt["employees"] += 1
            if _num_positive(props.get("annualrevenue")):
                tgt["revenue"] += 1
            if _present(props.get("founded_year")):
                tgt["year_founded"] += 1
            if _crm_present(props.get("crm_detected")):
                tgt["crm"] += 1
            if has_either:
                tgt["either"] += 1

    def pctrow(a):
        n = a["accounts"] or 1
        return {
            "cesl_pct": round(a["either"] / n * 100),
            "fields": [round(a[f] / n * 100) for f in COMPLETENESS_FIELDS],
        }

    return {
        "rows": [dict(label=b, **pctrow(agg[b])) for b in order],
        "all": pctrow(allb),
    }


def _build_band_crm_crosstab(companies):
    """Account counts as a seller-band x CRM crosstab (rows = bands + All,
    columns = HubSpot/Salesforce/Pipedrive/Other + All). Plain counts, so the
    renderer is a straight read. Row/column totals reconcile with by_band and
    by_crm."""
    crms = ["HubSpot", "Salesforce", "Pipedrive", "Other"]
    counts = {b: {c: 0 for c in crms} for b in BAND_VALUES}
    for co in companies:
        props = co.get("properties", {}) or {}
        b = _band_bucket(props)
        if b is None:
            continue
        counts[b][_crm_bucket(props)] += 1
    matrix = []
    for b in BAND_VALUES:
        row = [counts[b][c] for c in crms]
        row.append(sum(row))
        matrix.append(row)
    all_row = [sum(counts[b][c] for b in BAND_VALUES) for c in crms]
    all_row.append(sum(all_row))
    matrix.append(all_row)
    return {"columns": crms + ["All"],
            "row_labels": BAND_VALUES + ["All"],
            "matrix": matrix}


def _parse_hs_date(raw):
    """Parse a HubSpot date property (epoch-ms string or ISO date) -> date."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.isdigit():
        try:
            return datetime.utcfromtimestamp(int(s) / 1000).date()
        except (ValueError, OSError):
            return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _first_connections_weekly(contacts, weeks=27):
    """Reconstruct the ICP 1st-connections growth curve as a weekly cumulative
    series from the per-contact `linkedin_connected_date` (verified to hold the
    real connection date, not the backfill tag date). Each point is the total
    number of ICP CEO/SL contacts connected on/before that week end, so the last
    point equals first_connections_total (~1,285) and the funnel reconciles.
    Windowed to the last ~6 months so campaign-era growth is visible."""
    dates = []
    for c in contacts:
        p = c.get("properties", {}) or {}
        if (p.get("linkedin_connected") or "").strip().lower() != "true":
            continue
        d = _parse_hs_date(p.get("linkedin_connected_date"))
        if d:
            dates.append(d)
    if not dates:
        return None
    dates.sort()
    today = datetime.now(C.AEST).date()
    monday = today - timedelta(days=today.weekday())   # Monday of current week
    out = []
    for i in range(weeks - 1, -1, -1):
        wk = monday - timedelta(weeks=i)
        cutoff = wk + timedelta(days=6)                # Sunday of that week
        out.append({"w": wk.isoformat(),
                    "v": sum(1 for x in dates if x <= cutoff)})
    return out


def build_coverage_and_completeness():
    """One bulk company pull + one ICP-contact pull, then derive every coverage
    and completeness pivot in Python. Returns (coverage, completeness, series)
    where series is the weekly 1st-connections cumulative curve, or
    (None, None, None) if the bulk pull looks untrustworthy (wedged filter)."""
    company_props = ["hs_object_id", "no_sellers", "hubspot_technoligies",
                     "crm_detected", "industry", "numberofemployees",
                     "annualrevenue", "founded_year"]
    company_filters = [
        {"propertyName": "no_sellers", "operator": "IN", "values": BAND_VALUES},
        {"propertyName": "country", "operator": "IN", "values": ANZ},
    ]
    companies = hs_search_all("companies", company_filters, company_props)

    # Sanity gate: the ANZ 3+ base is ~4,700. If the filter wedged and returned
    # the whole portal (tens of thousands) or almost nothing, do NOT publish a
    # bad breakdown -- signal the assembler to carry forward instead.
    n = len(companies)
    if n < 3000 or n > 8000:
        C.log("  coverage: company pull returned {} rows (expected ~4,700) -- "
              "skipping coverage/completeness this run (carry forward)".format(n))
        return None, None, None

    contact_props = ["hs_object_id", "hs_persona", "linkedin_connected",
                     "linkedin_connected_date", "associatedcompanyid"]
    contacts = hs_search_all("contacts", icp_contact_base(), contact_props)
    roll = _company_rollup(contacts)
    series = _first_connections_weekly(contacts)

    hs_companies = [c for c in companies
                    if _is_hubspot_company(c.get("properties", {}) or {})]

    coverage = {
        "by_crm": _build_coverage_pivot(companies, _crm_bucket, CRM_COLUMNS, roll),
        "by_band_crm": _build_band_crm_crosstab(companies),
        "by_industry": _build_coverage_pivot(companies, _industry_bucket, INDUSTRY_COLUMNS, roll),
        "by_industry_hubspot": _build_coverage_pivot(hs_companies, _industry_bucket, INDUSTRY_COLUMNS, roll),
        "by_band": _build_coverage_pivot(companies, _band_bucket, BAND_COLUMNS, roll),
        "by_band_hubspot": _build_coverage_pivot(hs_companies, _band_bucket, BAND_COLUMNS, roll),
    }
    completeness = {
        "by_band": _build_completeness_pivot(companies, _band_bucket, BAND_VALUES, roll),
        "by_industry": _build_completeness_pivot(companies, _industry_bucket, INDUSTRY_COLUMNS[:-1], roll),
        "by_crm": _build_completeness_pivot(companies, _crm_bucket, CRM_COLUMNS[:-1], roll),
    }
    C.log("  coverage: {} companies ({} HubSpot), {} ICP contacts grouped to "
          "{} companies; 1st-conn weekly points: {}".format(
              n, len(hs_companies), len(contacts), len(roll),
              len(series) if series else 0))
    return coverage, completeness, series


def fetch():
    icp, bands, funnel_hubspot = build_icp_and_bands()
    pipeline, funnel_deals = build_pipeline()
    funnel_hubspot.update(funnel_deals)
    funnel_hubspot.update(build_funnel_ext_anchors())

    # Coverage/completeness (and the 1st-connections weekly curve) are
    # best-effort: a failure here must NOT blank the icp/bands/pipeline the rest
    # of the dashboard depends on.
    try:
        coverage, completeness, first_conn_weekly = build_coverage_and_completeness()
    except Exception as e:  # noqa: BLE001
        C.log("  coverage/completeness build failed: {} -- carrying forward".format(e))
        coverage, completeness, first_conn_weekly = None, None, None

    if first_conn_weekly:
        funnel_hubspot["first_connections_weekly"] = first_conn_weekly

    out = {
        "icp": icp,
        "bands": bands,
        "pipeline": pipeline,
        "funnel_hubspot": funnel_hubspot,
    }
    if coverage is not None:
        out["coverage"] = coverage
    if completeness is not None:
        out["completeness"] = completeness
    return out


def check(data):
    # Loud-fail if the ICP universe came back empty.
    try:
        return data["icp"]["combined"]["total"] > 0 and sum(data["bands"]["hubspot"]) >= 0
    except Exception:
        return False


if __name__ == "__main__":
    C.run_puller("hubspot_icp", fetch, check)
