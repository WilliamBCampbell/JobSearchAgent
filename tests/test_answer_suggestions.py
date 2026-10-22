"""Reviewable profile-based drafts with separately captured employer evidence."""

import pytest
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright, expect

from jobsearch.api import create_app
from jobsearch.employer_research import EmployerResearch
from jobsearch.form_answers import FormAnswers
from jobsearch.forms import FieldSpec, FormSpec
from jobsearch.llm.service import InferenceService
from jobsearch.openings import OfficialOpenings
from jobsearch.queue import Worker
from .test_browser import fixture_server  # noqa: F401
from .test_form_evidence import confirmed, field
from .test_interventions import pending_plan

PROMPT = "What do you know about Example POS and why does this role interest you?"
SOURCE = "https://example-employer.test/products/ordering"
DRAFT = (
    "Example POS connects phone ordering to restaurant POS systems. "
    "I would like to apply my experience deploying POS systems and building AI support tools "
    "to that integration work."
)


def pending(db):
    flow, aid, _ = pending_plan(db)
    spec = field(PROMPT)
    plan = FormSpec(
        adapter="lever", url=SOURCE, requisition="INT-101", fields=[FieldSpec(**spec)]
    )
    flow.update_payload(
        aid, {"form_schema": [spec], "form_plan": {"spec": plan.model_dump()}}
    )
    question = flow.ask(aid, spec["key"], spec["label"])
    flow.transition(aid, "missing_answers")
    return flow, aid, spec, question["id"]


def evidence(db, monkeypatch, supported=True):
    facts = [
        confirmed(
            db, "pos_work", "Deployed POS systems and trained staff.", "employment"
        ),
        confirmed(db, "ai_work", "Built AI support tools and APIs.", "skill"),
    ]
    monkeypatch.setattr(
        OfficialOpenings,
        "fetch",
        lambda self, url: (
            url,
            b"<html><title>Example POS ordering</title><main>Phone ordering sends orders to restaurant POS systems.</main></html>",
        ),
    )
    original = InferenceService.fixture

    def fixture(self, request, packet):
        if request.task == "form_answers":
            assert not packet["personal_context_ids"]
            assert {f["id"] for f in facts} <= {f["id"] for f in packet["facts"]}
            return {
                "answers": [
                    {
                        "key": f["key"],
                        "decision": "draft",
                        "value": DRAFT,
                        "fact_ids": [f["id"] for f in facts],
                        "source_refs": [
                            s["id"]
                            for s in packet["sources"]
                            if s["kind"] == "employer_research"
                        ],
                        "reason": "Connects confirmed POS and AI work to the documented product.",
                    }
                    for f in packet["form_fields"]
                ]
            }
        if request.task == "form_answer_review":
            return {
                "checks": [
                    {
                        "key": f["key"],
                        "supported": supported,
                        "issue": "" if supported else "Unsupported personal history.",
                    }
                    for f in packet["form_fields"]
                ]
            }
        return original(self, request, packet)

    monkeypatch.setattr(InferenceService, "fixture", fixture)


async def test_suggestion_job_uses_career_and_research_without_confirming(
    db, monkeypatch
):
    flow, aid, spec, qid = pending(db)
    evidence(db, monkeypatch)
    count = db.one("SELECT count(*) n FROM facts")["n"]
    with TestClient(create_app(db.settings)) as client:
        headers = {"X-JSA-Token": db.settings.token()}
        response = client.post(
            f"/api/questions/{qid}/draft-answer",
            headers=headers,
            json={"source_urls": [SOURCE]},
        )
        assert response.status_code == 200
        duplicate = client.post(
            f"/api/questions/{qid}/draft-answer",
            headers=headers,
            json={"source_urls": [SOURCE]},
        )
        assert duplicate.json()["job_id"] == response.json()["job_id"]
    worker = Worker(db)
    try:
        assert await worker.run_once()
    finally:
        await worker.browser.close()
    job = db.one("SELECT * FROM jobs WHERE id=%s", (response.json()["job_id"],))
    assert job["status"] == "complete", job["error"]
    reason = db.one("SELECT reason FROM questions WHERE id=%s", (qid,))["reason"]
    assert reason["suggestion"]["value"] == DRAFT
    assert (
        len(reason["context"]) == 2 and reason["employer_sources"][0]["url"] == SOURCE
    )
    assert FormAnswers(db).valid(flow.get(aid), spec, reason["suggestion"], draft=True)
    assert not FormAnswers(db).valid(flow.get(aid), spec, reason["suggestion"])
    assert spec["key"] not in flow.get(aid)["payload"]["answers"]
    assert flow.get(aid)["state"] == "missing_answers"
    assert db.one("SELECT count(*) n FROM facts")["n"] == count
    assert not db.all("SELECT * FROM approvals") and not db.all(
        "SELECT * FROM attempts"
    )


def test_research_is_application_scoped_and_tampering_invalidates_draft(
    db, monkeypatch
):
    flow, aid, spec, qid = pending(db)
    evidence(db, monkeypatch)
    FormAnswers(db).draft_answer(aid, qid, source_urls=[SOURCE])
    draft = db.one("SELECT reason FROM questions WHERE id=%s", (qid,))["reason"][
        "suggestion"
    ]
    other = flow.create("Other Employer", "Engineer", "OTHER")
    assert not EmployerResearch(db).sources(other)
    record = flow.get(aid)["payload"]["employer_research"][0]
    from pathlib import Path

    Path(record["path"]).write_text("Changed research")
    assert not FormAnswers(db).valid(flow.get(aid), spec, draft, draft=True)


def test_failed_review_leaves_no_suggestion(db, monkeypatch):
    flow, aid, _, qid = pending(db)
    evidence(db, monkeypatch, supported=False)
    result = FormAnswers(db).draft_answer(aid, qid, source_urls=[SOURCE])
    assert not result["draft_available"]
    reason = db.one("SELECT reason FROM questions WHERE id=%s", (qid,))["reason"]
    assert (
        "suggestion" not in reason
        and "Unsupported personal history" in reason["message"]
    )
    assert not flow.get(aid)["payload"]["answers"]


def test_user_answer_arriving_during_drafting_is_preserved(db, monkeypatch):
    flow, aid, spec, qid = pending(db)
    evidence(db, monkeypatch)
    original = FormAnswers.prepare

    def prepare_then_confirm(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        flow.answer(qid, "My own answer about the role.")
        return result

    monkeypatch.setattr(FormAnswers, "prepare", prepare_then_confirm)
    with pytest.raises(ValueError, match="unanswered text"):
        FormAnswers(db).draft_answer(aid, qid, source_urls=[SOURCE])
    assert (
        flow.get(aid)["payload"]["answers"][spec["key"]]["value"]
        == "My own answer about the role."
    )
    question = db.one("SELECT * FROM questions WHERE id=%s", (qid,))
    assert question["answer_fact"] and not question["needs_review"]
    assert "suggestion" not in question["reason"]


def test_research_rejects_local_files_without_fetching_them(db):
    _, aid, _, _ = pending(db)
    with pytest.raises(ValueError, match="HTTPS"):
        EmployerResearch(db).capture(aid, ["file:///etc/passwd"])


@pytest.mark.parametrize(
    "label,kind,purpose",
    [
        ("What is your desired salary?", "text", "ordinary"),
        ("I agree to the notice", "checkbox", "consent"),
        ("Gender", "text", "self_identification"),
    ],
)
def test_drafting_cannot_answer_commitments_or_surveys(db, label, kind, purpose):
    flow, aid, _, _ = pending(db)
    spec = field(label) | {"type": kind, "purpose": purpose}
    flow.update_payload(aid, {"form_schema": [spec]})
    qid = flow.ask(aid, spec["key"], label)["id"]
    with pytest.raises(ValueError, match="unanswered text"):
        FormAnswers(db).draft_answer(aid, qid)
    assert not db.all("SELECT * FROM llm_runs")


@pytest.mark.usefixtures("fixture_server")
async def test_ui_shows_editable_suggestion_and_sources_before_confirmation(
    db, monkeypatch
):
    flow, aid, spec, qid = pending(db)
    evidence(db, monkeypatch)
    FormAnswers(db).draft_answer(aid, qid, source_urls=[SOURCE])
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page()
        try:
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_role(
                "button", name="Privacy & settings", exact=True
            ).click()
            await page.get_by_label("Data mode", exact=True).select_option("fixture")
            await page.get_by_role("button", name="Applications", exact=True).click()
            await page.get_by_text("Intervention Demo", exact=True).click()
            panel = page.locator("#question-" + qid)
            await expect(
                panel.get_by_role("heading", name="AI suggestion", exact=True)
            ).to_be_visible()
            await expect(
                panel.get_by_role("button", name="Refresh suggestion")
            ).to_be_visible()
            await panel.get_by_text(
                "Employer sources for this suggestion", exact=True
            ).click()
            await expect(
                panel.get_by_role("link", name="Example POS ordering")
            ).to_have_attribute("href", SOURCE)
            edited = (
                DRAFT
                + " I would welcome a conversation about the team's integration work."
            )
            await panel.get_by_role("textbox", name=PROMPT, exact=True).fill(edited)
            async with page.expect_response(
                lambda r: r.url.endswith(f"/questions/{qid}/answer")
            ) as saved:
                await panel.get_by_role(
                    "button", name="Confirm answer", exact=True
                ).click()
            assert (await saved.value).status == 200
            assert flow.get(aid)["payload"]["answers"][spec["key"]]["value"] == edited
            assert not db.all("SELECT * FROM approvals") and not db.all(
                "SELECT * FROM attempts"
            )
        finally:
            await browser.close()
