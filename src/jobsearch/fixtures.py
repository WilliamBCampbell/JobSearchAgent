from __future__ import annotations
import json
import zipfile
from lxml import etree as E
from .config import atomic_write
from .db import js
from .memory import FactInput, Memory
from .tracker import NS, T, O, X, TRACKER_NAME
from .scoring import Assessment, Requirement, ENGINEERING, SOLUTIONS


ODS_MANIFEST = b'<?xml version="1.0" encoding="UTF-8"?>\n<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.3">\n<manifest:file-entry manifest:full-path="/" manifest:version="1.3" manifest:media-type="application/vnd.oasis.opendocument.spreadsheet"/>\n<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>\n<manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>\n</manifest:manifest>'
ODS_STYLES = b'<?xml version="1.0" encoding="UTF-8"?>\n<office:document-styles xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" office:version="1.3"><office:styles/><office:automatic-styles/><office:master-styles/></office:document-styles>'


def make_tracker(path):
    root = E.Element(
        O + "document-content",
        nsmap={"office": NS["o"], "table": NS["t"], "text": NS["x"]},
    )
    root.set(O + "version", "1.3")
    body = E.SubElement(root, O + "body")
    sheet = E.SubElement(body, O + "spreadsheet")
    table = E.SubElement(sheet, T + "table")
    table.set(T + "name", "Applications")
    rows = [
        ["JOB SEARCH SUMMARY"],
        ["Tracked", "3", "Submitted", "1", "Active", "1", "Rejected", "0"],
        ["Prepared", "2", "Offers", "0", "Last updated", "2026-01-01"],
        ["Unresolved", "0", "Manual follow-up", "0"],
        [
            "Company",
            "Role",
            "Date Applied",
            "Status",
            "Last Update",
            "Resume",
            "Cover Letter",
            "Notes",
        ],
        [
            "Archive Demo",
            "Solutions Engineer (AD-001)",
            "2026-01-01",
            "Submitted",
            "",
            "",
            "",
            "Synthetic prior application",
        ],
        [
            "Archive Demo",
            "Solutions Engineer (AD-002)",
            "",
            "Prepared, not submitted",
            "",
            "",
            "",
            "Deferred: fixture decision",
        ],
        [
            "Northstar Demo",
            "Software Engineer, APIs (NS-101)",
            "",
            "Prepared, not submitted",
            "",
            "",
            "",
            "Fixture target",
        ],
    ]
    for values in rows:
        row = E.SubElement(table, T + "table-row")
        for value in values:
            cell = E.SubElement(row, T + "table-cell")
            cell.set(O + "value-type", "string")
            E.SubElement(cell, X + "p").text = value
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.spreadsheet",
            compress_type=0,
        )
        z.writestr(
            "content.xml", E.tostring(root, xml_declaration=True, encoding="utf-8")
        )
        z.writestr("META-INF/manifest.xml", ODS_MANIFEST)
        z.writestr("styles.xml", ODS_STYLES)


def seed(db):
    root = db.settings.fixture_workspace
    root.mkdir(parents=True, exist_ok=True)
    facts = [
        ("professional_name", "Alex Example", "candidate"),
        ("email", "alex@example.invalid", "candidate"),
        ("location", "Columbus, Ohio", "candidate"),
        (
            "employment_example",
            {
                "employer": "Example Systems",
                "title": "Software Engineer",
                "start": "01/2020",
                "end": "08/2026",
                "notes": "Developed integrations and supported customer implementations.",
            },
            "employment",
        ),
        (
            "api_delivery",
            "Built C# REST APIs and PostgreSQL integrations for a support application.",
            "employment",
        ),
        (
            "mentoring",
            "Reviewed pull requests and mentored developers on an application team.",
            "employment",
        ),
        (
            "customer_work",
            "Ran customer discovery sessions and demonstrated application workflows.",
            "employment",
        ),
        (
            "framework_contribution",
            "Contributed integration tools to a shared agent framework; framework development was a team responsibility.",
            "employment",
        ),
        (
            "production_ai",
            "The fixture candidate has four months of production agent experience.",
            "candidate",
        ),
        ("csharp_years", 5, "skill_duration"),
    ]
    ids = {}
    for key, value, kind in facts:
        fact = Memory(db).put(
            FactInput(
                key=key,
                value=value,
                kind=kind,
                status="confirmed",
                provenance={
                    "type": "user_confirmation",
                    "source": "Synthetic evaluation fixture",
                },
            )
        )
        ids[key] = fact["id"]
    atomic_write(
        root / "Confirmed_Fixture_Answers.json",
        json.dumps(
            {"fixture": True, "confirmed": {k: v for k, v, _ in facts}}, indent=2
        ),
    )
    atomic_write(
        root / "Career_Notes.md",
        "# Synthetic career notes\n\n## API delivery\n\nBuilt C# REST APIs and PostgreSQL integrations.\n\n## Ownership clarification\n\nContributed to a shared framework. This fixture does not establish primary framework ownership.\n",
    )
    atomic_write(
        root / "Generated_Draft.md",
        "# Generated draft, unsupported\n\nThe candidate led a 200-person AI organization and holds a doctorate. These are deliberately unsupported claims for evaluation.\n",
    )
    if not (root / TRACKER_NAME).exists():
        make_tracker(root / TRACKER_NAME)
    # Import the two historical fixture rows only; the target is created through discovery.

    for req, state in [("AD-001", "submitted"), ("AD-002", "deferred")]:
        aid = "fixture-prior-" + req
        db.execute(
            "INSERT INTO applications(id,candidate,employer,role,requisition,state,payload,imported) VALUES(%s,'fixture','Archive Demo','Solutions Engineer',%s,%s,%s,true) ON CONFLICT DO NOTHING",
            (
                aid,
                req,
                state,
                js(
                    {
                        "tracker": {
                            "status": "Submitted"
                            if state == "submitted"
                            else "Prepared, not submitted"
                        },
                        "decision": "Deferred fixture role"
                        if state == "deferred"
                        else None,
                    }
                ),
            ),
        )
    db.set_setting("fixture_fact_ids", ids)
    return {
        "facts": ids,
        "workspace": str(root),
        "notice": "All demonstration employers and candidate details are synthetic.",
    }


def assessment(db, track="software", mandatory_gap=False):
    ids = db.setting("fixture_fact_ids", {})
    weights = SOLUTIONS if track == "solutions" else ENGINEERING
    mapping = {
        "technical": "api_delivery",
        "delivery": "api_delivery",
        "seniority": "mentoring",
        "domain": "customer_work",
        "customer": "customer_work",
    }
    deductions = {
        "technical": 2,
        "delivery": 2,
        "seniority": 1,
        "domain": 1,
        "ohio": 0,
        "compensation": 0,
        "customer": 1,
    }
    reqs = []
    for category in weights:
        reqs.append(
            Requirement(
                text={
                    "technical": "C# APIs",
                    "delivery": "Ship and support integrations",
                    "seniority": "Independent delivery and code review",
                    "domain": "Support workflows",
                    "ohio": "Ohio eligible",
                    "compensation": "Published annual base",
                    "customer": "Customer discovery",
                }[category],
                source="fixture:official-posting",
                category=category,
                classification="required"
                if category not in ("domain", "compensation")
                else "preferred",
                central=category == "technical",
                assessment="strong",
                fact_ids=[ids[mapping[category]]] if category in mapping else [],
                deduction=deductions[category],
                reason="Confirmed synthetic evidence; deductions represent a narrower project scope.",
            )
        )
    if mandatory_gap:
        reqs.append(
            Requirement(
                text="Two years of production agent delivery",
                source="fixture:official-posting",
                category="technical",
                classification="required",
                central=True,
                assessment="gap",
                fact_ids=[ids["production_ai"]],
                deduction=0,
                reason="Confirmed fixture history establishes four months, below two years.",
            )
        )
    return Assessment(
        track=track,
        requirements=reqs,
        ohio="confirmed",
        compensation={
            "base": {
                "min": 110000,
                "max": 140000,
                "currency": "USD",
                "period": "annual",
            },
            "bonus": None,
            "commission": None,
            "equity": None,
            "ote": None,
            "classification": "base",
        },
    )
