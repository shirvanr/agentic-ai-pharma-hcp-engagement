"""
tools.py - the tool layer the agent calls, plus the compliance / guardrail layer.

Design note: the business rules live here, in a separate tested layer, NOT
inside the model and NOT inside the prompt.
Rules change on a legal timetable, not a retraining timetable, and they have to
be readable by someone who is not a data scientist.

Every function returns a plain dict. Nothing here calls an LLM.
"""

import os
import re
import sqlite3
from datetime import date, datetime, timedelta

from retriever import best_excerpt, build_retriever

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "demo.db")

TODAY = date(2026, 8, 26)          # fixed "today" so the demo is reproducible
FREQUENCY_CAP_DAYS = 14            # minimum gap between proactive contacts
MODEL_VERSION = "nbe-v1.3"

# ---------------------------------------------------------------------------
# Guardrail rule identifiers - these strings are what shows up in the trace.
# ---------------------------------------------------------------------------
RULE_MARKET = "GR-01 MARKET-ACCESS"
RULE_CONSENT = "GR-02 CONSENT"
RULE_MLR = "GR-03 MLR-APPROVAL"
RULE_CITATION = "GR-04 CITATION-REQUIRED"
RULE_AGGREGATE = "GR-05 AGGREGATE-ONLY-SALES"
RULE_AE = "GR-06 ADVERSE-EVENT"
RULE_OFFLABEL = "GR-07 OFF-LABEL"
RULE_HOLDOUT = "GR-08 HOLDOUT-PROTECTION"
RULE_LOOP = "GR-09 LOOP-CAP"

# ---------------------------------------------------------------------------
# Adverse-event detection. Deliberately high-recall: in pharmacovigilance a
# false positive costs a review, a false negative is a reporting failure.
# ---------------------------------------------------------------------------
AE_PATTERNS = [
    r"\bnausea\b", r"\bvomit", r"\bdizz", r"\bside[- ]effect", r"\badverse\b",
    r"\breaction\b", r"\bhospitalis", r"\bhospitaliz", r"\badmitted to hospital\b",
    r"\bunwell\b", r"\brash\b", r"\ballerg", r"\bbleeding episode\b",
    r"\bfainted\b", r"\bsyncope\b", r"\bhypoglycaem", r"\bhypoglycem",
]

# ---------------------------------------------------------------------------
# Off-label detection. Approved indications are fictional and fixed.
# ---------------------------------------------------------------------------
APPROVED_INDICATIONS = {
    "glycora": "type 2 diabetes in adults",
    "metabolix": "weight management in adults with obesity",
    "hemoclear": "a rare inherited bleeding disorder in adults and children aged 2 and over",
}
OFFLABEL_PATTERNS = [
    (r"\btype 1 diabetes\b|\bt1d\b", "type 1 diabetes"),
    (r"\bchildren\b|\bpaediatric\b|\bpediatric\b|\badolescent", "paediatric / adolescent use"),
    (r"\bpregnan", "use in pregnancy"),
    (r"\boff[- ]label\b", "explicitly off-label use"),
    (r"\bcosmetic\b|\bnon[- ]obese\b|\bhealthy (adults|patients|people)\b",
     "use outside the approved population"),
    (r"\bunapproved\b|\bnot approved\b", "an unapproved indication"),
]
# products that DO have a paediatric indication, so the paediatric rule must not fire
PAEDIATRIC_OK = {"hemoclear"}

# ---------------------------------------------------------------------------
# HCP-level prescription request detection (GR-05)
# ---------------------------------------------------------------------------
RX_LEVEL_PATTERNS = [
    r"\bhow (many|much).{0,40}\b(prescri|rx|script)",
    r"\b(prescription|rx|script)s? (volume|data|numbers|count)s?\b.{0,40}\b(dr\.?|doctor|hcp|specific|individual|per)\b",
    r"\b(dr\.?|doctor|hcp)\b.{0,40}\b(prescription|rx|script)s?\b",
    r"\bwhat (is|are) .{0,30}\b(prescri|rx)\w*",
    r"\bindividual\b.{0,20}\b(prescription|rx|sales)\b",
    r"\bhcp[- ]level\b.{0,20}\b(sales|rx|prescription)",
]


# ===========================================================================
# Data access helpers
# ===========================================================================
def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def rows(sql, args=()):
    con = connect()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def one(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None


def get_user(username, password):
    r = one("SELECT * FROM users WHERE lower(username)=lower(?)", (username or "",))
    if r and r["password"] == password:
        return r
    return None


def list_hcps(market):
    return rows("SELECT * FROM hcps WHERE market=? ORDER BY name", (market,))


# ---------------------------------------------------------------------------
# Retriever - built once over the whole corpus. MLR filtering happens at query
# time so the trace can report exactly how many documents each rule removed.
# ---------------------------------------------------------------------------
_RETRIEVER = None


def get_retriever():
    global _RETRIEVER
    if _RETRIEVER is None:
        docs = rows("SELECT asset_id, title, therapy_area, topic, format, "
                    "approved_markets, mlr_status, mlr_expiry_date, body_text, "
                    "safety_text, product FROM content_assets")
        _RETRIEVER = build_retriever(docs)
    return _RETRIEVER


# ===========================================================================
# Trace
# ===========================================================================
class Trace(object):
    """Ordered record of everything the agent did. This is the demo."""

    def __init__(self):
        self.steps = []
        self.started = datetime.now()
        self.ae_hits = []
        self.citations = {}
        self.blocked = []

    def add(self, kind, title, detail="", meta=None):
        self.steps.append({
            "n": len(self.steps) + 1,
            "kind": kind,                       # ACCESS CHECK / PLAN / TOOL CALL / GUARDRAIL / ANSWER / ERROR
            "title": title,
            "detail": detail,
            "meta": meta or {},
            "t_ms": int((datetime.now() - self.started).total_seconds() * 1000),
        })
        return self.steps[-1]

    def elapsed_ms(self):
        return int((datetime.now() - self.started).total_seconds() * 1000)


class Ctx(object):
    """Who is asking. Every tool is scoped by this."""

    def __init__(self, username, market, full_name="", role=""):
        self.username = username
        self.market = market
        self.full_name = full_name or username
        self.role = role


# ===========================================================================
# Guardrail primitives
# ===========================================================================
def scan_adverse_events(texts):
    """High-recall keyword/pattern scan. Returns list of (matched_term, text)."""
    hits = []
    for t in texts:
        low = (t or "").lower()
        for pat in AE_PATTERNS:
            m = re.search(pat, low)
            if m:
                hits.append({"term": m.group(0), "text": t})
                break
    return hits


def check_offlabel(question):
    """Returns (is_offlabel, product, indication_text) for a free-text question."""
    low = (question or "").lower()
    product = None
    for p in APPROVED_INDICATIONS:
        if p in low:
            product = p
            break
    for pat, label in OFFLABEL_PATTERNS:
        if re.search(pat, low):
            if label.startswith("paediatric") and product in PAEDIATRIC_OK:
                continue
            if label.startswith("paediatric") and product is None:
                continue
            return True, product, label
    return False, product, None


def looks_like_hcp_level_rx(question):
    low = (question or "").lower()
    for pat in RX_LEVEL_PATTERNS:
        if re.search(pat, low):
            return True
    return False


def resolve_hcp(name_or_id):
    """Find an HCP across ALL markets - the market check happens afterwards, so
    that a cross-market attempt is detected and refused rather than silently
    returning 'not found'."""
    key = (name_or_id or "").strip()
    if not key:
        return None
    r = one("SELECT * FROM hcps WHERE upper(hcp_id)=upper(?)", (key,))
    if r:
        return r
    r = one("SELECT * FROM hcps WHERE lower(name)=lower(?)", (key,))
    if r:
        return r
    cleaned = re.sub(r"^\s*(dr\.?|doctor|prof\.?)\s+", "", key, flags=re.I).strip()
    r = one("SELECT * FROM hcps WHERE lower(name) LIKE lower(?)", ("%" + cleaned + "%",))
    if r:
        return r
    parts = [p for p in re.split(r"\s+", cleaned) if len(p) > 2]
    if parts:
        r = one("SELECT * FROM hcps WHERE lower(name) LIKE lower(?)", ("%" + parts[-1] + "%",))
        if r:
            return r
    return None


def _market_denied(ctx, hcp, trace):
    """GR-01. Returns an error dict if out of scope, else None."""
    if hcp["market"] == ctx.market:
        return None
    msg = ("Access denied. %s is registered in the %s market. Your access scope is "
           "%s HCPs only, so %s data cannot be shown to you. Contact the %s market "
           "team if you need this." %
           (hcp["name"], hcp["market"], ctx.market, hcp["market"], hcp["market"]))
    if trace:
        trace.add("GUARDRAIL", "%s -> BLOCK" % RULE_MARKET,
                  "user=%s scope=%s | requested HCP market=%s" %
                  (ctx.username, ctx.market, hcp["market"]),
                  {"rule": RULE_MARKET, "result": "BLOCK"})
        trace.blocked.append(RULE_MARKET)
    return {"error": "access_denied", "rule": RULE_MARKET, "message": msg}


def _days_since(iso):
    try:
        return (TODAY - date.fromisoformat(iso)).days
    except Exception:
        return None


def _record_ae(trace, hcp_name, notes):
    """GR-06. Deterministic adverse-event scan over call notes.

    This runs in the TOOLS layer, not in the prompt, and it runs on every tool
    that touches an HCP. Processing rep call notes creates a legal detection
    duty; discharging it must not depend on whether the model happened to plan a
    call to get_engagement_history.
    """
    hits = scan_adverse_events(notes)
    if not hits or not trace:
        return hits
    seen = {(h["hcp"], h["text"]) for h in trace.ae_hits}
    fresh = []
    for h in hits:
        key = (hcp_name, h["text"])
        if key in seen:
            continue
        seen.add(key)
        trace.ae_hits.append({"hcp": hcp_name, "term": h["term"], "text": h["text"]})
        fresh.append(h)
    if fresh:
        trace.add("GUARDRAIL", "%s -> ROUTED" % RULE_AE,
                  "%d call note(s) matched adverse-event patterns (%s). "
                  "Routed to Pharmacovigilance (simulated)." %
                  (len(fresh), ", ".join(sorted({h["term"] for h in fresh}))),
                  {"rule": RULE_AE, "result": "ROUTED"})
        trace.blocked.append(RULE_AE)
    return hits


def _register_asset_citation(trace, asset_id, why=""):
    """Assets the MODEL recommends are citable too, not only retrieved ones."""
    if not trace or not asset_id or asset_id in trace.citations:
        return
    a = one("SELECT * FROM content_assets WHERE asset_id=?", (asset_id,))
    if not a:
        return
    trace.citations[asset_id] = {
        "asset_id": a["asset_id"], "title": a["title"],
        "excerpt": best_excerpt(why or a["topic"], a["body_text"]),
        "score": None, "safety_text": a["safety_text"],
        "format": a["format"], "product": a["product"],
    }


# ===========================================================================
# TOOL 1 - get_hcp_profile
# ===========================================================================
def get_hcp_profile(ctx, trace=None, name_or_id=None, **_):
    hcp = resolve_hcp(name_or_id)
    if not hcp:
        return {"error": "not_found",
                "message": "No HCP matching '%s' in the demo database." % name_or_id}
    denied = _market_denied(ctx, hcp, trace)
    if denied:
        return denied

    gap = _days_since(hcp["last_contact_date"])
    out = {
        "hcp_id": hcp["hcp_id"], "name": hcp["name"], "specialty": hcp["specialty"],
        "market": hcp["market"], "city": hcp["city"],
        "prescribing_tier": hcp["prescribing_tier"],
        "segment": hcp["segment"],
        "consent_email": bool(hcp["consent_email"]),
        "consent_rep_visit": bool(hcp["consent_rep"]),
        "preferred_channel": hcp["preferred_channel"],
        "last_contact_date": hcp["last_contact_date"],
        "days_since_last_contact": gap,
    }
    _record_ae(trace, hcp["name"], [
        v["call_note"] for v in rows(
            "SELECT call_note FROM rep_visits WHERE hcp_id=? AND visit_date>=? "
            "ORDER BY visit_date DESC", (hcp["hcp_id"],
                                         (TODAY - timedelta(days=365)).isoformat()))])
    if not hcp["consent_email"] and trace:
        trace.add("GUARDRAIL", "%s -> FLAG" % RULE_CONSENT,
                  "%s has NOT consented to email. Email is not an eligible channel."
                  % hcp["name"], {"rule": RULE_CONSENT, "result": "FLAG"})
    if trace:
        trace.add("TOOL CALL", 'get_hcp_profile(name_or_id="%s")' % name_or_id,
                  "-> %s | %s | Tier %s | %s | email consent: %s | last contact %sd ago" %
                  (out["hcp_id"], out["specialty"], out["prescribing_tier"],
                   out["segment"], "YES" if out["consent_email"] else "NO", gap),
                  {"tool": "get_hcp_profile"})
    return out


# ===========================================================================
# TOOL 2 - get_engagement_history
# ===========================================================================
def get_engagement_history(ctx, trace=None, hcp_id=None, months=12, **_):
    hcp = resolve_hcp(hcp_id)
    if not hcp:
        return {"error": "not_found",
                "message": "No HCP matching '%s'." % hcp_id}
    denied = _market_denied(ctx, hcp, trace)
    if denied:
        return denied

    try:
        months = int(months)
    except Exception:
        months = 12
    months = max(1, min(24, months))
    cutoff = (TODAY - timedelta(days=30 * months)).isoformat()
    hid = hcp["hcp_id"]

    em = one("SELECT COUNT(*) sends, SUM(opened) opens, SUM(clicked) clicks, "
             "SUM(unsubscribed) unsubs FROM email_events WHERE hcp_id=? AND sent_at>=?",
             (hid, cutoff)) or {}
    sends = em.get("sends") or 0
    opens = em.get("opens") or 0
    clicks = em.get("clicks") or 0

    web = one("SELECT COUNT(*) sessions, COALESCE(SUM(dwell_seconds),0) dwell, "
              "COALESCE(SUM(pages_viewed),0) pages FROM web_sessions "
              "WHERE hcp_id=? AND session_date>=?", (hid, cutoff)) or {}

    visits = rows("SELECT visit_date, rep_name, duration_min, topics, call_note, outcome "
                  "FROM rep_visits WHERE hcp_id=? AND visit_date>=? "
                  "ORDER BY visit_date DESC", (hid, cutoff))

    ev = one("SELECT COUNT(*) registered, COALESCE(SUM(attended),0) attended "
             "FROM event_attendance WHERE hcp_id=?", (hid,)) or {}

    recent_90 = one("SELECT COUNT(*) n FROM email_events WHERE hcp_id=? AND sent_at>=?",
                    (hid, (TODAY - timedelta(days=90)).isoformat()))["n"]
    recent_visits_90 = one("SELECT COUNT(*) n FROM rep_visits WHERE hcp_id=? AND visit_date>=?",
                           (hid, (TODAY - timedelta(days=90)).isoformat()))["n"]

    out = {
        "hcp_id": hid, "name": hcp["name"], "window_months": months,
        "email": {"sends": sends, "opens": opens, "clicks": clicks,
                  "unsubscribes": em.get("unsubs") or 0,
                  "open_rate": round(opens / sends, 3) if sends else 0.0,
                  "click_rate": round(clicks / sends, 3) if sends else 0.0},
        "web": {"sessions": web.get("sessions") or 0,
                "total_dwell_minutes": int((web.get("dwell") or 0) / 60),
                "pages_viewed": web.get("pages") or 0},
        "rep_visits": {"count": len(visits),
                       "last_visit_date": visits[0]["visit_date"] if visits else None,
                       "recent_notes": [
                           {"date": v["visit_date"], "topic": v["topics"],
                            "outcome": v["outcome"], "note": v["call_note"]}
                           for v in visits[:4]]},
        "events": {"registered": ev.get("registered") or 0,
                   "attended": ev.get("attended") or 0},
        "contact_frequency_last_90d": recent_90 + recent_visits_90,
        "days_since_last_contact": _days_since(hcp["last_contact_date"]),
    }
    denom = sends + len(visits) + (ev.get("registered") or 0)
    engaged = opens + clicks + (ev.get("attended") or 0) + (web.get("sessions") or 0)
    out["engagement_rate"] = round(engaged / denom, 3) if denom else 0.0

    # ---- GR-06 adverse event scan over the call notes -------------------
    hits = _record_ae(trace, hcp["name"], [v["call_note"] for v in visits])
    out["adverse_event_flags"] = [
        {"term": h["term"], "note": h["text"]} for h in hits]

    if trace:
        trace.add("TOOL CALL",
                  'get_engagement_history(hcp_id="%s", months=%d)' % (hid, months),
                  "-> %d sends / %d opens (%.0f%% open rate) | %d web sessions "
                  "(%d min dwell) | %d rep visits | %d/%d events attended | "
                  "%d contacts in last 90d" %
                  (sends, opens, 100 * out["email"]["open_rate"],
                   out["web"]["sessions"], out["web"]["total_dwell_minutes"],
                   len(visits), out["events"]["attended"], out["events"]["registered"],
                   out["contact_frequency_last_90d"]),
                  {"tool": "get_engagement_history"})
    return out


# ===========================================================================
# TOOL 3 - get_nbe_recommendation
# ===========================================================================
def get_nbe_recommendation(ctx, trace=None, hcp_id=None, **_):
    # Portfolio mode: no HCP given -> rank the rep's whole panel for this cycle.
    if not hcp_id or str(hcp_id).strip().lower() in ("all", "portfolio", "my hcps", "none"):
        return _nbe_portfolio(ctx, trace)
    hcp = resolve_hcp(hcp_id)
    if not hcp:
        return {"error": "not_found", "message": "No HCP matching '%s'." % hcp_id}
    denied = _market_denied(ctx, hcp, trace)
    if denied:
        return denied

    hid = hcp["hcp_id"]
    _record_ae(trace, hcp["name"], [
        v["call_note"] for v in rows(
            "SELECT call_note FROM rep_visits WHERE hcp_id=? AND visit_date>=? "
            "ORDER BY visit_date DESC", (hid, (TODAY - timedelta(days=365)).isoformat()))])
    sc = rows("SELECT * FROM model_scores WHERE hcp_id=? ORDER BY expected_value DESC", (hid,))
    if not sc:
        return {"error": "no_scores",
                "message": "No model scores for %s in %s." % (hid, MODEL_VERSION)}

    titles = {a["asset_id"]: a["title"] for a in rows("SELECT asset_id,title FROM content_assets")}
    ranked = []
    for r in sc:
        item = {
            "rank": len(ranked) + 1,
            "channel": r["channel"],
            "asset_id": r["asset_id"],
            "asset_title": titles.get(r["asset_id"]),
            "propensity_engage": r["propensity_engage"],
            "propensity_optout": r["propensity_optout"],
            "fatigue_score": r["fatigue_score"],
            "expected_value": r["expected_value"],
            "reason_codes": r["reason_codes"],
            "eligible": True,
            "ineligibility_reason": None,
        }
        if r["channel"] == "email" and not hcp["consent_email"]:
            item["eligible"] = False
            item["ineligibility_reason"] = "%s: no email consent on record" % RULE_CONSENT
        if r["channel"] == "rep visit" and not hcp["consent_rep"]:
            item["eligible"] = False
            item["ineligibility_reason"] = "%s: no rep-visit permission on record" % RULE_CONSENT
        ranked.append(item)

    eligible = [r for r in ranked if r["eligible"]]
    top = eligible[0] if eligible else None
    for r in ranked:
        _register_asset_citation(trace, r["asset_id"], r["reason_codes"])
    holdout = bool(sc[0]["holdout_flag"])

    out = {
        "hcp_id": hid, "name": hcp["name"], "market": hcp["market"],
        "model_version": sc[0]["model_version"], "scored_at": sc[0]["scored_at"],
        "holdout_flag": holdout,
        "recommended_action": top,
        "ranked_actions": ranked,
        "notes": [],
    }
    if holdout:
        msg = ("%s is in the randomised measurement holdout. Model-driven outreach must "
               "NOT be actioned for this HCP - it would contaminate the read on the pilot. "
               "Business-as-usual contact only." % hcp["name"])
        out["notes"].append(msg)
        if trace:
            trace.add("GUARDRAIL", "%s -> WARN" % RULE_HOLDOUT, msg,
                      {"rule": RULE_HOLDOUT, "result": "WARN"})
            trace.blocked.append(RULE_HOLDOUT)
    if not hcp["consent_email"]:
        out["notes"].append("%s: email suppressed for this HCP (no consent)." % RULE_CONSENT)
        if trace:
            trace.add("GUARDRAIL", "%s -> BLOCK email channel" % RULE_CONSENT,
                      "%s has consent_email=0. Email removed from eligible actions."
                      % hcp["name"], {"rule": RULE_CONSENT, "result": "BLOCK"})
            trace.blocked.append(RULE_CONSENT)
    if top and top["channel"] == "no contact":
        out["notes"].append(
            "Top-ranked action is NO CONTACT. The model is explicitly recommending "
            "suppression this cycle - high fatigue and falling engagement. Contacting "
            "anyway raises opt-out risk.")

    if trace:
        trace.add("TOOL CALL", 'get_nbe_recommendation(hcp_id="%s")' % hid,
                  "-> %s | top action: %s (EV %.3f) | fatigue %.2f | holdout: %s | %s" %
                  (out["model_version"],
                   top["channel"] if top else "none eligible",
                   top["expected_value"] if top else 0.0,
                   sc[0]["fatigue_score"], "YES" if holdout else "no",
                   top["reason_codes"] if top else ""),
                  {"tool": "get_nbe_recommendation"})
    return out


def _nbe_portfolio(ctx, trace=None, limit=10):
    """Rank every HCP in the user's market by the expected value of their
    top ELIGIBLE action. Answers 'who is most worth contacting this cycle'."""
    panel = list_hcps(ctx.market)
    scores = rows("SELECT ms.* FROM model_scores ms JOIN hcps h USING(hcp_id) "
                  "WHERE h.market=? ORDER BY ms.expected_value DESC", (ctx.market,))
    by_hcp = {}
    for r in scores:
        by_hcp.setdefault(r["hcp_id"], []).append(r)

    titles = {a["asset_id"]: a["title"] for a in rows("SELECT asset_id,title FROM content_assets")}
    ranked, suppressed = [], []
    for h in panel:
        sc = by_hcp.get(h["hcp_id"], [])
        if not sc:
            continue
        pick = None
        for r in sc:
            if r["channel"] == "email" and not h["consent_email"]:
                continue
            if r["channel"] == "rep visit" and not h["consent_rep"]:
                continue
            pick = r
            break
        if not pick:
            continue
        item = {"hcp_id": h["hcp_id"], "name": h["name"], "specialty": h["specialty"],
                "city": h["city"], "prescribing_tier": h["prescribing_tier"],
                "segment": h["segment"],
                "recommended_channel": pick["channel"],
                "asset_id": pick["asset_id"], "asset_title": titles.get(pick["asset_id"]),
                "expected_value": pick["expected_value"],
                "propensity_engage": pick["propensity_engage"],
                "fatigue_score": pick["fatigue_score"],
                "reason_codes": pick["reason_codes"],
                "holdout_flag": bool(pick["holdout_flag"]),
                "consent_email": bool(h["consent_email"])}
        if pick["channel"] == "no contact":
            suppressed.append(item)
        else:
            ranked.append(item)

    for r in ranked:
        _register_asset_citation(trace, r["asset_id"], r["reason_codes"])
    ranked.sort(key=lambda r: -r["expected_value"])
    suppressed.sort(key=lambda r: -r["fatigue_score"])
    actionable = [r for r in ranked if not r["holdout_flag"]][:limit]
    held = [r for r in ranked if r["holdout_flag"]]

    out = {"mode": "portfolio", "market": ctx.market,
           "model_version": MODEL_VERSION,
           "panel_size": len(panel),
           "top_actions": actionable,
           "suppressed_no_contact": suppressed,
           "in_holdout_excluded": [{"hcp_id": r["hcp_id"], "name": r["name"]} for r in held],
           "notes": []}
    if held:
        out["notes"].append(
            "%s: %d HCP(s) excluded from this list because they are in the randomised "
            "measurement holdout." % (RULE_HOLDOUT, len(held)))
        if trace:
            trace.add("GUARDRAIL", "%s -> EXCLUDE %d HCP(s)" % (RULE_HOLDOUT, len(held)),
                      "Holdout HCPs removed from the outreach list so the pilot read "
                      "stays clean: %s" % ", ".join(r["name"] for r in held),
                      {"rule": RULE_HOLDOUT, "result": "EXCLUDE"})
            trace.blocked.append(RULE_HOLDOUT)
    no_consent = [h for h in panel if not h["consent_email"]]
    if no_consent:
        out["notes"].append(
            "%s: %d HCP(s) in this market have no email consent; email was removed from "
            "their eligible actions." % (RULE_CONSENT, len(no_consent)))
        if trace:
            trace.add("GUARDRAIL", "%s -> BLOCK email for %d HCP(s)"
                      % (RULE_CONSENT, len(no_consent)),
                      ", ".join(h["name"] for h in no_consent),
                      {"rule": RULE_CONSENT, "result": "BLOCK"})
            trace.blocked.append(RULE_CONSENT)
    if suppressed:
        out["notes"].append(
            "%d HCP(s) are scored 'no contact' this cycle - the model is recommending "
            "suppression, not an outreach action." % len(suppressed))

    if trace:
        trace.add("TOOL CALL", 'get_nbe_recommendation(hcp_id=None)  [portfolio mode]',
                  "-> %s | panel %d | %d actionable | %d suppressed (no contact) | "
                  "%d excluded (holdout)\n   %s" %
                  (MODEL_VERSION, len(panel), len(actionable), len(suppressed), len(held),
                   " | ".join("%s %s (EV %.2f)" % (r["name"], r["recommended_channel"],
                                                   r["expected_value"])
                              for r in actionable[:5])),
                  {"tool": "get_nbe_recommendation"})
    return out


# ===========================================================================
# TOOL 4 - search_approved_content  (RAG + GR-03 MLR gate)
# ===========================================================================
def search_approved_content(ctx, trace=None, query=None, market=None, k=5, **_):
    market = (market or ctx.market or "").strip() or ctx.market
    if market.lower() != ctx.market.lower():
        msg = ("Access denied. You asked for content approved in %s but your access "
               "scope is %s. Content approval differs by market and cross-market use "
               "is not permitted." % (market, ctx.market))
        if trace:
            trace.add("GUARDRAIL", "%s -> BLOCK" % RULE_MARKET, msg,
                      {"rule": RULE_MARKET, "result": "BLOCK"})
            trace.blocked.append(RULE_MARKET)
        return {"error": "access_denied", "rule": RULE_MARKET, "message": msg}

    try:
        k = int(k)
    except Exception:
        k = 5
    k = max(1, min(8, k))

    r = get_retriever()
    candidates = r.search(query or "", market=market, k=k)

    kept, blocked = [], {"expired MLR": 0, "pending MLR": 0, "not approved in market": 0}
    for d in candidates:
        if d["mlr_status"] == "expired":
            blocked["expired MLR"] += 1
            continue
        if d["mlr_status"] == "pending":
            blocked["pending MLR"] += 1
            continue
        try:
            if date.fromisoformat(d["mlr_expiry_date"]) <= TODAY:
                blocked["expired MLR"] += 1
                continue
        except Exception:
            pass
        approved = [m.strip().lower() for m in (d["approved_markets"] or "").split(",")]
        if market.lower() not in approved:
            blocked["not approved in market"] += 1
            continue
        kept.append(d)
        if len(kept) >= k:
            break

    results = []
    for d in kept:
        results.append({
            "asset_id": d["asset_id"], "title": d["title"], "score": d["score"],
            "therapy_area": d["therapy_area"], "topic": d["topic"],
            "format": d["format"], "product": d.get("product"),
            "mlr_status": d["mlr_status"], "mlr_expiry_date": d["mlr_expiry_date"],
            "approved_markets": d["approved_markets"],
            "excerpt": best_excerpt(query, d["body_text"]),
            "safety_text": d["safety_text"],
        })
        if trace:
            trace.citations[d["asset_id"]] = {
                "asset_id": d["asset_id"], "title": d["title"],
                "excerpt": best_excerpt(query, d["body_text"]),
                "score": d["score"], "safety_text": d["safety_text"],
                "format": d["format"], "product": d.get("product"),
            }

    n_blocked = sum(blocked.values())
    if trace:
        reasons = ", ".join("%d %s" % (v, k2) for k2, v in blocked.items() if v)
        trace.add("TOOL CALL",
                  'search_approved_content(query="%s", market="%s", k=%d)'
                  % (query, market, k),
                  "-> %d candidates -> %d blocked (%s) -> %d returned\n   %s" %
                  (len(candidates), n_blocked, reasons or "none", len(results),
                   " | ".join("%s (%.2f) \"%s\"" % (x["asset_id"], x["score"], x["title"])
                              for x in results) or "no approved match"),
                  {"tool": "search_approved_content"})
        if n_blocked:
            trace.add("GUARDRAIL", "%s -> %d asset(s) excluded" % (RULE_MLR, n_blocked),
                      "Excluded before retrieval reached the model: %s. The corpus the "
                      "model sees is MLR-approved, in-date and market-approved only."
                      % (reasons or "none"), {"rule": RULE_MLR, "result": "FILTER"})
            trace.blocked.append(RULE_MLR)

    return {"query": query, "market": market,
            "retriever": r.name,
            "candidates_considered": len(candidates),
            "blocked_count": n_blocked, "blocked_breakdown":
                {k2: v for k2, v in blocked.items() if v},
            "results": results}


# ===========================================================================
# TOOL 5 - get_market_sales  (GR-05 aggregate only)
# ===========================================================================
def get_market_sales(ctx, trace=None, market=None, product=None, months=6, **kwargs):
    # --- GR-05: refuse anything that asks for HCP-level prescription data ---
    hcp_ask = kwargs.get("hcp_id") or kwargs.get("hcp") or kwargs.get("hcp_name")
    if hcp_ask:
        msg = ("Refused. Prescription and sales data in this system are AGGREGATE ONLY - "
               "by market, region, product and month. There is no HCP-level prescription "
               "table, by design: a prescription does not carry a clean line back to the "
               "engagement that influenced it, and attributing sales to an individual "
               "doctor would be both unsupported by the data and a privacy problem. "
               "Engagement is measured at HCP level; sales are read at market level.")
        if trace:
            trace.add("GUARDRAIL", "%s -> REFUSE" % RULE_AGGREGATE,
                      "Request included an HCP identifier (%s). rx_sales has no hcp_id "
                      "column." % hcp_ask, {"rule": RULE_AGGREGATE, "result": "REFUSE"})
            trace.blocked.append(RULE_AGGREGATE)
        return {"error": "hcp_level_refused", "rule": RULE_AGGREGATE, "message": msg}

    market = (market or ctx.market or "").strip() or ctx.market
    if market.lower() != ctx.market.lower():
        msg = ("Access denied. Your access scope is %s. Sales for %s are not available "
               "to you." % (ctx.market, market))
        if trace:
            trace.add("GUARDRAIL", "%s -> BLOCK" % RULE_MARKET, msg,
                      {"rule": RULE_MARKET, "result": "BLOCK"})
            trace.blocked.append(RULE_MARKET)
        return {"error": "access_denied", "rule": RULE_MARKET, "message": msg}

    try:
        months = int(months)
    except Exception:
        months = 6
    months = max(1, min(12, months))

    sql = ("SELECT month, product, region, SUM(units) units, "
           "ROUND(SUM(revenue_index),2) revenue_index FROM rx_sales WHERE market=?")
    args = [market]
    if product:
        sql += " AND lower(product)=lower(?)"
        args.append(product)
    sql += " GROUP BY month, product, region ORDER BY month DESC"
    all_rows = rows(sql, tuple(args))
    months_avail = sorted({r["month"] for r in all_rows}, reverse=True)[:months]
    data = [r for r in all_rows if r["month"] in months_avail]

    by_month = {}
    for r in data:
        key = (r["month"], r["product"])
        by_month.setdefault(key, 0)
        by_month[key] += r["units"]
    summary = [{"month": m, "product": p, "units": u}
               for (m, p), u in sorted(by_month.items(), reverse=True)]

    out = {"market": market, "product": product or "all", "months": months,
           "granularity": "market / region / product / month - AGGREGATE ONLY",
           "hcp_level_available": False,
           "note": ("There is no HCP-level prescription data in this system by design. "
                    "Sales are observed broader and less often than engagement."),
           "rows": data[:60], "monthly_totals": summary}
    if trace:
        tot = sum(r["units"] for r in data)
        trace.add("TOOL CALL",
                  'get_market_sales(market="%s", product=%s, months=%d)'
                  % (market, ("\"%s\"" % product) if product else "None", months),
                  "-> %d aggregate rows | %s units total | granularity: market/region/"
                  "product/month | HCP-level: NOT AVAILABLE BY DESIGN"
                  % (len(data), "{:,}".format(tot)), {"tool": "get_market_sales"})
    return out


# ===========================================================================
# TOOL 6 - check_compliance
# ===========================================================================
def check_compliance(ctx, trace=None, hcp_id=None, asset_id=None, channel=None, **_):
    hcp = resolve_hcp(hcp_id)
    if not hcp:
        return {"error": "not_found", "message": "No HCP matching '%s'." % hcp_id}
    denied = _market_denied(ctx, hcp, trace)
    if denied:
        return denied

    reasons = []
    allowed = True

    # --- consent -----------------------------------------------------------
    ch = (channel or "").strip().lower()
    if ch in ("email", "e-mail"):
        if not hcp["consent_email"]:
            allowed = False
            reasons.append("%s BLOCK: %s has no email consent on record."
                           % (RULE_CONSENT, hcp["name"]))
        else:
            reasons.append("%s PASS: email consent on record." % RULE_CONSENT)
    elif ch in ("rep visit", "rep_visit", "visit"):
        if not hcp["consent_rep"]:
            allowed = False
            reasons.append("%s BLOCK: no rep-visit permission on record." % RULE_CONSENT)
        else:
            reasons.append("%s PASS: rep-visit permission on record." % RULE_CONSENT)
    elif ch:
        reasons.append("%s PASS: '%s' does not require channel-specific consent."
                       % (RULE_CONSENT, channel))

    # --- frequency cap -----------------------------------------------------
    gap = _days_since(hcp["last_contact_date"])
    if gap is not None and gap < FREQUENCY_CAP_DAYS:
        allowed = False
        reasons.append("FREQUENCY CAP BLOCK: last contact was %d days ago; the cap is "
                       "%d days between proactive contacts." % (gap, FREQUENCY_CAP_DAYS))
    else:
        reasons.append("FREQUENCY CAP PASS: %s days since last contact (cap %d)."
                       % (gap, FREQUENCY_CAP_DAYS))

    # --- asset MLR + market approval --------------------------------------
    asset_info = None
    if asset_id:
        a = one("SELECT * FROM content_assets WHERE upper(asset_id)=upper(?)", (asset_id,))
        if not a:
            allowed = False
            reasons.append("%s BLOCK: asset %s not found in the approved library."
                           % (RULE_MLR, asset_id))
        else:
            expired = False
            try:
                expired = date.fromisoformat(a["mlr_expiry_date"]) <= TODAY
            except Exception:
                pass
            approved_markets = [m.strip() for m in a["approved_markets"].split(",")]
            if a["mlr_status"] != "approved" or expired:
                allowed = False
                reasons.append("%s BLOCK: %s has MLR status '%s'%s. It must not be used."
                               % (RULE_MLR, asset_id, a["mlr_status"],
                                  " and expired on %s" % a["mlr_expiry_date"] if expired else ""))
            elif ctx.market not in approved_markets:
                allowed = False
                reasons.append("%s BLOCK: %s is approved for %s only, not %s."
                               % (RULE_MLR, asset_id, "/".join(approved_markets), ctx.market))
            else:
                reasons.append("%s PASS: %s approved, valid to %s, cleared for %s."
                               % (RULE_MLR, asset_id, a["mlr_expiry_date"], ctx.market))
            asset_info = {"asset_id": a["asset_id"], "title": a["title"],
                          "mlr_status": a["mlr_status"],
                          "mlr_expiry_date": a["mlr_expiry_date"],
                          "approved_markets": a["approved_markets"]}

    # --- holdout -----------------------------------------------------------
    hold = one("SELECT holdout_flag FROM model_scores WHERE hcp_id=? LIMIT 1", (hcp["hcp_id"],))
    in_holdout = bool(hold["holdout_flag"]) if hold else False
    if in_holdout:
        reasons.append("%s WARN: HCP is in the randomised measurement holdout - "
                       "model-driven outreach must not be actioned." % RULE_HOLDOUT)

    out = {"hcp_id": hcp["hcp_id"], "name": hcp["name"], "channel": channel,
           "asset": asset_info, "allowed": allowed, "in_holdout": in_holdout,
           "reasons": reasons}
    if trace:
        trace.add("TOOL CALL",
                  'check_compliance(hcp_id="%s", asset_id=%s, channel=%s)'
                  % (hcp["hcp_id"], ("\"%s\"" % asset_id) if asset_id else "None",
                     ("\"%s\"" % channel) if channel else "None"),
                  "-> allowed=%s\n   %s" % (allowed, "\n   ".join(reasons)),
                  {"tool": "check_compliance"})
        trace.add("GUARDRAIL",
                  "COMPLIANCE CHECK -> %s" % ("ALLOW" if allowed else "BLOCK"),
                  " | ".join(reasons), {"rule": "COMPLIANCE", "result":
                                        "ALLOW" if allowed else "BLOCK"})
    return out


# ===========================================================================
# Registry + JSON schemas for tool-calling
# ===========================================================================
TOOL_FUNCTIONS = {
    "get_hcp_profile": get_hcp_profile,
    "get_engagement_history": get_engagement_history,
    "get_nbe_recommendation": get_nbe_recommendation,
    "search_approved_content": search_approved_content,
    "get_market_sales": get_market_sales,
    "check_compliance": check_compliance,
}

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "get_hcp_profile",
        "description": "Look up one healthcare professional by name or HCP id. Returns "
                       "specialty, market, prescribing tier, segment, consent flags, "
                       "preferred channel and days since last contact.",
        "parameters": {"type": "object", "properties": {
            "name_or_id": {"type": "string",
                           "description": "HCP name (e.g. 'Sarah Miller') or id (e.g. 'HCP-US-004')"}},
            "required": ["name_or_id"]}}},
    {"type": "function", "function": {
        "name": "get_engagement_history",
        "description": "Email, web, rep-visit and event history for one HCP, plus a "
                       "computed engagement rate, recency and contact frequency, and "
                       "the recent rep call notes.",
        "parameters": {"type": "object", "properties": {
            "hcp_id": {"type": "string", "description": "HCP id or name"},
            "months": {"type": "integer", "description": "Look-back window, default 12"}},
            "required": ["hcp_id"]}}},
    {"type": "function", "function": {
        "name": "get_nbe_recommendation",
        "description": "The Next Best Engagement model output. With an hcp_id it "
                       "returns that HCP's ranked channel/content actions with expected "
                       "value, propensities, fatigue score, reason codes, model version "
                       "and holdout status. With NO hcp_id it returns the whole panel "
                       "for the user's market, ranked - use that for questions like "
                       "'which of my doctors are most worth contacting'. Note that "
                       "'no contact' is a real recommendation, not an error.",
        "parameters": {"type": "object", "properties": {
            "hcp_id": {"type": "string",
                       "description": "HCP id or name. Omit for the whole panel."}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "search_approved_content",
        "description": "Search the MLR-approved content library. Only assets that are "
                       "approved, in date and cleared for the user's market are ever "
                       "returned. Every result carries its asset_id, which MUST be cited.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to search for, e.g. 'renal dosing'"},
            "market": {"type": "string", "description": "Market. Defaults to the user's market."},
            "k": {"type": "integer", "description": "How many results, default 5"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_market_sales",
        "description": "Aggregate sales by market, region, product and month. HCP-level "
                       "prescription data does not exist in this system and requests for "
                       "it are refused.",
        "parameters": {"type": "object", "properties": {
            "market": {"type": "string"},
            "product": {"type": "string", "description": "Glycora, Metabolix or Hemoclear"},
            "months": {"type": "integer", "description": "Look-back window, default 6"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "check_compliance",
        "description": "Run the compliance rules for a planned action: consent for the "
                       "channel, MLR status and market approval of the asset, the "
                       "frequency cap, and holdout status. Returns allowed plus reasons.",
        "parameters": {"type": "object", "properties": {
            "hcp_id": {"type": "string"},
            "asset_id": {"type": "string"},
            "channel": {"type": "string",
                        "description": "email, rep visit, webinar invite or portal content"}},
            "required": ["hcp_id"]}}},
]


def run_tool(name, ctx, trace, args):
    """Dispatch with a hard error boundary - a tool must never raise into the UI."""
    fn = TOOL_FUNCTIONS.get(name)
    if not fn:
        return {"error": "unknown_tool", "message": "No tool called '%s'." % name}
    try:
        return fn(ctx, trace, **(args or {}))
    except TypeError as e:
        return {"error": "bad_arguments", "message": "Bad arguments for %s: %s" % (name, e)}
    except Exception as e:
        if trace:
            trace.add("ERROR", "tool %s failed" % name, str(e))
        return {"error": "tool_failed", "message": "%s could not complete: %s" % (name, e)}
