"""Breezy's public application definition, excluding tokens and recruiter settings."""

import json
import re
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from .forms import (
    FieldSpec,
    FormSpec,
    MATERIAL,
    NON_QUALIFICATION,
    field_key,
    non_qualification_field,
)
from .openings import same_company


def definition(body):
    node = BeautifulSoup(body, "html.parser").select_one(
        'script#portal-data[type="application/json"]'
    )
    if not node:
        raise ValueError("The Breezy application definition is unavailable")
    return json.loads(node.get_text())["data"]


def breezy_form(body, url, requisition, employer=None):
    data = definition(body)
    position = data["position"]
    parts = urlparse(url).path.strip("/").split("/")
    if parts != ["p", requisition, "apply"] or position.get("_id") != requisition:
        raise ValueError("The Breezy form does not identify this requisition")
    if position.get("state") != "published":
        raise ValueError("The Breezy opening is no longer published")
    if employer and not same_company(data["company"]["name"], employer):
        raise ValueError("The Breezy employer does not match the application")
    config = position["application_form"]
    spec = FormSpec(adapter="breezy", url=url, requisition=requisition, fields=[])
    basics = [
        ("name", "cName", "Full name", "text", "professional_name"),
        ("email_address", "cEmail", "Email address", "email", "email"),
        ("phone_number", "cPhoneNumber", "Phone number", "tel", "phone"),
        ("address", "cAddress", "Address", "text", None),
        ("resume", "cResume", "Resume", "file", "resume"),
        ("summary", "cSummary", "Summary", "textarea", None),
        ("cover_letter", "cCoverLetter", "Cover letter", "textarea", None),
    ]
    for setting, name, label, kind, key in basics:
        mode = config.get(
            setting, "required" if setting in ("name", "email_address") else "hidden"
        )
        if mode == "hidden":
            continue
        spec.fields.append(
            FieldSpec(
                key=key or field_key(name, label, []),
                name=name,
                label=label,
                type=kind,
                required=mode == "required",
            )
        )
    for setting in (
        "education",
        "work_history",
        "profile_photo",
        "salary",
        "preferred_location",
    ):
        if config.get(setting) == "required":
            spec.unsupported.append(
                "Required Breezy control needs an adapter: " + setting
            )
    if data.get("gdprEnabled") or data.get("eeocEnabled"):
        spec.unsupported.append(
            "Breezy privacy or demographic controls need additional review"
        )
    for section in data.get("questions") or []:
        for index, question in enumerate(section.get("questions", [])):
            name = f"section_{section['_id']}_question_{index}"
            label = BeautifulSoup(question["text"], "html.parser").get_text(
                " ", strip=True
            )
            description = BeautifulSoup(
                question.get("body") or "", "html.parser"
            ).get_text(" ", strip=True)
            if description:
                label += "\n" + description
            if not question.get("_id"):
                spec.unsupported.append("A Breezy question has no stable identity")
            spec.question_ids[name] = str(question.get("_id") or "")
            kind = {
                "text": "text",
                "paragraph": "textarea",
                "dropdown": "select",
                "multiplechoice": "radio",
                "checkboxes": "checkbox",
            }.get((question.get("type") or {}).get("id", "text"))
            if not kind:
                spec.unsupported.append("Unsupported Breezy question: " + label)
                continue
            options = [o["text"] for o in question.get("options", [])]
            consent = (
                kind == "checkbox"
                and len(options) == 1
                and options[0].strip().casefold()
                in ("i understand", "i agree", "i acknowledge")
            )
            field = FieldSpec(
                key=field_key(name, label, options),
                name=name,
                label=label,
                type=kind,
                required=bool(question.get("required")),
                options=options,
                material=consent
                or bool(MATERIAL.search(label))
                or bool(
                    re.search(
                        r"familial|personal relationship|current or former employee",
                        label,
                        re.I,
                    )
                ),
                purpose="consent" if consent else "ordinary",
            )
            field.qualification = (
                field.required
                and not consent
                and not NON_QUALIFICATION.search(label)
                and not re.search(
                    r"contact method|current or former employee|familial|personal relationship|what state|most recent job title",
                    label,
                    re.I,
                )
                and not non_qualification_field(
                    field.model_dump() | {"qualification": None}
                )
            )
            spec.fields.append(field)
    return spec
