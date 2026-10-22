"""Map known profile facts to exact employer fields without model inference."""

import re

from .config import digest
from .memory import Memory


PROFILE_LABELS = {
    "linkedin": {
        "linkedin",
        "linkedin url",
        "linkedin profile",
        "linkedin profile url",
        "linkedin profile link",
        "your linkedin profile",
        "linkedin: if you have no linkedin page type n/a",
    },
    "github": {
        "github",
        "github url",
        "github profile",
        "github profile url",
        "github: if you have no github page type n/a",
    },
    "state": {"state", "state of residence", "what state are you located in"},
    "website": {"personal website", "personal website url"},
    "email": {"email", "email address", "e-mail", "e-mail address"},
    "phone": {"phone", "phone number", "telephone", "telephone number"},
    "professional_name": {"name", "full name", "professional name"},
    "first_name": {"first name"},
    "last_name": {"last name"},
}
REFERRAL_STATUS = re.compile(
    r"(?:were you referred by (?:an? |one of our )?(?:employee|team member)|"
    r"have you been referred by (?:an? |one of our )?(?:employee|team member)|"
    r"are you an employee referral)"
)
REFERRER_NAME = re.compile(
    r"(?:employee referral name|referrer(?:'s)? name|name of the employee who referred you|"
    r"if one of our [\w .&'-]{1,80} team members referred you to this role, please let us know who)"
)


def mapping(field):
    if field.get("type") not in (
        "text",
        "textarea",
        "email",
        "tel",
        "url",
        "select",
        "radio",
    ):
        return None
    label = re.sub(r"\s+", " ", field["label"].strip().casefold()).rstrip("?:* ")
    label = re.sub(r"\s*\(optional\)$", "", label)
    for key, labels in PROFILE_LABELS.items():
        if (
            label in labels
            and not field.get("material")
            and (
                not field.get("options")
                or (key == "state" and field.get("type") == "select")
            )
        ):
            return key, "identity"
    if not field.get("material"):
        if REFERRAL_STATUS.fullmatch(label):
            return "agent_employee_referral", "referral_status"
        if REFERRER_NAME.fullmatch(label) and not field.get("options"):
            return "agent_employee_referral", "referrer_name"
    return None


class ProfileAnswers:
    def __init__(self, db):
        self.db = db

    def resolve(self, app, field):
        selected = mapping(field)
        if not selected:
            return None
        key, transform = selected
        # This preference applies to applications prepared through this app,
        # never to historical tracker imports or other candidates.
        if key == "agent_employee_referral" and app.get("imported"):
            return None
        answer = Memory(self.db).resolve(
            key, app["candidate"], app["id"], retrieve=False
        )
        if answer.get("source") == "question":
            return answer
        value = answer["value"]
        if transform != "identity":
            if value is not False:
                return None  # A real referral needs its own confirmed identity.
            value = "No" if transform == "referral_status" else "Not applicable"
            if field.get("options"):
                choices = [o for o in field["options"] if o.casefold().strip() == "no"]
                if len(choices) != 1:
                    return None
                value = choices[0]
        from .forms import FieldSpec, valid_value

        if not valid_value(FieldSpec(**field), value):
            return None
        return {
            "source": "profile_memory",
            "scope": app["id"],
            "value": value,
            "fact_ids": answer["fact_ids"],
            "version_ids": answer["version_ids"],
            "provenance": {
                "type": "profile_memory",
                "profile_key": key,
                "field_hash": digest(field),
                "transform": transform,
                "evidence": answer["provenance"],
            },
        }

    def valid(self, app, field, answer):
        expected = self.resolve(app, field)
        return bool(
            expected
            and expected.get("source") == "profile_memory"
            and expected == answer
        )
