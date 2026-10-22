"""Document-only checks; model decisions never update career facts."""

import json
import re
from datetime import datetime

from .contracts import PackageDraft


INTERNAL = re.compile(
    r"\b(?:candidate confirmed|confirmed (?:on|October|September|August)|"
    r"confirmation date|superseded|unconfirmed|fact_ids|evidence_versions|"
    r"do not (?:claim|describe|reuse)|this establishes|\w+ confirmed|"
    r"he clarified|deployment dates are now confirmed)\b",
    re.I,
)


# Administrative identifiers belong in application records, not employer-facing prose.
LETTER_METADATA = re.compile(
    r"\b(?:requisition|req\.?|job[ _-]*(?:id|number)|posting[ _-]*id|application[ _-]*id)"
    r"\s*(?:(?:id|number|no\.?)\s*)?[:#]?\s*(?=[a-z0-9_.-]*\d)[a-z0-9][a-z0-9_.-]*\b"
    r"|\b(?:fact_ids|source_refs|evidence_versions|application_id|requisition_id)\b",
    re.I,
)


def validate_cover_letter_text(text, application):
    if LETTER_METADATA.search(text) or INTERNAL.search(text):
        raise ValueError(
            "Cover letter contains internal tracking metadata; remove requisition labels, IDs, and evidence notes"
        )
    for key in ("id", "requisition"):
        value = str(application.get(key) or "").strip()
        # Short numeric requisitions can coincide with legitimate dates or metrics.
        # Their labeled forms are rejected above; longer identifiers are never prose.
        if len(value) >= 6 and re.search(
            r"(?<!\w)" + re.escape(value) + r"(?!\w)", text, re.I
        ):
            raise ValueError(
                "Cover letter contains an application or requisition ID; remove internal tracking metadata"
            )


def employment_facts(facts):
    rows = [
        f
        for f in facts
        if f["kind"] == "employment"
        and isinstance(f["value"], dict)
        and all(f["value"].get(k) for k in ("employer", "title", "start", "end"))
    ]

    def date(row):
        value = row["value"]["start"]
        for pattern in ("%m/%Y", "%Y-%m", "%Y"):
            try:
                return datetime.strptime(value, pattern)
            except ValueError:
                pass
        raise ValueError("Employment start date needs a supported format")

    return sorted(rows, key=date, reverse=True)


def claims(document):
    yield "summary", document["summary"]
    for i, group in enumerate(document["skills"]):
        yield f"skills.{i}", group
    for i, role in enumerate(document["experience"]):
        for j, bullet in enumerate(role["bullets"]):
            yield f"experience.{i}.{j}", bullet
    for i, education in enumerate(document["education"]):
        yield f"education.{i}", education
    for i, paragraph in enumerate(document["cover_letter"]):
        yield f"cover_letter.{i}", paragraph


def validate_document(document, packet):
    from ..packages import prose_check

    document = PackageDraft.model_validate(document).model_dump()
    facts = {f["id"]: f for f in packet["facts"]}
    expected = {f["id"] for f in employment_facts(packet["facts"])}
    actual = [r["employment_fact_id"] for r in document["experience"]]
    if set(actual) != expected or len(actual) != len(expected):
        raise ValueError(
            "Resume must retain each confirmed employment record exactly once"
        )
    if not expected:
        raise ValueError(
            "Confirm employment titles and dates before preparing a resume"
        )
    education_ids = {
        f["id"] for f in packet["facts"] if f["key"].startswith("education_")
    }
    if education_ids and not document["education"]:
        raise ValueError("Resume omitted confirmed education or training")
    for entry in document["education"]:
        if not education_ids.intersection(entry["fact_ids"]):
            raise ValueError("Education must cite confirmed education or training")
    seen = set()
    for location, claim in claims(document):
        if set(claim["fact_ids"]) - facts.keys():
            raise ValueError("Document cites an unknown or out-of-scope career fact")
        text = claim["text"]
        if location.startswith("cover_letter."):
            validate_cover_letter_text(text, packet.get("application") or {})
        if INTERNAL.search(text) or prose_check(text):
            raise ValueError(
                f"Document contains internal notes or unfinished prose at {location}"
            )
        normalized = " ".join(text.lower().split())
        # A cover letter can revisit experience, but should develop it in its own prose.
        if normalized in seen:
            raise ValueError(f"Document repeats the same claim at {location}")
        seen.add(normalized)
        evidence = json.dumps([facts[i]["value"] for i in claim["fact_ids"]])
        # Numbers need support in the cited facts, not elsewhere in the corpus.
        for number in re.findall(r"(?<![\w])\d+(?:\.\d+)?(?![\w])", text):
            if number not in re.findall(r"(?<![\w])\d+(?:\.\d+)?(?![\w])", evidence):
                raise ValueError(f"Unsupported number in document at {location}")
    if sum(len(c["text"].split()) for _, c in claims(document)) > 900:
        raise ValueError("Document is too long; shorten the resume and cover letter")
    if document["missing_questions"]:
        raise ValueError(
            "Package needs candidate input: " + "; ".join(document["missing_questions"])
        )
    return document


def fixture_document(packet):
    """A deterministic structured example, limited to the supplied confirmed facts."""
    roles = employment_facts(packet["facts"])
    prose = [
        f
        for f in packet["facts"]
        if isinstance(f["value"], str)
        and f["kind"] in ("skill", "employment")
        and not INTERNAL.search(f["value"])
    ]
    if not prose or not roles:
        raise ValueError(
            "Fixture document needs employment records and career evidence"
        )
    chosen = prose[0]
    summary = {"text": chosen["value"], "fact_ids": [chosen["id"]]}
    experience = []
    for role in roles:
        text = (
            role["value"].get("notes")
            or f"Worked as {role['value']['title']} at {role['value']['employer']}."
        )
        experience.append(
            {
                "employment_fact_id": role["id"],
                "bullets": [{"text": text, "fact_ids": [role["id"]]}],
            }
        )
    return {
        "summary": summary,
        "skills": [
            {
                "label": "Experience",
                "text": roles[0]["value"]["title"],
                "fact_ids": [roles[0]["id"]],
            }
        ],
        "experience": experience,
        "education": [
            {"text": f["value"], "fact_ids": [f["id"]]}
            for f in packet["facts"]
            if f["key"].startswith("education_") and isinstance(f["value"], str)
        ],
        "cover_letter": [
            {
                "text": "I'm interested in this position because I would like to apply my experience in a new role. My work includes: "
                + chosen["value"],
                "fact_ids": [chosen["id"]],
                "source_refs": [],
            },
            {
                "text": f"My background also includes work as a {roles[0]['value']['title']} at {roles[0]['value']['employer']}.",
                "fact_ids": [roles[0]["id"]],
                "source_refs": [],
            },
        ],
        "missing_questions": [],
    }
