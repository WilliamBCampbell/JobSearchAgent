import pytest
from playwright.async_api import async_playwright, expect
from jobsearch.attention_summary import concerns
from jobsearch.db import js, uid
from jobsearch.notifications import Notifications
from jobsearch.fixtures import assessment
from jobsearch.queue import enqueue
from jobsearch.hunt import Hunt
from jobsearch.workflow import ApprovalInput
from .test_selection import conditional_app
from .test_workflow_tracker import ready
from .test_browser import fixture_server  # noqa: F401


def inbox_app(db):
    flow, aid = conditional_app(db)
    db.execute("UPDATE applications SET state='review' WHERE id=%s", (aid,))
    return flow, aid


def test_initial_score_survives_rescore_and_unknown_is_not_invented(db):
    flow, aid = inbox_app(db)
    first = flow.get(aid)["score"]["total"]
    data = assessment(db)
    data.requirements[0].deduction += 10
    flow.score(aid, data)
    db.execute("UPDATE applications SET state='review' WHERE id=%s", (aid,))
    notice = Notifications(db).snapshot("fixture")["items"][0]
    assert notice["initial_score"]["total"] == first
    assert notice["current_score"] == first - 10
    assert notice["initial_score"]["source"] == "first_applied_score"
    db.execute("DELETE FROM audit WHERE event='fit.initial_score'")
    notice = Notifications(db).snapshot("fixture")["items"][0]
    assert notice["initial_score"] is None and notice["current_score"] == first - 10


def test_legacy_evaluation_is_labeled_and_read_status_is_stable(db):
    _, aid = inbox_app(db)
    for total, days in [(45, 2), (75, 1)]:
        rid = uid()
        db.execute(
            "INSERT INTO llm_runs(id,task,candidate,application_id,fingerprint,backend,model,effort,status,input,output,created_at) VALUES(%s,'fit_assessment','fixture',%s,%s,'fixture','fixture','low','complete','{}',%s,now()-%s*interval '1 day')",
            (rid, aid, rid, js({"score_preview": {"total": total}}), days),
        )
    service = Notifications(db)
    notice = service.snapshot("fixture")["items"][0]
    assert notice["initial_score"]["total"] == 45
    assert notice["initial_score"]["source"] == "first_saved_evaluation"
    service.mark_read(notice["id"], "fixture")
    db.execute(
        "UPDATE applications SET score=jsonb_set(score,'{total}','87') WHERE id=%s",
        (aid,),
    )
    assert service.snapshot("fixture")["unread"] == 0
    assert service.snapshot("real")["items"] == []


def test_concerns_prioritize_required_gaps_and_omit_resolved_facts():
    base = {
        "classification": "required",
        "assessment": "unconfirmed",
        "material": True,
        "central": False,
        "deduction": 0,
    }
    rows = [
        base
        | {"text": "Optional tool", "classification": "preferred", "deduction": 20},
        base | {"text": "Central skill", "central": True},
        base | {"text": "License", "assessment": "hard_stop"},
        base | {"text": "Resolved", "assessment": "strong"},
        base | {"text": "Minor", "material": False},
        base | {"text": "Residence"},
        base | {"text": "central skill", "central": True},
    ]
    assert [r["text"] for r in concerns({"requirements": rows})] == [
        "License",
        "Central skill",
        "Residence",
        "Optional tool",
    ]


def test_discard_cancels_queue_resolves_notices_and_preserves_history(db):
    flow, aid, _ = ready(db)
    hid, lid = uid(), uid()
    db.execute(
        "INSERT INTO hunts(id,candidate,request) VALUES(%s,'fixture','{}')", (hid,)
    )
    db.execute(
        "INSERT INTO hunt_leads(id,hunt_id,ordinal,discovery,application_id,status) VALUES(%s,%s,0,'{}',%s,'qualified')",
        (lid, hid, aid),
    )
    job = flow.authorize(
        aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
    )
    db.execute(
        "INSERT INTO followups(id,application_id,candidate,category,detail) VALUES(%s,%s,'fixture','manual_application','{}')",
        (uid(), aid),
    )
    Notices = Notifications(db)
    assert Notices.snapshot("fixture")["pending"]
    before = flow.get(aid)
    flow.control(aid, "discard", "Not a role I want to pursue")
    after = flow.get(aid)
    assert (
        after["state"] == "skipped"
        and after["score"] == before["score"]
        and after["payload"] == before["payload"]
    )
    assert db.one("SELECT invalidated FROM approvals")["invalidated"]
    assert (
        db.one("SELECT status FROM jobs WHERE id=%s", (job["job_id"],))["status"]
        == "cancelled"
    )
    assert not db.all("SELECT * FROM followups WHERE NOT resolved")
    Hunt(db).lead(lid, "ready")  # A stale coordinator tick cannot reopen the lead.
    assert (
        db.one("SELECT status FROM hunt_leads WHERE id=%s", (lid,))["status"]
        == "discarded"
    )
    assert Notices.snapshot("fixture")["pending"] == 0
    assert Notices.snapshot("fixture", True)["items"][0]["resolved_at"]
    assert (
        db.one("SELECT detail FROM audit WHERE event='workflow.discarded'")["detail"][
            "prior_state"
        ]
        == "authorized"
    )
    assert (
        db.one("SELECT state FROM applications WHERE requisition='AD-002'")["state"]
        == "deferred"
    )
    with pytest.raises(ValueError, match="Discarded"):
        enqueue(db, "package", {"application_id": aid})


@pytest.mark.parametrize(
    "blocker", ["attempt", "receipt", "imported", "running", "unresolved"]
)
def test_discard_cannot_hide_outcomes_or_interrupt_active_work(db, blocker):
    flow, aid, _ = ready(db)
    if blocker == "attempt":
        flow.authorize(
            aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
        )
        flow.begin_attempt(aid)
    elif blocker == "receipt":
        db.execute("UPDATE applications SET state='manual_handoff' WHERE id=%s", (aid,))
        flow.record_receipt(
            aid,
            None,
            {"confirmed": True, "text": "Synthetic receipt", "requisition": "NS-101"},
            "user_confirmation",
        )
    elif blocker == "imported":
        db.execute("UPDATE applications SET imported=true WHERE id=%s", (aid,))
    elif blocker == "unresolved":
        db.execute("UPDATE applications SET state='unresolved' WHERE id=%s", (aid,))
    else:
        job = enqueue(db, "fill", {"application_id": aid})
        db.execute("UPDATE jobs SET status='running' WHERE id=%s", (job["job_id"],))
    before = flow.get(aid)
    with pytest.raises(ValueError):
        flow.control(aid, "discard")
    assert flow.get(aid) == before


def test_paused_application_can_be_discarded_without_resuming(db):
    flow, aid = inbox_app(db)
    flow.control(aid, "pause")
    assert Notifications(db).snapshot("fixture")["items"][0]["can_discard"]
    flow.control(aid, "discard")
    assert flow.get(aid)["state"] == "skipped" and not flow.get(aid)["paused"]


@pytest.mark.usefixtures("fixture_server")
async def test_inbox_shows_score_concerns_and_discards_role(db):
    flow, aid = inbox_app(db)
    initial = flow.get(aid)["score"]["total"]
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_label("Workspace", exact=True).select_option("fixture")
            await (
                page.locator("nav")
                .get_by_role("button", name="Needs attention", exact=False)
                .click()
            )
            card = page.locator("article.intervention").filter(
                has_text="Selection Demo"
            )
            await expect(
                card.get_by_text(f"Initial score: {initial:g}/100", exact=True)
            ).to_be_visible()
            await expect(
                card.get_by_role("heading", name="Biggest current requirement concerns")
            ).to_be_visible()
            await expect(
                card.get_by_role("button", name="Discard", exact=True)
            ).to_be_enabled()
            await card.get_by_role("button", name="Discard", exact=True).click()
            await expect(
                page.get_by_text("No applications are waiting on you.", exact=True)
            ).to_be_visible()
            assert flow.get(aid)["state"] == "skipped"
            assert not errors
        finally:
            await browser.close()
