# ruff: noqa: F811
import json
import pytest

from jobsearch.browser import BrowserWorker
from jobsearch.config import atomic_write, file_hash
from jobsearch.db import uid
from jobsearch.fixtures import assessment, seed
from jobsearch.forms import FormPreparation, greenhouse_form
from jobsearch.greenhouse_fixture import definition
from jobsearch.memory import FactInput, Memory
from jobsearch.workflow import Workflow, ApprovalInput
from .test_browser import fixture_server  # noqa: F401
from .test_scoring_search import official


def prepared(db, scenario="greenhouse"):
    seed(db)
    flow = Workflow(db)
    app = flow.create("Example Greenhouse", "Solutions Engineer", "GH-1")
    aid = app["id"]
    flow.set_official(
        aid,
        official()
        | {
            "application_url": db.settings.origin + "/fixture/greenhouse/" + aid,
        },
    )
    score = assessment(db)
    spec = greenhouse_form(definition(), "", "GH-1")
    from jobsearch.scoring import Requirement

    for f in spec.fields:
        if f.qualification:
            fact = Memory(db).put(
                FactInput(
                    key="qualification_" + f.key,
                    value="Confirmed fixture qualification",
                    scope=aid,
                    status="confirmed",
                    provenance={"type": "user_confirmation"},
                )
            )
            score.requirements.append(
                Requirement(
                    text=f.label,
                    source="fixture",
                    category="technical",
                    classification="required",
                    central=True,
                    assessment="strong",
                    fact_ids=[fact["id"]],
                    deduction=0,
                    reason="Confirmed in this synthetic fixture.",
                )
            )
    flow.score(aid, score)
    flow.select(aid)
    flow.transition(aid, "preparing")
    artifacts = []
    for name in ("resume", "cover_letter"):
        path = db.settings.fixture_workspace / (name + ".pdf")
        atomic_write(path, b"%PDF-1.4\nSynthetic attachment\n%%EOF\n")
        artifact = uid()
        db.execute(
            "INSERT INTO artifacts(id,application_id,path,hash,kind) VALUES(%s,%s,%s,%s,%s)",
            (artifact, aid, str(path), file_hash(path), name + ".pdf"),
        )
        artifacts.append(artifact)
    flow.update_payload(aid, {"scenario": scenario, "attachments": artifacts})
    flow.transition(aid, "prepared")
    answers = {
        "first_name": "Alex",
        "last_name": "Example",
        "preferred_name": "Alex",
        "email": "alex@example.test",
        "phone": "+12025550123",
        "question_101": "Yes",
        "question_102": "No",
        "question_103": "I enjoy helping customers solve integration problems.",
        "question_104": "https://www.linkedin.com/in/example",
        "question_105": "Not applicable",
    }
    for f in spec.fields:
        if f.name in answers:
            Memory(db).put(
                FactInput(
                    key=f.key,
                    value=answers[f.name],
                    candidate="fixture",
                    kind="answer",
                    scope=aid,
                    status="confirmed",
                    provenance={"type": "user_confirmation"},
                )
            )
    return flow, aid


@pytest.mark.parametrize("scenario", ["greenhouse", "greenhouse_delayed"])
async def test_greenhouse_fills_custom_dropdowns_files_and_blocks_all_sending(
    db, fixture_server, scenario
):
    flow, aid = prepared(db, scenario)
    worker = BrowserWorker(db)
    try:
        result = await worker.fill(aid)
        assert result["filled"], result
        session = worker.greenhouse.sessions[aid]
        page = session["page"]
        assert await page.locator("#first_name").input_value() == "Alex"
        assert (
            await page.locator("#question_103").input_value()
            == "I enjoy helping customers solve integration problems."
        )
        assert (
            await page.locator("#question_102").evaluate(
                "e=>e.parentElement.querySelector('.select__single-value').textContent"
            )
            == "No"
        )
        assert await page.locator(".file-upload__filename").inner_text() == "resume.pdf"
        assert session["gate"]["blocked"] >= 2
        assert not db.all("SELECT * FROM attempts")
        assert not db.all("SELECT * FROM approvals")
        assert not db.all("SELECT * FROM receipts")
        assert (
            flow.get(aid)["payload"]["form_plan"]["omitted_attachments"][0]["kind"]
            == "cover_letter.pdf"
        )
        assert len(flow.review(aid)["payload"]["attachments"]) == 1
        assert await page.locator("#jsa-browser-status").is_visible()
        # Both the click guard and network boundary hold even for direct fetch.
        await page.get_by_role("button", name="Submit application").click()
        assert await page.evaluate(
            "async()=>{try{await fetch(location.href,{method:'POST',body:'x'});return false}catch{return true}}"
        )
        assert flow.get(aid)["state"] == "review"
        spec = FormPreparation(db).validate_ready(flow.get(aid))
        await page.locator("#email").fill("changed@example.test")
        assert not await worker.greenhouse.values_match(page, spec, flow.review(aid))
    finally:
        await worker.close()


@pytest.mark.parametrize(
    "scenario,state",
    [
        ("greenhouse_challenge", "awaiting_challenge"),
        ("greenhouse_changed", "review"),
    ],
)
async def test_greenhouse_challenge_and_new_required_field_stop_before_filling(
    db, fixture_server, scenario, state
):
    flow, aid = prepared(db, scenario)
    worker = BrowserWorker(db)
    try:
        if scenario == "greenhouse_changed":
            with pytest.raises(ValueError, match="added a required"):
                await worker.fill(aid)
        else:
            result = await worker.fill(aid)
            assert result["state"] == state
        assert flow.get(aid)["state"] == state
        assert (db.settings.state / "handoffs" / (aid + ".html")).is_file()
        page = worker.greenhouse.sessions[aid]["page"]
        assert await page.locator("#email").input_value() == ""
        assert not db.all("SELECT * FROM attempts")
    finally:
        await worker.close()


async def test_old_handoff_can_restart_with_saved_answers_and_no_submission(
    db, fixture_server
):
    flow, aid = prepared(db)
    flow.transition(aid, "filling")
    flow.transition(aid, "manual_handoff")
    worker = BrowserWorker(db)
    try:
        assert (await worker.fill(aid))["filled"]
        await worker.close()
        restarted = BrowserWorker(db)
        try:
            assert (await restarted.fill(aid))["filled"]
            assert not db.all("SELECT * FROM attempts")
        finally:
            await restarted.close()
    finally:
        await worker.close()


def test_greenhouse_consent_flags_and_descriptions_are_not_discarded():
    data = definition()
    assert not greenhouse_form(data, "", "GH").unsupported
    data["data_compliance"][0]["requires_processing_consent"] = True
    assert greenhouse_form(data, "", "GH").unsupported
    data["questions"][0]["description"] = "<p>Use your legal name.</p>"
    assert "First Name: Use your legal name." in greenhouse_form(data, "", "GH").notices


def test_legacy_greenhouse_notice_offers_retry_without_changing_application(db):
    from jobsearch.notifications import Notifications

    flow, aid = prepared(db)
    flow.transition(aid, "filling")
    flow.transition(aid, "manual_handoff")
    flow.update_payload(
        aid,
        {
            "form_plan": {
                "spec": {
                    "adapter": "greenhouse",
                    "unsupported": [
                        "Greenhouse browser completion needs a portal adapter; use the saved answer plan"
                    ],
                }
            }
        },
    )
    before = flow.get(aid)
    notice = next(
        n
        for n in Notifications(db).snapshot("fixture")["items"]
        if n["application_id"] == aid and n["kind"] == "handoff"
    )
    assert notice["detail"]["title"] == "Greenhouse browser filling is available"
    assert flow.get(aid) == before


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("greenhouse", "submitted"),
        ("greenhouse_no_receipt", "unresolved"),
    ],
)
async def test_greenhouse_approved_upload_and_submission_requires_receipt(
    db, fixture_server, scenario, expected
):
    flow, aid = prepared(db, scenario)
    worker = BrowserWorker(db)
    try:
        result = await worker.fill(aid)
        assert result["filled"]
        with pytest.raises(ValueError, match="approval"):
            await worker.greenhouse.submit(aid)
        assert not db.all("SELECT * FROM attempts")
        flow.authorize(
            aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
        )
        result = await worker.greenhouse.submit(aid)
        assert result["state"] == expected, (
            result,
            db.all("SELECT event,detail FROM audit WHERE event LIKE 'greenhouse.%%'"),
        )
        assert db.one("SELECT count(*) n FROM attempts")["n"] == 1
        assert db.one("SELECT count(*) n FROM artifacts WHERE attached")["n"] == 1
        assert bool(db.all("SELECT * FROM receipts")) == (expected == "submitted")
        assert db.one("SELECT 1 FROM audit WHERE event='fixture.greenhouse_uploaded'")
        assert db.one("SELECT 1 FROM audit WHERE event='fixture.greenhouse_received'")
        await worker.close()
        restarted = BrowserWorker(db)
        try:
            with pytest.raises(ValueError, match="existing attempt"):
                await restarted.greenhouse.submit(aid)
        finally:
            await restarted.close()
    finally:
        await worker.close()


async def test_greenhouse_changed_approval_blocks_sending(db, fixture_server):
    flow, aid = prepared(db)
    worker = BrowserWorker(db)
    try:
        await worker.fill(aid)
        flow.authorize(
            aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
        )
        flow.update_payload(aid, {"attachments": []})
        with pytest.raises(ValueError, match="approval"):
            await worker.greenhouse.submit(aid)
        assert not db.all("SELECT * FROM attempts")
    finally:
        await worker.close()


def test_greenhouse_request_contract_rejects_extra_consent_and_changed_answers():
    from jobsearch.greenhouse_transport import validate_application

    spec = greenhouse_form(
        {
            "questions": [
                {
                    "label": "Email",
                    "required": True,
                    "fields": [{"name": "email", "type": "input_text"}],
                },
                {
                    "label": "Authorized?",
                    "required": True,
                    "fields": [
                        {
                            "name": "question_1",
                            "type": "multi_value_single_select",
                            "values": [
                                {"label": "Yes", "value": 1},
                                {"label": "No", "value": 0},
                            ],
                        }
                    ],
                },
            ]
        },
        "https://job-boards.greenhouse.io/example/jobs/1",
        "1",
    )
    payload = {
        "attachments": [],
        "answers": {
            spec.fields[0].key: {"value": "alex@example.test"},
            spec.fields[1].key: {"value": "No"},
        },
    }
    application = {
        "email": "alex@example.test",
        "answers_attributes": {
            "1": {"question_id": "1", "priority": 0, "boolean_value": 0},
        },
    }

    def valid(value):
        return validate_application(
            json.dumps({"job_application": value}).encode(),
            spec,
            payload,
            {"question_1": {"No": 0, "Yes": 1}},
            {},
        )

    assert valid(application)
    assert not valid(application | {"data_compliance": {"consent": True}})
    assert not valid(application | {"email": "other@example.test"})
    assert not valid(
        application | {"cover_letter_url": "https://unapproved.example/file"}
    )
    assert not valid(application | {"answers_attributes": {"1": {"boolean_value": 1}}})


async def test_greenhouse_guided_ui_fills_then_approves_fixture(db, fixture_server):
    from playwright.async_api import async_playwright, expect
    from jobsearch.queue import Worker

    flow, aid = prepared(db)
    worker = Worker(db)
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
            await page.get_by_text("Example Greenhouse", exact=True).click()
            async with page.expect_response(lambda r: r.url.endswith("/fill")):
                await (
                    page.get_by_role("region", name="Application next step")
                    .get_by_role(
                        "button", name="Inspect form and prepare answers", exact=True
                    )
                    .click()
                )
            await worker.run_once()
            await expect(
                page.get_by_role("heading", name="Your browser form is filled")
            ).to_be_visible(timeout=10000)
            await expect(
                page.get_by_text(
                    "The agent will upload the reviewed files, send the application, and check for employer confirmation.",
                    exact=False,
                )
            ).to_be_visible()
            assert not db.all("SELECT * FROM approvals")
            assert not db.all("SELECT * FROM attempts")
            await page.get_by_role(
                "button", name="Go to final review", exact=True
            ).click()
            await expect(
                page.get_by_text(
                    "The employer form has no upload field for this document.",
                    exact=False,
                )
            ).to_be_visible()
            async with page.expect_response(
                lambda r: r.url.endswith("/approve")
            ) as response:
                await page.get_by_role(
                    "button", name="Approve & submit fixture application", exact=True
                ).click()
            assert (await response.value).status == 200
            await worker.run_once()
            await expect(
                page.get_by_role("heading", name="Application received", exact=True)
            ).to_be_visible(timeout=10000)
            assert flow.get(aid)["state"] == "submitted"
            assert not errors
        finally:
            await browser.close()
            await worker.browser.close()


def test_greenhouse_upload_binds_filename_bytes_and_storage_policy(tmp_path):
    import httpx
    from types import SimpleNamespace
    from jobsearch.config import digest
    from jobsearch.greenhouse_transport import approved_upload

    path = tmp_path / "resume.pdf"
    content = b"%PDF-1.4\nSynthetic upload contract\n%%EOF\n"
    path.write_bytes(content)
    db = SimpleNamespace(one=lambda *args: {"path": str(path)})
    payload = {
        "application_id": "fixture",
        "attachments": [
            {
                "id": "fixture-file",
                "kind": "resume.pdf",
                "name": "resume.pdf",
                "hash": digest(content),
            },
        ],
    }
    metadata = {
        "url": "https://storage.example.test",
        "resume": {
            "fields": {"policy": "synthetic-policy"},
            "key": "resume/{timestamp}/{unique_id}",
        },
    }

    def upload(
        filename="resume.pdf",
        data=b"",
        policy="synthetic-policy",
        key="resume/1234567890123/abc",
    ):
        request = httpx.Request(
            "POST",
            metadata["url"],
            files=[
                ("utf8", (None, "✓")),
                ("key", (None, key)),
                ("policy", (None, policy)),
                ("authenticity_token", (None, "1234")),
                ("Content-Type", (None, "application/octet-stream")),
                ("file", (filename, data, "application/pdf")),
            ],
        )
        return approved_upload(
            db, request.read(), request.headers["content-type"], payload, metadata
        )

    field, url, encoded, _ = upload()
    assert field == "resume" and url.endswith("resume/1234567890123/abc")
    assert (
        content in encoded
    )  # CDP's omitted bytes come only from the approved artifact.
    with pytest.raises(ValueError, match="unapproved"):
        upload(filename="other.pdf")
    with pytest.raises(ValueError, match="bytes"):
        upload(data=b"unapproved contents")
    with pytest.raises(ValueError, match="policy"):
        upload(policy="changed-policy")
    with pytest.raises(ValueError, match="destination"):
        upload(key="another-application/1234567890123/abc")
    path.write_bytes(b"changed after approval")
    with pytest.raises(ValueError, match="bytes"):
        upload()


async def test_failed_upload_can_resume_from_ui_with_original_approval(
    db, fixture_server, monkeypatch
):
    from playwright.async_api import async_playwright, expect
    from jobsearch.queue import Worker

    flow, aid = prepared(db)
    worker = Worker(db)
    try:
        await worker.browser.fill(aid)
        flow.authorize(
            aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
        )
        original_fill = worker.browser.greenhouse.fill_values

        async def blocked_upload(*args, **kwargs):
            raise ValueError("Fixture upload stopped before the final request.")

        monkeypatch.setattr(worker.browser.greenhouse, "fill_values", blocked_upload)
        await worker.run_once()
        failed = db.one("SELECT * FROM jobs WHERE kind='submit'")
        assert failed["status"] == "failed"
        assert failed["checkpoint"]["stage"] == "filling_approved_answers"
        assert flow.submission_recovery(aid)["can_retry"]
        attempt = db.one("SELECT id FROM attempts")["id"]
        assert not db.all(
            "SELECT * FROM audit WHERE event='fixture.greenhouse_received'"
        )
        monkeypatch.setattr(worker.browser.greenhouse, "fill_values", original_fill)
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                executable_path="/usr/bin/chromium", headless=True
            )
            try:
                page = await browser.new_page()
                await page.goto(db.settings.origin + "/#token=" + db.settings.token())
                await page.get_by_label("Workspace", exact=True).select_option(
                    "fixture"
                )
                await page.get_by_role(
                    "button", name="Applications", exact=True
                ).click()
                await page.get_by_text("Example Greenhouse", exact=True).click()
                await expect(
                    page.get_by_role(
                        "heading", name="The application stopped before sending"
                    )
                ).to_be_visible()
                await expect(
                    page.get_by_role("heading", name="Record employer confirmation")
                ).to_have_count(0)
                async with page.expect_response(
                    lambda r: r.url.endswith("/retry-submission")
                ) as response:
                    await page.get_by_role(
                        "button", name="Retry approved application", exact=True
                    ).click()
                assert (await response.value).status == 200
                await worker.run_once()
                await expect(
                    page.get_by_role("heading", name="Application received", exact=True)
                ).to_be_visible(timeout=10000)
            finally:
                await browser.close()
        assert flow.get(aid)["state"] == "submitted"
        assert db.one("SELECT count(*) n FROM approvals")["n"] == 1
        assert db.one("SELECT count(*) n FROM attempts")["n"] == 1
        assert db.one("SELECT id,result FROM attempts")["id"] == attempt
        assert db.one("SELECT result FROM attempts")["result"]["send_started"]
        assert not flow.submission_recovery(aid)["can_retry"]
    finally:
        await worker.browser.close()


async def test_native_request_mismatch_reports_local_block_without_sending(
    db, fixture_server
):
    from jobsearch.queue import Worker

    flow, aid = prepared(db, "greenhouse_extra_history")
    worker = Worker(db)
    try:
        await worker.browser.fill(aid)
        flow.authorize(
            aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
        )
        await worker.run_once()
        job = db.one("SELECT * FROM jobs WHERE kind='submit'")
        assert job["status"] == "failed"
        assert job["checkpoint"]["stage"] == "submission_blocked"
        assert job["checkpoint"]["reason_code"] == "unreviewed_employment"
        assert "No final application request was sent" in job["error"]
        assert not db.all(
            "SELECT * FROM audit WHERE event='fixture.greenhouse_received'"
        )
        assert not db.one("SELECT result FROM attempts")["result"]["send_started"]
        assert flow.submission_recovery(aid)["can_retry"]
    finally:
        await worker.browser.close()
