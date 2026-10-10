#!/usr/bin/env python3
"""
build_data.py - Build the synthetic SQLite demo database for the
HCP Engagement Assistant.

Everything in here is FICTIONAL. Fictional doctors, fictional products,
fictional clinical content. Deterministic: random.seed(42).

Idempotent: drops and rebuilds data/demo.db every run.
"""

import os
import random
import sqlite3
from datetime import date, timedelta

random.seed(42)

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "demo.db")

TODAY = date(2026, 8, 26)          # fixed "today" so the demo is reproducible
HISTORY_START = TODAY - timedelta(days=365)

MARKETS = ["USA", "Turkey", "Denmark"]

PRODUCTS = {
    "Glycora": "Type 2 Diabetes",
    "Metabolix": "Obesity / Weight Management",
    "Hemoclear": "Rare Blood Disorder",
}

# --------------------------------------------------------------------------
# 1. Users
# --------------------------------------------------------------------------
USERS = [
    ("alex", "alex", "USA", "Alex Morgan", "Field Sales Representative"),
    ("elif", "elif", "Turkey", "Elif Polat", "Field Sales Representative"),
    ("jonas", "jonas", "Denmark", "Jonas Lund", "Field Sales Representative"),
    ("freja", "freja", "Denmark", "Freja Holt", "Field Sales Representative"),
]

# --------------------------------------------------------------------------
# 2. HCPs - all names entirely invented
# --------------------------------------------------------------------------
HCP_NAMES = {
    "USA": [
        "Sarah Miller", "James Whitfield", "Angela Ruiz", "Robert Castellano",
        "Priya Raghavan", "Michael Doherty", "Linda Ashworth", "Carlos Benitez",
        "Nicole Draper", "Ethan Vandermeer", "Grace Okonkwo", "Daniel Fairbanks",
        "Rachel Sandoval", "Peter Lindqvist", "Monica Delgado",
    ],
    "Turkey": [
        "Elif Demir", "Mehmet Kaya", "Zeynep Arslan", "Burak Ozturk",
        "Ayse Yilmaz", "Emre Sahin", "Selin Aydin", "Kerem Dogan",
        "Nazli Cetin", "Onur Tekin", "Pelin Korkmaz", "Serkan Gunes",
    ],
    "Denmark": [
        "Anders Kjaer", "Mette Larsen", "Soren Holm", "Line Bruun",
        "Kasper Norgaard", "Sofie Dalgaard", "Jonas Vestergaard", "Camilla Blicher",
        "Rasmus Thygesen", "Freja Lindholm", "Henrik Skovgaard", "Ida Munkholm",
        "Nikolaj Ravn",
    ],
}

CITIES = {
    "USA": ["Boston", "Chicago", "Austin", "Denver", "Seattle", "Atlanta", "Phoenix"],
    "Turkey": ["Istanbul", "Ankara", "Izmir", "Bursa", "Antalya", "Adana"],
    "Denmark": ["Copenhagen", "Aarhus", "Odense", "Aalborg", "Esbjerg", "Roskilde"],
}

SPECIALTIES = ["Endocrinology", "Diabetology", "General Practice", "Cardiology", "Haematology"]
SEGMENTS = ["Clinical Adopter", "Evidence Seeker", "Pragmatic Prescriber",
            "Low Engagement", "Digital First", "Key Opinion Leader"]
CHANNELS = ["email", "rep visit", "webinar invite", "portal content", "no contact"]

# HCPs forced to have no e-mail consent (guardrail #2 must visibly fire)
NO_EMAIL_CONSENT = {"HCP-US-003", "HCP-US-011", "HCP-TR-005", "HCP-DK-002", "HCP-DK-009"}
# HCPs forced into the "fatigued / disengaged" profile so 'no contact' wins
FATIGUED = {"HCP-US-006", "HCP-US-013", "HCP-TR-004", "HCP-TR-010", "HCP-DK-005", "HCP-DK-012"}
# HCPs in the measurement holdout
HOLDOUT = {"HCP-US-002", "HCP-US-009", "HCP-TR-007", "HCP-DK-004", "HCP-DK-011", "HCP-TR-002"}

MARKET_CODE = {"USA": "US", "Turkey": "TR", "Denmark": "DK"}


def build_hcps():
    rows = []
    for market in MARKETS:
        code = MARKET_CODE[market]
        for i, name in enumerate(HCP_NAMES[market], start=1):
            hcp_id = "HCP-%s-%03d" % (code, i)
            specialty = random.choice(SPECIALTIES)
            city = random.choice(CITIES[market])
            tier = random.choices(["A", "B", "C"], weights=[0.25, 0.45, 0.30])[0]
            consent_email = 0 if hcp_id in NO_EMAIL_CONSENT else random.choices([1, 0], [0.96, 0.04])[0]
            consent_rep = random.choices([1, 0], [0.95, 0.05])[0]
            if hcp_id in FATIGUED:
                last_gap = random.randint(3, 12)
            else:
                last_gap = random.randint(5, 120)
            last_contact = TODAY - timedelta(days=last_gap)
            preferred = random.choice(["email", "rep visit", "webinar invite", "portal content"])
            if not consent_email and preferred == "email":
                preferred = "rep visit"
            segment = random.choice(SEGMENTS)
            rows.append((hcp_id, "Dr. " + name, specialty, market, city, tier,
                         consent_email, consent_rep, preferred,
                         last_contact.isoformat(), segment))
    return rows


# --------------------------------------------------------------------------
# 3. Rep visits + call notes
# --------------------------------------------------------------------------
NOTE_TEMPLATES = [
    "Discussed {topic}. {name} asked for the latest data and wants a follow-up summary by email.",
    "Short corridor conversation about {topic}. Interested but time-poor; asked me to send material instead.",
    "Reviewed {topic} with the wider practice team. Two colleagues joined; good engagement.",
    "{name} pushed back on formulary positioning and asked how {product} compares on cost of therapy.",
    "Follow-up on the {topic} discussion from last quarter. Now trialling in a small number of patients.",
    "Covered {topic}. Main objection was administration burden for elderly patients.",
    "Requested the {topic} clinical summary and the webinar recording. Sent afterwards.",
    "No new clinical discussion; mainly logistics around the upcoming regional meeting.",
    "Detailed {product} positioning for {topic}. Asked specifically about renal patients.",
    "{name} is running a local audit and wants comparative real-world data on {topic}.",
    "Discussed {topic}. Wants to see head-to-head evidence before changing practice.",
    "Brief visit. Left the {topic} leave-behind and agreed to reconnect after the congress.",
]

# Exactly three adverse-event-flavoured notes, spread across the three markets.
AE_NOTES = [
    ("HCP-TR-002",
     "Dr. Kaya mentioned that a patient developed persistent nausea and mild dizziness after "
     "starting Glycora. He reduced the dose and the symptoms improved but he wanted it noted."),
    ("HCP-US-004",
     "Dr. Castellano reported that one of his patients experienced an injection site reaction "
     "and reported feeling unwell for two days after the second Metabolix dose."),
    ("HCP-DK-003",
     "Dr. Holm raised a safety question: a patient on Hemoclear had an unexpected bleeding "
     "episode and was hospitalised for observation last month."),
]

VISIT_TOPICS = [
    "renal dosing", "cardiovascular outcomes", "titration schedule", "HbA1c targets",
    "weight management pathway", "gastrointestinal tolerability", "prophylaxis dosing",
    "annualised bleed rate", "patient adherence", "hypoglycaemia risk",
    "elderly patients", "real-world evidence", "injection technique",
    "formulary positioning", "comorbidity management",
]

REP_BY_MARKET = {
    "USA": ["Alex Morgan", "Alicia Frost"],
    "Turkey": ["Elif Polat", "Baris Aksoy"],
    "Denmark": ["Jonas Lund", "Freja Holt"],
}


def build_visits(hcps):
    rows = []
    vid = 1
    ae_by_hcp = {h: t for h, t in AE_NOTES}
    for h in hcps:
        hcp_id, name, specialty, market = h[0], h[1], h[2], h[3]
        surname = name.replace("Dr. ", "").split()[-1]
        n_visits = random.randint(2, 7)
        if hcp_id in FATIGUED:
            n_visits = random.randint(6, 10)
        used_ae = False
        for _ in range(n_visits):
            days_ago = random.randint(5, 350)
            vdate = TODAY - timedelta(days=days_ago)
            topic = random.choice(VISIT_TOPICS)
            product = random.choice(list(PRODUCTS.keys()))
            if hcp_id in ae_by_hcp and not used_ae and days_ago < 120:
                note = ae_by_hcp[hcp_id]
                used_ae = True
                topic = "safety follow-up"
                outcome = "Escalated"
            else:
                note = random.choice(NOTE_TEMPLATES).format(
                    topic=topic, name="Dr. " + surname, product=product)
                outcome = random.choices(
                    ["Positive", "Neutral", "Follow-up required", "No interest"],
                    weights=[0.35, 0.30, 0.25, 0.10])[0]
            rows.append(("VIS-%05d" % vid, hcp_id, vdate.isoformat(),
                         random.choice(REP_BY_MARKET[market]),
                         random.randint(8, 45), topic, note, outcome))
            vid += 1
        # guarantee the seeded AE note exists even if the date draw missed it
        if hcp_id in ae_by_hcp and not used_ae:
            vdate = TODAY - timedelta(days=random.randint(20, 90))
            rows.append(("VIS-%05d" % vid, hcp_id, vdate.isoformat(),
                         random.choice(REP_BY_MARKET[market]), 25,
                         "safety follow-up", ae_by_hcp[hcp_id], "Escalated"))
            vid += 1
    return rows


# --------------------------------------------------------------------------
# 4. Content assets - the RAG corpus
# --------------------------------------------------------------------------
TOPICS = [
    # (topic, therapy_area, product, [bespoke sentences])
    ("Renal dosing in type 2 diabetes", "Type 2 Diabetes", "Glycora", [
        "Renal dosing of {product} does not require adjustment in patients with mild to moderate renal impairment (eGFR 30-89 mL/min/1.73m2).",
        "In the fictional RENALIS-2 programme, renal function remained stable over 52 weeks in patients receiving {product} at the standard maintenance dose.",
        "Patients with severe renal impairment were excluded from the pivotal renal analyses, and no dosing recommendation is made for that group.",
        "Renal monitoring at initiation and annually thereafter is described in the approved prescribing information.",
    ]),
    ("Cardiovascular outcomes", "Type 2 Diabetes", "Glycora", [
        "The fictional CARDIA-T2 outcomes study reported a lower rate of the composite cardiovascular endpoint in the {product} arm over a median 3.1 years.",
        "Cardiovascular benefit was consistent across prespecified subgroups including baseline HbA1c and prior cardiovascular disease.",
        "The cardiovascular analysis was event-driven and the composite endpoint combined cardiovascular death, non-fatal myocardial infarction and non-fatal stroke.",
        "No cardiovascular claim should be extended beyond the studied population.",
    ]),
    ("Titration and dose escalation", "Type 2 Diabetes", "Glycora", [
        "{product} titration begins at the starting dose for four weeks before escalation, which limits early gastrointestinal effects.",
        "A slower titration schedule may be considered for patients who report nausea during dose escalation.",
        "Titration decisions should be individualised and documented at each review.",
        "The maintenance dose is reached in most patients by week eight of the titration schedule.",
    ]),
    ("Hypoglycaemia risk management", "Type 2 Diabetes", "Glycora", [
        "Hypoglycaemia rates with {product} monotherapy were low in the fictional pivotal programme, but risk increases when combined with sulfonylureas or insulin.",
        "Dose reduction of the concomitant agent should be considered to lower hypoglycaemia risk.",
        "Patient education on recognising and treating hypoglycaemia is part of the approved support pathway.",
        "Nocturnal hypoglycaemia was reported infrequently and is described in the safety section.",
    ]),
    ("HbA1c reduction evidence", "Type 2 Diabetes", "Glycora", [
        "Mean HbA1c reduction from baseline with {product} was 1.4 percentage points at week 52 in the fictional GLYCA-1 trial.",
        "A greater proportion of patients reached the HbA1c target of below 7.0 percent compared with the active comparator arm.",
        "HbA1c response was durable through the extension period with no evidence of secondary failure.",
        "These HbA1c data come from a randomised, double-blind fictional study population.",
    ]),
    ("Elderly and frail patients", "Type 2 Diabetes", "Glycora", [
        "No dose adjustment of {product} is required on the basis of age alone in the approved population.",
        "Elderly patients were represented in the fictional pivotal programme and efficacy was consistent with the overall population.",
        "Frailty, polypharmacy and falls risk should be considered when setting glycaemic targets in elderly patients.",
        "Administration burden in elderly patients is addressed in the patient support material.",
    ]),
    ("Real-world evidence", "Type 2 Diabetes", "Glycora", [
        "A fictional retrospective cohort of 18,400 patients treated with {product} in routine care reported outcomes consistent with the trial programme.",
        "Real-world persistence at 12 months was 71 percent in the cohort described.",
        "Real-world evidence is observational and residual confounding cannot be excluded.",
        "The real-world dataset was drawn from anonymised electronic health records in three fictional regions.",
    ]),
    ("Combination with metformin", "Type 2 Diabetes", "Glycora", [
        "{product} added to metformin produced an additional HbA1c reduction in the fictional combination study.",
        "The combination did not increase the rate of gastrointestinal discontinuation relative to metformin alone.",
        "Combination therapy is described in the approved indication and should follow local treatment guidelines.",
        "Metformin dose was held constant during the fictional combination study.",
    ]),
    ("Hepatic impairment", "Type 2 Diabetes", "Glycora", [
        "No dose adjustment of {product} is recommended in mild or moderate hepatic impairment.",
        "Patients with severe hepatic impairment were not studied and no recommendation can be made.",
        "Liver function testing is not routinely required during {product} therapy.",
        "Hepatic safety signals were not identified in the fictional pooled safety analysis.",
    ]),
    ("Injection technique and device", "Type 2 Diabetes", "Glycora", [
        "The {product} pen device delivers a fixed dose and requires no reconstitution.",
        "Injection technique training reduces missed doses in the first month of therapy.",
        "Rotation of injection sites is recommended to reduce injection site reactions.",
        "A device demonstration is available as an approved leave-behind item.",
    ]),
    ("Patient adherence and support", "Type 2 Diabetes", "Glycora", [
        "Structured adherence support improved persistence with {product} in a fictional service-evaluation programme.",
        "Reminder-based support was most effective in the first 90 days of therapy.",
        "Adherence material is patient-facing and must be supplied unaltered.",
        "Adherence and persistence are reported separately in the evaluation.",
    ]),
    ("Weight management pathway", "Obesity / Weight Management", "Metabolix", [
        "{product} is positioned within a structured weight management pathway alongside diet and physical activity.",
        "The fictional WEIGHT-FORWARD study reported a mean body weight reduction of 12.8 percent at week 68.",
        "Patients who did not achieve a 5 percent reduction by week 16 were reassessed in the study protocol.",
        "The weight management pathway includes review of comorbidities at each stage.",
    ]),
    ("Cardiometabolic risk reduction", "Obesity / Weight Management", "Metabolix", [
        "Improvements in waist circumference, blood pressure and lipid parameters accompanied weight reduction with {product} in the fictional programme.",
        "Cardiometabolic improvements were observed alongside, and not independently of, weight reduction.",
        "No cardiovascular outcome claim is approved for {product}.",
        "Cardiometabolic parameters were prespecified secondary endpoints.",
    ]),
    ("Gastrointestinal tolerability", "Obesity / Weight Management", "Metabolix", [
        "Gastrointestinal effects with {product} were most common during dose escalation and were usually mild to moderate.",
        "Nausea was the most frequently reported gastrointestinal event and typically resolved without discontinuation.",
        "Slower escalation and dietary advice are described as tolerability measures.",
        "Discontinuation because of gastrointestinal events occurred in a minority of patients in the fictional study.",
    ]),
    ("Long-term weight maintenance", "Obesity / Weight Management", "Metabolix", [
        "Weight regain after treatment discontinuation was observed in the fictional extension study of {product}.",
        "Continued therapy was associated with maintenance of the weight reduction achieved at week 68.",
        "Long-term maintenance requires an ongoing behavioural component.",
        "The maintenance data are limited to the studied fictional population and duration.",
    ]),
    ("Comorbidity management in obesity", "Obesity / Weight Management", "Metabolix", [
        "Obesity-related comorbidities including sleep apnoea and osteoarthritis were tracked as exploratory endpoints in the fictional programme.",
        "Comorbidity improvement was reported descriptively and is not an approved claim for {product}.",
        "Comorbidity assessment is recommended before initiating weight management therapy.",
        "Patients with multiple comorbidities may require coordinated specialist input.",
    ]),
    ("Nutritional and behavioural counselling", "Obesity / Weight Management", "Metabolix", [
        "All patients in the fictional {product} programme received structured nutritional and behavioural counselling.",
        "The counselling component is part of the approved treatment pathway and is not optional.",
        "Counselling frequency in the study protocol was monthly for the first six months.",
        "Behavioural support material is available as an approved patient leave-behind.",
    ]),
    ("Adolescent population data", "Obesity / Weight Management", "Metabolix", [
        "Data in the adolescent population are limited and the fictional adolescent study is ongoing.",
        "{product} is not approved for use in patients under 18 years of age.",
        "Any discussion of adolescent use would be outside the approved indication.",
        "The adolescent programme design is described for medical information purposes only.",
    ]),
    ("Prophylaxis dosing", "Rare Blood Disorder", "Hemoclear", [
        "{product} prophylaxis is administered on a fixed weekly schedule in the approved indication.",
        "Prophylaxis dosing is weight-based and reassessed at each clinical review.",
        "Missed prophylaxis doses should be administered as soon as recognised, following the approved guidance.",
        "The prophylaxis regimen was compared with on-demand treatment in the fictional pivotal study.",
    ]),
    ("Annualised bleed rate", "Rare Blood Disorder", "Hemoclear", [
        "The fictional HEMO-1 study reported a median annualised bleed rate of 1.2 with {product} prophylaxis.",
        "A substantial proportion of patients experienced zero treated bleeds over the 12-month period.",
        "Annualised bleed rate was the primary endpoint and was analysed using a negative binomial model.",
        "Bleed rates in the on-demand comparator arm were considerably higher.",
    ]),
    ("Inhibitor development and management", "Rare Blood Disorder", "Hemoclear", [
        "Inhibitor development was monitored throughout the fictional {product} programme using a standardised assay.",
        "Patients with a history of inhibitors were excluded from the pivotal study.",
        "Inhibitor testing is recommended at defined intervals in the approved monitoring schedule.",
        "Management of confirmed inhibitors requires specialist haematology input.",
    ]),
    ("Paediatric considerations", "Rare Blood Disorder", "Hemoclear", [
        "The fictional paediatric cohort of the {product} programme included patients aged 2 to 11 years.",
        "Paediatric pharmacokinetics supported the weight-based dosing used in the adult population.",
        "Venous access in younger paediatric patients is discussed in the treatment guidance.",
        "Paediatric safety findings were consistent with the adult population.",
    ]),
    ("Home treatment and self-administration", "Rare Blood Disorder", "Hemoclear", [
        "Home treatment with {product} is supported once the patient or caregiver has completed the training programme.",
        "Self-administration reduces time to treatment for breakthrough bleeds.",
        "A treatment diary is recommended to support home treatment records.",
        "Home treatment suitability is assessed by the treating haematology centre.",
    ]),
    ("Laboratory monitoring", "Rare Blood Disorder", "Hemoclear", [
        "Laboratory monitoring during {product} therapy follows the schedule in the approved prescribing information.",
        "Assay selection can affect measured activity levels and the recommended assay is specified.",
        "Trough level monitoring may inform individualised prophylaxis dosing.",
        "Laboratory results should be interpreted alongside the bleeding history.",
    ]),
    ("Perioperative management", "Rare Blood Disorder", "Hemoclear", [
        "Perioperative management with {product} requires planning with the treating haematology centre.",
        "Target factor levels for minor and major surgery are specified in the approved guidance.",
        "Postoperative monitoring continues until haemostasis is secure.",
        "Elective surgery should be scheduled to allow adequate perioperative planning.",
    ]),
    ("Health economics and budget impact", "Type 2 Diabetes", "Glycora", [
        "A fictional budget impact model estimated the cost of introducing {product} into a 100,000-patient population.",
        "The model used list prices and does not reflect confidential local agreements.",
        "Cost offsets in the model came from reduced complication-related hospital admissions.",
        "Health economic material is for payer audiences and is not for use with prescribers.",
    ]),
    ("Formulary and access positioning", "Obesity / Weight Management", "Metabolix", [
        "Formulary positioning of {product} varies by market and local reimbursement criteria apply.",
        "Access material describes the approved indication and reimbursement pathway only.",
        "Prescribers should confirm local eligibility criteria before initiating therapy.",
        "The access summary is reviewed whenever local reimbursement terms change.",
    ]),
]

GENERIC = [
    "This material is intended for healthcare professionals only and summarises data from the approved prescribing information.",
    "The study population, endpoints and statistical approach are described in full in the source publication.",
    "Results from the fictional clinical programme should be interpreted in the context of the studied population.",
    "Efficacy findings were consistent across the prespecified subgroups reported in the study.",
    "Adverse events were collected throughout the treatment and follow-up periods and coded to a standard dictionary.",
    "Discontinuation rates because of adverse events were reported for both treatment arms.",
    "The comparator was selected to reflect routine clinical practice at the time the study was designed.",
    "Baseline characteristics were balanced between the randomised treatment groups.",
    "No conclusions should be drawn about populations that were not represented in the study.",
    "Prescribers should consult the local approved prescribing information before making treatment decisions.",
    "This summary does not replace the full prescribing information for {product}.",
    "The data described relate to the approved indication in {ta} only.",
    "Confidence intervals and p-values for the primary endpoint are provided in the source publication.",
    "Sensitivity analyses supported the primary result and are described in the supplementary material.",
    "Where data are described as exploratory, they are hypothesis-generating and not confirmatory.",
    "The information presented has been reviewed and approved for the markets listed on this asset.",
    "Patient-facing components of this material must be supplied without alteration.",
    "Questions outside the approved indication should be directed to Medical Information.",
]

SAFETY_TEXT = (
    "Fair balance (fictional): This material describes a fictional medicinal product created "
    "for demonstration purposes only. The most frequently reported adverse reactions were "
    "gastrointestinal (nausea, diarrhoea, vomiting), injection site reactions and headache. "
    "Serious adverse reactions, contraindications, warnings and precautions are described in "
    "the full prescribing information. This product is not a real medicine and must not be "
    "used for any clinical purpose. Suspected adverse reactions relating to any real product "
    "should be reported through the applicable national reporting system."
)

FORMATS = ["detail aid", "clinical summary", "webinar recording", "leave-behind"]


def make_body(topic, ta, product, bespoke, n_target=190):
    parts = [
        "%s - %s (%s)." % (topic, product, ta),
    ]
    pool = [b.format(product=product, ta=ta) for b in bespoke]
    random.shuffle(pool)
    parts.extend(pool[:3])
    gen = random.sample(GENERIC, 8)
    for g in gen:
        if len(" ".join(parts).split()) >= n_target:
            break
        parts.append(g.format(product=product, ta=ta))
    parts.append(
        "For the full data set relating to %s, refer to the approved source publication "
        "or contact Medical Information." % topic.lower())
    text = " ".join(parts)
    words = text.split()
    if len(words) > 250:
        text = " ".join(words[:248]) + " ..."
    return text


def build_assets():
    rows = []
    idx = 0
    # 60 assets: cycle through the topic list, ~2 assets per topic
    plan = []
    while len(plan) < 60:
        for t in TOPICS:
            plan.append(t)
            if len(plan) == 60:
                break
    # deterministic slots for the guardrail-relevant statuses
    expired_slots = {3, 9, 14, 21, 27, 33, 41, 52}          # 8 expired
    pending_slots = {6, 17, 29, 44, 57}                      # 5 pending
    single_market_slots = {1: "USA", 8: "Turkey", 12: "Denmark",
                           23: "USA", 35: "Turkey", 48: "Denmark"}  # 6 single-market

    for i, (topic, ta, product, bespoke) in enumerate(plan):
        idx += 1
        asset_id = "AST-%04d" % (100 + idx)
        fmt = FORMATS[idx % len(FORMATS)]
        if i in expired_slots:
            status = "expired"
            expiry = TODAY - timedelta(days=random.randint(20, 400))
        elif i in pending_slots:
            status = "pending"
            expiry = TODAY + timedelta(days=random.randint(200, 500))
        else:
            status = "approved"
            expiry = TODAY + timedelta(days=random.randint(60, 700))
        if i in single_market_slots:
            approved_markets = single_market_slots[i]
        else:
            approved_markets = ",".join(
                random.choice([["USA", "Turkey", "Denmark"],
                               ["USA", "Denmark"],
                               ["USA", "Turkey"],
                               ["Turkey", "Denmark"],
                               ["USA", "Turkey", "Denmark"]]))
        title = topic if i < len(TOPICS) else "%s - %s" % (topic, fmt.title())
        rows.append((asset_id, title, ta, topic, fmt, approved_markets, status,
                     expiry.isoformat(), make_body(topic, ta, product, bespoke),
                     SAFETY_TEXT, product))
    return rows


# --------------------------------------------------------------------------
# 5. Behavioural tables
# --------------------------------------------------------------------------
CAMPAIGNS = ["CMP-2025-Q3-GLY", "CMP-2025-Q4-MET", "CMP-2026-Q1-GLY",
             "CMP-2026-Q1-HEM", "CMP-2026-Q2-MET", "CMP-2026-Q3-GLY"]


def build_email_events(hcps, assets):
    rows = []
    eid = 1
    by_market = {}
    for a in assets:
        for m in a[5].split(","):
            by_market.setdefault(m, []).append(a[0])
    profile = {}
    for h in hcps:
        hcp_id, market, tier = h[0], h[3], h[5]
        if h[6] == 0:                      # no email consent -> very few historic sends
            n = random.randint(4, 10)
        elif hcp_id in FATIGUED:
            n = random.randint(55, 70)     # over-contacted
        else:
            n = random.randint(38, 58)
        base_open = {"A": 0.20, "B": 0.12, "C": 0.07}[tier]
        if hcp_id in FATIGUED:
            base_open *= 0.35
        opens = clicks = unsubs = 0
        pool = by_market.get(market, [a[0] for a in assets])
        for _ in range(n):
            days_ago = random.randint(1, 365)
            sent = TODAY - timedelta(days=days_ago)
            recency_boost = 1.25 if days_ago < 90 else 1.0
            opened = 1 if random.random() < base_open * recency_boost else 0
            clicked = 1 if opened and random.random() < 0.32 else 0
            unsub = 1 if (not opened and random.random() < 0.004) else 0
            opens += opened
            clicks += clicked
            unsubs += unsub
            rows.append(("EML-%06d" % eid, hcp_id, random.choice(CAMPAIGNS),
                         random.choice(pool), sent.isoformat(), opened, clicked, unsub))
            eid += 1
        profile[hcp_id] = {"sends": n, "opens": opens, "clicks": clicks, "unsubs": unsubs}
    return rows, profile


def build_web_sessions(hcps, assets):
    rows = []
    sid = 1
    profile = {}
    asset_ids = [a[0] for a in assets]
    for h in hcps:
        hcp_id, tier = h[0], h[5]
        n = {"A": random.randint(6, 16), "B": random.randint(2, 9),
             "C": random.randint(0, 5)}[tier]
        if hcp_id in FATIGUED:
            n = max(0, n - 4)
        total_dwell = 0
        for _ in range(n):
            d = TODAY - timedelta(days=random.randint(1, 365))
            dwell = random.randint(20, 480)
            total_dwell += dwell
            rows.append(("WEB-%06d" % sid, hcp_id, d.isoformat(),
                         random.choice(asset_ids), dwell, random.randint(1, 7)))
            sid += 1
        profile[hcp_id] = {"sessions": n, "dwell": total_dwell}
    return rows, profile


EVENT_NAMES = [
    ("Global Metabolic Health Congress 2026", "congress"),
    ("Diabetes Care Update Webinar - Spring", "webinar"),
    ("Obesity Management Advisory Board", "advisory board"),
    ("Regional Endocrinology Symposium", "congress"),
    ("Rare Bleeding Disorders Webinar", "webinar"),
    ("Cardiometabolic Risk Roundtable", "advisory board"),
    ("Nordic Diabetology Meeting 2026", "congress"),
    ("Primary Care Diabetes Webinar", "webinar"),
    ("Haematology Practice Forum", "advisory board"),
]


def build_events(hcps):
    ev_rows = []
    att_rows = []
    eid = 1
    for market in MARKETS:
        for name, etype in EVENT_NAMES:
            if random.random() < 0.7:
                d = TODAY - timedelta(days=random.randint(10, 340))
                ev_rows.append(("EVT-%04d" % eid, name, etype, d.isoformat(), market))
                eid += 1
    by_market = {}
    for e in ev_rows:
        by_market.setdefault(e[4], []).append(e[0])
    profile = {}
    for h in hcps:
        hcp_id, market, tier = h[0], h[3], h[5]
        pool = by_market.get(market, [])
        n = min(len(pool), {"A": 4, "B": 2, "C": 1}[tier])
        chosen = random.sample(pool, n) if n else []
        attended = 0
        for e in chosen:
            reg = 1
            att = 1 if random.random() < 0.62 else 0
            attended += att
            att_rows.append((hcp_id, e, reg, att))
        profile[hcp_id] = {"registered": len(chosen), "attended": attended}
    return ev_rows, att_rows, profile


REGIONS = {
    "USA": ["Northeast", "Midwest", "South", "West"],
    "Turkey": ["Marmara", "Aegean", "Central Anatolia", "Mediterranean"],
    "Denmark": ["Capital Region", "Central Jutland", "Southern Denmark", "North Jutland"],
}


def build_rx_sales():
    """AGGREGATE ONLY. There is deliberately no hcp_id column here."""
    rows = []
    for market in MARKETS:
        for region in REGIONS[market]:
            for product in PRODUCTS:
                base = random.randint(2200, 9000)
                trend = random.uniform(-0.01, 0.035)
                for m in range(12):
                    month_date = date(TODAY.year, TODAY.month, 1) - timedelta(days=30 * (11 - m))
                    month = month_date.strftime("%Y-%m")
                    units = int(base * ((1 + trend) ** m) * random.uniform(0.92, 1.08))
                    revenue_index = round(units * random.uniform(0.9, 1.1) / 100.0, 2)
                    rows.append((market, region, product, month, units, revenue_index))
    return rows


# --------------------------------------------------------------------------
# 6. Model scores - the simulated ML output layer
# --------------------------------------------------------------------------
REASON_LIB = {
    "email": [
        "opened {opens} of last {sends} clinical emails",
        "clicked through on {clicks} campaign assets in 12 months",
        "email open rate {rate}% vs market average 12%",
    ],
    "rep visit": [
        "{visits} rep visits in 12 months, last {gap} days ago",
        "positive outcome recorded on most recent visit",
        "tier {tier} prescriber with active follow-up request",
    ],
    "webinar invite": [
        "attended {attended} of {registered} events registered",
        "high dwell time on webinar recordings",
        "no event attendance in the last 6 months",
    ],
    "portal content": [
        "{sessions} portal sessions, {dwell} minutes total dwell",
        "high dwell time on {topic} content",
        "returned to the content portal within 14 days",
    ],
    "no contact": [
        "{contacts} contacts in the last 90 days - above frequency cap guidance",
        "engagement falling while contact frequency rises",
        "fatigue score {fat} - suppress outreach this cycle",
        "no engagement recorded on last {sends} sends",
    ],
}

AFFINITY_TOPICS = ["renal dosing", "cardiovascular outcomes", "weight management",
                   "tolerability", "prophylaxis dosing", "real-world evidence",
                   "titration", "adherence"]


def build_model_scores(hcps, email_p, web_p, event_p, visits, assets):
    visit_count = {}
    last_visit = {}
    for v in visits:
        visit_count[v[1]] = visit_count.get(v[1], 0) + 1
        d = date.fromisoformat(v[2])
        if v[1] not in last_visit or d > last_visit[v[1]]:
            last_visit[v[1]] = d

    approved_by_market = {}
    for a in assets:
        if a[6] != "approved" or date.fromisoformat(a[7]) <= TODAY:
            continue
        for m in a[5].split(","):
            approved_by_market.setdefault(m, []).append((a[0], a[3]))

    rows = []
    no_contact_top = []
    per_hcp = []
    for h in hcps:
        hcp_id, market, tier = h[0], h[3], h[5]
        consent_email, consent_rep = h[6], h[7]
        ep = email_p[hcp_id]
        wp = web_p[hcp_id]
        vp = event_p[hcp_id]
        sends = max(1, ep["sends"])
        open_rate = ep["opens"] / sends
        click_rate = ep["clicks"] / sends
        vcount = visit_count.get(hcp_id, 0)
        gap = (TODAY - last_visit[hcp_id]).days if hcp_id in last_visit else 365
        dwell_min = wp["dwell"] // 60

        # ---- fatigue: recent contact volume vs engagement -------------------
        recent_contacts = int(sends * 0.25) + vcount     # last ~90 days
        fatigue = 0.62 * min(1.0, recent_contacts / 24.0)
        fatigue += 0.22 * (1.0 - min(1.0, open_rate / 0.20))
        if gap < 21:
            fatigue += 0.08
        if hcp_id in FATIGUED:
            fatigue = max(fatigue, 0.76)
        fatigue = round(min(0.95, max(0.05, fatigue)), 3)

        tier_w = {"A": 1.15, "B": 1.0, "C": 0.82}[tier]
        engagement_index = min(1.0, open_rate * 2.2 + click_rate * 2.5
                               + min(1.0, dwell_min / 40.0) * 0.35
                               + (vp["attended"] / 4.0) * 0.25)

        topic = random.choice(AFFINITY_TOPICS)
        pool = approved_by_market.get(market, [])
        best = [aid for aid, tp in pool if topic.split()[0].lower() in tp.lower()]
        pref_asset = best[0] if best else (pool[0][0] if pool else None)

        scored = []
        for ch in CHANNELS:
            asset_id = None
            if ch == "email":
                p = engagement_index * 0.85 + open_rate * 0.5
                p *= (1.0 - 0.55 * fatigue)
                if not consent_email:
                    p *= 0.05
                asset_id = pref_asset
            elif ch == "rep visit":
                p = 0.30 + min(1.0, gap / 120.0) * 0.35 + engagement_index * 0.25
                p *= (1.0 - 0.35 * fatigue)
                if not consent_rep:
                    p *= 0.05
                asset_id = pref_asset
            elif ch == "webinar invite":
                p = 0.15 + (vp["attended"] / 4.0) * 0.5 + engagement_index * 0.2
                p *= (1.0 - 0.45 * fatigue)
            elif ch == "portal content":
                p = 0.12 + min(1.0, dwell_min / 45.0) * 0.55 + click_rate * 0.6
                p *= (1.0 - 0.30 * fatigue)
                asset_id = pref_asset
            else:  # no contact
                p = fatigue * 0.85 + (1.0 - engagement_index) * 0.25
            p = round(min(0.97, max(0.02, p + random.uniform(-0.03, 0.03))), 3)

            optout = round(min(0.9, max(0.005,
                          fatigue * 0.22 + (1 - engagement_index) * 0.06
                          + (0.12 if ch == "email" and not consent_email else 0)
                          - (0.15 if ch == "no contact" else 0))), 3)

            if ch == "no contact":
                # Suppression value. The raw score is calibrated against a
                # frequency-cap threshold after all HCPs are scored (below), so
                # that suppression wins only for genuinely fatigued, disengaged
                # HCPs rather than for everyone with a mediocre open rate.
                ev = round(0.62 * fatigue + 0.26 * (1.0 - engagement_index)
                           + 0.12 * optout, 3)
            else:
                ev = round(p * tier_w * (1.0 - optout) * 1.0, 3)

            fmt = {"opens": ep["opens"], "sends": sends, "clicks": ep["clicks"],
                   "rate": int(open_rate * 100), "visits": vcount, "gap": gap,
                   "tier": tier, "attended": vp["attended"],
                   "registered": max(1, vp["registered"]), "sessions": wp["sessions"],
                   "dwell": dwell_min, "topic": topic, "contacts": recent_contacts,
                   "fat": fatigue}
            reasons = ", ".join(r.format(**fmt) for r in random.sample(REASON_LIB[ch], 2))
            scored.append([hcp_id, ch, asset_id, p, optout, fatigue, ev, reasons])

        per_hcp.append((hcp_id, scored))

    # ---- calibrate the suppression threshold ----------------------------
    # Pick the offset that leaves suppression ("no contact") as the top-ranked
    # action for a realistic minority of HCPs (target 6-9), not for everyone.
    def n_suppressed(offset):
        n = 0
        for _hid, sc in per_hcp:
            nc = max(r[6] for r in sc if r[1] == "no contact") - offset
            act = max(r[6] for r in sc if r[1] != "no contact")
            if nc > act:
                n += 1
        return n

    offset = 0.0
    for cand in [x / 200.0 for x in range(0, 120)]:
        if n_suppressed(cand) <= 8:
            offset = cand
            break

    for hcp_id, scored in per_hcp:
        for r in scored:
            if r[1] == "no contact":
                r[6] = round(max(0.02, r[6] - offset), 3)
        scored.sort(key=lambda r: -r[6])
        if scored[0][1] == "no contact":
            no_contact_top.append(hcp_id)
        for r in scored:
            rows.append(tuple(r) + (1 if hcp_id in HOLDOUT else 0, "nbe-v1.3", TODAY.isoformat()))
    return rows, no_contact_top


# --------------------------------------------------------------------------
# 7. Write the database
# --------------------------------------------------------------------------
SCHEMA = """
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS hcps;
DROP TABLE IF EXISTS rep_visits;
DROP TABLE IF EXISTS email_events;
DROP TABLE IF EXISTS web_sessions;
DROP TABLE IF EXISTS events;
DROP TABLE IF EXISTS event_attendance;
DROP TABLE IF EXISTS content_assets;
DROP TABLE IF EXISTS rx_sales;
DROP TABLE IF EXISTS model_scores;

CREATE TABLE users (username TEXT PRIMARY KEY, password TEXT, market TEXT,
                    full_name TEXT, role TEXT);
CREATE TABLE hcps (hcp_id TEXT PRIMARY KEY, name TEXT, specialty TEXT, market TEXT,
                   city TEXT, prescribing_tier TEXT, consent_email INTEGER,
                   consent_rep INTEGER, preferred_channel TEXT, last_contact_date TEXT,
                   segment TEXT);
CREATE TABLE rep_visits (visit_id TEXT PRIMARY KEY, hcp_id TEXT, visit_date TEXT,
                         rep_name TEXT, duration_min INTEGER, topics TEXT,
                         call_note TEXT, outcome TEXT);
CREATE TABLE email_events (event_id TEXT PRIMARY KEY, hcp_id TEXT, campaign_id TEXT,
                           asset_id TEXT, sent_at TEXT, opened INTEGER, clicked INTEGER,
                           unsubscribed INTEGER);
CREATE TABLE web_sessions (session_id TEXT PRIMARY KEY, hcp_id TEXT, session_date TEXT,
                           asset_id TEXT, dwell_seconds INTEGER, pages_viewed INTEGER);
CREATE TABLE events (event_id TEXT PRIMARY KEY, event_name TEXT, event_type TEXT,
                     event_date TEXT, market TEXT);
CREATE TABLE event_attendance (hcp_id TEXT, event_id TEXT, registered INTEGER,
                               attended INTEGER);
CREATE TABLE content_assets (asset_id TEXT PRIMARY KEY, title TEXT, therapy_area TEXT,
                             topic TEXT, format TEXT, approved_markets TEXT,
                             mlr_status TEXT, mlr_expiry_date TEXT, body_text TEXT,
                             safety_text TEXT, product TEXT);
-- rx_sales is AGGREGATE ONLY. There is deliberately no hcp_id column.
CREATE TABLE rx_sales (market TEXT, region TEXT, product TEXT, month TEXT,
                       units INTEGER, revenue_index REAL);
CREATE TABLE model_scores (hcp_id TEXT, channel TEXT, asset_id TEXT,
                           propensity_engage REAL, propensity_optout REAL,
                           fatigue_score REAL, expected_value REAL, reason_codes TEXT,
                           holdout_flag INTEGER, model_version TEXT, scored_at TEXT);
CREATE INDEX idx_email_hcp ON email_events(hcp_id);
CREATE INDEX idx_web_hcp ON web_sessions(hcp_id);
CREATE INDEX idx_visit_hcp ON rep_visits(hcp_id);
CREATE INDEX idx_scores_hcp ON model_scores(hcp_id);
"""


def main():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)

    hcps = build_hcps()
    assets = build_assets()
    visits = build_visits(hcps)
    emails, email_p = build_email_events(hcps, assets)
    web, web_p = build_web_sessions(hcps, assets)
    ev_rows, att_rows, event_p = build_events(hcps)
    rx = build_rx_sales()
    scores, no_contact_top = build_model_scores(hcps, email_p, web_p, event_p, visits, assets)

    con.executemany("INSERT INTO users VALUES (?,?,?,?,?)", USERS)
    con.executemany("INSERT INTO hcps VALUES (?,?,?,?,?,?,?,?,?,?,?)", hcps)
    con.executemany("INSERT INTO rep_visits VALUES (?,?,?,?,?,?,?,?)", visits)
    con.executemany("INSERT INTO email_events VALUES (?,?,?,?,?,?,?,?)", emails)
    con.executemany("INSERT INTO web_sessions VALUES (?,?,?,?,?,?)", web)
    con.executemany("INSERT INTO events VALUES (?,?,?,?,?)", ev_rows)
    con.executemany("INSERT INTO event_attendance VALUES (?,?,?,?)", att_rows)
    con.executemany("INSERT INTO content_assets VALUES (?,?,?,?,?,?,?,?,?,?,?)", assets)
    con.executemany("INSERT INTO rx_sales VALUES (?,?,?,?,?,?)", rx)
    con.executemany("INSERT INTO model_scores VALUES (?,?,?,?,?,?,?,?,?,?,?)", scores)
    con.commit()

    # ---------------- summary --------------------------------------------
    print("\nBuilt %s" % DB_PATH)
    print("-" * 62)
    print("%-22s %10s" % ("TABLE", "ROWS"))
    print("-" * 62)
    for t in ["users", "hcps", "rep_visits", "email_events", "web_sessions",
              "events", "event_attendance", "content_assets", "rx_sales", "model_scores"]:
        n = con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        print("%-22s %10d" % (t, n))
    print("-" * 62)

    def one(sql):
        return con.execute(sql).fetchone()[0]

    checks = []
    checks.append(("HCPs per market",
                   str(con.execute("SELECT market, COUNT(*) FROM hcps GROUP BY market"
                                   ).fetchall())))
    checks.append(("HCPs with consent_email = 0",
                   one("SELECT COUNT(*) FROM hcps WHERE consent_email = 0")))
    checks.append(("Expired assets", one("SELECT COUNT(*) FROM content_assets "
                                         "WHERE mlr_status='expired'")))
    checks.append(("Pending assets", one("SELECT COUNT(*) FROM content_assets "
                                         "WHERE mlr_status='pending'")))
    checks.append(("Single-market assets",
                   one("SELECT COUNT(*) FROM content_assets "
                       "WHERE approved_markets NOT LIKE '%,%'")))
    checks.append(("Adverse-event call notes",
                   one("SELECT COUNT(*) FROM rep_visits WHERE call_note LIKE '%nausea%' "
                       "OR call_note LIKE '%bleeding episode%' "
                       "OR call_note LIKE '%injection site reaction%'")))
    checks.append(("HCPs where 'no contact' ranks #1", len(no_contact_top)))
    checks.append(("HCPs in holdout",
                   one("SELECT COUNT(DISTINCT hcp_id) FROM model_scores WHERE holdout_flag=1")))
    cols = [c[1] for c in con.execute("PRAGMA table_info(rx_sales)").fetchall()]
    checks.append(("rx_sales columns", ", ".join(cols)))
    checks.append(("rx_sales contains hcp_id?", "YES - BUG" if "hcp_id" in cols else "NO - correct"))
    checks.append(("Email open rate overall",
                   "%.1f%%" % (100.0 * one("SELECT AVG(opened) FROM email_events"))))
    checks.append(("Asset body word count (min/avg/max)",
                   str(con.execute(
                       "SELECT MIN(LENGTH(body_text)-LENGTH(REPLACE(body_text,' ',''))+1),"
                       " CAST(AVG(LENGTH(body_text)-LENGTH(REPLACE(body_text,' ',''))+1) AS INT),"
                       " MAX(LENGTH(body_text)-LENGTH(REPLACE(body_text,' ',''))+1)"
                       " FROM content_assets").fetchone())))

    print("VERIFICATION")
    print("-" * 62)
    for k, v in checks:
        print("%-38s %s" % (k, v))
    print("-" * 62)
    print("'no contact' top-ranked for: %s" % ", ".join(no_contact_top))
    print()
    con.close()


if __name__ == "__main__":
    main()
