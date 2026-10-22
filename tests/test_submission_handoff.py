"""External submission packets preserve preparation without sending anything."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright, expect

from jobsearch.api import create_app
from jobsearch.config import Settings, file_hash
from jobsearch.memory import FactInput, Memory
from jobsearch.queue import Worker, enqueue
from jobsearch.submission_handoff import SubmissionHandoff
from jobsearch.workflow import ApprovalInput
from jobsearch.db import DB, uid
from .test_package_revisions import prepared
from .test_browser import fixture_server  # noqa: F401


def answer_plan(db):
    flow, aid = prepared(db, reviewed=True)
    fact = Memory(db).put(
        FactInput(
            key="project",
            value="Built APIs and support tools.\nPortfolio: https://portfolio.example",
            kind="answer",
            candidate="fixture",
            scope=aid,
            status="confirmed",
            provenance={"type": "user_confirmation"},
        )
    )
    fields = [
        {
            "key": "project",
            "name": "project",
            "label": "Describe a project",
            "type": "textarea",
            "required": True,
        },
        {
            "key": "resume",
            "name": "resume",
            "label": "Resume",
            "type": "file",
            "required": True,
        },
        {
            "key": "perspective",
            "name": "perspective",
            "label": "Why this role?",
            "type": "textarea",
            "required": True,
        },
    ]
    flow.update_payload(
        aid,
        {
            "form_schema": fields,
            "answers": {
                "project": {
                    "value": fact["value"],
                    "fact_ids": [fact["id"]],
                    "version_ids": [],
                    "provenance": fact["provenance"],
                }
            },
            "form_plan": {
                "spec": {
                    "adapter": "lever",
                    "fields": fields,
                    "url": "https://employer.example/apply/EVAL-1",
                    "notices": [],
                    "unsupported": [],
                }
            },
        },
    )
    flow.ask(
        aid,
        "perspective",
        "Why this role?",
        {
            "reason": {
                "code": "draft_needs_review",
                "suggestion": {"value": "I want to build useful support tools."},
            }
        },
    )
    return flow, aid


def test_packet_contains_fields_answers_paths_and_private_copies(db):
    flow, aid = answer_plan(db)
    before = flow.review(aid)
    result = SubmissionHandoff(db).export(aid, before["hash"])
    folder = Path(result["directory"])
    packet = json.loads((folder / "application.json").read_text())
    rows = packet["field_answers"]
    assert rows[0]["answer"] == before["payload"]["answers"]["project"]["value"]
    assert rows[0]["status"] == "prepared"
    assert rows[1]["document_paths"] == [
        f["path"] for f in packet["documents"] if f["kind"] == "resume.pdf"
    ]
    assert rows[2]["status"] == "needs_review"
    assert "I want to build useful support tools." not in result["prompt"]
    assert "needs regeneration" in result["prompt"]
    assert "Describe a project" in result["prompt"]
    assert "Built APIs and support tools." in result["prompt"]
    for relative, expected in json.loads(
        (folder / "manifest.json").read_text()
    ).items():
        assert file_hash(folder / relative) == expected
    document = packet["documents"][0]
    assert Path(document["path"]).is_absolute()
    Path(document["source_path"]).write_bytes(b"Changed after export")
    assert file_hash(Path(document["path"])) == document["hash"]
    assert SubmissionHandoff(db).latest(aid)["prompt"] == result["prompt"]
    assert not db.all("SELECT * FROM attempts")
    assert not db.all("SELECT * FROM approvals")
    assert not db.all("SELECT * FROM jobs WHERE kind='submit'")
    assert flow.get(aid)["state"] == "manual_handoff"


def test_current_ai_suggestion_is_embedded_as_a_draft(db, monkeypatch):
    from jobsearch.form_answers import FormAnswers
    from .test_answer_suggestions import evidence, DRAFT
    from .test_form_evidence import field

    flow, aid = prepared(db, reviewed=True)
    spec = field("What interests you about this role?")
    flow.update_payload(aid, {"form_schema": [spec]})
    question = flow.ask(aid, spec["key"], spec["label"])
    evidence(db, monkeypatch)
    FormAnswers(db).draft_answer(aid, question["id"])
    exported = SubmissionHandoff(db).export(aid)
    assert DRAFT in exported["prompt"]
    packet = json.loads((Path(exported["directory"]) / "application.json").read_text())
    assert packet["field_answers"][0]["status"] == "needs_review"
    assert packet["field_answers"][0]["answer"] is None


def test_export_rejects_unreviewed_or_changed_material(db):
    flow, aid = prepared(db)
    with pytest.raises(ValueError, match="Review the current"):
        SubmissionHandoff(db).export(aid)
    from jobsearch.package_review import PackageReview, PackageReviewInput

    PackageReview(db).accept(aid, PackageReviewInput(version="original"))
    with pytest.raises(ValueError, match="changed"):
        SubmissionHandoff(db).export(aid, "0" * 64)
    artifact = db.one("SELECT path FROM artifacts WHERE application_id=%s", (aid,))
    Path(artifact["path"]).write_text("Changed")
    with pytest.raises(ValueError, match="changed"):
        SubmissionHandoff(db).export(aid)
    assert not SubmissionHandoff(db).latest(aid)


def test_export_excludes_unrelated_candidate_and_application_facts(db):
    _, aid = prepared(db, reviewed=True)
    for candidate, scope, value in [
        ("fixture", "another-application", "Other application secret"),
        ("real", "global", "Other candidate secret"),
        ("fixture", "global", "Visible career fact"),
    ]:
        Memory(db).put(
            FactInput(
                key="context_" + value.split()[0],
                value=value,
                candidate=candidate,
                scope=scope,
                status="confirmed",
                provenance={"type": "user_confirmation"},
            )
        )
    exported = SubmissionHandoff(db).export(aid)
    packet = (Path(exported["directory"]) / "application.json").read_text()
    assert "Visible career fact" in packet
    assert "Other application secret" not in packet
    assert "Other candidate secret" not in packet


def test_terminal_application_gets_reference_prompt_without_status_change(db):
    flow, aid = prepared(db, reviewed=True)
    db.execute("UPDATE applications SET state='submitted' WHERE id=%s", (aid,))
    result = SubmissionHandoff(db).export(aid)
    assert result["mode"] == "reference"
    assert flow.get(aid)["state"] == "submitted"
    assert not db.all("SELECT * FROM attempts")


def test_saved_prompt_is_stale_after_application_status_changes(db):
    _, aid = prepared(db, reviewed=True)
    service = SubmissionHandoff(db)
    service.export(aid)
    assert not service.latest(aid)["stale"]
    db.execute(
        "UPDATE applications SET state='submitted',revision=revision+1 WHERE id=%s",
        (aid,),
    )
    assert service.latest(aid)["stale"]
    assert service.export(aid)["mode"] == "reference"


def test_legacy_settings_and_actions_cannot_enable_submission(db, monkeypatch):
    monkeypatch.setenv("JSA_LIVE_APPLICATIONS", "1")
    assert not Settings(live_applications=True).live_applications
    flow, aid = prepared(db, reviewed=True)
    for kind in ("submit", "reconcile", "continue_verification"):
        with pytest.raises(ValueError):
            enqueue(db, kind, {"application_id": aid})
    with pytest.raises(ValueError):
        flow.authorize(
            aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
        )
    assert not db.all("SELECT * FROM attempts")


async def test_worker_rejects_legacy_submission_jobs(db):
    worker = Worker(db)
    for kind in ("submit", "reconcile", "continue_verification"):
        with pytest.raises(ValueError):
            await worker.dispatch({"kind": kind, "input": {}})
    await worker.browser.close()


def test_api_exports_and_retrieves_prompt(db):
    flow, aid = answer_plan(db)
    client = TestClient(create_app(db.settings, manage_profiles=False))
    headers = {"X-JSA-Token": db.settings.token()}
    path = f"/api/applications/{aid}/submission-handoff"
    assert client.get(path, headers=headers).json() is None
    response = client.post(
        path, headers=headers, json={"reviewed_hash": flow.review(aid)["hash"]}
    )
    assert response.status_code == 200, response.text
    assert client.get(path, headers=headers).json() == response.json()
    assert "Describe a project" in response.json()["prompt"]


def test_unknown_prior_attempt_requires_reconciliation(db):
    flow, aid = prepared(db, reviewed=True)
    approval = uid()
    reviewed_hash = flow.review(aid)["hash"]
    db.execute(
        "INSERT INTO approvals(id,application_id,payload_hash,actor) VALUES(%s,%s,%s,'fixture')",
        (approval, aid, reviewed_hash),
    )
    db.execute(
        "INSERT INTO attempts(id,application_id,payload_hash,approval_id,state) VALUES(%s,%s,%s,%s,'unresolved')",
        (uid(), aid, reviewed_hash, approval),
    )
    db.execute("UPDATE applications SET state='unresolved' WHERE id=%s", (aid,))
    result = SubmissionHandoff(db).export(aid)
    assert result["mode"] == "reconcile"
    assert "Execution mode: reconcile" in result["prompt"]
    assert len(db.all("SELECT * FROM attempts")) == 1
    assert flow.get(aid)["state"] == "unresolved"


def test_export_stays_in_selected_profile(db, tmp_path):
    from jobsearch.user_profiles import UserProfiles
    from .test_user_profiles import create_profile

    profiles = UserProfiles(db.settings)
    profiles.initialize()
    row = create_profile(profiles, tmp_path)
    second = DB(profiles.resolve(row["id"]))
    _, first_id = prepared(db, reviewed=True)
    _, second_id = prepared(second, reviewed=True)
    with pytest.raises(ValueError):
        SubmissionHandoff(second).export(first_id)
    result = SubmissionHandoff(second).export(second_id)
    assert Path(result["directory"]).is_relative_to(second.settings.state)
    packet = json.loads((Path(result["directory"]) / "application.json").read_text())
    assert packet["profile"]["id"] == row["id"]
    assert SubmissionHandoff(db).latest(first_id) is None


async def test_worker_keeps_inspection_and_answer_generation(db, monkeypatch):
    from jobsearch.forms import FormPreparation, FormSpec

    flow, aid = answer_plan(db)
    fields = flow.get(aid)["payload"]["form_schema"][:2]
    inspected = []

    def inspect(self, app):
        inspected.append(app["id"])
        return FormSpec(
            adapter="lever",
            url=app["official"]["application_url"],
            requisition=app["requisition"],
            fields=fields,
        )

    monkeypatch.setattr(FormPreparation, "inspect", inspect)
    worker = Worker(db)

    async def no_fill(*args, **kwargs):
        pytest.fail("Retired browser filling must not run")

    monkeypatch.setattr(worker.browser, "fill", no_fill)
    enqueue(db, "fill", {"application_id": aid})
    try:
        await worker.run_once()
        job = db.one("SELECT * FROM jobs WHERE kind='fill'")
        assert job["status"] == "complete", job["error"]
        assert inspected == [aid]
        assert flow.get(aid)["payload"]["answers"]["project"]["value"].startswith(
            "Built APIs"
        )
        assert not db.all("SELECT * FROM attempts")
    finally:
        await worker.browser.close()


@pytest.mark.usefixtures("fixture_server")
async def test_ui_copies_answers_and_complete_submission_prompt(db):
    _, aid = answer_plan(db)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        context = await browser.new_context(
            permissions=["clipboard-read", "clipboard-write"]
        )
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_role(
                "button", name="Privacy & settings", exact=True
            ).click()
            await page.get_by_label("Data mode", exact=True).select_option("fixture")
            await page.get_by_role("button", name="Applications", exact=True).click()
            await page.get_by_text("Evaluation Demo", exact=True).click()
            await page.get_by_role(
                "button", name="Copy answer: Describe a project", exact=True
            ).click()
            assert (
                await page.evaluate("navigator.clipboard.readText()")
                == "Built APIs and support tools.\nPortfolio: https://portfolio.example"
            )
            await page.get_by_role(
                "button", name="Copy answer: Resume", exact=True
            ).click()
            copied_path = await page.evaluate("navigator.clipboard.readText()")
            assert Path(copied_path).is_absolute() and copied_path.endswith(
                "resume.pdf"
            )
            await page.get_by_role(
                "button", name="Copy all questions and answers", exact=True
            ).click()
            assert "Describe a project\nBuilt APIs" in await page.evaluate(
                "navigator.clipboard.readText()"
            )
            await page.locator("#submission-handoff > summary").click()
            await page.get_by_role(
                "button", name="Create submission prompt", exact=True
            ).click()
            await page.get_by_role(
                "button", name="Copy submission prompt", exact=True
            ).click()
            copied = await page.evaluate("navigator.clipboard.readText()")
            assert "Describe a project" in copied and "Built APIs" in copied
            assert "resume.pdf" in copied and "application.json" in copied
            await expect(
                page.get_by_role("button", name="Approve & send", exact=True)
            ).to_have_count(0)
            assert not errors
            assert not db.all("SELECT * FROM attempts WHERE application_id=%s", (aid,))
        finally:
            await browser.close()
