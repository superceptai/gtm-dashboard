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
    funnel_hubspot = {"icp_contacts": icp_contacts, "connected": connected}
    return icp, bands, funnel_hubspot


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


def fetch():
    icp, bands, funnel_hubspot = build_icp_and_bands()
    pipeline, funnel_deals = build_pipeline()
    funnel_hubspot.update(funnel_deals)
    return {
        "icp": icp,
        "bands": bands,
        "pipeline": pipeline,
        "funnel_hubspot": funnel_hubspot,
    }


def check(data):
    # Loud-fail if the ICP universe came back empty.
    try:
        return data["icp"]["combined"]["total"] > 0 and sum(data["bands"]["hubspot"]) >= 0
    except Exception:
        return False


if __name__ == "__main__":
    C.run_puller("hubspot_icp", fetch, check)
