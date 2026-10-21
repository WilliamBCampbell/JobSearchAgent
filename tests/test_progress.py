"""Regression cases for the sequential application story and reusable checkpoints."""

from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from jobsearch.api import create_app
from jobsearch.config import digest
from jobsearch.db import js, uid
from jobsearch.fixtures import assessment
from jobsearch.forms import greenhouse_form
from jobsearch.llm.service import InferenceService
from jobsearch.llm.contracts import InferenceRequest
from jobsearch.progress import progress
from jobsearch.queue import Worker, enqueue
from jobsearch.requirement_decisions import (
    RequirementDecisions,
    RequirementDecisionInput,
)
from jobsearch.workflow import ApprovalInput
from .test_package_revisions import prepared
from .test_llm import application


def snapshot(db, flow, aid):
    a = flow.get(aid)
    a["questions"] = db.all("SELECT * FROM questions WHERE application_id=%s", (aid,))
    a["jobs"] = db.all(
        "SELECT * FROM jobs WHERE input->>'application_id'=%s ORDER BY created_at DESC",
        (aid,),
    )
    return a


def test_reviewed_package_survives_rescore_and_selection_recovery(db):
    flow, aid = prepared(db, reviewed=True)
    before = flow.get(aid)["payload"]
    db.execute("UPDATE applications SET state='selected' WHERE id=%s", (aid,))
    result = flow.score(aid, assessment(db))
    assert result["state"] == "selected"
    assert result["payload"] == before
    assert RequirementDecisions(db).view(result)["editable"]
    step = progress(snapshot(db, flow, aid))
    assert step["step"] == 2 and step["action"] == "continue"
    flow.continue_prepared(aid)
    assert flow.get(aid)["state"] == "prepared"
    assert flow.get(aid)["payload"] == before
    assert not db.all("SELECT * FROM approvals")


async def test_repeated_package_job_reuses_version_without_model_call(db, monkeypatch):
    flow, aid = prepared(db, reviewed=True)
    before = flow.get(aid)["payload"]
    monkeypatch.setattr(
        "jobsearch.packages.Packages.prepare",
        lambda *a, **kw: pytest.fail("Must reuse reviewed files"),
    )
    job = enqueue(db, "package", {"application_id": aid})
    assert await Worker(db).run_once()
    result = db.one("SELECT * FROM jobs WHERE id=%s", (job["job_id"],))
    assert result["status"] == "complete" and result["result"]["reused"]
    assert flow.get(aid)["payload"] == before


def test_review_gate_is_enforced_on_continue_and_approval(db):
    flow, aid = prepared(db, reviewed=True)
    db.execute(
        "UPDATE applications SET payload=payload-'document_review' WHERE id=%s", (aid,)
    )
    assert progress(snapshot(db, flow, aid))["step"] == 1
    with TestClient(create_app(db.settings)) as c:
        headers = {"X-JSA-Token": db.settings.token(), "Origin": db.settings.origin}
        assert (
            c.post(
                f"/api/applications/{aid}/continue", json={}, headers=headers
            ).status_code
            == 400
        )
        assert (
            c.post(
                f"/api/applications/{aid}/prepare", json={}, headers=headers
            ).status_code
            == 400
        )
    assert not db.all("SELECT * FROM jobs")
    with pytest.raises(ValueError, match="document version"):
        flow.authorize(aid, ApprovalInput(approve=True, reviewed_hash="0" * 64))


def test_stale_opening_has_recovery_action_before_saved_later_steps(db):
    flow, aid = prepared(db, reviewed=True)
    old = flow.get(aid)["official"] | {
        "verified_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    }
    flow.set_official(aid, old)
    step = progress(snapshot(db, flow, aid))
    assert step["step"] == 0 and step["action"] == "refresh-opening"
    assert step["completed"][1]  # Saved, never requested again.
    assert not step["completed"][0]


def test_profile_fields_do_not_introduce_career_requirements(db):
    spec = greenhouse_form(
        {
            "questions": [
                {
                    "label": "LinkedIn Profile",
                    "required": True,
                    "fields": [{"name": "question_123", "type": "input_text"}],
                },
                {
                    "label": "Have you built production C# services?",
                    "required": True,
                    "fields": [{"name": "question_456", "type": "input_text"}],
                },
            ]
        },
        "https://job-boards.greenhouse.io/demo/jobs/1",
        "1",
    )
    assert not spec.fields[0].qualification
    assert spec.fields[1].qualification
    flow, aid = prepared(db, reviewed=True)
    flow.update_payload(aid, {"form_schema": [f.model_dump() for f in spec.fields]})
    data = assessment(db)
    from jobsearch.scoring import Requirement

    data.requirements.append(
        Requirement(
            text="LinkedIn Profile",
            source="form:fixture",
            category="technical",
            classification="required",
            central=True,
            assessment="unconfirmed",
            deduction=0,
            reason="Legacy form requirement",
        )
    )
    result = flow.score(aid, data)
    assert result["score"]["primary"]
    row = result["score"]["requirements"][-1]
    assert not row["central"] and not row["material"]
    packet = InferenceService(db).context(
        InferenceRequest(task="fit_assessment", application_id=aid)
    )
    assert not any(
        r["text"] == "LinkedIn Profile"
        for r in packet["application"]["mandatory_requirements"]
    )


def test_fit_context_ignores_progress_but_not_new_facts(db):
    app = application(db)
    service = InferenceService(db)
    req = InferenceRequest(task="fit_assessment", application_id=app["id"])
    before = digest(service.context(req))
    db.execute("UPDATE applications SET revision=revision+1 WHERE id=%s", (app["id"],))
    assert digest(service.context(req)) == before
    from jobsearch.memory import Memory, FactInput

    Memory(db).put(
        FactInput(
            key="new_career_fact",
            value="Built test services",
            status="confirmed",
            provenance={"type": "user_confirmation"},
        )
    )
    assert digest(service.context(req)) != before


def test_noncentral_unconfirmed_experience_can_be_resolved_after_selection(db):
    flow, aid = prepared(db, reviewed=True)
    data = assessment(db)
    data.requirements[0].central = False
    data.requirements[0].assessment = "unconfirmed"
    flow.score(aid, data)
    db.execute("UPDATE applications SET state='selected' WHERE id=%s", (aid,))
    view = RequirementDecisions(db).view(flow.get(aid))
    row = view["requirements"][0]
    RequirementDecisions(db).decide(
        aid,
        RequirementDecisionInput(
            requirement_key=row["key"],
            score_hash=view["score_hash"],
            decision="confirm",
            note="Confirmed for fixture",
        ),
    )
    assert flow.get(aid)["state"] == "selected"
    assert flow.get(aid)["score"]["requirements"][0]["assessment"] == "strong"


async def test_recovered_job_does_not_display_previous_failure_as_current(
    db, monkeypatch
):
    flow, aid = prepared(db, reviewed=True)
    job = enqueue(db, "rescore", {"application_id": aid})
    db.execute(
        "UPDATE jobs SET error='Previous failure',checkpoint=%s WHERE id=%s",
        (js({"stage": "failed"}), job["job_id"]),
    )
    worker = Worker(db)

    async def recovered(_):
        return {"state": "prepared"}

    monkeypatch.setattr(worker, "dispatch", recovered)
    await worker.run_once()
    row = db.one("SELECT * FROM jobs WHERE id=%s", (job["job_id"],))
    assert row["status"] == "complete" and row["error"] is None
    assert row["checkpoint"]["stage"] != "failed"


def test_attempted_application_never_projects_a_preparation_retry(db):
    flow, aid = prepared(db, reviewed=True)
    a = snapshot(db, flow, aid)
    a["attempts"] = [{"id": uid()}]
    assert progress(a) is None


@pytest.mark.parametrize("changed", [False, True])
async def test_opening_refresh_rechecks_fit_without_replacing_package(
    db, monkeypatch, changed
):
    flow, aid = prepared(db, reviewed=True)
    before = flow.get(aid)
    official = before["official"] | {
        "requisition": before["requisition"],
        "status": "verified",
    }
    if changed:
        official["source_hash"] = digest("New official requirements")
    monkeypatch.setattr(
        "jobsearch.verification.Verification.verify", lambda *a: official
    )
    enqueue(db, "refresh_opening", {"application_id": aid})
    assert await Worker(db).run_once()
    after = flow.get(aid)
    assert after["state"] == before["state"]
    assert after["payload"] == before["payload"]
    assert bool(after["score"].get("evidence_changed")) == changed
    assert after["score"]["primary"] is not changed
    if changed:
        assert (
            progress(snapshot(db, flow, aid))["title"] == "Reassess the changed posting"
        )


async def test_paused_application_keeps_queued_work_for_resume(db):
    flow, aid = prepared(db, reviewed=True)
    job = enqueue(db, "package", {"application_id": aid})
    flow.control(aid, "pause")
    worker = Worker(db)
    assert not await worker.run_once()
    assert (
        db.one("SELECT status FROM jobs WHERE id=%s", (job["job_id"],))["status"]
        == "queued"
    )
    flow.control(aid, "resume")
    assert await worker.run_once()
    assert (
        db.one("SELECT status FROM jobs WHERE id=%s", (job["job_id"],))["status"]
        == "complete"
    )


def test_authorized_run_can_resume_only_before_any_attempt(db):
    flow, aid = prepared(db, reviewed=True)
    db.execute("UPDATE applications SET state='review' WHERE id=%s", (aid,))
    flow.update_payload(aid, {"browser_hash": flow.review(aid)["hash"]})
    job = flow.authorize(
        aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
    )
    db.execute("UPDATE jobs SET status='failed' WHERE id=%s", (job["job_id"],))
    with TestClient(create_app(db.settings)) as c:
        headers = {"X-JSA-Token": db.settings.token(), "Origin": db.settings.origin}
        response = c.post(
            f"/api/applications/{aid}/resume-approved", json={}, headers=headers
        )
        assert response.status_code == 200
        assert db.one("SELECT count(*) n FROM approvals")["n"] == 1
        assert not db.all("SELECT * FROM attempts")
        flow.begin_attempt(aid)
        assert (
            c.post(
                f"/api/applications/{aid}/resume-approved", json={}, headers=headers
            ).status_code
            == 400
        )


def test_failed_initial_preparation_has_an_executable_retry(db):
    from jobsearch.workflow import Workflow

    app = application(db)
    flow = Workflow(db)
    flow.score(app["id"], assessment(db))
    flow.select(app["id"])
    flow.transition(app["id"], "preparing")
    guide = progress(snapshot(db, flow, app["id"]))
    assert (
        guide["action"] == "prepare" and guide["button"] == "Retry document preparation"
    )
    with TestClient(create_app(db.settings)) as c:
        r = c.post(
            f"/api/applications/{app['id']}/prepare",
            json={},
            headers={"X-JSA-Token": db.settings.token(), "Origin": db.settings.origin},
        )
        assert r.status_code == 200


@pytest.mark.parametrize(
    "label",
    [
        "What are your salary and additional compensation expectations for this position?",
        "What interests you about this role, and what would you bring?",
    ],
)
def test_required_answers_are_not_automatically_qualifications(label):
    spec = greenhouse_form(
        {
            "questions": [
                {
                    "label": label,
                    "required": True,
                    "fields": [{"name": "question_custom", "type": "textarea"}],
                }
            ]
        },
        "https://job-boards.greenhouse.io/demo/jobs/1",
        "1",
    )
    assert spec.fields[0].required
    assert not spec.fields[0].qualification
