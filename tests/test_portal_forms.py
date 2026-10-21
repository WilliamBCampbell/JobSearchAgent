# ruff: noqa: F811
import pytest
from jobsearch.forms import FormPreparation, FormSpec, FieldSpec, lever_form, field_key
from jobsearch.workflow import ApprovalInput
from jobsearch.queue import Worker
from jobsearch.memory import FactInput
from .test_browser import fixture_server, prepare_browser_app  # noqa: F401


async def planned(db, scenario="lever"):
    flow, aid = await prepare_browser_app(db, scenario)
    worker = Worker(db)
    result = await worker.browser.fill(aid)
    assert result["state"] == "missing_answers"
    question = db.one(
        "SELECT * FROM questions WHERE application_id=%s AND answer_fact IS NULL",
        (aid,),
    )
    assert question["prompt"] == "Annual base salary target"
    flow.answer(question["id"], "120000")
    await worker.run_once()
    assert flow.get(aid)["state"] == "review"
    return flow, aid, worker


async def test_hosted_form_plan_to_approved_browser_receipt(db, fixture_server):
    flow, aid, worker = await planned(db)
    try:
        assert not worker.browser.portal.sessions
        assert not db.all("SELECT * FROM attempts")
        with pytest.raises(ValueError, match="approval"):
            await worker.browser.portal.submit(aid)
        flow.authorize(
            aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
        )
        await worker.run_once()
        job = db.one("SELECT * FROM jobs WHERE kind='submit'")
        assert job["status"] == "complete", job.get("error")
        assert flow.get(aid)["state"] == "submitted", (
            job,
            db.all(
                "SELECT event,detail FROM audit WHERE event LIKE 'portal.%%' OR event LIKE 'fixture.%%'"
            ),
            await worker.browser.portal.sessions[aid][1].locator("body").inner_text(),
        )
        assert db.one("SELECT count(*) n FROM receipts")["n"] == 1
        event = db.one("SELECT detail FROM audit WHERE event='portal.attempt_finished'")
        assert event["detail"]["post_observed"]
        assert (
            event["detail"]["blocked_requests"] >= 2
        )  # input-triggered GET and resume upload
        assert len(db.all("SELECT * FROM attempts")) == 1
    finally:
        await worker.browser.close()


async def test_hosted_form_no_receipt_and_restart_never_resubmits(db, fixture_server):
    flow, aid, worker = await planned(db, "lever_no_receipt")
    try:
        flow.authorize(
            aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
        )
        await worker.run_once()
        assert flow.get(aid)["state"] == "unresolved"
        assert not db.all("SELECT * FROM receipts")
        await worker.browser.close()
        other = Worker(db)
        try:
            with pytest.raises(ValueError, match="attempt already exists"):
                await other.browser.portal.submit(aid)
            assert not other.browser.portal.sessions
        finally:
            await other.browser.close()
        assert db.one("SELECT count(*) n FROM attempts")["n"] == 1
    finally:
        await worker.browser.close()


async def test_changed_employer_form_invalidates_review_before_entering_data(
    db, fixture_server
):
    flow, aid, worker = await planned(db)
    try:
        flow.authorize(
            aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
        )
        # Simulate an employer edit, independent of candidate payload changes.
        db.execute(
            "UPDATE applications SET payload=jsonb_set(payload,'{scenario}','\"lever_changed\"') WHERE id=%s",
            (aid,),
        )
        await worker.run_once()
        assert flow.get(aid)["state"] == "review"
        assert not db.all("SELECT * FROM attempts")
        assert db.one("SELECT invalidated FROM approvals")["invalidated"]
        page = worker.browser.portal.sessions[aid][1]
        assert await page.locator("[name=name]").input_value() == ""
    finally:
        await worker.browser.close()


async def test_verification_challenge_pauses_without_candidate_data(db, fixture_server):
    flow, aid, worker = await planned(db, "lever_challenge")
    try:
        flow.authorize(
            aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
        )
        await worker.run_once()
        assert flow.get(aid)["state"] == "awaiting_challenge"
        assert not db.all("SELECT * FROM attempts")
        assert (
            await worker.browser.portal.sessions[aid][1]
            .locator("[name=email]")
            .input_value()
            == ""
        )
    finally:
        await worker.browser.close()


def test_custom_questions_are_scoped_and_wording_changes_keys():
    a = field_key("cards[x][field0]", "Travel up to 10%?", ["Yes", "No"])
    b = field_key("cards[x][field0]", "Travel up to 50%?", ["Yes", "No"])
    assert a != b
    with pytest.raises(ValueError, match="application scope"):
        FactInput(
            key=a,
            value="Yes",
            status="confirmed",
            scope="global",
            provenance={"type": "user_confirmation"},
        )


def test_parser_preserves_group_options_implicit_consent_and_unknown_widgets():
    body = """<form id="application-form" method="POST"><input name="name" required><input name="email" required>
    <input name="resume" type="file"><label>Travel commitment<input name="travel" type="radio" value="Yes" required><input name="travel" type="radio" value="No"></label>
    <p>By submitting, I certify that these answers are accurate.</p><input type="range" name="unsupported">
    <button id="btn-submit">Submit application</button></form>"""
    spec = lever_form(body, "https://jobs.lever.co/example/req/apply", "req")
    travel = next(f for f in spec.fields if f.name == "travel")
    assert travel.options == ["Yes", "No"] and travel.material and travel.qualification
    assert spec.notices and spec.unsupported


async def test_outgoing_payload_checks_answers_files_and_unknown_controls(
    db, fixture_server
):
    import httpx
    from jobsearch.portal_browser import approved_multipart, matches_multipart
    from jobsearch.config import digest

    flow, aid, worker = await planned(db)
    try:
        payload = flow.review(aid)["payload"]
        spec = FormSpec.model_validate(payload["form_plan"]["spec"])
        fields = [
            (f.name, (None, str(payload["answers"][f.key]["value"])))
            for f in spec.fields
            if f.key in payload["answers"]
        ]
        # CDP provides the filename but omits upload bytes. The adapter restores only
        # the locally hashed attachment already approved in this application.
        parts = fields + [
            ("resume", (payload["attachments"][0]["name"], b"", "application/pdf"))
        ]
        request = httpx.Request("POST", spec.url, files=parts)
        encoded, content_type = approved_multipart(
            db, request.read(), request.headers["content-type"], spec, payload
        )
        assert matches_multipart(encoded, content_type, spec, payload)
        assert payload["attachments"][0]["hash"] != digest(b"")
        for bad in (
            [(k, (None, "Changed name") if k == "name" else v) for k, v in parts],
            parts + [("unreviewedConsent", (None, "Yes"))],
            fields + [("resume", ("different.pdf", b"changed", "application/pdf"))],
            fields
            + [
                (
                    "resume",
                    (payload["attachments"][0]["name"], b"changed", "application/pdf"),
                )
            ],
        ):
            request = httpx.Request("POST", spec.url, files=bad)
            with pytest.raises(ValueError):
                approved_multipart(
                    db, request.read(), request.headers["content-type"], spec, payload
                )
    finally:
        await worker.browser.close()


async def test_correcting_form_answer_invalidates_authorization(db, fixture_server):
    flow, aid, worker = await planned(db)
    try:
        old_review = flow.review(aid)
        flow.authorize(
            aid, ApprovalInput(approve=True, reviewed_hash=old_review["hash"])
        )
        q = db.one("SELECT * FROM questions WHERE application_id=%s", (aid,))
        old = q["answer_fact"]
        flow.answer(q["id"], "125000")
        assert flow.get(aid)["state"] == "review"
        assert not db.one("SELECT active FROM facts WHERE id=%s", (old,))["active"]
        assert db.one(
            "SELECT invalidated FROM approvals WHERE application_id=%s", (aid,)
        )["invalidated"]
        assert flow.review(aid)["hash"] != old_review["hash"]
        with pytest.raises(ValueError):
            await worker.browser.portal.submit(aid)
        assert not db.all("SELECT * FROM attempts")
        # Refresh uses the correction without repeating the question or retrieval.
        result = await worker.browser.fill(aid)
        assert result["state"] == "review"
        assert not flow.review(aid)["questions"]
    finally:
        await worker.browser.close()


async def test_real_sending_gate_blocks_before_browser_launch(db, fixture_server):
    flow, aid, worker = await planned(db)
    try:
        db.execute("UPDATE applications SET candidate='real' WHERE id=%s", (aid,))
        with pytest.raises(ValueError, match="disabled"):
            flow.authorize(aid, ApprovalInput(approve=True, reviewed_hash="0" * 64))
        with pytest.raises(ValueError, match="disabled"):
            await worker.browser.portal.submit(aid)
        assert not worker.browser.portal.sessions
        assert not db.all("SELECT * FROM attempts")
    finally:
        await worker.browser.close()


async def test_form_qualification_uses_scoped_answer_in_model_packet(
    db, fixture_server
):
    from jobsearch.llm.service import InferenceService
    from jobsearch.llm.contracts import InferenceRequest

    flow, aid, worker = await planned(db)
    try:
        plan = flow.get(aid)["payload"]["form_plan"]
        spec = FormSpec.model_validate(plan["spec"])
        field = FieldSpec(
            name="cards[required][field0]",
            key=field_key(
                "cards[required][field0]",
                "Can you maintain production C# APIs independently?",
                ["Yes", "No"],
            ),
            label="Can you maintain production C# APIs independently?",
            type="select",
            options=["Yes", "No"],
            required=True,
            qualification=True,
        )
        spec.fields.append(field)
        flow.transition(aid, "filling")
        assert FormPreparation(db).plan_spec(aid, spec)["state"] == "missing_answers"
        q = db.one(
            "SELECT * FROM questions WHERE application_id=%s AND key=%s",
            (aid, field.key),
        )
        with pytest.raises(ValueError, match="available employer option"):
            flow.answer(q["id"], "Maybe")
        flow.answer(q["id"], "Yes")
        flow.transition(aid, "filling")
        assert FormPreparation(db).plan_spec(aid, spec)["state"] == "scored"
        assert not flow.get(aid)["score"]["primary"]
        packet = InferenceService(db).context(
            InferenceRequest(task="fit_assessment", application_id=aid)
        )
        fact = next(f for f in packet["facts"] if f["key"] == field.key)
        assert fact["question"] == field.label and fact["value"] == "Yes"
        assert fact["scope"] == aid
        assert not any(f["key"] == "email" for f in packet["facts"])
    finally:
        await worker.browser.close()


async def test_guided_form_review_correction_and_approval_ui(db, fixture_server):
    from playwright.async_api import async_playwright, expect

    flow, aid, worker = await planned(db)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_label("Workspace", exact=True).select_option("fixture")
            await page.get_by_role("button", name="Applications", exact=True).click()
            await page.get_by_text("Northstar Demo", exact=True).click()
            await page.get_by_text(
                "Document previews and attachment selection", exact=True
            ).click()
            preview = page.get_by_role("img", name="Rendered resume page", exact=True)
            await expect(preview).to_be_visible()
            assert await preview.evaluate(
                "image => image.complete && image.naturalWidth > 0"
            )
            await page.get_by_text("Cover letter PDF preview", exact=True).click()
            await expect(
                page.get_by_role("img", name="Rendered cover letter page", exact=True)
            ).to_be_visible()
            await page.get_by_text("Prepared answers and sources", exact=True).click()
            panel = page.get_by_role("region", name="Employer application plan")
            await panel.wait_for()
            row = panel.get_by_role("row").filter(has_text="Annual base salary target")
            await row.get_by_role("button", name="Change answer").click()
            await page.get_by_role(
                "textbox", name="Annual base salary target", exact=True
            ).fill("125000")
            async with page.expect_response(
                lambda r: "/questions/" in r.url and r.url.endswith("/answer")
            ):
                await page.get_by_role(
                    "button", name="Confirm answer", exact=True
                ).click()
            # The UI schedules a refresh of the plan; let it finish enqueuing first.
            for _ in range(30):
                if db.one("SELECT 1 FROM jobs WHERE kind='fill' AND status='queued'"):
                    break
                await page.wait_for_timeout(100)
            await worker.run_once()
            await page.wait_for_timeout(2200)
            async with page.expect_response(
                lambda r: r.url.endswith("/approve")
            ) as pending:
                await page.get_by_role(
                    "button", name="Approve & submit fixture application", exact=True
                ).click()
            assert (await pending.value).status == 200
            await worker.run_once()
            await page.get_by_role(
                "heading", name="Application received", exact=True
            ).wait_for()
            assert flow.get(aid)["state"] == "submitted"
            assert not errors
        finally:
            await browser.close()
            await worker.browser.close()


async def test_expired_opening_verification_blocks_approval(db, fixture_server):
    from datetime import datetime, timezone, timedelta
    from jobsearch.db import js

    flow, aid, worker = await planned(db)
    try:
        official = flow.get(aid)["official"] | {
            "verified_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        }
        db.execute(
            "UPDATE applications SET official=%s WHERE id=%s", (js(official), aid)
        )
        with pytest.raises(ValueError, match="24 hours"):
            flow.authorize(
                aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
            )
        assert not db.all("SELECT * FROM approvals")
        assert not db.all("SELECT * FROM attempts")
    finally:
        await worker.browser.close()


def test_contact_reuse_requires_matching_question_not_just_input_name():
    assert field_key("name", "Full name", []) == "professional_name"
    assert field_key("name", "Legal name on government identification", []).startswith(
        "application_form_"
    )
    assert field_key(
        "email", "Do you agree to receive marketing email?", []
    ).startswith("application_form_")


def test_form_hash_includes_question_explanations_and_policy_links():
    import html
    import json

    template = html.escape(
        json.dumps(
            {
                "fields": [
                    {
                        "text": "Travel commitment",
                        "description": "Travel can include international assignments.",
                        "required": True,
                    }
                ]
            }
        ),
        quote=True,
    )
    body = f'''<form id="application-form"><input name="name" required><input name="email" required><input type="file" name="resume">
    <input type="hidden" name="cards[x][baseTemplate]" value="{template}"><textarea name="cards[x][field0]" required></textarea>
    <a href="https://example.com/privacy-v1">Privacy policy</a><button id="btn-submit">Submit application</button></form>'''
    first = lever_form(body, "https://jobs.lever.co/example/req/apply", "req")
    assert any("international assignments" in f.label for f in first.fields)
    assert "Privacy policy: https://example.com/privacy-v1" in first.notices
    second = lever_form(body.replace("privacy-v1", "privacy-v2"), first.url, "req")
    assert first.fingerprint != second.fingerprint


async def test_cancelled_form_inspection_does_not_start_a_model_job(db, monkeypatch):
    from jobsearch.workflow import Workflow
    from jobsearch.queue import enqueue

    flow = Workflow(db)
    app = flow.create("Cancelled Demo", "Software Engineer", "CANCEL-FORM-1")
    flow.update_payload(app["id"], {"form_plan": {"pending": True}})
    queued = enqueue(db, "fill", {"application_id": app["id"]})
    worker = Worker(db)

    async def cancelled_fill(aid, *args):
        db.execute(
            "UPDATE jobs SET cancel_requested=true WHERE id=%s", (queued["job_id"],)
        )
        return {"state": "scored"}

    monkeypatch.setattr(worker.browser, "fill", cancelled_fill)
    try:
        await worker.run_once()
        assert (
            db.one("SELECT status FROM jobs WHERE id=%s", (queued["job_id"],))["status"]
            == "cancelled"
        )
        assert not db.one("SELECT 1 FROM jobs WHERE kind='form_fit'")
    finally:
        await worker.browser.close()
