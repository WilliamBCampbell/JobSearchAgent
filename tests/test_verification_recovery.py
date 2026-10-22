# ruff: noqa: F811
"""Explicit negative outcome reconciliation for completed initial 428 runs."""

import pytest
from pydantic import ValidationError

from jobsearch.db import js, uid
from jobsearch.queue import enqueue
from jobsearch.workflow import NotSubmittedInput
from .test_greenhouse_recovery import stopped
from .test_browser import fixture_server  # noqa: F401


@pytest.mark.parametrize(
    "confirmed,title",
    [
        (False, "Resolve the unfinished verification"),
        (True, "Approved application is ready to retry"),
    ],
)
def test_verification_notifications_do_not_claim_nothing_was_sent(confirmed, title):
    from jobsearch.notifications import Notifications

    issues = Notifications.issues(
        {
            "id": "fixture",
            "state": "unresolved",
            "imported": False,
            "payload": {},
            "submission_recovery": {
                "kind": "verification",
                "can_retry": confirmed,
                "can_confirm_not_submitted": not confirmed,
            },
        },
        [],
        [],
        [],
    )
    assert issues[0][1]["title"] == title
    assert "no final application request" not in issues[0][1]["message"]


def verification_stopped(db):
    flow, aid, attempt, path = stopped(db)
    db.execute("UPDATE attempts SET result=result || %s", (js({"send_started": True}),))
    db.execute(
        "UPDATE audit SET detail=detail || %s WHERE event='greenhouse.attempt_finished'",
        (js({"post_observed": True, "post_status": 428, "receipt_status": None}),),
    )
    return flow, aid, attempt, path


def decision(flow, aid):
    recovery = flow.submission_recovery(aid)
    return NotSubmittedInput(
        confirm=True,
        **{key: recovery[key] for key in ("attempt_id", "run_id", "reviewed_hash")},
    )


def test_negative_confirmation_does_not_send_and_retry_preserves_history(db):
    flow, aid, attempt, _ = verification_stopped(db)
    assert flow.submission_recovery(aid)["can_confirm_not_submitted"]
    with pytest.raises(ValueError):
        flow.retry_submission(aid)
    request = decision(flow, aid)
    flow.confirm_not_submitted(aid, request)
    assert flow.get(aid)["state"] == "unresolved"
    assert not db.all("SELECT * FROM jobs WHERE status='queued'")
    assert not db.all("SELECT * FROM receipts")
    assert db.one("SELECT result FROM attempts")["result"]["send_started"]
    assert flow.submission_recovery(aid, validate=True)["can_retry"]
    flow.retry_submission(aid)
    assert flow.begin_attempt(aid, mark_attached=False) == attempt
    assert db.one("SELECT count(*) n FROM approvals")["n"] == 1
    assert db.one("SELECT count(*) n FROM attempts")["n"] == 1
    archived = db.one(
        "SELECT detail FROM audit WHERE event='submission.previous_run_preserved'"
    )["detail"]["result"]
    assert archived["send_started"] is True
    assert archived["not_submitted_confirmation"]["run_id"] == request.run_id
    new = db.one("SELECT result FROM attempts")["result"]
    assert new["run_id"] != request.run_id
    assert "not_submitted_confirmation" not in new
    flow.unresolved(aid, "New worker stopped with an unknown outcome")
    db.execute("UPDATE jobs SET status='failed'")
    assert not flow.submission_recovery(aid)["can_retry"]
    with pytest.raises(ValueError):
        flow.confirm_not_submitted(aid, request)


def test_legacy_opening_timeout_does_not_erase_confirmed_verification_outcome(db):
    flow, aid, attempt, _ = verification_stopped(db)
    flow.confirm_not_submitted(aid, decision(flow, aid))
    original = db.one("SELECT result FROM attempts WHERE id=%s", (attempt,))["result"]
    db.audit(
        "greenhouse.attempt_finished",
        aid,
        {
            "attempt_id": attempt,
            "run_id": original["run_id"],
            "stage": "opening_form",
            "post_observed": False,
            "post_status": None,
            "receipt_status": None,
        },
    )
    assert flow.submission_recovery(aid)["can_retry"]
    assert (
        db.one("SELECT result FROM attempts WHERE id=%s", (attempt,))["result"]
        == original
    )


@pytest.mark.parametrize(
    "blocker",
    [
        "success",
        "unknown",
        "receipt_status",
        "no_stop",
        "wrong_run",
        "continuation",
        "continuation_job",
        "active_job",
        "receipt",
        "paused",
        "approval",
    ],
)
def test_unknown_or_changed_outcomes_cannot_be_cleared(db, blocker):
    flow, aid, _, _ = verification_stopped(db)
    request = decision(flow, aid)
    if blocker in ("success", "unknown", "receipt_status"):
        change = (
            {"receipt_status": 200}
            if blocker == "receipt_status"
            else {"post_status": 200 if blocker == "success" else None}
        )
        db.execute(
            "UPDATE audit SET detail=detail || %s WHERE event='greenhouse.attempt_finished'",
            (js(change),),
        )
    elif blocker == "no_stop":
        db.execute("DELETE FROM audit WHERE event='greenhouse.attempt_finished'")
    elif blocker == "wrong_run":
        db.execute("UPDATE attempts SET result=result || %s", (js({"run_id": uid()}),))
    elif blocker == "continuation":
        db.execute(
            "UPDATE attempts SET result=result || %s", (js({"verification_count": 1}),)
        )
    elif blocker == "continuation_job":
        enqueue(db, "continue_verification", {"application_id": aid})
        db.execute("UPDATE jobs SET status='failed' WHERE kind='continue_verification'")
    elif blocker == "active_job":
        enqueue(db, "reconcile", {"application_id": aid})
    elif blocker == "receipt":
        flow.record_receipt(
            aid,
            request.attempt_id,
            {
                "confirmed": True,
                "text": "Fixture confirmation",
                "requisition": flow.get(aid)["requisition"],
            },
        )
    elif blocker == "paused":
        flow.control(aid, "pause")
    else:
        db.execute("UPDATE approvals SET invalidated=true")
    assert not flow.submission_recovery(aid).get("can_confirm_not_submitted")
    with pytest.raises(ValueError):
        flow.confirm_not_submitted(aid, request)


def test_confirmation_cannot_replace_approval_or_attachment_validation(db):
    flow, aid, _, path = verification_stopped(db)
    request = decision(flow, aid)
    with pytest.raises(ValidationError):
        NotSubmittedInput(**(request.model_dump() | {"confirm": False}))
    with pytest.raises(ValueError):
        flow.confirm_not_submitted(
            aid, request.model_copy(update={"reviewed_hash": "a" * 64})
        )
    flow.confirm_not_submitted(aid, request)
    path.write_bytes(b"modified attachment")
    assert not flow.submission_recovery(aid, validate=True)["can_retry"]
    with pytest.raises(ValueError):
        flow.retry_submission(aid)


async def test_ui_negative_outcome_then_separate_retry(db, fixture_server):
    from playwright.async_api import async_playwright, expect

    flow, aid, _, _ = verification_stopped(db)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        try:
            page = await browser.new_page()
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_role(
                "button", name="Privacy & settings", exact=True
            ).click()
            await page.get_by_label("Data mode", exact=True).select_option("fixture")
            await page.get_by_role("button", name="Applications", exact=True).click()
            await page.get_by_text("Northstar Demo", exact=True).click()
            await expect(
                page.get_by_role("heading", name="Resolve the unfinished verification")
            ).to_be_visible()
            button = page.get_by_role(
                "button", name="Confirm not submitted", exact=True
            )
            await expect(button).to_be_disabled()
            await page.get_by_label(
                "I did not complete verification or submit this application elsewhere."
            ).check()
            async with page.expect_response(
                lambda r: r.url.endswith("/confirm-not-submitted")
            ) as response:
                await button.click()
            assert (await response.value).status == 200
            assert not db.all("SELECT * FROM jobs WHERE status='queued'")
            assert not db.all("SELECT * FROM receipts")
            await expect(
                page.get_by_role("heading", name="Verification was not completed")
            ).to_be_visible(timeout=10000)
            async with page.expect_response(
                lambda r: r.url.endswith("/retry-submission")
            ) as response:
                await page.get_by_role(
                    "button", name="Retry approved application", exact=True
                ).click()
            assert (await response.value).status == 200
            assert (
                db.one(
                    "SELECT count(*) n FROM jobs WHERE status='queued' AND kind='submit'"
                )["n"]
                == 1
            )
            assert db.one("SELECT count(*) n FROM approvals")["n"] == 1
        finally:
            await browser.close()
