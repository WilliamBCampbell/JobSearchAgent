# ruff: noqa: F811
"""Form readiness and failures before a retry starts sending."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Page, TimeoutError as BrowserTimeout

from jobsearch.forms import FormPreparation, FormSpec, greenhouse_form
from jobsearch.greenhouse_browser import GreenhouseBrowser
from jobsearch.greenhouse_fixture import definition
from .test_greenhouse_browser import prepared, fixture_server  # noqa: F401
from .test_verification_recovery import verification_stopped, decision


async def test_form_opens_while_a_background_request_is_pending(
    db, fixture_server, monkeypatch
):
    flow, aid = prepared(db)
    spec = greenhouse_form(
        definition(), db.settings.origin + "/fixture/greenhouse/" + aid, "GH-1"
    )
    background = db.settings.origin + "/fixture/pending-background"
    entered, release = asyncio.Event(), asyncio.Event()
    original = Page.goto

    async def goto(page, url, **kwargs):
        async def pending(route):
            entered.set()
            await release.wait()
            await route.fulfill(body="", content_type="application/javascript")

        await page.route(background, pending)
        await page.add_init_script(
            "window.addEventListener('DOMContentLoaded',()=>fetch("
            + repr(background)
            + "))"
        )
        return await original(page, url, **kwargs)

    monkeypatch.setattr(Page, "goto", goto)
    adapter = GreenhouseBrowser(db)
    try:
        page, _ = await asyncio.wait_for(
            adapter.session(flow.get(aid), spec), timeout=10
        )
        await asyncio.wait_for(entered.wait(), timeout=3)
        assert not release.is_set()
        await adapter.validate_dom(page, spec)
        assert not db.all("SELECT * FROM attempts")
    finally:
        release.set()
        await adapter.close()


async def test_retry_opening_timeout_preserves_the_original_outcome_and_approval(
    db, monkeypatch
):
    flow, aid, attempt, _ = verification_stopped(db)
    flow.confirm_not_submitted(aid, decision(flow, aid))
    flow.retry_submission(aid)
    db.execute("UPDATE jobs SET status='failed'")
    app = flow.get(aid)
    spec = FormSpec(**app["payload"]["form_plan"]["spec"])
    original = db.one("SELECT result FROM attempts WHERE id=%s", (attempt,))["result"]
    monkeypatch.setattr(FormPreparation, "validate_ready", lambda *args: spec)
    monkeypatch.setattr("jobsearch.forms.greenhouse_form", lambda *args: spec)
    adapter = GreenhouseBrowser(db)
    monkeypatch.setattr(
        adapter,
        "session",
        AsyncMock(side_effect=BrowserTimeout("Synthetic opening timeout")),
    )
    with pytest.raises(ValueError, match="timed out during opening form"):
        await adapter.submit(aid)
    assert flow.get(aid)["state"] == "unresolved"
    result = db.one("SELECT result FROM attempts WHERE id=%s", (attempt,))["result"]
    assert result["run_id"] == original["run_id"]
    assert (
        result["not_submitted_confirmation"] == original["not_submitted_confirmation"]
    )
    assert flow.get(aid)["payload"]["browser_hash"] == app["payload"]["browser_hash"]
    assert flow.submission_recovery(aid)["can_retry"]
    assert not db.one("SELECT 1 FROM approvals WHERE invalidated")
    latest = db.one(
        "SELECT detail FROM audit WHERE event='greenhouse.attempt_finished' ORDER BY seq DESC LIMIT 1"
    )["detail"]
    assert latest["attempt_id"] is None
    assert latest["post_observed"] is False
