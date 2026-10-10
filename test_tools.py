#!/usr/bin/env python3
"""
test_tools.py - exercises every tool and every guardrail with no LLM involved.
Run:  python3 test_tools.py
Prints PASS/FAIL per check and exits non-zero if anything fails.
"""

import sys

import tools
from tools import Ctx, Trace

PASSED = []
FAILED = []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print("  [%s] %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def section(t):
    print("\n" + t)
    print("-" * 88)


ALEX = Ctx("alex", "USA", "Alex Morgan", "Field Sales Representative")
ELIF = Ctx("elif", "Turkey", "Elif Polat", "Field Sales Representative")
JONAS = Ctx("jonas", "Denmark", "Jonas Lund", "Field Sales Representative")


def main():
    section("0. AUTH")
    check("valid login alex/alex", tools.get_user("alex", "alex") is not None)
    check("valid login freja/freja", tools.get_user("freja", "freja") is not None)
    check("wrong password rejected", tools.get_user("alex", "nope") is None)
    check("unknown user rejected", tools.get_user("nobody", "x") is None)

    section("1. TOOL get_hcp_profile")
    t = Trace()
    p = tools.get_hcp_profile(ALEX, t, name_or_id="Sarah Miller")
    check("resolves by plain name", p.get("hcp_id", "").startswith("HCP-US"), p.get("hcp_id", ""))
    check("returns consent flags", "consent_email" in p and "consent_rep_visit" in p)
    p2 = tools.get_hcp_profile(ALEX, t, name_or_id="Dr. Sarah Miller")
    check("resolves with 'Dr.' prefix", p2.get("hcp_id") == p.get("hcp_id"))
    p3 = tools.get_hcp_profile(ALEX, t, name_or_id=p["hcp_id"])
    check("resolves by hcp_id", p3.get("hcp_id") == p["hcp_id"])
    p4 = tools.get_hcp_profile(ALEX, t, name_or_id="Zzz Nonexistent")
    check("unknown HCP -> clean not_found", p4.get("error") == "not_found")

    section("2. GUARDRAIL GR-01 market access control")
    t = Trace()
    r = tools.get_hcp_profile(ALEX, t, name_or_id="Anders Kjaer")     # Danish HCP
    check("USA rep blocked from Danish HCP profile", r.get("error") == "access_denied",
          r.get("rule", ""))
    check("refusal names the rule", tools.RULE_MARKET in (r.get("message", "") + r.get("rule", ""))
          or r.get("rule") == tools.RULE_MARKET)
    check("block appears in trace", any(s["kind"] == "GUARDRAIL" and "MARKET" in s["title"]
                                        for s in t.steps))
    r2 = tools.get_engagement_history(ALEX, t, hcp_id="Anders Kjaer")
    check("history tool also blocked cross-market", r2.get("error") == "access_denied")
    r3 = tools.get_nbe_recommendation(ALEX, t, hcp_id="Mehmet Kaya")
    check("recommendation tool blocked cross-market", r3.get("error") == "access_denied")
    r4 = tools.check_compliance(ALEX, t, hcp_id="Mette Larsen", channel="email")
    check("compliance tool blocked cross-market", r4.get("error") == "access_denied")
    r5 = tools.search_approved_content(ALEX, t, query="renal dosing", market="Denmark")
    check("content search blocked for other market", r5.get("error") == "access_denied")
    r6 = tools.get_market_sales(ALEX, t, market="Turkey")
    check("sales blocked for other market", r6.get("error") == "access_denied")
    ok = tools.get_hcp_profile(JONAS, t, name_or_id="Anders Kjaer")
    check("Danish rep CAN see Danish HCP", ok.get("hcp_id", "").startswith("HCP-DK"))

    section("3. TOOL get_engagement_history")
    t = Trace()
    h = tools.get_engagement_history(ALEX, t, hcp_id="HCP-US-004", months=12)
    check("returns email block", "email" in h and "open_rate" in h["email"])
    check("returns web block", "web" in h)
    check("returns rep visit notes", len(h["rep_visits"]["recent_notes"]) > 0)
    check("computes engagement_rate", isinstance(h.get("engagement_rate"), float))
    check("computes contact frequency 90d", h.get("contact_frequency_last_90d", -1) >= 0)

    section("4. GUARDRAIL GR-06 adverse event detection")
    t = Trace()
    h = tools.get_engagement_history(ALEX, t, hcp_id="HCP-US-004")
    check("AE flagged on seeded USA note", len(h.get("adverse_event_flags", [])) > 0,
          str([f["term"] for f in h.get("adverse_event_flags", [])]))
    check("AE routed step in trace", any("ADVERSE" in s["title"] for s in t.steps))
    check("trace carries AE payload for the banner", len(t.ae_hits) > 0)
    t2 = Trace()
    h2 = tools.get_engagement_history(ELIF, t2, hcp_id="HCP-TR-002")
    check("AE flagged on seeded Turkey note", len(h2.get("adverse_event_flags", [])) > 0)
    t3 = Trace()
    h3 = tools.get_engagement_history(JONAS, t3, hcp_id="HCP-DK-003")
    check("AE flagged on seeded Denmark note", len(h3.get("adverse_event_flags", [])) > 0)

    section("5. TOOL get_nbe_recommendation + GR-02 consent + GR-08 holdout")
    t = Trace()
    n = tools.get_nbe_recommendation(ALEX, t, hcp_id="HCP-US-001")
    check("returns ranked actions", len(n.get("ranked_actions", [])) == 5)
    check("carries model_version", n.get("model_version") == "nbe-v1.3")
    check("carries reason codes", bool(n["ranked_actions"][0]["reason_codes"]))
    check("top action has expected_value", "expected_value" in (n.get("recommended_action") or {}))

    no_consent = tools.rows("SELECT hcp_id FROM hcps WHERE consent_email=0 AND market='USA'")
    hid = no_consent[0]["hcp_id"]
    t = Trace()
    n = tools.get_nbe_recommendation(ALEX, t, hcp_id=hid)
    email_row = [r for r in n["ranked_actions"] if r["channel"] == "email"][0]
    check("email marked ineligible for no-consent HCP", email_row["eligible"] is False,
          hid)
    check("recommended action is never email there",
          (n.get("recommended_action") or {}).get("channel") != "email")
    check("consent block visible in trace",
          any("CONSENT" in s["title"] for s in t.steps))

    hold = tools.rows("SELECT DISTINCT hcp_id FROM model_scores ms "
                      "JOIN hcps h USING(hcp_id) WHERE ms.holdout_flag=1 AND h.market='USA'")
    t = Trace()
    n = tools.get_nbe_recommendation(ALEX, t, hcp_id=hold[0]["hcp_id"])
    check("holdout HCP flagged", n.get("holdout_flag") is True, hold[0]["hcp_id"])
    check("holdout warning in trace", any("HOLDOUT" in s["title"] for s in t.steps))

    nc = tools.rows("SELECT ms.hcp_id FROM model_scores ms JOIN hcps h USING(hcp_id) "
                    "WHERE h.market='USA' GROUP BY ms.hcp_id")
    found_nc = None
    for r in nc:
        rec = tools.get_nbe_recommendation(ALEX, None, hcp_id=r["hcp_id"])
        if (rec.get("recommended_action") or {}).get("channel") == "no contact":
            found_nc = r["hcp_id"]
            break
    check("at least one USA HCP recommends 'no contact'", found_nc is not None, str(found_nc))

    section("6. TOOL search_approved_content + GR-03 MLR gate")
    t = Trace()
    s = tools.search_approved_content(ALEX, t, query="renal dosing in type 2 diabetes", k=5)
    check("returns results", len(s.get("results", [])) > 0, "%d hits" % len(s.get("results", [])))
    check("every result has an asset_id", all(r.get("asset_id") for r in s["results"]))
    check("every result has a score", all("score" in r for r in s["results"]))
    check("every result has an excerpt", all(r.get("excerpt") for r in s["results"]))
    check("all results MLR-approved", all(r["mlr_status"] == "approved" for r in s["results"]))
    check("all results approved for USA",
          all("USA" in r["approved_markets"] for r in s["results"]))
    check("top hit is renal-related", "renal" in s["results"][0]["title"].lower(),
          s["results"][0]["title"])
    check("retriever name reported", bool(s.get("retriever")), s.get("retriever"))

    # force the MLR gate to actually exclude something
    t = Trace()
    total_blocked = 0
    for q in ["cardiovascular outcomes", "weight management pathway", "prophylaxis dosing",
              "titration schedule", "annualised bleed rate", "health economics budget impact",
              "formulary access positioning", "adolescent population data"]:
        s = tools.search_approved_content(ALEX, t, query=q, k=5)
        total_blocked += s.get("blocked_count", 0)
    check("MLR gate excludes assets across queries", total_blocked > 0,
          "%d exclusions" % total_blocked)
    check("MLR exclusion logged in trace", any("MLR" in s2["title"] for s2 in t.steps))

    expired = tools.rows("SELECT asset_id FROM content_assets WHERE mlr_status='expired'")
    ids_returned = set()
    t = Trace()
    for q in ["renal", "cardiovascular", "weight", "bleed", "titration", "adherence",
              "hypoglycaemia", "paediatric", "monitoring", "surgery"]:
        for r in tools.search_approved_content(ALEX, t, query=q, k=8).get("results", []):
            ids_returned.add(r["asset_id"])
    check("no expired asset ever returned",
          not (ids_returned & {e["asset_id"] for e in expired}))
    pending = {r["asset_id"] for r in
               tools.rows("SELECT asset_id FROM content_assets WHERE mlr_status='pending'")}
    check("no pending asset ever returned", not (ids_returned & pending))
    wrong_mkt = {r["asset_id"] for r in tools.rows(
        "SELECT asset_id FROM content_assets WHERE approved_markets NOT LIKE '%USA%'")}
    check("no non-USA asset returned to a USA rep", not (ids_returned & wrong_mkt))

    section("7. GUARDRAIL GR-05 aggregate-only sales")
    t = Trace()
    m = tools.get_market_sales(ALEX, t, product="Glycora", months=6)
    check("aggregate sales returned", len(m.get("rows", [])) > 0)
    check("declares HCP level unavailable", m.get("hcp_level_available") is False)
    cols = set()
    for r in m["rows"]:
        cols |= set(r.keys())
    check("no hcp_id in any returned row", "hcp_id" not in cols, str(sorted(cols)))
    t = Trace()
    ref = tools.get_market_sales(ALEX, t, market="USA", hcp_id="HCP-US-004")
    check("HCP-level Rx request refused", ref.get("error") == "hcp_level_refused")
    check("refusal explains why", "aggregate" in ref.get("message", "").lower())
    check("refusal visible in trace", any("AGGREGATE" in s["title"] for s in t.steps))
    schema_cols = [c["name"] for c in tools.rows("PRAGMA table_info(rx_sales)")]
    check("rx_sales table itself has no hcp_id column", "hcp_id" not in schema_cols,
          ", ".join(schema_cols))

    section("8. GUARDRAIL GR-05 question-level Rx detection")
    for q in ["How many prescriptions did Dr. Miller write last month?",
              "What is Dr. Sarah Miller's Rx volume?",
              "Show me HCP-level sales for my territory"]:
        check("detects Rx-at-HCP-level: %r" % q[:42], tools.looks_like_hcp_level_rx(q))
    for q in ["What are Glycora sales in my market?",
              "What should I discuss with Dr. Miller next?"]:
        check("does NOT misfire on: %r" % q[:42], not tools.looks_like_hcp_level_rx(q))

    section("9. GUARDRAIL GR-07 off-label detection")
    for q, exp in [("Can I promote Metabolix for adolescents?", True),
                   ("Is Glycora suitable for type 1 diabetes?", True),
                   ("Can I discuss Metabolix in pregnancy?", True),
                   ("Any off-label uses for Glycora?", True),
                   ("What is the renal dosing for Glycora?", False),
                   ("What should I discuss with Dr. Miller?", False)]:
        is_off, prod, label = tools.check_offlabel(q)
        check("off-label=%s for %r" % (exp, q[:40]), is_off == exp, str(label or ""))
    is_off, _, _ = tools.check_offlabel("Hemoclear paediatric dosing")
    check("paediatric NOT off-label for Hemoclear", is_off is False)

    section("10. TOOL check_compliance")
    t = Trace()
    approved = tools.rows(
        "SELECT asset_id FROM content_assets WHERE mlr_status='approved' "
        "AND approved_markets LIKE '%USA%' AND mlr_expiry_date > '2026-08-26' LIMIT 1")
    exp_asset = tools.rows("SELECT asset_id FROM content_assets WHERE mlr_status='expired' LIMIT 1")
    pend_asset = tools.rows("SELECT asset_id FROM content_assets WHERE mlr_status='pending' LIMIT 1")
    non_usa = tools.rows("SELECT asset_id FROM content_assets "
                         "WHERE approved_markets NOT LIKE '%USA%' AND mlr_status='approved' LIMIT 1")

    c = tools.check_compliance(ALEX, t, hcp_id="HCP-US-001", asset_id=approved[0]["asset_id"],
                               channel="portal content")
    check("approved asset passes MLR check",
          any("PASS" in r and "MLR" in r for r in c["reasons"]), approved[0]["asset_id"])
    c = tools.check_compliance(ALEX, t, hcp_id="HCP-US-001", asset_id=exp_asset[0]["asset_id"],
                               channel="portal content")
    check("expired asset blocked", c["allowed"] is False and
          any("expired" in r for r in c["reasons"]), exp_asset[0]["asset_id"])
    c = tools.check_compliance(ALEX, t, hcp_id="HCP-US-001", asset_id=pend_asset[0]["asset_id"])
    check("pending asset blocked", c["allowed"] is False)
    if non_usa:
        c = tools.check_compliance(ALEX, t, hcp_id="HCP-US-001", asset_id=non_usa[0]["asset_id"])
        check("wrong-market asset blocked", c["allowed"] is False, non_usa[0]["asset_id"])
    c = tools.check_compliance(ALEX, t, hcp_id=hid, channel="email")
    check("email to no-consent HCP blocked", c["allowed"] is False and
          any("no email consent" in r for r in c["reasons"]), hid)
    c = tools.check_compliance(ALEX, t, hcp_id="HCP-US-001", asset_id="AST-9999")
    check("unknown asset blocked cleanly", c["allowed"] is False)
    fresh = tools.rows("SELECT hcp_id FROM hcps WHERE market='USA' "
                       "AND last_contact_date > '2026-08-16' LIMIT 1")
    if fresh:
        c = tools.check_compliance(ALEX, t, hcp_id=fresh[0]["hcp_id"], channel="portal content")
        check("frequency cap fires for recent contact", c["allowed"] is False and
              any("FREQUENCY CAP BLOCK" in r for r in c["reasons"]), fresh[0]["hcp_id"])

    section("11. ERROR BOUNDARY - nothing may raise")
    t = Trace()
    check("unknown tool handled", tools.run_tool("nope", ALEX, t, {}).get("error") == "unknown_tool")
    check("bad args handled",
          tools.run_tool("get_hcp_profile", ALEX, t, {"wrong": 1}).get("error") in
          ("bad_arguments", "not_found"))
    check("None args handled", tools.run_tool("get_market_sales", ALEX, t, None) is not None)
    check("garbage months handled",
          "rows" in tools.run_tool("get_market_sales", ALEX, t, {"months": "abc"}))
    check("garbage k handled",
          "results" in tools.run_tool("search_approved_content", ALEX, t,
                                      {"query": "renal", "k": "many"}))
    check("empty query handled",
          "results" in tools.run_tool("search_approved_content", ALEX, t, {"query": ""}))

    section("12. TRACE INTEGRITY")
    t = Trace()
    tools.get_hcp_profile(ALEX, t, name_or_id="Sarah Miller")
    tools.search_approved_content(ALEX, t, query="renal dosing", k=3)
    check("steps are numbered in order",
          [s["n"] for s in t.steps] == list(range(1, len(t.steps) + 1)))
    check("every step has a kind", all(s["kind"] for s in t.steps))
    check("citations collected", len(t.citations) > 0, "%d assets" % len(t.citations))
    check("elapsed time available", t.elapsed_ms() >= 0)

    print("\n" + "=" * 88)
    print("RESULT: %d passed, %d failed" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("FAILED CHECKS:")
        for f in FAILED:
            print("  - %s" % f)
    print("=" * 88)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
