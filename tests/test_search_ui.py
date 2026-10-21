"""Exercise the actual search button and durable worker from the real-records view."""

import pytest
from pathlib import Path
from playwright.async_api import async_playwright
from jobsearch.config import REPO
from jobsearch.queue import Worker
from .test_browser import fixture_server  # noqa: F401


@pytest.mark.usefixtures("fixture_server")
async def test_search_button_switches_to_fixtures_and_displays_results(db):
    assert (REPO / "web/dist/index.html").exists(), (
        "Build the UI before running browser tests"
    )
    original = db.settings.workspace / "untouched.txt"
    original.write_text("Original workspace remains unchanged")
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
            for track, profile in [
                ("software", "software_dev"),
                ("applied_ai", "ai"),
                ("solutions", "solutions"),
            ]:
                await page.get_by_label("Workspace", exact=True).select_option("real")
                await page.get_by_role("button", name="Find roles", exact=True).click()
                details = page.get_by_text("Sample search for testing", exact=True)
                if not await page.get_by_label("Search track", exact=True).is_visible():
                    await details.click()
                await page.get_by_label("Search track", exact=True).select_option(track)
                button = page.get_by_role(
                    "button", name="Search fixture sources", exact=True
                )
                assert await button.is_enabled()
                async with page.expect_response(
                    lambda r: r.url.endswith("/api/search")
                    and r.request.method == "POST"
                ) as pending:
                    await button.click()
                response = await pending.value
                assert response.status == 200
                result = await response.json()
                await page.get_by_text(
                    "Sample search queued in the fixture workspace.", exact=False
                ).wait_for()
                assert (
                    await page.get_by_label("Workspace", exact=True).input_value()
                    == "fixture"
                )
                job = db.one("SELECT * FROM jobs WHERE id=%s", (result["job_id"],))
                assert job["input"]["mode"] == "fixture"
                assert (
                    job["input"]["track"] == track
                    and job["input"]["profile"] == profile
                )
                await worker.run_once()
                await page.get_by_role(
                    "heading", name="3 discovery groups", exact=True
                ).wait_for()
                job = db.one("SELECT * FROM jobs WHERE id=%s", (result["job_id"],))
                assert job["status"] == "complete"
                assert Path(job["result"]["folder"]).is_relative_to(
                    db.settings.fixture_workspace
                )
            assert not errors
            assert original.read_text() == "Original workspace remains unchanged"
            assert list(db.settings.workspace.iterdir()) == [original]
            assert not db.all("SELECT * FROM applications")
        finally:
            await browser.close()
            await worker.browser.close()


@pytest.mark.usefixtures("fixture_server")
async def test_real_agent_button_starts_a_durable_run_and_restores_it(db):
    db.set_setting("live_search", True)
    db.set_setting("llm", {"backend": "codex_exec", "enabled": True})
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page()
        try:
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_role("button", name="Find roles", exact=True).click()
            await page.get_by_label("Agent search track").select_option("solutions")
            async with page.expect_response(
                lambda r: r.url.endswith("/api/hunts") and r.request.method == "POST"
            ) as pending:
                await page.get_by_role(
                    "button", name="Find my best matches", exact=True
                ).click()
            response = await pending.value
            assert response.status == 200
            hunt = await response.json()
            assert (
                hunt["candidate"] == "real" and hunt["request"]["track"] == "solutions"
            )
            assert db.one("SELECT count(*) n FROM jobs")["n"] == 1
            assert not db.all("SELECT * FROM llm_runs")
            await page.reload()
            await page.get_by_role("button", name="Find roles", exact=True).click()
            await page.get_by_role("button", name="Pause after current task").wait_for()
            await page.get_by_role("button", name="Pause after current task").click()
            await page.get_by_role(
                "button", name="Resume search", exact=True
            ).wait_for()
            assert (
                db.one("SELECT status FROM hunts WHERE id=%s", (hunt["id"],))["status"]
                == "paused"
            )
        finally:
            await browser.close()
