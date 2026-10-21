"""Questions needing personal judgment cannot be answered from career fit alone."""

import re
from .self_identification import sensitive_field

PERSONAL = re.compile(
    r"what interests|what.*(?:attracts|appeals|like about)|why.*(?:interested|apply|join|work|want|role|position|company)|"
    r"motivat|passion|personal (?:interest|values|goal)|what.*(?:value|enjoy)|proud|inspir|strengths|weaknesses|"
    r"what.*(?:looking for|excites)|career (?:goals|aspirations)|hobbies|outside of work|free time",
    re.I,
)
PERSONAL_KEYS = re.compile(
    r"interest|motivation|personal_values|career_goals|career_aspirations", re.I
)
COMMITMENT = re.compile(
    r"salary|compensation|\bpay\b|wage|bonus|commission|equity|\bote\b|relocat|travel|schedule|hours|reference|authoriz|consent|agree|attest|certif|signature|sponsor|privacy|background|start.date|office|camera|accommodat|essential.function|commit|willing|availab|on.?site|hybrid|acknowledg|accept|\bconfirm\b|\bsign(?:ed|ing)?\b",
    re.I,
)


def personal_question(field):
    return bool(PERSONAL.search(field["label"]))


def requires_explicit_answer(field):
    return bool(
        sensitive_field(field)
        or field.get("material")
        or COMMITMENT.search(field["label"] + " " + field.get("name", ""))
    )


def personal_fact(fact):
    field = fact.get("provenance", {}).get("form_field", {})
    return fact["kind"] in ("preference", "answer", "candidate") and bool(
        PERSONAL_KEYS.search(fact["key"])
        or (isinstance(field, dict) and field.get("label") and personal_question(field))
    )


def experience_narrative(field):
    """Open-ended requests for examples belong to answer preparation.

    Identify intent independently of the employer control type. Explicit
    tenure/credential thresholds still need separate qualification assessment.
    """
    label = field["label"]
    if re.search(
        r"\b(?:at least|minimum|must|require[ds]?)\b|\b\d+\+?\s*(?:years?|months?)\b|\b(?:degree|certifi\w*|clearance)\b",
        label,
        re.I,
    ):
        return False
    return bool(
        re.search(
            r"\b(?:share|describe|explain|provide|identify|highlight)\b|\btell us\b|\bwhat\b.*\b(?:built|implemented)\b",
            label,
            re.I,
        )
        and re.search(
            r"\b(?:projects?|contributions?|examples?|built|implemented|github|workflows?)\b",
            label,
            re.I,
        )
    )


def narrative_choice_mismatch(field):
    return bool(field.get("options") and experience_narrative(field))


def narrative_context_key(field):
    from .config import digest

    return "application_narrative_context_" + digest(field["key"])[:28]


def answer_guidance(field):
    if not experience_narrative(field):
        return None
    mismatch = narrative_choice_mismatch(field)
    return {
        "narrative": True,
        "choice_mismatch": mismatch,
        "message": (
            "The employer asks for examples but its form only accepts a choice. Write your examples and interview-demo details below for this application's records. Separately choose the option to send. Your notes will not be inserted into another field or attachment."
            if mismatch
            else "Describe the project, what you contributed, and what you can show. Use confirmed examples and identify any limits on sharing the work."
        ),
    }


def narrative_field(field):
    return field | {"type": "textarea", "options": [], "qualification": False}
