"""
agent.py - the agent loop, kept explicit on purpose.

No framework. The loop is twelve lines you can read: system prompt -> question ->
the model may call tools -> execute them -> feed the results back -> repeat, with
a hard stop. The point of keeping it explicit is that the trace is honest: every
step shown in the UI is a step that actually happened here.

Three layers, in order:
  1. Pre-flight guardrails   - deterministic, run before any model call
  2. Tool-calling loop       - the LLM decides which tools to call
  3. Post-checks             - citation check, adverse-event routing

If no LLM key is configured, or every provider fails, layer 2 is replaced by a
deterministic rule-based planner and a templated answer. The demo never breaks
and the answer is clearly labelled.
"""

import json
import re
import time

import llm
import tools
from tools import Ctx, Trace  # noqa: F401  (re-exported for the UI)

MAX_ITERATIONS = 5
WALL_CLOCK_LIMIT_S = 15.0

SYSTEM_PROMPT = """You are the HCP Engagement Assistant used by pharmaceutical \
field sales representatives. You write short pre-call briefs and answer questions \
about the healthcare professionals (HCPs) the representative covers.

The user is {full_name} ({role}), whose access scope is the {market} market only.
Today is 26 August 2026. All data, products and clinical content in this system are
FICTIONAL and exist for demonstration purposes.

HOW YOU WORK
- Answer ONLY from tool results. If a tool did not return it, you do not know it.
- Never invent a clinical claim, a statistic, a study name or a product fact.
- Cite the asset_id (for example AST-0142) after every statement that comes from
  content. No asset_id, no claim.
- If data is unavailable, say so plainly. Do not guess and do not fill the gap.
- Never give HCP-level prescription or sales figures. They do not exist in this
  system by design: sales are observed at market level only.
- 'no contact' is a legitimate recommendation from the model. If it is the top
  action, say so clearly and explain why - do not substitute a different channel.
- If an HCP is in the measurement holdout, say that model-driven outreach must not
  be actioned for them.
- Respect consent: never recommend email for an HCP without email consent.

TOOLS
Call get_hcp_profile first when a specific doctor is named. Call
get_engagement_history for behaviour and recent call notes. Call
get_nbe_recommendation for the model's recommended action, or with no hcp_id for
the whole panel. Call search_approved_content for anything clinical or
promotional. Call check_compliance before recommending a specific asset on a
specific channel. Call get_market_sales only for aggregate market questions.

STYLE
Write for a representative who is about to walk into a meeting. Be concrete and
brief: short paragraphs or bullets, no preamble, no restating the question.
Aim for under 250 words. Lead with the recommended action and why."""


# ===========================================================================
# Pre-flight guardrails - deterministic, no LLM involved
# ===========================================================================
def preflight(ctx, question, trace):
    """Returns a refusal dict if the question must be stopped before the model."""
    trace.add("ACCESS CHECK",
              "user=%s  market=%s  ->  scope OK" % (ctx.username, ctx.market),
              "Access scope: %s HCPs and %s-approved content only. Every tool call "
              "below is filtered by this scope (%s)." %
              (ctx.market, ctx.market, tools.RULE_MARKET),
              {"rule": tools.RULE_MARKET, "result": "PASS"})

    # GR-05 - HCP-level prescription request
    if tools.looks_like_hcp_level_rx(question):
        msg = ("I can't provide that, and it isn't a limitation of this demo - it is "
               "a deliberate design decision.\n\n"
               "**Prescription data in this system is aggregate only**: market, region, "
               "product and month. There is no HCP-level prescription table. Two reasons:\n\n"
               "1. A prescription does not carry a clean line back to the engagement that "
               "influenced it. Attributing an individual doctor's Rx volume to your last "
               "call would be a claim the data cannot support.\n"
               "2. Individual-level prescribing data raises privacy and compliance issues "
               "that vary by market.\n\n"
               "What I can give you is aggregate sales for %s by product and month, and "
               "HCP-level *engagement* - opens, clicks, web sessions, visits - which is "
               "measured properly. Ask me for either." % ctx.market)
        trace.add("GUARDRAIL", "%s -> REFUSE" % tools.RULE_AGGREGATE,
                  "Question asks for prescription figures at HCP level. Refused before "
                  "any tool or model call. rx_sales has no hcp_id column by design.",
                  {"rule": tools.RULE_AGGREGATE, "result": "REFUSE"})
        trace.blocked.append(tools.RULE_AGGREGATE)
        return {"answer": msg, "refused": True}

    # GR-07 - off-label
    is_off, product, label = tools.check_offlabel(question)
    if is_off:
        approved = tools.APPROVED_INDICATIONS.get(product or "", None)
        msg = ("I can't help with that. You are asking about **%s**, which is outside "
               "the approved indication.\n\n" % label)
        if product and approved:
            msg += ("%s is approved for %s only. Discussing it outside that indication "
                    "would be off-label promotion.\n\n" % (product.title(), approved))
        msg += ("There is also no MLR-approved material covering it, so I have nothing "
                "I am permitted to show you. Route the question to Medical Information - "
                "they can respond to an unsolicited request from an HCP; a commercial "
                "representative cannot.")
        trace.add("GUARDRAIL", "%s -> REFUSE" % tools.RULE_OFFLABEL,
                  "Detected: %s%s. Refused before any retrieval - there is no approved "
                  "content for an unapproved indication, so retrieval is not the "
                  "mitigation, refusal is." %
                  (label, " (product: %s)" % product.title() if product else ""),
                  {"rule": tools.RULE_OFFLABEL, "result": "REFUSE"})
        trace.blocked.append(tools.RULE_OFFLABEL)
        return {"answer": msg, "refused": True}
    return None


# ===========================================================================
# Rule-based planner - used in fallback mode, and as a safety net when the
# model answers without calling anything.
# ===========================================================================
CONTENT_HINTS = ["material", "content", "asset", "evidence", "data on", "study",
                 "approved", "leave-behind", "detail aid", "clinical", "dosing",
                 "renal", "cardiovascular", "titration", "hypoglyc", "weight",
                 "tolerability", "bleed", "prophylaxis", "adherence", "safety",
                 "hba1c", "efficacy", "find me", "send", "brochure", "summary"]
SALES_HINTS = ["sales", "units", "revenue", "market performance", "how is",
               "trend", "volumes", "growth"]
PANEL_HINTS = ["my doctors", "my hcps", "which of my", "most worth", "who should i",
               "priorit", "this month", "this week", "my panel", "my territory",
               "top hcps", "worth contacting", "focus on"]
STOPWORDS = set("""what should i discuss with dr doctor next about tell me the a an and or
for of to on in is are can you please find get show which how why when where my me""".split())


def find_hcp_mention(question):
    """Look for an HCP name or id anywhere in the question, across ALL markets so
    that a cross-market attempt is caught and refused rather than missed."""
    m = re.search(r"\bHCP-[A-Z]{2}-\d{3}\b", question, re.I)
    if m:
        return m.group(0)
    all_hcps = tools.rows("SELECT hcp_id, name FROM hcps")
    low = question.lower()
    best = None
    for h in all_hcps:
        plain = h["name"].replace("Dr. ", "")
        if plain.lower() in low:
            return h["hcp_id"]
        surname = plain.split()[-1]
        if len(surname) > 3 and re.search(r"\b%s\b" % re.escape(surname.lower()), low):
            best = h["hcp_id"]
    return best


def clean_query(question):
    words = [w for w in re.findall(r"[a-zA-Z0-9\-]+", question.lower())
             if w not in STOPWORDS and len(w) > 2]
    return " ".join(words[:14]) or question


def run_plan(ctx, question, trace, meta, retrieved_ids, tool_results):
    """Execute the deterministic plan, stopping early if access is denied - one
    clear refusal reads better than the same refusal repeated per tool."""
    for name, args in plan_tools(ctx, question):
        out = tools.run_tool(name, ctx, trace, args)
        tool_results.append((name, out))
        if name == "search_approved_content":
            meta["retriever"] = out.get("retriever", meta["retriever"])
            retrieved_ids += [x["asset_id"] for x in out.get("results", [])]
        if isinstance(out, dict) and out.get("error") in (
                "access_denied", "hcp_level_refused"):
            break
    return tool_results


def plan_tools(ctx, question):
    """Deterministic plan: which tools would a sensible rep-facing assistant call?"""
    low = question.lower()
    plan = []
    hcp = find_hcp_mention(question)
    if hcp:
        plan.append(("get_hcp_profile", {"name_or_id": hcp}))
        plan.append(("get_engagement_history", {"hcp_id": hcp, "months": 12}))
        plan.append(("get_nbe_recommendation", {"hcp_id": hcp}))
    elif any(h in low for h in PANEL_HINTS):
        plan.append(("get_nbe_recommendation", {}))
    if any(h in low for h in CONTENT_HINTS):
        plan.append(("search_approved_content",
                     {"query": clean_query(question), "market": ctx.market, "k": 4}))
    if any(h in low for h in SALES_HINTS):
        product = None
        for p in ("glycora", "metabolix", "hemoclear"):
            if p in low:
                product = p.title()
        plan.append(("get_market_sales",
                     {"market": ctx.market, "product": product, "months": 6}))
    if not plan:
        plan.append(("search_approved_content",
                     {"query": clean_query(question), "market": ctx.market, "k": 4}))
    return plan


# ===========================================================================
# Fallback answer writer - deterministic, no LLM
# ===========================================================================
def render_fallback(ctx, question, results, reason="no LLM key configured"):
    """Build a readable answer straight from tool results. Clearly labelled."""
    out = ["**[FALLBACK MODE - %s. This answer is assembled deterministically from "
           "tool results, not written by a model.]**" % reason, ""]
    for name, res in results:
        if not isinstance(res, dict):
            continue
        if res.get("error"):
            out.append("**%s** - %s" % (name, res.get("message", res["error"])))
            out.append("")
            continue
        if name == "get_hcp_profile":
            out.append("**%s** - %s, Tier %s, %s (%s)" %
                       (res["name"], res["specialty"], res["prescribing_tier"],
                        res["city"], res["market"]))
            out.append("- Segment: %s | Preferred channel: %s" %
                       (res["segment"], res["preferred_channel"]))
            out.append("- Email consent: %s | Rep-visit consent: %s" %
                       ("YES" if res["consent_email"] else "NO - email not permitted",
                        "YES" if res["consent_rep_visit"] else "NO"))
            out.append("- Last contact: %s (%s days ago)" %
                       (res["last_contact_date"], res["days_since_last_contact"]))
        elif name == "get_engagement_history":
            e, w = res["email"], res["web"]
            out.append("**Engagement, last %d months**" % res["window_months"])
            out.append("- Email: %d sends, %d opens (%.0f%%), %d clicks" %
                       (e["sends"], e["opens"], 100 * e["open_rate"], e["clicks"]))
            out.append("- Web: %d sessions, %d minutes total dwell" %
                       (w["sessions"], w["total_dwell_minutes"]))
            out.append("- Rep visits: %d (last %s) | Events: %d of %d attended" %
                       (res["rep_visits"]["count"], res["rep_visits"]["last_visit_date"],
                        res["events"]["attended"], res["events"]["registered"]))
            out.append("- Contacts in last 90 days: %d" % res["contact_frequency_last_90d"])
            notes = res["rep_visits"]["recent_notes"]
            if notes:
                out.append("- Most recent call note (%s): \"%s\"" %
                           (notes[0]["date"], notes[0]["note"]))
        elif name == "get_nbe_recommendation" and res.get("mode") == "portfolio":
            out.append("**Priority panel for %s** (model %s, panel of %d)" %
                       (res["market"], res["model_version"], res["panel_size"]))
            for r in res["top_actions"][:6]:
                out.append("%d. **%s** - %s (Tier %s). Action: **%s**%s. "
                           "Expected value %.2f. Why: %s" %
                           (res["top_actions"].index(r) + 1, r["name"], r["specialty"],
                            r["prescribing_tier"], r["recommended_channel"],
                            " with %s (%s)" % (r["asset_id"], r["asset_title"])
                            if r["asset_id"] else "",
                            r["expected_value"], r["reason_codes"]))
            if res["suppressed_no_contact"]:
                out.append("")
                out.append("**Do not contact this cycle** (model recommends suppression): %s"
                           % ", ".join("%s (fatigue %.2f)" % (r["name"], r["fatigue_score"])
                                       for r in res["suppressed_no_contact"]))
            for n in res.get("notes", []):
                out.append("- %s" % n)
        elif name == "get_nbe_recommendation":
            top = res.get("recommended_action") or {}
            out.append("**Next Best Engagement for %s** (model %s, scored %s)" %
                       (res["name"], res["model_version"], res["scored_at"]))
            out.append("- Recommended action: **%s**%s (expected value %.2f)" %
                       (top.get("channel", "n/a"),
                        " - %s \"%s\"" % (top.get("asset_id"), top.get("asset_title"))
                        if top.get("asset_id") else "",
                        top.get("expected_value", 0)))
            out.append("- Why: %s" % top.get("reason_codes", ""))
            out.append("- Fatigue score %.2f | opt-out risk %.2f" %
                       (top.get("fatigue_score", 0), top.get("propensity_optout", 0)))
            for r in res.get("ranked_actions", [])[1:4]:
                out.append("- Alternative: %s (EV %.2f)%s" %
                           (r["channel"], r["expected_value"],
                            " - NOT ELIGIBLE: %s" % r["ineligibility_reason"]
                            if not r["eligible"] else ""))
            for n in res.get("notes", []):
                out.append("- %s" % n)
        elif name == "search_approved_content":
            if not res.get("results"):
                out.append("**No approved content matched** \"%s\" for %s. "
                           "%d candidate(s) were excluded by the MLR gate."
                           % (res.get("query"), res.get("market"),
                              res.get("blocked_count", 0)))
            else:
                out.append("**Approved content** (retriever: %s, %d excluded by the "
                           "MLR gate)" % (res.get("retriever"), res.get("blocked_count", 0)))
                for r in res["results"]:
                    out.append("- **%s** \"%s\" (%s, relevance %.2f) - %s"
                               % (r["asset_id"], r["title"], r["format"],
                                  r["score"], r["excerpt"]))
        elif name == "get_market_sales":
            out.append("**Aggregate sales - %s** (%s)" % (res["market"], res["granularity"]))
            seen = set()
            for r in res.get("monthly_totals", [])[:12]:
                key = (r["month"], r["product"])
                if key in seen:
                    continue
                seen.add(key)
                out.append("- %s %s: %s units" % (r["month"], r["product"],
                                                  "{:,}".format(r["units"])))
            out.append("- %s" % res["note"])
        elif name == "check_compliance":
            out.append("**Compliance check** - allowed: %s" % res["allowed"])
            for r in res["reasons"]:
                out.append("- %s" % r)
        out.append("")
    return "\n".join(out).strip()


# ===========================================================================
# Post-check - GR-04 citation required
# ===========================================================================
CLAIM_HINTS = ["study", "trial", "reduction", "efficacy", "endpoint", "dosing",
               "dose", "evidence", "data show", "reported", "percent", "%",
               "outcomes", "patients", "safety", "indication", "hba1c",
               "bleed", "weight", "renal", "cardiovascular", "tolerab"]


def citation_check(answer, trace, retrieved_ids):
    cited = set(re.findall(r"\bAST-\d{4}\b", answer or ""))
    low = (answer or "").lower()
    makes_claim = any(h in low for h in CLAIM_HINTS)
    if not retrieved_ids:
        trace.add("GUARDRAIL", "%s -> N/A" % tools.RULE_CITATION,
                  "No content was retrieved for this answer, so there is nothing to cite.",
                  {"rule": tools.RULE_CITATION, "result": "N/A"})
        return cited, True
    if cited:
        unknown = cited - set(retrieved_ids)
        if unknown:
            trace.add("GUARDRAIL", "%s -> FAIL" % tools.RULE_CITATION,
                      "Answer cites asset id(s) that were never retrieved: %s. This is a "
                      "hallucinated citation and would be blocked before a rep saw it."
                      % ", ".join(sorted(unknown)),
                      {"rule": tools.RULE_CITATION, "result": "FAIL"})
            trace.blocked.append(tools.RULE_CITATION)
            return cited, False
        trace.add("GUARDRAIL", "%s -> PASS" % tools.RULE_CITATION,
                  "%d asset id(s) cited, all traceable to retrieved approved content: %s"
                  % (len(cited), ", ".join(sorted(cited))),
                  {"rule": tools.RULE_CITATION, "result": "PASS"})
        return cited, True
    if makes_claim:
        trace.add("GUARDRAIL", "%s -> FLAG" % tools.RULE_CITATION,
                  "The answer contains content-style statements but cites no asset_id. "
                  "In production this would be held for review rather than shown.",
                  {"rule": tools.RULE_CITATION, "result": "FLAG"})
        trace.blocked.append(tools.RULE_CITATION)
        return cited, False
    trace.add("GUARDRAIL", "%s -> PASS" % tools.RULE_CITATION,
              "No content claims made, so no citation required.",
              {"rule": tools.RULE_CITATION, "result": "PASS"})
    return cited, True


# ===========================================================================
# The agent
# ===========================================================================
def _summarise_for_model(result):
    """Trim tool output before it goes back into the prompt - HCP data minimised,
    long bodies dropped. Keeps the prompt small and the latency low."""
    if not isinstance(result, dict):
        return json.dumps(result)[:4000]
    r = json.loads(json.dumps(result))
    if "results" in r and isinstance(r["results"], list):
        for x in r["results"]:
            x.pop("safety_text", None)
            if "excerpt" in x:
                x["excerpt"] = x["excerpt"][:600]
    if "rows" in r and isinstance(r["rows"], list):
        r["rows"] = r["rows"][:24]
    if "ranked_actions" in r:
        r["ranked_actions"] = r["ranked_actions"][:5]
    # portfolio mode returns the whole panel; the model only needs the top slice
    if "top_actions" in r and isinstance(r["top_actions"], list):
        r["top_actions"] = r["top_actions"][:6]
        for x in r["top_actions"]:
            for k in ("propensity_engage", "city", "segment", "consent_email"):
                x.pop(k, None)
    if "suppressed_no_contact" in r and isinstance(r["suppressed_no_contact"], list):
        r["suppressed_no_contact"] = [
            {"name": x.get("name"), "fatigue_score": x.get("fatigue_score"),
             "reason_codes": x.get("reason_codes")}
            for x in r["suppressed_no_contact"][:5]]
    return json.dumps(r)[:6000]


def answer(ctx, question, trace=None):
    """Main entry point. Returns a dict; never raises."""
    trace = trace or Trace()
    t_start = time.time()
    meta = {"provider": "-", "model": "-", "iterations": 0, "tokens": 0,
            "llm_ms": 0, "fallback": False, "refused": False,
            "retriever": "-", "attempts": []}
    retrieved_ids = []

    try:
        stop = preflight(ctx, question, trace)
        if stop:
            meta["refused"] = True
            meta["total_ms"] = trace.elapsed_ms()
            trace.add("ANSWER", "refused by guardrail",
                      "%d ms total - no model call was made." % trace.elapsed_ms(),
                      {"result": "REFUSED"})
            return {"answer": stop["answer"], "trace": trace, "meta": meta,
                    "citations": [], "ae_hits": trace.ae_hits}

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT.format(
                full_name=ctx.full_name, role=ctx.role or "sales representative",
                market=ctx.market)},
            {"role": "user", "content": question},
        ]

        used_llm = False
        final_text = ""
        tool_results = []

        if llm.any_key_present():
            for it in range(MAX_ITERATIONS):
                meta["iterations"] = it + 1
                if time.time() - t_start > WALL_CLOCK_LIMIT_S:
                    trace.add("GUARDRAIL", "%s -> STOP" % tools.RULE_LOOP,
                              "Wall-clock limit of %.0fs reached. Returning what the "
                              "agent has." % WALL_CLOCK_LIMIT_S,
                              {"rule": tools.RULE_LOOP, "result": "STOP"})
                    trace.blocked.append(tools.RULE_LOOP)
                    break
                res = llm.chat(messages, tools=tools.TOOL_SCHEMAS)
                meta["attempts"] = res.attempts
                if not res.ok:
                    trace.add("ERROR", "all LLM providers failed",
                              "; ".join("%s: %s" % (a[0], a[2]) for a in res.attempts)
                              + " - switching to the deterministic fallback path.",
                              {"result": "FALLBACK"})
                    break
                used_llm = True
                meta["provider"], meta["model"] = res.provider, res.model
                meta["tokens"] += res.total_tokens
                meta["llm_ms"] += res.latency_ms
                trace.add("PLAN" if it == 0 else "RE-PLAN",
                          "model=%s via %s (%d ms)" % (res.model, res.provider,
                                                       res.latency_ms),
                          ("decided to call %d tool(s): %s" %
                           (len(res.tool_calls),
                            ", ".join(c["name"] for c in res.tool_calls)))
                          if res.tool_calls else "answered without further tool calls",
                          {"tokens": res.total_tokens})

                if not res.tool_calls:
                    final_text = res.text
                    break

                messages.append({"role": "assistant", "content": res.text or None,
                                 "tool_calls": [
                                     {"id": c["id"], "type": "function",
                                      "function": {"name": c["name"],
                                                   "arguments": json.dumps(c["arguments"])}}
                                     for c in res.tool_calls]})
                for c in res.tool_calls:
                    out = tools.run_tool(c["name"], ctx, trace, c["arguments"])
                    tool_results.append((c["name"], out))
                    if c["name"] == "search_approved_content":
                        meta["retriever"] = out.get("retriever", meta["retriever"])
                        retrieved_ids += [x["asset_id"] for x in out.get("results", [])]
                    messages.append({"role": "tool", "tool_call_id": c["id"],
                                     "name": c["name"],
                                     "content": _summarise_for_model(out)})
            else:
                trace.add("GUARDRAIL", "%s -> STOP" % tools.RULE_LOOP,
                          "Hard stop at %d iterations. Returning what the agent has."
                          % MAX_ITERATIONS, {"rule": tools.RULE_LOOP, "result": "STOP"})
                trace.blocked.append(tools.RULE_LOOP)

            # Planner safety net: the model answered but never looked anything up.
            if used_llm and not tool_results:
                trace.add("RE-PLAN", "planner fallback engaged",
                          "The model answered without calling a tool. Running the "
                          "deterministic plan and re-asking so the answer is grounded.",
                          {"result": "PLANNER"})
                run_plan(ctx, question, trace, meta, retrieved_ids, tool_results)
                messages.append({"role": "user", "content":
                    "Tool results follow. Answer the original question using ONLY these, "
                    "citing asset_ids.\n\n" + "\n\n".join(
                        "%s -> %s" % (n, _summarise_for_model(o)) for n, o in tool_results)})
                res = llm.chat(messages)
                if res.ok:
                    meta["tokens"] += res.total_tokens
                    meta["llm_ms"] += res.latency_ms
                    final_text = res.text
        else:
            trace.add("PLAN", "no LLM key configured -> rule-based planner",
                      "Provider chain: %s. Falling back to the deterministic planner so "
                      "the demo still answers." % (", ".join(
                          p.name for p in llm.PROVIDERS) + " -> rule-based"),
                      {"result": "FALLBACK"})

        # ---- deterministic path -----------------------------------------
        if not final_text:
            meta["fallback"] = True
            if not tool_results:
                run_plan(ctx, question, trace, meta, retrieved_ids, tool_results)
            final_text = render_fallback(
                ctx, question, tool_results,
                "every LLM provider failed or timed out" if llm.any_key_present()
                else "no LLM key configured")
            if not used_llm:
                meta["provider"], meta["model"] = "fallback", "rule-based"

        # ---- post-checks -------------------------------------------------
        cited, _ = citation_check(final_text, trace, retrieved_ids)
        if trace.ae_hits:
            trace.add("GUARDRAIL", "%s -> PHARMACOVIGILANCE BANNER" % tools.RULE_AE,
                      "%d suspected adverse event(s) surfaced from the source call notes. "
                      "A banner is shown to the user and the record is routed (simulated)."
                      % len(trace.ae_hits), {"rule": tools.RULE_AE, "result": "ROUTED"})

        meta["total_ms"] = trace.elapsed_ms()
        trace.add("ANSWER",
                  "%s | %d tokens | %d ms total" %
                  (("%s/%s" % (meta["provider"], meta["model"])) if not meta["fallback"]
                   or used_llm else "rule-based fallback",
                   meta["tokens"], meta["total_ms"]),
                  "iterations: %d/%d | tool calls: %d | assets cited: %s | "
                  "guardrails fired: %s" %
                  (meta["iterations"], MAX_ITERATIONS, len(tool_results),
                   ", ".join(sorted(cited)) or "none",
                   ", ".join(sorted(set(trace.blocked))) or "none"),
                  {"result": "OK"})

        citations = [trace.citations[a] for a in sorted(cited) if a in trace.citations]
        if not citations:
            citations = [trace.citations[a] for a in sorted(set(retrieved_ids))
                         if a in trace.citations]
        return {"answer": final_text, "trace": trace, "meta": meta,
                "citations": citations, "ae_hits": trace.ae_hits}

    except Exception as e:                      # nothing may reach the UI as a traceback
        trace.add("ERROR", "agent failed", str(e)[:300], {"result": "ERROR"})
        meta["total_ms"] = trace.elapsed_ms()
        return {"answer": "Something went wrong while answering that. The trace on the "
                          "right shows how far the agent got. Please try rephrasing.",
                "trace": trace, "meta": meta, "citations": [], "ae_hits": trace.ae_hits}


def render_trace_text(trace):
    """Plain-text rendering of the trace - used by the CLI and by the UI."""
    lines = []
    for s in trace.steps:
        head = "[%d] %-13s %s" % (s["n"], s["kind"], s["title"])
        lines.append(head)
        if s["detail"]:
            for d in str(s["detail"]).split("\n"):
                lines.append("    %s" % d.strip())
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    from tools import Ctx as C
    who = sys.argv[1] if len(sys.argv) > 1 else "alex"
    u = tools.one("SELECT * FROM users WHERE username=?", (who,))
    ctx = C(u["username"], u["market"], u["full_name"], u["role"])
    questions = sys.argv[2:] or [
        "What should I discuss with Dr. Sarah Miller next?",
        "Which of my doctors are most worth contacting this month?",
        "Find me approved material on renal dosing.",
        "What can you tell me about Dr. Anders Kjaer?",
        "How many prescriptions did Dr. Miller write last month?",
        "Can I promote Metabolix for adolescents?",
    ]
    print("User: %s (%s) | LLM: %s\n" % (ctx.full_name, ctx.market, llm.status_line()))
    for q in questions:
        print("=" * 92)
        print("Q: %s" % q)
        print("=" * 92)
        r = answer(ctx, q)
        print(render_trace_text(r["trace"]))
        print("-" * 92)
        print(r["answer"])
        if r["ae_hits"]:
            print("\n!! PHARMACOVIGILANCE: %d suspected adverse event(s)" % len(r["ae_hits"]))
        print()
