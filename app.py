#!/usr/bin/env python3
"""
app.py - Gradio UI for the HCP Engagement Assistant.

Left column: the chat. Right column: the live agent trace, which is the point
of the whole thing - you can see every access check, tool call, retrieval score
and guardrail decision that produced the answer.

Run:  python3 app.py       (then open http://<host>:7860)
"""

import base64
import html
import os
import traceback

import gradio as gr

import agent
import llm
import tools

ACCENT = "#0b4f5c"
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "logo.png")


def _logo_data_uri():
    """Inline the logo as a data URI so the header needs no static file route."""
    with open(LOGO_PATH, "rb") as fh:
        return "data:image/png;base64," + base64.b64encode(fh.read()).decode()


LOGO = _logo_data_uri()

BANNER = (
    "A demonstration built by <b>Reza Shirvan</b>. All data, products and clinical content "
    "are entirely fictional. No real physician, no real medicine and no real "
    "clinical evidence appears anywhere in this system."
)

CSS = """
.gradio-container {max-width: 1680px !important; background:%(page)s !important;}

/* ---------- header ---------- */
#hdr {background:linear-gradient(135deg,%(accent)s 0%%,#12707f 100%%); color:#fff;
      padding:15px 22px; border-radius:12px; margin-bottom:8px;
      display:flex; align-items:center; gap:18px;
      box-shadow:0 2px 10px rgba(11,79,92,.18);}
#hdr .logo {background:#fff; border-radius:9px; padding:4px 10px; flex:0 0 auto;
            line-height:0; box-shadow:0 1px 4px rgba(0,0,0,.14);}
#hdr .logo img {height:60px; width:auto; display:block;}
#hdr .txt {flex:1 1 auto;}
#hdr h1 {margin:0; font-size:20px; font-weight:600; letter-spacing:.2px;
         color:#fff !important;}
#hdr .sub {font-size:12px; opacity:.86; margin-top:5px; line-height:1.55; max-width:105ch;
           color:#fff !important;}
#hdr .sub b {color:#fff !important;}

/* ---------- status strip ---------- */
#strip {background:#fff; border:1px solid %(line)s; border-left:4px solid %(accent)s;
        padding:9px 14px; border-radius:9px; font-size:12.5px;
        font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
        color:#2a3b60; margin-bottom:10px; box-shadow:0 1px 3px rgba(16,32,70,.05);}
#strip b {color:%(accent)s;}

/* ---------- adverse-event banner ---------- */
#aebanner {background:#fff5f5; border:1px solid #e9b4b4; border-left:5px solid #c8393f;
           color:#8a1f24; padding:12px 15px; border-radius:9px; font-size:13px;
           margin-bottom:10px; box-shadow:0 1px 4px rgba(200,57,63,.10);}
#aebanner b {color:#7d1216; letter-spacing:.3px;}
#aebanner i {color:#94454a;}

/* ---------- trace panel (light) ---------- */
#trace {font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
        font-size:11.6px; line-height:1.55; background:%(panel)s;
        border:1px solid %(line)s; color:#26364f; padding:12px;
        border-radius:11px; height:640px; overflow-y:auto;}
#trace .st {margin-bottom:8px; padding:7px 10px; background:#fff;
            border:1px solid %(line)s; border-left:3px solid #9fb0cd;
            border-radius:7px; box-shadow:0 1px 2px rgba(16,32,70,.04);}
#trace .hd {font-weight:600; letter-spacing:.1px;}
#trace .dt {color:#5b6b88; white-space:pre-wrap; margin-top:3px;}
#trace .k-access .hd{color:#1f4fb0} #trace .k-access{border-left-color:#2f66cc}
#trace .k-plan   .hd{color:#6338a8} #trace .k-plan  {border-left-color:#7b4fc4}
#trace .k-tool   .hd{color:#0a6f68} #trace .k-tool  {border-left-color:#12968c}
#trace .k-pass   .hd{color:#256c30} #trace .k-pass  {border-left-color:#3a9448}
#trace .k-warn   .hd{color:#8a5a08} #trace .k-warn  {border-left-color:#d3910f;
                                                     background:#fffdf5}
#trace .k-block  .hd{color:#a82a30} #trace .k-block {border-left-color:#c8393f;
                                                     background:#fff7f7}
#trace .k-answer .hd{color:#256c30} #trace .k-answer{border-left-color:#3a9448;
                                                     background:#f6fbf6}
#trace .k-error  .hd{color:#a82a30} #trace .k-error {border-left-color:#c8393f;
                                                     background:#fff7f7}
#trace .idle {color:#7787a3; padding:6px 2px;}
#trace::-webkit-scrollbar {width:9px}
#trace::-webkit-scrollbar-thumb {background:#c6d1e4; border-radius:5px}

/* ---------- panel titles ---------- */
.paneltitle {font-size:15px; font-weight:600; color:%(accent)s; margin:0 0 2px 2px;}
.panelsub {font-size:11.5px; color:#65748f; margin:0 0 8px 2px; line-height:1.45;}

/* ---------- example buttons ---------- */
.exq button {font-size:11.7px !important; text-align:left !important; min-height:54px;
             line-height:1.4 !important; white-space:normal !important;
             background:#fff !important; border:1px solid %(line)s !important;
             color:#33445f !important; border-radius:9px !important;
             box-shadow:0 1px 2px rgba(16,32,70,.05) !important;}
.exq button:hover {border-color:%(accent)s !important; background:#f4f7fd !important;
                   color:%(accent)s !important;}

footer {display:none !important;}
""" % {"accent": ACCENT, "page": "#f4f6fb", "panel": "#eef2f9", "line": "#dde4f0"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def auth_fn(username, password):
    try:
        return tools.get_user(username, password) is not None
    except Exception:
        return False


def ctx_for(request):
    """Build the access-scoped context from the logged-in Gradio user."""
    username = getattr(request, "username", None) if request else None
    u = tools.one("SELECT * FROM users WHERE lower(username)=lower(?)",
                  (username or "alex",))
    if not u:
        u = tools.one("SELECT * FROM users LIMIT 1")
    return tools.Ctx(u["username"], u["market"], u["full_name"], u["role"])


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _kind_class(step):
    k, res = step["kind"], (step.get("meta") or {}).get("result", "")
    if k == "ACCESS CHECK":
        return "k-access"
    if k in ("PLAN", "RE-PLAN"):
        return "k-plan"
    if k == "TOOL CALL":
        return "k-tool"
    if k == "ERROR":
        return "k-error"
    if k == "ANSWER":
        return "k-answer"
    if res in ("BLOCK", "REFUSE", "FAIL", "STOP", "EXCLUDE"):
        return "k-block"
    if res in ("WARN", "FLAG", "FILTER", "ROUTED"):
        return "k-warn"
    return "k-pass"


def render_trace(trace):
    if not trace or not trace.steps:
        return ("<div id='trace'><div class='idle'>The agent trace appears here.<br><br>"
                "Every access check, tool call, retrieval score and guardrail decision "
                "behind an answer is listed in the order it happened.</div></div>")
    out = ["<div id='trace'>"]
    for s in trace.steps:
        head = "[%d] %-13s %s" % (s["n"], s["kind"], s["title"])
        out.append("<div class='st %s'><div class='hd'>%s</div>"
                   % (_kind_class(s), html.escape(head)))
        if s["detail"]:
            out.append("<div class='dt'>%s</div>" % html.escape(str(s["detail"])))
        out.append("</div>")
    out.append("</div>")
    return "".join(out)


def render_ae(ae_hits):
    if not ae_hits:
        return gr.update(value="", visible=False)
    items = "".join(
        "<li><b>%s</b> - matched “%s”<br><i>%s</i></li>"
        % (html.escape(h["hcp"]), html.escape(h["term"]), html.escape(h["text"]))
        for h in ae_hits[:4])
    msg = ("<div id='aebanner'><b>SUSPECTED ADVERSE EVENT DETECTED IN SOURCE DATA</b><br>"
           "Routed to Pharmacovigilance (simulated). A reporting obligation applies: "
           "processing this text creates a detection duty, so the pipeline must discharge "
           "it.<ul style='margin:7px 0 0 18px;padding:0'>%s</ul></div>" % items)
    return gr.update(value=msg, visible=True)


def render_citations(citations):
    if not citations:
        return gr.update(value="_No approved content was used for this answer._",
                         visible=False)
    parts = []
    for c in citations:
        score = "" if c.get("score") is None else " · relevance %.2f" % c["score"]
        parts.append(
            "**%s - %s**  \n<sub>%s%s%s</sub>\n\n%s\n\n<sub>%s</sub>\n\n---"
            % (c["asset_id"], c["title"], c.get("format", ""),
               " · %s" % c["product"] if c.get("product") else "", score,
               c.get("excerpt", ""), c.get("safety_text", "")))
    return gr.update(value="\n\n".join(parts), visible=True)


def strip_html(ctx, meta=None):
    model = llm.status_line()
    if meta and meta.get("provider") not in ("-", None):
        model = "%s / %s" % (meta["provider"], meta["model"])
        if meta.get("fallback") and meta.get("provider") == "fallback":
            model = "rule-based fallback (no LLM key)"
    extra = ""
    if meta:
        extra = ("  ·  Latency: %d ms  ·  Tokens: %d  ·  Iterations: %d/%d"
                 % (meta.get("total_ms", 0), meta.get("tokens", 0),
                    meta.get("iterations", 0), agent.MAX_ITERATIONS))
        if meta.get("retriever", "-") not in ("-", None):
            extra += "  ·  Retriever: %s" % meta["retriever"]
    return ("<div id='strip'>User: <b>%s</b> (%s)  ·  Market: <b>%s</b>  ·  "
            "Access scope: <b>%s HCPs and %s-approved content only</b>  ·  "
            "Model: <b>%s</b>%s</div>"
            % (html.escape(ctx.full_name), html.escape(ctx.username),
               ctx.market, ctx.market, ctx.market, html.escape(model), extra))


# ---------------------------------------------------------------------------
# Market-aware example questions
# ---------------------------------------------------------------------------
AE_HCP = {"USA": "Robert Castellano", "Turkey": "Mehmet Kaya", "Denmark": "Soren Holm"}
FOREIGN = {"USA": ("Anders Kjaer", "Denmark"), "Turkey": ("Sarah Miller", "USA"),
           "Denmark": ("Sarah Miller", "USA")}


def examples_for(market):
    try:
        top = tools.rows(
            "SELECT h.name FROM model_scores ms JOIN hcps h USING(hcp_id) "
            "WHERE h.market=? AND ms.channel!='no contact' "
            "ORDER BY ms.expected_value DESC LIMIT 1", (market,))
        brief_hcp = top[0]["name"].replace("Dr. ", "") if top else "Sarah Miller"
        # An HCP whose TOP-ranked action is actually suppression - not merely one
        # with a high 'no contact' score.
        nc = tools.rows(
            "SELECT h.name FROM hcps h JOIN model_scores ms USING(hcp_id) "
            "WHERE h.market=? AND ms.expected_value = ("
            "  SELECT MAX(m2.expected_value) FROM model_scores m2 "
            "  WHERE m2.hcp_id = h.hcp_id) "
            "AND ms.channel='no contact' ORDER BY ms.fatigue_score DESC LIMIT 1",
            (market,))
        nc_hcp = nc[0]["name"].replace("Dr. ", "") if nc else brief_hcp
    except Exception:
        brief_hcp, nc_hcp = "Sarah Miller", "Sarah Miller"
    foreign, fmkt = FOREIGN.get(market, ("Anders Kjaer", "Denmark"))
    return [
        "Give me a pre-call brief for Dr. %s." % brief_hcp,
        "Which of my doctors are most worth contacting this month?",
        "Should I contact Dr. %s this cycle?" % nc_hcp,
        "Find me approved material on renal dosing in type 2 diabetes.",
        "What should I discuss with Dr. %s next?" % AE_HCP.get(market, brief_hcp),
        "What can you tell me about Dr. %s?" % foreign,
        "How many prescriptions did Dr. %s write last month?" % brief_hcp,
        "Can I promote Metabolix for adolescents?",
    ]


EXAMPLE_HINTS = [
    "normal pre-call briefing",
    "portfolio ranking · holdout + consent guardrails",
    "the model recommends NO CONTACT",
    "RAG retrieval · MLR gate blocks expired content",
    "triggers the pharmacovigilance banner",
    "triggers the cross-market access refusal",
    "refused: sales are aggregate-only by design",
    "refused: off-label indication",
]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def on_load(request: gr.Request):
    ctx = ctx_for(request)
    ex = examples_for(ctx.market)
    return ([strip_html(ctx), render_trace(None),
             gr.update(value="", visible=False),
             gr.update(value="", visible=False)]
            + [gr.update(value="%s\n(%s)" % (q, h)) for q, h in zip(ex, EXAMPLE_HINTS)])


def respond(message, history, request: gr.Request):
    history = list(history or [])
    message = (message or "").strip()
    if not message:
        return history, "", gr.update(), gr.update(), gr.update(), gr.update()
    ctx = ctx_for(request)
    history.append({"role": "user", "content": message})
    try:
        r = agent.answer(ctx, message)
        history.append({"role": "assistant", "content": r["answer"]})
        return (history, "", render_trace(r["trace"]), render_ae(r["ae_hits"]),
                render_citations(r["citations"]), strip_html(ctx, r["meta"]))
    except Exception:
        traceback.print_exc()
        history.append({"role": "assistant",
                        "content": "Something went wrong handling that question. "
                                   "Nothing was lost - please try again or rephrase."})
        return (history, "", render_trace(None), gr.update(value="", visible=False),
                gr.update(visible=False), strip_html(ctx))


def use_example(btn_label):
    return (btn_label or "").split("\n")[0]


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
def build():
    theme = gr.themes.Base(primary_hue=gr.themes.colors.blue,
                           font=[gr.themes.GoogleFont("Inter"), "system-ui", "sans-serif"])
    with gr.Blocks(title="HCP Engagement Assistant",
                   theme=theme, css=CSS, fill_height=True) as demo:
        gr.HTML("<div id='hdr'><div class='logo'><img src='%s' alt='Pharma logo'></div>"
                "<div class='txt'><h1>HCP Engagement Assistant</h1>"
                "<div class='sub'>%s</div></div></div>" % (LOGO, BANNER))
        strip = gr.HTML()
        ae = gr.HTML(visible=False)

        with gr.Row():
            with gr.Column(scale=55):
                chat = gr.Chatbot(type="messages", height=430, show_label=False,
                                  avatar_images=(None, None),
                                  placeholder="Ask about the HCPs you cover.")
                with gr.Row():
                    box = gr.Textbox(placeholder="e.g. What should I discuss with "
                                                 "Dr. Sarah Miller next?",
                                     show_label=False, scale=8, lines=1,
                                     autofocus=True)
                    send = gr.Button("Ask", variant="primary", scale=1, min_width=80)
                    clear = gr.Button("Clear", scale=1, min_width=80)
                gr.HTML("<div class='paneltitle' style='margin-top:6px'>Try one of "
                        "these</div><div class='panelsub'>Each one demonstrates a "
                        "different guardrail.</div>")
                ex_btns = []
                for _ in range(2):
                    with gr.Row(elem_classes="exq"):
                        for _ in range(4):
                            ex_btns.append(gr.Button("", size="sm"))
                with gr.Accordion("Citations - approved assets used in this answer",
                                  open=False):
                    cites = gr.Markdown("_Ask something to see citations._")

            with gr.Column(scale=45):
                gr.HTML("<div class='paneltitle'>Live agent trace</div>"
                        "<div class='panelsub'>Every step below actually executed. The "
                        "agent loop is plain Python, not a framework, so nothing here "
                        "is reconstructed after the fact.</div>")
                trace = gr.HTML(render_trace(None))

        outs = [chat, box, trace, ae, cites, strip]
        send.click(respond, [box, chat], outs)
        box.submit(respond, [box, chat], outs)
        clear.click(lambda: ([], "", render_trace(None),
                             gr.update(value="", visible=False),
                             gr.update(value="_Ask something to see citations._"),
                             gr.update()),
                    None, outs)
        for b in ex_btns:
            b.click(use_example, b, box).then(respond, [box, chat], outs)

        demo.load(on_load, None, [strip, trace, ae, cites] + ex_btns)
    return demo


if __name__ == "__main__":
    if not os.path.exists(tools.DB_PATH):
        print("data/demo.db not found - run:  python3 build_data.py")
        raise SystemExit(1)
    print("LLM layer: %s" % llm.status_line())
    print("Logins: alex/alex (USA), elif/elif (Turkey), jonas/jonas (Denmark), "
          "freja/freja (Denmark)")
    build().launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("PORT", "7860")),
        auth=auth_fn,
        auth_message="HCP Engagement Assistant - demo. Fictional data only. "
                     "Logins are listed in the README.",
        share=os.environ.get("GRADIO_SHARE", "0") == "1",
        show_api=False,
        favicon_path=None,
    )
