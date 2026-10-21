# ruff: noqa: F811
import json
import pytest
from fastapi.testclient import TestClient
from jobsearch.api import create_app
from jobsearch.db import DB
from jobsearch.forms import FieldSpec, FormSpec, FormPreparation
from jobsearch.memory import Memory, FactInput
from jobsearch.notifications import Notifications
from jobsearch.queue import enqueue
from jobsearch.workflow import Workflow, ApprovalInput
from .test_browser import fixture_server  # noqa: F401


def pending_plan(db):
    from jobsearch.fixtures import seed, assessment
    from .test_scoring_search import official

    seed(db)
    flow = Workflow(db)
    app = flow.create("Intervention Demo", "Software Engineer", "INT-101")
    aid = app["id"]
    flow.set_official(aid, official())
    flow.score(aid, assessment(db))
    flow.update_payload(aid, {"package": {"files": []}})
    # This fixture isolates the form-answer stage; browser submission remains guarded.
    db.execute("UPDATE applications SET state='filling' WHERE id=%s", (aid,))
    spec = FormSpec(
        adapter="lever",
        url=db.settings.origin + "/fixture/form",
        requisition="INT-101",
        fields=[
            FieldSpec(
                key="application_form_travel",
                name="travel",
                label="Travel commitment",
                type="select",
                required=True,
                options=["No travel", "Up to 10%"],
            ),
            FieldSpec(
                key="application_form_notes",
                name="notes",
                label="Optional notes",
                type="textarea",
            ),
        ],
    )
    return flow, aid, spec


def test_uncertain_answers_save_handoff_and_persistent_deduplicated_notification(db):
    flow, aid, spec = pending_plan(db)
    Memory(db).put(
        FactInput(
            key="application_form_travel",
            scope=aid,
            value="Up to 10%",
            status="unconfirmed",
            provenance={"type": "generated_draft"},
        )
    )
    result = FormPreparation(db).plan_spec(aid, spec)
    assert result["state"] == "missing_answers"
    assert result["handoff"]["review_url"].endswith(aid)
    handoff = json.loads((db.settings.state / "handoffs" / f"{aid}.json").read_text())
    assert handoff["application"]["questions"][0]["prompt"] == "Travel commitment"
    assert not flow.get(aid)["payload"]["answers"]
    assert len(db.all("SELECT * FROM questions")) == 1  # Optional notes remain blank.
    inbox = Notifications(db).snapshot("fixture")
    notice = inbox["items"][0]
    assert inbox["applications"] == 1 and inbox["unread"] == 1
    Notifications(db).mark_read(notice["id"], "fixture")
    flow.transition(aid, "filling")
    FormPreparation(db).plan_spec(aid, spec)
    restarted = Notifications(DB(db.settings)).snapshot("fixture")
    assert restarted["pending"] == 1 and restarted["unread"] == 0
    assert restarted["items"][0]["id"] == notice["id"]
    assert len(db.all("SELECT * FROM followups WHERE NOT resolved")) == 1
    with pytest.raises(ValueError):
        flow.authorize(aid, ApprovalInput(reviewed_hash="a" * 64, approve=True))
    assert not db.all("SELECT * FROM attempts")
    q = db.one("SELECT * FROM questions")
    flow.answer(q["id"], "No travel")
    assert len(db.all("SELECT * FROM jobs WHERE kind='fill' AND status='queued'")) == 1
    assert Notifications(db).snapshot("fixture")["pending"] == 0
    assert not db.all("SELECT * FROM followups WHERE NOT resolved")
    # A new unresolved item is a new unread event, even after acknowledgment.
    flow.ask(aid, q["key"], q["prompt"])
    assert Notifications(db).snapshot("fixture")["unread"] == 1


def test_conflicting_answers_require_user_confirmation_and_stay_scoped(db):
    flow, aid, spec = pending_plan(db)
    mem = Memory(db)
    prior = [
        mem.put(
            FactInput(
                key="application_form_travel",
                scope=aid,
                value=value,
                status="confirmed",
                provenance={"type": "user_confirmation"},
            )
        )
        for value in ("No travel", "Up to 10%")
    ]
    FormPreparation(db).plan_spec(aid, spec)
    q = db.one("SELECT * FROM questions")
    assert q["reason"]["code"] == "conflicting_evidence"
    assert set(q["reason"]["fact_ids"]) == {f["id"] for f in prior}
    assert not flow.get(aid)["payload"]["answers"]
    flow.answer(q["id"], "No travel")
    answer = mem.resolve(q["key"], "fixture", aid)
    assert answer["value"] == "No travel" and answer["source"] != "question"
    current = db.one(
        "SELECT * FROM facts WHERE active AND key=%s AND scope=%s", (q["key"], aid)
    )
    assert set(current["provenance"]["superseded_fact_ids"]) == {f["id"] for f in prior}
    assert not db.all("SELECT * FROM conflicts WHERE NOT resolved")
    other = flow.create("Intervention Demo", "Software Engineer", "INT-102")
    assert mem.resolve(q["key"], "fixture", other["id"])["source"] == "question"
    assert Notifications(db).snapshot("fixture")["pending"] == 0


def test_stale_attachments_do_not_prevent_manual_answer_handoff(db):
    flow, aid, spec = pending_plan(db)
    flow.update_payload(aid, {"attachments": ["missing-artifact"]})
    FormPreparation(db).plan_spec(aid, spec)
    handoff = json.loads((db.settings.state / "handoffs" / f"{aid}.json").read_text())
    assert handoff["application"]["evidence_error"]
    assert handoff["application"]["payload"]["application_id"] == aid
    assert handoff["application"]["payload"]["attachments"] == []
    assert handoff["application"]["questions"][0]["prompt"] == "Travel commitment"
    assert Notifications(db).snapshot("fixture")["items"][0]["kind"] == "answers"


@pytest.mark.parametrize("expired", [False, True])
def test_incompatible_or_expired_answers_need_confirmation(db, expired):
    flow, aid, spec = pending_plan(db)
    old = Memory(db).put(
        FactInput(
            key="application_form_travel",
            scope=aid,
            value="No travel" if expired else "Occasionally",
            status="confirmed",
            provenance={"type": "user_confirmation"},
            effective_to="2000-01-01" if expired else None,
        )
    )
    FormPreparation(db).plan_spec(aid, spec)
    q = db.one("SELECT * FROM questions")
    assert q["reason"]["code"] == (
        "personal_commitment_needed" if expired else "incompatible_answer"
    )
    assert flow.get(aid)["state"] == "missing_answers"
    flow.answer(q["id"], "No travel")
    assert not db.one("SELECT active FROM facts WHERE id=%s", (old["id"],))["active"]
    assert Memory(db).resolve(q["key"], "fixture", aid)["value"] == "No travel"
    assert Notifications(db).snapshot("fixture")["pending"] == 0


@pytest.mark.parametrize(
    "state,kind",
    [
        ("awaiting_challenge", "challenge"),
        ("unresolved", "receipt"),
        ("manual_handoff", "continue"),
        ("review", "continue"),
    ],
)
def test_existing_blockers_are_visible_and_read_is_not_resolution(db, state, kind):
    flow, aid, _ = pending_plan(db)
    db.execute("UPDATE applications SET state=%s WHERE id=%s", (state, aid))
    notice = Notifications(db).snapshot("fixture")["items"][0]
    assert notice["kind"] == kind
    Notifications(db).mark_read(notice["id"], "fixture")
    assert flow.get(aid)["state"] == state
    assert Notifications(db).snapshot("fixture")["pending"] == 1
    db.execute("UPDATE applications SET state='submitted' WHERE id=%s", (aid,))
    assert Notifications(db).snapshot("fixture")["pending"] == 0
    assert Notifications(db).snapshot("fixture", include_resolved=True)["items"][0][
        "resolved_at"
    ]


def test_failed_task_notification_resolves_after_successful_retry(db):
    _, aid, _ = pending_plan(db)
    job = enqueue(db, "fill", {"application_id": aid})
    db.execute("UPDATE jobs SET status='failed' WHERE id=%s", (job["job_id"],))
    assert any(
        n["kind"] == "worker" for n in Notifications(db).snapshot("fixture")["items"]
    )
    retry = enqueue(db, "fill", {"application_id": aid})
    db.execute("UPDATE jobs SET status='complete' WHERE id=%s", (retry["job_id"],))
    assert not any(
        n["kind"] == "worker" for n in Notifications(db).snapshot("fixture")["items"]
    )


def test_prepared_manual_followups_and_separate_requisitions_remain_visible(db):
    from jobsearch.db import js

    flow, aid, _ = pending_plan(db)
    other = flow.create("Intervention Demo", "Software Engineer", "INT-102")["id"]
    for app_id in (aid, other):
        db.execute(
            "UPDATE applications SET state='prepared',imported=true WHERE id=%s",
            (app_id,),
        )
        db.execute(
            "INSERT INTO followups(id,application_id,candidate,category,detail) VALUES(%s,%s,'fixture','manual_application',%s)",
            (
                app_id,
                app_id,
                js({"reason": "The saved form needs a personal narrative"}),
            ),
        )
    inbox = Notifications(db).snapshot("fixture")
    assert inbox["applications"] == 2
    assert {n["kind"] for n in inbox["items"]} == {"followup"}
    assert {n["requisition"] for n in inbox["items"]} == {"INT-101", "INT-102"}
    db.execute("UPDATE followups SET resolved=true WHERE application_id=%s", (aid,))
    inbox = Notifications(db).snapshot("fixture")
    assert inbox["applications"] == 1
    assert inbox["items"][0]["application_id"] == other
    assert flow.get(aid)["state"] == flow.get(other)["state"] == "prepared"


def test_notifications_require_auth_and_preserve_imported_status(db):
    _, aid, _ = pending_plan(db)
    db.execute(
        "UPDATE applications SET candidate='real',state='unresolved',imported=true WHERE id=%s",
        (aid,),
    )
    before = db.one("SELECT * FROM applications WHERE id=%s", (aid,))
    with TestClient(create_app(db.settings)) as client:
        assert client.get("/api/notifications?candidate=real").status_code == 401
        headers = {"X-JSA-Token": db.settings.token()}
        notice = client.get(
            "/api/notifications?candidate=real", headers=headers
        ).json()["items"][0]
        assert (
            client.get("/api/notifications?candidate=fixture", headers=headers).json()[
                "pending"
            ]
            == 0
        )
        assert (
            client.post(
                f"/api/notifications/{notice['id']}/read?candidate=real",
                headers=headers,
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/notifications/{notice['id']}/read?candidate=fixture",
                headers=headers,
            ).status_code
            == 400
        )
    assert before == db.one("SELECT * FROM applications WHERE id=%s", (aid,))


async def test_inbox_opens_question_and_confirmation_resolves_notice(
    db, fixture_server
):
    from playwright.async_api import async_playwright, expect

    flow, aid, spec = pending_plan(db)
    FormPreparation(db).plan_spec(aid, spec)
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
            await page.get_by_role(
                "button", name="View notifications", exact=True
            ).click()
            inbox = page.get_by_role("region", name="Application notifications")
            await expect(
                inbox.get_by_role("heading", name="Answer 1 outstanding question")
            ).to_be_visible()
            await inbox.get_by_role("button", name="Mark as read").click()
            await expect(
                inbox.get_by_text("SEEN · STILL OPEN", exact=True)
            ).to_be_visible()
            await page.reload()
            await page.get_by_label("Workspace", exact=True).select_option("fixture")
            await page.get_by_role(
                "button", name="View notifications", exact=True
            ).click()
            await expect(
                page.get_by_text("SEEN · STILL OPEN", exact=True)
            ).to_be_visible()
            await page.get_by_role("button", name="Review missing answers").click()
            await page.get_by_text("Other actions and handoff tools", exact=True).click()
            await expect(
                page.get_by_role("link", name="Open saved handoff")
            ).to_be_visible()
            await page.get_by_label("Travel commitment", exact=True).select_option(
                "No travel"
            )
            await page.get_by_role("button", name="Confirm answer", exact=True).click()
            await expect(
                page.get_by_role("button", name="Needs attention (0)", exact=True)
            ).to_be_visible()
            assert (
                len(db.all("SELECT * FROM jobs WHERE kind='fill' AND status='queued'"))
                == 1
            )
            assert not db.all("SELECT * FROM approvals")
            assert not db.all("SELECT * FROM attempts")
            assert not errors
        finally:
            await browser.close()
