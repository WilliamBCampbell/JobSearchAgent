"""Explicit self-identification choices, separate from career evidence and inference."""

import html
import re
from bs4 import BeautifulSoup
from .config import digest

LEAVE_BLANK = "Leave this question blank"
LEGACY_BLOCKER = (
    "Additional demographic or consent questions need review before browser filling"
)
SENSITIVE = re.compile(
    r"gender|racial|ethnic|\brace\b|veteran|disabilit|transgender|sexual.orientation|self.identif",
    re.I,
)


def sensitive_field(field):
    return field.get("purpose") in (
        "self_identification",
        "demographic_consent",
    ) or bool(SENSITIVE.search(field.get("label", "") + " " + field.get("name", "")))


def sensitive_fact(fact):
    return (
        fact["key"].startswith("self_identification_")
        or bool(SENSITIVE.search(fact["key"]))
        or sensitive_field(fact.get("provenance", {}).get("form_field") or {})
    )


def identity(field):
    # Employer IDs and option order are transport details, not answer meaning.
    # Wording, selection cardinality, choices, and free-form capability must match.
    def normalize(s):
        return re.sub(r"\s+", " ", s.strip()).casefold()

    return (
        "self_identification_"
        + digest(
            [
                normalize(field["label"]),
                field["type"],
                sorted(normalize(o) for o in field.get("options", [])),
                sorted(normalize(o) for o in field.get("free_form_options", [])),
            ]
        )[:28]
    )


def choices(field, value):
    if isinstance(value, dict):
        value = value.get("choices")
    if value in (None, "", LEAVE_BLANK):
        return []
    return value if isinstance(value, list) else [value]


def valid(field, value):
    chosen = choices(field, value)
    if not all(isinstance(v, str) for v in chosen):
        return False
    if len(chosen) != len(set(chosen)) or not set(chosen).issubset(field.options):
        return False
    if field.type != "multiselect" and len(chosen) > 1:
        return False
    if not chosen:
        return not field.required and value in (LEAVE_BLANK, [])
    free = set(chosen) & set(field.free_form_options)
    if free:
        return (
            isinstance(value, dict)
            and set(value) == {"choices", "text"}
            and isinstance(value["text"], str)
            and 0 < len(value["text"].strip()) <= 2000
        )
    return not isinstance(value, dict)


def add_greenhouse_sections(spec, data):
    from .forms import FieldSpec, field_key

    def notice(text):
        if text:
            spec.notices.append(
                BeautifulSoup(html.unescape(text), "html.parser").get_text(
                    " ", strip=True
                )
            )

    def add(name, label, kind, required, options, binding, free=()):
        if (
            kind not in ("multi_value_single_select", "multi_value_multi_select")
            or not options
            or len(options) != len({str(v) for v in options.values()})
        ):
            spec.unsupported.append("Unsupported self-identification control: " + label)
            return
        f = FieldSpec(
            key="pending",
            name=str(name),
            label=label,
            type="select" if kind == "multi_value_single_select" else "multiselect",
            required=required,
            options=list(options),
            purpose="self_identification",
            binding=binding,
            option_ids=options,
            free_form_options=list(free),
        )
        f.key = identity(f.model_dump())
        spec.fields.append(f)

    for section in data.get("compliance") or []:
        notice(section.get("description"))
        for question in section.get("questions", []):
            for raw in question.get("fields", []):
                name = raw["name"]
                if name not in {
                    "gender",
                    "race",
                    "veteran_status",
                    "disability_status",
                }:
                    spec.unsupported.append(
                        "Unrecognized self-identification field: " + question["label"]
                    )
                    continue
                label = {
                    "VeteranStatus": "Veteran Status",
                    "DisabilityStatus": "Disability Status",
                }.get(question["label"], question["label"])
                add(
                    name,
                    label,
                    raw["type"],
                    bool(question.get("required")),
                    {v["label"]: v["value"] for v in raw.get("values", [])},
                    "eeoc",
                )
    demographic = data.get("demographic_questions") or {}
    notice(demographic.get("description"))
    for q in demographic.get("questions", []):
        add(
            q["id"],
            q["label"],
            q["type"],
            bool(q.get("required")),
            {v["label"]: v["id"] for v in q.get("answer_options", [])},
            "demographic",
            [v["label"] for v in q.get("answer_options", []) if v.get("free_form")],
        )
    compliance = data.get("data_compliance") or []
    if isinstance(compliance, dict):
        compliance = [compliance]
    for c in compliance:
        # Different processing/retention terms need their actual displayed text.
        for flag in (
            "requires_consent",
            "requires_processing_consent",
            "requires_retention_consent",
        ):
            if c.get(flag):
                spec.unsupported.append(
                    "Review the employer's processing or retention consent in its browser: "
                    + flag
                )
        if c.get("demographic_data_consent_applies"):
            company = data.get("company_name")
            if not company:
                spec.unsupported.append(
                    "The demographic consent does not identify the employer"
                )
                continue
            name = "gdpr_demographic_data_consent_given"
            label = f"By checking this box, I consent to {company} collecting, storing, and processing my responses to the demographic data surveys above."
            spec.fields.append(
                FieldSpec(
                    key=field_key(name, label, ["I consent"]),
                    name=name,
                    label=label,
                    type="checkbox",
                    options=["I consent"],
                    material=True,
                    purpose="demographic_consent",
                    binding="data_compliance",
                )
            )
