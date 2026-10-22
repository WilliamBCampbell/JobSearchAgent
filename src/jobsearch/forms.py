"""Employer form contracts and local answer planning. Inspection sends no candidate data."""

from __future__ import annotations

import json
import re
import httpx
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Literal
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from .config import digest
from .memory import Memory
from .openings import OfficialOpenings
from .workflow import Workflow
from .answer_scopes import allows_global


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    name: str
    label: str
    type: Literal[
        "text",
        "email",
        "tel",
        "url",
        "number",
        "date",
        "textarea",
        "select",
        "radio",
        "checkbox",
        "file",
        "multiselect",
    ]
    required: bool = False
    options: list[str] = Field(default_factory=list)
    material: bool = False
    qualification: bool = False
    purpose: Literal[
        "ordinary", "self_identification", "demographic_consent", "consent"
    ] = "ordinary"
    binding: Literal["standard", "eeoc", "demographic", "data_compliance"] = "standard"
    option_ids: dict[str, str | int] = Field(default_factory=dict)
    free_form_options: list[str] = Field(default_factory=list)


class FormSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter: Literal["lever", "greenhouse", "breezy"]
    version: int = 1
    url: str
    requisition: str
    fields: list[FieldSpec]
    notices: list[str] = Field(default_factory=list)
    unsupported: list[str] = Field(default_factory=list)
    submit_label: str = "Submit application"
    question_ids: dict[str, str] = Field(default_factory=dict)

    @property
    def fingerprint(self):
        contract = self.model_dump()
        if not self.question_ids:
            contract.pop("question_ids")
        return digest(contract)


# Exact contact labels map to profile keys. Custom answers default to application
# scope; explicit global reuse retains their exact wording and options in the key.
CONTACTS = {
    "name": "professional_name",
    "email": "email",
    "phone": "phone",
    "first_name": "first_name",
    "last_name": "last_name",
    "urls[LinkedIn]": "linkedin",
    "urls[GitHub]": "github",
}
MATERIAL = re.compile(
    r"salary|compensation|travel|relocat|sponsor|authoriz|consent|agree|certif|attest|signature|schedule|privacy|background|start date",
    re.I,
)
NON_QUALIFICATION = re.compile(
    r"how did you hear|preferred.*name|salary|compensation|consent|privacy|pronoun|gender|race|veteran|disabilit|cover letter|why.*(?:interested|join)|additional information",
    re.I,
)


def profile_field(field):
    # Use the same exact-label mapping as answer preparation, including custom
    # employer field IDs. A required contact field is not a career qualification.
    from .profile_answers import mapping

    return mapping(field) is not None


def non_qualification_field(field):
    from .form_policy import personal_question, experience_narrative

    return (
        profile_field(field)
        or personal_question(field)
        or experience_narrative(field)
        or field.get("qualification") is False
    )


def field_key(name, label, options):
    aliases = {
        "name": {"name", "full name", "professional name"},
        "email": {"email", "email address", "e-mail"},
        "phone": {"phone", "phone number", "telephone"},
        "first_name": {"first name"},
        "last_name": {"last name"},
        "urls[LinkedIn]": {"linkedin", "linkedin url", "urls[linkedin]"},
        "urls[GitHub]": {"github", "github url", "urls[github]"},
    }
    normalized = re.sub(r"\s+", " ", label.replace("_", " ").strip().casefold())
    if name in CONTACTS and normalized in aliases.get(name, set()) and not options:
        return CONTACTS[name]
    return "application_form_" + digest([name, label, options])[:28]


def lever_form(body: str, url: str, requisition: str) -> FormSpec:
    soup = BeautifulSoup(body, "html.parser")
    form = soup.select_one("form#application-form")
    if not form:
        raise ValueError(
            "The employer application form is unavailable or requires user verification"
        )
    if (
        form.get("action")
        and urljoin(url, form["action"]).split("?")[0] != url.split("?")[0]
    ):
        raise ValueError("Employer form action changed; the adapter needs review")
    if form.get("method", "POST").upper() != "POST":
        raise ValueError("Unexpected employer form method")
    spec = FormSpec(adapter="lever", url=url, requisition=requisition, fields=[])
    cards = {}
    for el in form.select('input[type="hidden"][name$="[baseTemplate]"]'):
        try:
            data = json.loads(el.get("value", "{}"))
            prefix = el["name"].removesuffix("[baseTemplate]")
            for i, field in enumerate(data.get("fields", [])):
                cards[f"{prefix}[field{i}]"] = field
        except (ValueError, KeyError):
            spec.unsupported.append(
                "The employer's custom question definitions could not be read"
            )
    groups = {}
    for el in form.select("input,textarea,select"):
        kind = el.get("type", "text") if el.name == "input" else el.name
        if kind in ("hidden", "submit", "button", "reset"):
            continue
        name = el.get("name", "")
        if not name:
            if el.get("id") != "customPronounsOption":
                spec.unsupported.append("An unnamed control requires manual inspection")
            continue
        groups.setdefault(name, []).append(el)
    for name, els in groups.items():
        el = els[0]
        kind = el.get("type", "text") if el.name == "input" else el.name
        card = cards.get(name, {})
        parent = (
            el.find_parent(class_="application-question")
            or el.find_parent(class_="custom-question")
            or el.find_parent("label")
            or el.parent
        )
        label_el = parent.select_one(".application-label,.text,.card-field-label")
        label = card.get("text") or (
            label_el.get_text(" ", strip=True) if label_el else ""
        )
        if not label and el.get("id"):
            lab = form.find("label", attrs={"for": el["id"]})
            label = lab.get_text(" ", strip=True) if lab else ""
        label = (
            label
            or {
                "name": "Full name",
                "resume": "Resume/CV",
                "email": "Email",
                "phone": "Phone",
                "pronouns": "Pronouns",
            }.get(name)
            or name
        )
        label = re.sub(r"[✱*]\s*$", "", label).strip()
        description = BeautifulSoup(
            card.get("description") or "", "html.parser"
        ).get_text(" ", strip=True)
        if description:
            label += "\n" + description
        required = bool(
            card.get("required")
            or any(e.has_attr("required") for e in els)
            or (label_el and label_el.select_one(".required"))
        )
        # Lever marks resume mandatory in text rather than the input attribute.
        if name == "resume":
            required = True
        if kind == "select":
            options = [
                o.get("value", o.get_text())
                for o in el.select("option")
                if o.get("value", o.get_text())
            ]
        elif kind in ("radio", "checkbox"):
            options = [e.get("value", "on") for e in els if e.get("type") == kind]
        else:
            options = []
        if kind not in FieldSpec.model_fields["type"].annotation.__args__:
            spec.unsupported.append(f"Unsupported control: {label}")
            continue
        if len(els) > 1 and kind not in ("checkbox", "radio"):
            spec.unsupported.append(f"Repeated control needs an adapter: {label}")
        if name == "pronouns":
            # Custom free-text pronouns use the same name; leave this optional group blank.
            spec.unsupported.extend(
                ["Required pronouns control needs manual inspection"]
                if required
                else []
            )
            continue
        material = bool(MATERIAL.search(label))
        spec.fields.append(
            FieldSpec(
                key=field_key(name, label, options),
                name=name,
                label=label,
                type=kind,
                required=required,
                options=options,
                material=material,
                qualification=required
                and name not in CONTACTS
                and kind != "file"
                and not NON_QUALIFICATION.search(label),
            )
        )
    # Preserve all visible policy/attestation text, including implicit agreement on Submit.
    for el in form.select(
        ".application-privacy-policy,.privacy-policy,.application-consent,.consent,.application-submit,.application-footer"
    ):
        text = el.get_text(" ", strip=True)
        if text and text not in spec.notices:
            spec.notices.append(text)
    for text in form.stripped_strings:
        if re.search(
            r"by (?:applying|submitting)|I (?:agree|certify|acknowledge)|data will be processed",
            text,
            re.I,
        ):
            parent = soup.find(string=text)
            notice = parent.parent.get_text(" ", strip=True) if parent else text
            if notice not in spec.notices:
                spec.notices.append(notice)
    for link in form.select("a[href]"):
        text = link.get_text(" ", strip=True)
        if re.search(r"privacy|terms|consent", text, re.I):
            target = urljoin(url, link["href"])
            if urlparse(target).scheme == "https":
                notice = text + ": " + target
                if notice not in spec.notices:
                    spec.notices.append(notice)
    button = form.select_one("#btn-submit")
    if not button:
        spec.unsupported.append("The reviewed final action is missing")
    elif button.get_text(" ", strip=True) != spec.submit_label:
        spec.unsupported.append("The employer changed the final action")
    if not {"name", "email", "resume"}.issubset({f.name for f in spec.fields}):
        spec.unsupported.append(
            "The standard name, email, or resume control is missing"
        )
    for field in spec.fields:
        if non_qualification_field(field.model_dump()):
            field.qualification = False
    return spec


def greenhouse_form(data, url, requisition):
    spec = FormSpec(
        adapter="greenhouse",
        url=url,
        requisition=requisition,
        fields=[],
    )
    for question in data.get("questions", []):
        label = question["label"]
        fields = question.get("fields", [])
        has_file = any(f.get("type") == "input_file" for f in fields)
        for field in fields:
            name, kind = field["name"], field["type"]
            if has_file and name in ("resume_text", "cover_letter_text"):
                continue
            mapping = {
                "input_text": "text",
                "textarea": "textarea",
                "input_file": "file",
                "multi_value_single_select": "select",
                "multi_value_multi_select": "checkbox",
            }
            if kind not in mapping:
                spec.unsupported.append("Unsupported control: " + label)
                continue
            options = [str(v["label"]) for v in field.get("values", [])]
            required = bool(question.get("required"))
            parsed = FieldSpec(
                key=field_key(name, label, options),
                name=name,
                label=label,
                type=mapping[kind],
                required=required,
                options=options,
                material=bool(MATERIAL.search(label)),
                qualification=required
                and name not in CONTACTS
                and mapping[kind] != "file"
                and not NON_QUALIFICATION.search(label),
            )
            if non_qualification_field(parsed.model_dump()):
                parsed.qualification = False
            from .self_identification import sensitive_field, identity

            if (
                sensitive_field(parsed.model_dump())
                and parsed.type in ("select", "checkbox")
                and not parsed.material
            ):
                parsed.purpose = "self_identification"
                if parsed.type == "checkbox":
                    parsed.type = "multiselect"
                parsed.option_ids = {
                    str(v["label"]): v["value"] for v in field.get("values", [])
                }
                parsed.key = identity(parsed.model_dump())
                parsed.qualification = False
            spec.fields.append(parsed)
    from .self_identification import add_greenhouse_sections

    add_greenhouse_sections(spec, data)
    if data.get("education") not in (None, "education_optional", "education_hidden"):
        spec.unsupported.append("Required education controls need confirmed answers")
    if data.get("location_questions"):
        spec.unsupported.append("Location controls need an additional browser adapter")
    for question in data.get("questions", []):
        if question.get("description"):
            spec.notices.append(
                question["label"]
                + ": "
                + BeautifulSoup(question["description"], "html.parser").get_text(
                    " ", strip=True
                )
            )
    if data.get("include_ai_disclaimer") and data.get("ai_disclaimer"):
        spec.notices.append(
            BeautifulSoup(data["ai_disclaimer"], "html.parser").get_text(
                " ", strip=True
            )
        )
    return spec


class FormPreparation:
    def __init__(self, db):
        self.db = db
        self.flow = Workflow(db)

    def inspect(self, app):
        url = app["official"].get("application_url", "")
        u = urlparse(url)
        service = OfficialOpenings(self.db)
        if app["candidate"] == "fixture" and app["payload"].get(
            "scenario", ""
        ).startswith("lever"):
            import httpx

            url = self.db.settings.origin + "/fixture/lever/" + app["id"] + "/apply"
            body = httpx.get(
                url, headers={"X-JSA-Token": self.db.settings.token()}, timeout=15
            ).text
            return lever_form(body, url, app["requisition"])
        if app["candidate"] == "fixture" and app["payload"].get(
            "scenario", ""
        ).startswith("greenhouse"):
            from .greenhouse_fixture import definition

            url = self.db.settings.origin + "/fixture/greenhouse/" + app["id"]
            return greenhouse_form(
                definition(app["payload"].get("scenario")), url, app["requisition"]
            )
        if (
            not app["official"].get("open")
            or app["official"].get("status") != "verified"
        ):
            raise ValueError(
                "Verify the official opening before inspecting its application"
            )
        if u.hostname in ("jobs.lever.co", "jobs.eu.lever.co"):
            parts = u.path.strip("/").split("/")
            if len(parts) < 2 or parts[1] != app["requisition"]:
                raise ValueError(
                    "The official route does not identify this requisition"
                )
            url = f"https://{u.hostname}/{parts[0]}/{parts[1]}/apply"
            final, body = service.fetch(url)
            if final != url:
                raise ValueError(
                    "The application route redirected; verify the opening again"
                )
            spec = lever_form(body.decode(), url, app["requisition"])
        elif u.hostname in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
            parts = u.path.strip("/").split("/")
            if len(parts) != 3 or parts[1] != "jobs" or not parts[2].isdigit():
                raise ValueError("The Greenhouse application route is unsupported")
            data = service.get_json(
                f"https://boards-api.greenhouse.io/v1/boards/{parts[0]}/jobs/{parts[2]}?questions=true&demographic_questions=true"
            )
            if str(data.get("requisition_id") or data.get("id")) != app["requisition"]:
                raise ValueError(
                    "The form requisition does not match the verified opening"
                )
            spec = greenhouse_form(data, url, app["requisition"])
        elif u.hostname and u.hostname.endswith(".breezy.hr"):
            from .breezy import breezy_form

            parts = u.path.strip("/").split("/")
            if parts not in (
                ["p", app["requisition"]],
                ["p", app["requisition"], "apply"],
            ):
                raise ValueError("The Breezy route does not identify this requisition")
            url = f"https://{u.hostname}/p/{app['requisition']}/apply"
            final, body = service.fetch(url)
            if final != url:
                raise ValueError(
                    "The Breezy application redirected; verify the opening again"
                )
            spec = breezy_form(body, url, app["requisition"], app["employer"])
        else:
            raise ValueError(
                "This employer portal needs an application adapter. A normal-browser handoff is available"
            )
        # Raw responses remain private; normalized contracts contain no hidden tokens.
        self.db.audit(
            "form.inspected",
            app["id"],
            {
                "adapter": spec.adapter,
                "fingerprint": spec.fingerprint,
                "field_count": len(spec.fields),
            },
        )
        return spec

    def plan(self, aid, job_id=None, progress=lambda _: None, cancelled=lambda: False):
        app = self.flow.get(aid)
        self.flow.editable(app)
        if app["state"] not in (
            "prepared",
            "missing_answers",
            "review",
            "filling",
            "awaiting_challenge",
            "manual_handoff",
        ):
            raise ValueError(
                "Prepare the application package before inspecting its form"
            )
        if app["paused"] or self.db.one(
            "SELECT 1 FROM attempts WHERE application_id=%s", (aid,)
        ):
            raise ValueError(
                "Resume paused work or reconcile the previous attempt before filling"
            )
        if app["state"] != "filling":
            self.flow.transition(aid, "filling")
        try:
            spec = self.inspect(app)
        except httpx.HTTPError:
            return self.handoff(
                aid,
                "The employer form could not be read. Inspect the official route in your normal browser",
            )
        except (ValueError, OSError) as exc:
            return self.handoff(aid, str(exc))
        return self.plan_spec(aid, spec, job_id, progress, cancelled)

    def plan_spec(
        self, aid, spec, job_id=None, progress=lambda _: None, cancelled=lambda: False
    ):
        from .form_answers import FormAnswers
        from .profile_answers import ProfileAnswers
        from .self_identification import sensitive_field

        app = self.flow.get(aid)
        answers, missing, consents = {}, [], {"employer_notices": spec.notices}
        assistance = FormAnswers(self.db)
        saved_questions = {
            q["key"]: q
            for q in self.db.all(
                "SELECT key,reason,needs_review FROM questions WHERE application_id=%s",
                (aid,),
            )
        }
        corrections = {key for key, q in saved_questions.items() if q["needs_review"]}
        resolutions, unknown = {}, []
        for field in spec.fields:
            if field.type == "file":
                continue
            answer = Memory(self.db).resolve(
                field.key, app["candidate"], aid, query=field.label, retrieve=False
            )
            explicit = sensitive_field(field.model_dump()) or field.purpose in (
                "demographic_consent",
                "consent",
            )
            if explicit:
                facts = [
                    self.db.one(
                        "SELECT key,value,provenance FROM facts WHERE id=%s", (fid,)
                    )
                    for fid in answer.get("fact_ids", [])
                ]
                if not facts or any(
                    not f
                    or f["provenance"].get("type") != "user_confirmation"
                    or f["key"] != field.key
                    or f["value"] != answer.get("value")
                    for f in facts
                ):
                    answer = {
                        "source": "question",
                        "reason": {
                            "code": "explicit_choice_needed",
                            "message": (
                                "Choose whether you consent to this employer receiving your survey responses. This consent applies only to this application."
                                if field.purpose == "demographic_consent"
                                else "Choose your answer"
                                + (
                                    "."
                                    if field.required
                                    else ", or leave this optional question blank."
                                )
                                + " Save it globally to reuse it when another application asks the same question with the same choices."
                            ),
                        },
                    }
            if (
                not explicit
                and answer.get("source") == "question"
                and not answer.get("conflicting_facts")
                and field.key not in corrections
            ):
                answer = (
                    ProfileAnswers(self.db).resolve(app, field.model_dump()) or answer
                )
            if (
                not explicit
                and answer.get("source") == "question"
                and not answer.get("conflicting_facts")
                and field.key not in corrections
            ):
                previous = app["payload"].get("answers", {}).get(field.key, {})
                reason = saved_questions.get(field.key, {}).get("reason", {})
                if assistance.valid(app, field.model_dump(), previous):
                    answer = previous
                    self.db.metric("form_answer_evidence_reused")
                elif assistance.valid(
                    app, field.model_dump(), reason.get("suggestion", {}), draft=True
                ):
                    answer = {"source": "question", "reason": reason}
                elif field.required:
                    unknown.append(field.model_dump())
            resolutions[field.key] = answer
        resolutions.update(
            assistance.prepare(app, unknown, job_id, progress, cancelled)
        )
        for field in spec.fields:
            if field.type == "file":
                continue
            answer = resolutions[field.key]
            if (
                answer.get("source") == "question"
                or not valid_value(field, answer.get("value"))
                or (
                    answer.get("scope") == "global"
                    and not allows_global(field.key, field.model_dump())
                )
            ):
                if field.required or field.purpose != "ordinary":
                    question = self.flow.ask(aid, field.key, field.label, answer)
                    if question["answer_fact"]:
                        self.db.execute(
                            "UPDATE questions SET needs_review=true WHERE id=%s",
                            (question["id"],),
                        )
                    missing.append(field.key)
                continue
            answers[field.key] = answer
            if field.material:
                consents[field.label] = answer["value"]
        from .self_identification import choices

        demographic_selected = any(
            f.binding == "demographic"
            and choices(f, answers.get(f.key, {}).get("value"))
            for f in spec.fields
        )
        if not demographic_selected:
            self.db.execute(
                "UPDATE questions SET needs_review=false WHERE application_id=%s AND key=ANY(%s) AND reason->>'code'='survey_consent_needed'",
                (
                    aid,
                    [f.key for f in spec.fields if f.purpose == "demographic_consent"],
                ),
            )
        for f in spec.fields:
            if (
                f.purpose == "demographic_consent"
                and demographic_selected
                and f.key in answers
                and not answers[f.key]["value"]
            ):
                q = self.flow.ask(
                    aid,
                    f.key,
                    f.label,
                    {
                        "reason": {
                            "code": "survey_consent_needed",
                            "message": "The employer requires consent to receive your survey answers. Consent for this application, or change the optional survey answers to leave them blank.",
                        }
                    },
                )
                self.db.execute(
                    "UPDATE questions SET needs_review=true WHERE id=%s", (q["id"],)
                )
                missing.append(f.key)
        self.db.execute(
            "UPDATE questions SET needs_review=false WHERE application_id=%s AND needs_review AND NOT (key=ANY(%s))",
            (aid, [f.key for f in spec.fields]),
        )
        pending_corrections = self.db.all(
            "SELECT key FROM questions WHERE application_id=%s AND needs_review", (aid,)
        )
        for question in pending_corrections:
            if question["key"] not in missing:
                missing.append(question["key"])
        schema = [f.model_dump() for f in spec.fields]
        plan = {
            "spec": spec.model_dump(),
            "fingerprint": spec.fingerprint,
            "inspected_at": datetime.now(timezone.utc).isoformat(),
        }
        if spec.adapter in ("greenhouse", "breezy"):
            accepted = {
                (f.name if spec.adapter == "greenhouse" else f.key) + ".pdf"
                for f in spec.fields
                if f.type == "file"
            }
            selected = []
            for artifact_id in app["payload"].get("attachments", []):
                artifact = self.db.one(
                    "SELECT id,path,kind FROM artifacts WHERE id=%s AND application_id=%s",
                    (artifact_id, aid),
                )
                if not artifact:
                    raise ValueError("A selected document is no longer available")
                selected.append({**artifact, "name": Path(artifact["path"]).name})
            omitted = [a for a in selected if a["kind"] not in accepted]
            if omitted:
                plan["omitted_attachments"] = [
                    {
                        "name": a["name"],
                        "kind": a["kind"],
                        "reason": "The employer form has no upload field for this document.",
                    }
                    for a in omitted
                ]
                self.flow.update_payload(
                    aid,
                    {
                        "attachments": [
                            a["id"] for a in selected if a["kind"] in accepted
                        ]
                    },
                )
        self.flow.update_payload(
            aid,
            {
                "form_plan": plan,
                "form_schema": schema,
                "answers": answers,
                "consents": consents,
                "browser_hash": None,
            },
        )
        # Retire unanswered controls removed by an employer edit; never copy their values.
        self.db.execute(
            "DELETE FROM questions WHERE application_id=%s AND answer_fact IS NULL AND NOT (key=ANY(%s))",
            (aid, missing),
        )
        if missing:
            handoff = self.flow.require_answers(aid)
            return {
                "state": "missing_answers",
                "questions": missing,
                "adapter": spec.adapter,
                "handoff": handoff,
            }
        if spec.unsupported:
            return self.handoff(aid, "; ".join(spec.unsupported))
        # New required qualifications must pass the existing score gate before review.
        introduced = [
            f
            for f in spec.fields
            if f.qualification
            and not any(
                r["text"] == f.label for r in app["score"].get("requirements", [])
            )
        ]
        stale = any(
            r.get("fact_ids")
            and not Memory(self.db)._valid({"fact_ids": r["fact_ids"]})
            for r in app["score"].get("requirements", [])
        )
        if introduced or stale:
            from .scoring import Assessment, Requirement

            score = app["score"]
            requirements = [Requirement(**r) for r in score["requirements"]]
            for requirement in requirements:
                if requirement.fact_ids and not Memory(self.db)._valid(
                    {"fact_ids": requirement.fact_ids}
                ):
                    requirement.assessment = "unconfirmed"
                    requirement.fact_ids = []
                    requirement.reason = (
                        "Candidate evidence changed; reevaluation is required"
                    )
            for f in introduced:
                requirements.append(
                    Requirement(
                        text=f.label,
                        source=spec.url,
                        category="technical",
                        classification="required",
                        central=True,
                        assessment="unconfirmed",
                        deduction=0,
                        reason="The application form introduced this requirement; reevaluate against confirmed answers before approval.",
                    )
                )
            self.flow.score(
                aid,
                Assessment(
                    track=score["track"],
                    requirements=requirements,
                    ohio=score["ohio"],
                    compensation=score["compensation"],
                ),
            )
            return {
                "state": "scored",
                "reevaluate": True,
                "message": "The employer form introduced required qualifications. Fit must be reevaluated.",
            }
        try:
            self.validate_ready(self.flow.get(aid))
        except ValueError as exc:
            from .progress import opening_current

            current = self.flow.get(aid)
            if (
                not opening_current(current["official"])
                or not current["score"].get("primary")
                or current["score"].get("evidence_changed")
            ):
                self.flow.transition(aid, "scored")
                return {"state": "scored", "blocked_on": "fit", "message": str(exc)}
            return self.handoff(aid, str(exc))
        review = self.flow.review(aid)
        self.flow.update_payload(
            aid, {"browser_hash": review["hash"] if spec.adapter == "lever" else None}
        )
        self.flow.transition(aid, "review")
        return {
            "state": "review",
            "hash": review["hash"],
            "adapter": spec.adapter,
            "local_plan": True,
        }

    def validate_ready(self, app):
        plan = app["payload"].get("form_plan", {})
        spec = FormSpec.model_validate(plan.get("spec", {}))
        if (
            spec.fingerprint != plan.get("fingerprint")
            or spec.requisition != app["requisition"]
            or spec.unsupported
        ):
            raise ValueError("Employer form plan is incomplete or changed")
        if app["candidate"] != "fixture":
            u = urlparse(spec.url)
            original = urlparse(app["official"].get("application_url", ""))
            parts = u.path.strip("/").split("/")
            lever_route = (
                spec.adapter == "lever"
                and u.hostname in ("jobs.lever.co", "jobs.eu.lever.co")
                and len(parts) == 3
                and parts[1] == app["requisition"]
                and parts[2] == "apply"
                and original.path.strip("/").split("/")[:2] == parts[:2]
            )
            greenhouse_route = (
                spec.adapter == "greenhouse"
                and u.hostname in ("boards.greenhouse.io", "job-boards.greenhouse.io")
                and len(parts) == 3
                and parts[1] == "jobs"
                and parts[2].isdigit()
                and original.path == u.path
            )
            breezy_route = (
                spec.adapter == "breezy"
                and u.hostname
                and u.hostname.endswith(".breezy.hr")
                and parts == ["p", app["requisition"], "apply"]
                and original.path.strip("/").split("/")[:2] == parts[:2]
            )
            if (
                u.scheme != "https"
                or u.port not in (None, 443)
                or u.username
                or u.query
                or u.fragment
                or original.hostname != u.hostname
                or not (lever_route or greenhouse_route or breezy_route)
            ):
                raise ValueError(
                    "The planned route does not match the official employer requisition"
                )
            from .verification import public_url

            public_url(spec.url)
        keys = [f.key for f in spec.fields]
        if len(keys) != len(set(keys)):
            raise ValueError("Ambiguous employer fields need manual inspection")
        answers = app["payload"].get("answers", {})
        if set(answers) - set(keys):
            raise ValueError(
                "The answer plan includes controls outside the reviewed form"
            )
        expected_consents = {"employer_notices": spec.notices}
        for field in spec.fields:
            if field.material and field.key in answers:
                expected_consents[field.label] = answers[field.key]["value"]
        if app["payload"].get("consents") != expected_consents:
            raise ValueError("Employer consents changed; prepare the answer plan again")
        for field in spec.fields:
            if (
                field.type != "file"
                and (field.required or field.purpose != "ordinary")
                and (
                    field.key not in answers
                    or not valid_value(field, answers[field.key]["value"])
                )
            ):
                raise ValueError(
                    "A required employer answer is missing or invalid: " + field.label
                )
        from .self_identification import choices

        for field in spec.fields:
            if field.purpose != "ordinary":
                facts = [
                    self.db.one(
                        "SELECT key,value,provenance FROM facts WHERE id=%s", (fid,)
                    )
                    for fid in answers.get(field.key, {}).get("fact_ids", [])
                ]
                if not facts or any(
                    not f
                    or f["provenance"].get("type") != "user_confirmation"
                    or f["key"] != field.key
                    or f["value"] != answers[field.key]["value"]
                    for f in facts
                ):
                    raise ValueError(
                        "An explicit user choice is required: " + field.label
                    )
            if field.purpose == "demographic_consent" and not answers.get(
                field.key, {}
            ).get("value"):
                if any(
                    f.binding == "demographic"
                    and choices(f, answers.get(f.key, {}).get("value"))
                    for f in spec.fields
                ):
                    raise ValueError(
                        "Consent to sending the survey answers, or leave those questions blank"
                    )
        selected = self.flow.review_payload(app)["attachments"]
        files = [f for f in spec.fields if f.type == "file"]
        if spec.adapter == "lever" and (
            len(files) != 1
            or files[0].name != "resume"
            or len(selected) != 1
            or selected[0]["kind"] != "resume.pdf"
        ):
            raise ValueError(
                "This adapter requires exactly one reviewed PDF resume and one resume upload control"
            )
        if spec.adapter == "breezy":
            if (
                len(files) != 1
                or files[0].key != "resume"
                or len(selected) != 1
                or selected[0]["kind"] != "resume.pdf"
            ):
                raise ValueError("Select one PDF resume for this Breezy form")
        if spec.adapter == "greenhouse":
            names = {f.name for f in files}
            if names - {"resume", "cover_letter"}:
                raise ValueError("This form has an unsupported custom document upload")
            kinds = [a["kind"] for a in selected]
            if len(kinds) != len(set(kinds)) or set(kinds) - {
                n + ".pdf" for n in names
            }:
                raise ValueError("Choose one PDF per available employer upload field")
            if any(f.required and f.name + ".pdf" not in kinds for f in files):
                raise ValueError("Select the PDF required by the employer form")
        if any(
            r.get("fact_ids")
            and not Memory(self.db)._valid({"fact_ids": r["fact_ids"]})
            for r in app["score"].get("requirements", [])
        ):
            raise ValueError(
                "The fit assessment uses corrected evidence; reevaluate before approval"
            )
        try:
            verified_at = datetime.fromisoformat(
                app["official"].get("verified_at", "").replace("Z", "+00:00")
            )
        except ValueError:
            raise ValueError(
                "Verify the official opening again before approval"
            ) from None
        if verified_at.tzinfo is None or verified_at <= datetime.now(
            timezone.utc
        ) - timedelta(hours=24):
            raise ValueError(
                "Official verification is more than 24 hours old; verify the opening again before approval"
            )
        if not app["score"].get("primary"):
            raise ValueError(
                "This application does not pass the primary recommendation gates"
            )
        if app["score"].get("evidence_changed"):
            raise ValueError("The official posting changed; rescore before continuing")
        return spec

    def handoff(self, aid, reason):
        from .browser import BrowserWorker

        self.flow.transition(aid, "manual_handoff")
        from .db import js
        from .tracker import Tracker

        app = self.flow.get(aid)
        self.db.execute(
            "INSERT INTO followups(id,application_id,candidate,category,detail) VALUES(%s,%s,%s,'manual_application',%s) ON CONFLICT(id) DO UPDATE SET detail=EXCLUDED.detail,resolved=false",
            (
                digest([aid, "manual_application"]),
                aid,
                app["candidate"],
                js(
                    {
                        "reason": reason,
                        "receipt": "not_attempted",
                        "next_step": "Complete the saved plan in a normal browser; record the employer receipt here",
                    }
                ),
            ),
        )
        Tracker(self.db).manual_report(app["candidate"])
        return BrowserWorker(self.db).handoff(aid, reason)


def valid_value(field, value):
    if field.purpose == "self_identification":
        from .self_identification import valid

        return valid(field, value)
    if field.type == "checkbox":
        return (
            isinstance(value, list)
            and set(value).issubset(field.options)
            and (bool(value) or not field.required)
        )
    if field.options:
        return isinstance(value, str) and value in field.options
    return (
        isinstance(value, (str, int, float))
        and not isinstance(value, bool)
        and bool(str(value).strip())
    )
