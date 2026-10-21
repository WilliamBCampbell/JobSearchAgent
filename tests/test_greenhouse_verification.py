# ruff: noqa: F811
"""Email challenges use the same attempt; codes stay in the employer browser."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jobsearch.greenhouse_transport import GreenhouseTransport
from jobsearch.queue import Worker
from jobsearch.workflow import ApprovalInput
from .test_greenhouse_browser import prepared, fixture_server  # noqa: F401
from .test_greenhouse_native_contract import native_request


@pytest.mark.parametrize(
    "code,recipient,pending",
    [
        ("captcha-failed", "alex@example.test", True),
        ("invalid-security-code", None, True),
        ("expired-security-code", None, False),
        ("security-code-error", None, False),
        ("unknown", None, False),
    ],
)
async def test_response_classification_does_not_store_challenge_content(
    code, recipient, pending
):
    spec, payload, _ = native_request()
    events = []
    db = SimpleNamespace(
        settings=SimpleNamespace(origin="http://127.0.0.1:8766"),
        execute=lambda *args: events.append(args),
    )
    transport = GreenhouseTransport(
        db, {"id": "fixture", "candidate": "fixture"}, spec, {"payload": payload}, {}
    )
    transport.attempt = "attempt"
    await transport.response(
        SimpleNamespace(
            url=spec.url,
            status=428,
            request=SimpleNamespace(method="POST"),
            json=AsyncMock(
                return_value={
                    "code": code,
                    "security_code_recipient": recipient,
                    "security_code": "Ab12Cd34",
                    "request_token": "private-fixture-token",
                }
            ),
        )
    )
    assert transport.verification_pending is pending
    assert transport.post_status == 428
    assert all(
        secret not in repr(events)
        for secret in ("Ab12Cd34", "private-fixture-token", "alex@example.test")
    )


async def test_continuation_releases_only_one_matching_request():
    spec, payload, body = native_request()
    events = []
    db = SimpleNamespace(
        settings=SimpleNamespace(
            origin="http://127.0.0.1:8766", token=lambda: "fixture"
        ),
        execute=lambda *args: events.append(args),
        audit=lambda *args: events.append(args),
    )
    transport = GreenhouseTransport(
        db, {"id": "fixture", "candidate": "fixture"}, spec, {"payload": payload}, {}
    )
    transport.check = lambda: None
    transport.attempt = "attempt"
    transport.posted = True
    transport.verification_pending = True
    transport.request_token = "native-fixture-token"
    body.update(security_code="Ab12Cd34", request_token="native-fixture-token")

    def route():
        return SimpleNamespace(
            request=SimpleNamespace(
                url=spec.url,
                method="POST",
                headers={},
                post_data_buffer=json.dumps(body).encode(),
            ),
            abort=AsyncMock(),
            continue_=AsyncMock(),
        )

    blocked = route()
    await transport.handle(blocked, {"phase": "send"})
    blocked.continue_.assert_not_awaited()  # No explicit continue action.
    transport.verification_allowed = True
    body["request_token"] = "different-token"
    blocked = route()
    await transport.handle(blocked, {"phase": "send"})
    blocked.continue_.assert_not_awaited()
    body["request_token"] = "native-fixture-token"
    body["job_application"]["email"] = "changed@example.test"
    blocked = route()
    await transport.handle(blocked, {"phase": "send"})
    blocked.continue_.assert_not_awaited()
    body["job_application"]["email"] = "alex@example.test"
    valid = route()
    await transport.handle(valid, {"phase": "send"})
    valid.continue_.assert_awaited_once()
    repeated = route()
    await transport.handle(repeated, {"phase": "send"})
    repeated.continue_.assert_not_awaited()
    assert transport.verification_count == 1
    transport.verification_pending = True
    transport.verification_allowed = True
    transport.verification_count = 3
    exhausted = route()
    await transport.handle(exhausted, {"phase": "send"})
    exhausted.continue_.assert_not_awaited()
    assert all(
        secret not in repr(events)
        for secret in ("Ab12Cd34", "native-fixture-token", "alex@example.test")
    )


async def test_email_challenge_wrong_code_then_receipt_same_approval(
    db, fixture_server
):
    from playwright.async_api import expect

    flow, aid = prepared(db, "greenhouse_email_challenge")
    worker = Worker(db)
    try:
        await worker.browser.fill(aid)
        flow.authorize(
            aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
        )
        await worker.run_once()
        assert flow.get(aid)["state"] == "awaiting_challenge"
        attempt = db.one("SELECT * FROM attempts WHERE application_id=%s", (aid,))
        assert attempt["result"]["send_started"]
        assert attempt["result"].get("challenge_type") == "email", attempt["result"]
        assert not flow.submission_recovery(aid)["can_retry"]
        page = worker.browser.greenhouse.sessions[aid]["page"]
        ui = await worker.browser.greenhouse.sessions[aid]["browser"].new_page()
        await ui.goto(db.settings.origin + "/#token=" + db.settings.token())
        await ui.get_by_label("Workspace", exact=True).select_option("fixture")
        await ui.get_by_role("button", name="Applications", exact=True).click()
        await ui.get_by_text("Example Greenhouse", exact=True).click()
        await expect(
            ui.get_by_role(
                "heading", name="Enter the emailed code in the employer browser"
            )
        ).to_be_visible()
        await expect(
            ui.get_by_role(
                "button", name="Open browser and fill application", exact=True
            )
        ).to_have_count(0)
        # An empty code cannot send, and a fresh worker cannot reconstruct a send.
        with pytest.raises(ValueError, match="eight-character"):
            await worker.browser.greenhouse.continue_verification(aid)
        fresh = Worker(db)
        with pytest.raises(ValueError, match="no longer connected"):
            await fresh.browser.greenhouse.continue_verification(aid)
        for value, state in (
            ("ZZ99ZZ99", "awaiting_challenge"),
            ("Ab12Cd34", "submitted"),
        ):
            for i, char in enumerate(value):
                await page.locator(f"#security-input-{i}").fill(char)
            async with ui.expect_response(
                lambda r: r.url.endswith("/continue-verification")
            ) as response:
                await ui.get_by_role(
                    "button", name="Continue verification", exact=True
                ).click()
            assert (await response.value).status == 200
            await worker.run_once()
            assert flow.get(aid)["state"] == state
            if state == "awaiting_challenge":
                await expect(
                    ui.get_by_text("Greenhouse did not accept that code.", exact=False)
                ).to_be_visible(timeout=10000)
        await expect(
            ui.get_by_role("heading", name="Employer receipt recorded", exact=True)
        ).to_be_visible(timeout=10000)
        assert db.one("SELECT count(*) n FROM approvals")["n"] == 1
        assert db.one("SELECT count(*) n FROM attempts")["n"] == 1
        assert db.one("SELECT count(*) n FROM receipts")["n"] == 1
        assert (
            db.one(
                "SELECT count(*) n FROM audit WHERE event='fixture.greenhouse_received'"
            )["n"]
            == 1
        )
        assert (
            db.one(
                "SELECT count(*) n FROM audit WHERE event='fixture.greenhouse_uploaded'"
            )["n"]
            == 1
        )
        rows = [
            db.all("SELECT * FROM " + table)
            for table in ("jobs", "attempts", "audit", "facts", "answer_cache")
        ]
        assert "Ab12Cd34" not in repr(rows)
        assert "ZZ99ZZ99" not in repr(rows)
        assert "synthetic-native-request-token" not in repr(rows)
    finally:
        await worker.browser.close()


def test_restart_during_verification_never_requeues_a_send(db):
    from .test_workflow_tracker import ready

    flow, aid, _ = ready(db)
    job = flow.authorize(
        aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
    )
    flow.begin_attempt(aid)
    flow.transition(aid, "receipt_verification")
    db.execute(
        "UPDATE jobs SET kind='continue_verification',status='running',worker='dead',lease_until=now()-interval '1 minute' WHERE id=%s",
        (job["job_id"],),
    )
    Worker(db).recover()
    assert flow.get(aid)["state"] == "unresolved"
    assert (
        db.one("SELECT status FROM jobs WHERE id=%s", (job["job_id"],))["status"]
        == "unresolved"
    )
    assert not db.all("SELECT * FROM receipts")
    assert not db.all("SELECT * FROM jobs WHERE status='queued'")
