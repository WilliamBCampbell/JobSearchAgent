# ruff: noqa: F811
"""Synthetic survey choices, reusable memory, transport, and visible form controls."""

import copy
import json
import pytest

from jobsearch.answer_scopes import allows_global
from jobsearch.forms import FormPreparation, greenhouse_form, valid_value
from jobsearch.greenhouse_fixture import definition
from jobsearch.greenhouse_transport import validate_application
from jobsearch.self_identification import LEAVE_BLANK
from jobsearch.memory import Memory, FactInput
from jobsearch.llm.service import InferenceService
from jobsearch.llm.contracts import InferenceRequest
from jobsearch.browser import BrowserWorker
from jobsearch.workflow import ApprovalInput
from .test_greenhouse_browser import prepared
from .test_browser import fixture_server  # noqa: F401


def survey():
    return greenhouse_form(definition("greenhouse_demographics"), "", "GH-1")


def save_choices(db, flow, aid, global_answers=False, self_describe=False):
    result = FormPreparation(db).plan(aid)
    assert result["state"] == "missing_answers", result
    fields = flow.get(aid)["payload"]["form_schema"]
    values = {
        "gender": "Decline To Self Identify",
        "race": "Asian",
        "veteran_status": "I don't wish to answer",
        "8001": {
            "choices": ["Non-binary", "I prefer to self-describe"],
            "text": "Synthetic self-description",
        }
        if self_describe
        else ["Non-binary"],
        "8002": LEAVE_BLANK,
        "gdpr_demographic_data_consent_given": ["I consent"],
    }
    for q in db.all(
        "SELECT * FROM questions WHERE application_id=%s AND (answer_fact IS NULL OR needs_review)",
        (aid,),
    ):
        field = next(f for f in fields if f["key"] == q["key"])
        flow.answer(
            q["id"],
            values[field["name"]],
            "global"
            if global_answers and field["purpose"] == "self_identification"
            else "application",
        )
    # Worker jobs are exercised explicitly by these tests.
    db.execute("UPDATE jobs SET status='cancelled' WHERE status='queued'")
    return values


def test_survey_identity_and_scopes():
    original = survey()
    assert not original.unsupported
    data = definition("greenhouse_demographics")
    data["demographic_questions"]["questions"][0]["id"] = 123456
    for option in data["demographic_questions"]["questions"][0]["answer_options"]:
        option["id"] += 100000
    data["demographic_questions"]["questions"][0]["answer_options"].reverse()
    changed = greenhouse_form(data, "", "OTHER")
    old = next(f for f in original.fields if f.name == "8001")
    new = next(f for f in changed.fields if f.name == "123456")
    assert old.key == new.key and original.fingerprint != changed.fingerprint
    assert allows_global(new.key, new.model_dump())
    data["demographic_questions"]["questions"][0]["label"] += " for this survey"
    assert (
        next(
            f for f in greenhouse_form(data, "", "OTHER").fields if f.name == "123456"
        ).key
        != old.key
    )
    for f in original.fields:
        if f.purpose == "demographic_consent":
            assert not allows_global(f.key, f.model_dump())
        if f.purpose == "self_identification":
            assert valid_value(f, LEAVE_BLANK)
            assert not valid_value(f, "Invented option")
    assert not valid_value(old, {"choices": ["I prefer to self-describe"], "text": ""})
    assert valid_value(
        old, {"choices": ["I prefer to self-describe"], "text": "My description"}
    )


def test_answers_reuse_without_inference_and_correction_invalidates(db, monkeypatch):
    flow, aid = prepared(db, "greenhouse_demographics")
    from jobsearch.form_answers import FormAnswers

    def no_inference(self, app, fields, *args):
        assert not fields, "Survey answers must not be inferred"
        return {}

    monkeypatch.setattr(FormAnswers, "prepare", no_inference)
    from jobsearch.corpus import Corpus

    def no_retrieval(*args, **kwargs):
        raise AssertionError("Self-identification lookup must not call RAG")

    monkeypatch.setattr(Corpus, "retrieve", no_retrieval)
    save_choices(db, flow, aid, global_answers=True)
    assert FormPreparation(db).plan(aid)["state"] == "review"
    f = next(f for f in survey().fields if f.name == "8001")
    assert Memory(db).resolve(f.key, "fixture", "another-role", retrieve=False)[
        "value"
    ] == ["Non-binary"]
    old = Memory(db).resolve(f.key, "fixture", aid, retrieve=False)
    q = db.one(
        "SELECT * FROM questions WHERE application_id=%s AND key=%s", (aid, f.key)
    )
    flow.answer(q["id"], ["Man"], "global")
    assert not Memory(db)._valid(old)
    assert Memory(db).resolve(f.key, "fixture", "another-role", retrieve=False)[
        "value"
    ] == ["Man"]
    # A new application with changed portal IDs uses the same confirmed answer.
    other = flow.create("Other Example", "Engineer", "OTHER")["id"]
    assert (
        Memory(db).resolve(f.key, "fixture", other, retrieve=False)["source"]
        != "question"
    )
    consent = next(f for f in survey().fields if f.purpose == "demographic_consent")
    assert (
        Memory(db).resolve(consent.key, "fixture", other, retrieve=False)["source"]
        == "question"
    )


def test_demographics_never_in_model_packets(db):
    flow, aid = prepared(db, "greenhouse_demographics")
    save_choices(db, flow, aid, global_answers=True)
    from jobsearch.config import atomic_write, digest
    from jobsearch.db import js

    body = b"<h1>Example engineer</h1><p>Build APIs.</p>"
    path = db.settings.state / "official-captures" / "survey-test.html"
    atomic_write(path, body)
    official = flow.get(aid)["official"] | {
        "capture_path": str(path),
        "source_hash": digest(body),
    }
    db.execute("UPDATE applications SET official=%s WHERE id=%s", (js(official), aid))
    ordinary = next(f for f in survey().fields if f.name == "question_103")
    for task in ("fit_assessment", "package_draft", "form_answers"):
        req = InferenceRequest(
            task=task,
            candidate="fixture",
            application_id=aid,
            form_fields=[ordinary.model_dump()] if task == "form_answers" else [],
        )
        packet = InferenceService(db).context(req)
        assert all(
            not f["key"].startswith("self_identification_") for f in packet["facts"]
        )
    f = next(f for f in survey().fields if f.name == "8001")
    with pytest.raises(ValueError, match="explicit choice"):
        InferenceService(db).context(
            InferenceRequest(
                task="form_answers",
                candidate="fixture",
                application_id=aid,
                form_fields=[f.model_dump()],
            )
        )


def test_imported_claim_cannot_answer_survey_and_consent_is_explicit(db):
    flow, aid = prepared(db, "greenhouse_demographics")
    f = next(f for f in survey().fields if f.name == "gender")
    Memory(db).put(
        FactInput(
            kind="answer",
            key=f.key,
            value="Male",
            scope=aid,
            status="confirmed",
            provenance={"type": "explicit_record"},
        )
    )
    result = FormPreparation(db).plan(aid)
    assert f.key in result["questions"]
    save_choices(db, flow, aid)
    consent = next(f for f in survey().fields if f.purpose == "demographic_consent")
    q = db.one(
        "SELECT * FROM questions WHERE application_id=%s AND key=%s", (aid, consent.key)
    )
    with pytest.raises(ValueError, match="application scope"):
        flow.answer(q["id"], ["I consent"], "global")
    flow.answer(q["id"], [])
    assert consent.key in FormPreparation(db).plan(aid)["questions"]
    q = db.one("SELECT * FROM questions WHERE id=%s", (q["id"],))
    assert q["needs_review"] and q["reason"]["code"] == "survey_consent_needed"
    survey_field = next(f for f in survey().fields if f.name == "8001")
    question = db.one(
        "SELECT id FROM questions WHERE application_id=%s AND key=%s",
        (aid, survey_field.key),
    )
    flow.answer(question["id"], [])
    assert FormPreparation(db).plan(aid)["state"] == "review"
    assert not db.one("SELECT needs_review FROM questions WHERE id=%s", (q["id"],))[
        "needs_review"
    ]


def native_payload():
    spec = survey().model_copy(
        update={"fields": [f for f in survey().fields if f.purpose != "ordinary"]}
    )
    values = {
        "gender": "Male",
        "race": "Asian",
        "veteran_status": LEAVE_BLANK,
        "8001": {
            "choices": ["Non-binary", "I prefer to self-describe"],
            "text": "Synthetic description",
        },
        "8002": LEAVE_BLANK,
        "gdpr_demographic_data_consent_given": ["I consent"],
    }
    payload = {
        "answers": {f.key: {"value": values[f.name]} for f in spec.fields},
        "attachments": [],
    }
    outgoing = {
        "job_application": {
            "gender": "1",
            "race": "2",
            "demographic_answers": [
                {
                    "question_id": 8001,
                    "answer_options": [
                        {"answer_option_id": "9002"},
                        {"answer_option_id": "9004", "text": "Synthetic description"},
                    ],
                }
            ],
            "data_compliance": {"gdpr_demographic_data_consent_given": True},
        }
    }
    return spec, payload, outgoing


def test_native_transport_binds_exact_choices_text_consent_and_ids():
    spec, payload, outgoing = native_payload()
    assert validate_application(json.dumps(outgoing), spec, payload, {}, {})
    for change in ("choice", "extra", "consent", "text", "duplicate", "race"):
        edited = copy.deepcopy(outgoing)
        job = edited["job_application"]
        if change == "choice":
            job["demographic_answers"][0]["answer_options"][0]["answer_option_id"] = (
                "9003"
            )
        if change == "extra":
            job["data_compliance"]["gdpr_consent_given"] = True
        if change == "consent":
            job["data_compliance"]["gdpr_demographic_data_consent_given"] = False
        if change == "text":
            job["demographic_answers"][0]["answer_options"][1]["text"] = "Changed"
        if change == "duplicate":
            job["demographic_answers"] *= 2
        if change == "race":
            job["race"] = "5"
        assert not validate_application(json.dumps(edited), spec, payload, {}, {}), (
            change
        )


async def test_demographic_browser_review_and_approved_fixture_submission(
    db, fixture_server
):
    flow, aid = prepared(db, "greenhouse_demographics")
    save_choices(db, flow, aid, global_answers=True, self_describe=True)
    worker = BrowserWorker(db)
    try:
        result = await worker.fill(aid)
        assert result.get("filled"), result
        assert not db.all("SELECT * FROM attempts")
        spec = FormPreparation(db).validate_ready(flow.get(aid))
        review = flow.review(aid)
        page = worker.greenhouse.sessions[aid]["page"]
        assert await worker.greenhouse.values_match(page, spec, review)
        await page.locator('[id="8001-freeform"]').fill("Changed description")
        assert not await worker.greenhouse.values_match(page, spec, review)
        await page.locator('[id="8001-freeform"]').fill("Synthetic self-description")
        flow.authorize(aid, ApprovalInput(reviewed_hash=review["hash"], approve=True))
        sent = await worker.greenhouse.submit(aid)
        assert sent.get("state") == "submitted", sent
        assert len(db.all("SELECT * FROM attempts")) == 1
        assert len(db.all("SELECT * FROM receipts")) == 1
    finally:
        await worker.close()


async def test_survey_ui_refresh_global_choices_and_separate_question_controls(
    db, fixture_server
):
    from playwright.async_api import async_playwright, expect
    from jobsearch.self_identification import LEGACY_BLOCKER

    flow, aid = prepared(db, "greenhouse_demographics")
    spec = survey()
    spec.unsupported = [LEGACY_BLOCKER]
    flow.update_payload(
        aid,
        {
            "package": {"files": []},
            "form_plan": {"spec": spec.model_dump(), "fingerprint": spec.fingerprint},
        },
    )
    flow.transition(aid, "filling")
    flow.transition(aid, "manual_handoff")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def open_app():
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_role(
                "button", name="Privacy & settings", exact=True
            ).click()
            await page.get_by_label("Data mode", exact=True).select_option("fixture")
            await page.get_by_role("button", name="Applications", exact=True).click()
            await page.get_by_text("Example Greenhouse", exact=True).click()

        try:
            await open_app()
            await expect(
                page.get_by_role(
                    "button", name="Refresh form and prepare answers", exact=True
                )
            ).to_be_visible()
            assert FormPreparation(db).plan(aid)["state"] == "missing_answers"
            await open_app()
            for name, value in [
                ("gender", "Decline To Self Identify"),
                ("race", "Asian"),
            ]:
                f = next(f for f in survey().fields if f.name == name)
                q = db.one(
                    "SELECT * FROM questions WHERE application_id=%s AND key=%s",
                    (aid, f.key),
                )
                editor = page.locator("#question-" + q["id"])
                await editor.get_by_role(
                    "combobox", name=f.label, exact=True
                ).select_option(label=value)
                await editor.get_by_role("radio", name="Global", exact=True).check()
                await expect(
                    editor.get_by_role(
                        "radio", name="This application only", exact=True
                    )
                ).not_to_be_checked()
                await editor.get_by_role(
                    "button", name="Confirm answer", exact=True
                ).click()
                await expect(editor).to_have_count(0)
                fact = db.one("SELECT * FROM facts WHERE key=%s AND active", (f.key,))
                assert fact["scope"] == "global" and fact["value"] == value
            await page.reload()
            await page.get_by_role(
                "button", name="Privacy & settings", exact=True
            ).click()
            await page.get_by_label("Data mode", exact=True).select_option("fixture")
            await page.get_by_role("button", name="Applications", exact=True).click()
            await page.get_by_text("Example Greenhouse", exact=True).click()
            assert not errors
            assert not db.all("SELECT * FROM approvals")
            assert not db.all("SELECT * FROM attempts")
        finally:
            await browser.close()


def test_handoff_notice_names_fit_or_attempt_blocker():
    from jobsearch.notifications import Notifications
    from .test_scoring_search import official

    app = {
        "id": "synthetic",
        "state": "manual_handoff",
        "imported": False,
        "paused": False,
        "official": official(),
        "payload": {
            "package": {"files": []},
            "form_plan": {"spec": {"adapter": "greenhouse", "unsupported": []}},
        },
        "score": {
            "scores": {"technical": 30},
            "primary": False,
            "reasons": ["Two material requirements need confirmation"],
            "requirements": [],
        },
    }
    issues = Notifications.issues(app, [], [], [])
    assert any(
        i[1]["title"] == "Resolve the fit blockers before continuing" for i in issues
    )
    app["has_attempt"] = True
    issues = Notifications.issues(app, [], [], [])
    assert any(i[1]["title"] == "Check the previous submission outcome" for i in issues)
