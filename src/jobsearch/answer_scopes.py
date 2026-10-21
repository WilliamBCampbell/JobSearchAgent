"""Scope rules shared by memory, question answering, and the UI API."""

import re
from .config import digest

SCOPED = re.compile(
    r"requirement_decision_|application_form_|salary|compensation|relocat|travel|schedule|hours|reference|authorization|consent|attestation|start_date|office_preference|camera|background|accommodat|essential_function",
    re.I,
)
ROLE_SPECIFIC = re.compile(
    r"salary|compensation|pay\b|wage|bonus|commission|equity|\bote\b|relocat|travel|schedule|hours|reference|authoriz|consent|agree|attest|certif|signature|sponsor|privacy|background|start.date|office|camera|accommodat|essential.function|"
    r"this (?:role|position|job|company|employer)|our (?:company|team)|why|interest|join|hear about|commit|willing|availab|on.?site|hybrid|acknowledg|accept|\bconfirm\b|\bsign(?:ed|ing)?\b",
    re.I,
)

# These ask about candidate status, not permission to act for the candidate.
# Match the complete question so appended consents or country-specific wording
# cannot inherit this exception. Reuse still requires the exact field identity.
WORK_STATUS_QUESTIONS = {
    "do you require or will you require a work authorization",
    "do you require or will you require work authorization",
    "will you now or in the future require sponsorship for employment visa status",
    "do you now or in the future require sponsorship for employment visa status",
}


def reusable_work_status(field):
    label = (
        re.sub(r"\s+", " ", field.get("label", "").strip().casefold())
        .rstrip("?")
        .strip()
    )
    return (
        label in WORK_STATUS_QUESTIONS
        and field.get("type") in ("text", "textarea", "select", "radio")
        and all(
            str(o).strip().casefold() in ("yes", "no") for o in field.get("options", [])
        )
    )


def allows_global(key, field=None):
    if key.startswith("self_identification_"):
        from .self_identification import identity

        return bool(
            isinstance(field, dict)
            and field.get("purpose") == "self_identification"
            and not field.get("material")
            and key == identity(field)
        )
    if not key.startswith("application_form_"):
        return not SCOPED.search(key)
    if not isinstance(field, dict) or not field.get("label") or not field.get("name"):
        return False
    expected = (
        "application_form_"
        + digest([field["name"], field["label"], field.get("options", [])])[:28]
    )
    if key != expected:
        return False
    if reusable_work_status(field):
        return True
    return (
        not field.get("material", False)
        and field.get("type") != "file"
        and not ROLE_SPECIFIC.search(field["label"] + " " + field["name"])
    )


def fact_allows_global(fact):
    return allows_global(fact["key"], fact["provenance"].get("global_form_field"))
