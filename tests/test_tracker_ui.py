import pytest
from playwright.async_api import async_playwright, expect
from jobsearch.config import file_hash
from jobsearch.tracker import TRACKER_NAME, read_ods
from .test_browser import fixture_server  # noqa: F401
from .test_tracker_updates import prepared


@pytest.mark.usefixtures("fixture_server")
async def test_metrics_filters_charts_and_reviewed_real_mode_write(db):
    aid = prepared(db, real=True)
    path = db.settings.workspace / TRACKER_NAME
    original = file_hash(path)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_role(
                "button", name="Metrics & tracker", exact=True
            ).click()
            await expect(
                page.get_by_role("heading", name="Tracker status", exact=True)
            ).to_be_visible()
            await expect(
                page.get_by_role("heading", name="Applications by month", exact=True)
            ).to_be_visible()
            records = page.locator("section.panel").filter(
                has=page.get_by_role("heading", name="Spreadsheet records", exact=True)
            )
            await expect(
                records.get_by_text("3 of 3 records", exact=True)
            ).to_be_visible()
            await page.get_by_label("Search records").fill("AD-002")
            await expect(
                records.get_by_text("1 of 3 records", exact=True)
            ).to_be_visible()
            await page.get_by_label("Search records").fill("")
            await page.get_by_label("Status", exact=True).select_option("Submitted")
            await expect(
                records.get_by_text("1 of 3 records", exact=True)
            ).to_be_visible()
            await page.get_by_role(
                "button", name="Enable writes to this tracker", exact=True
            ).click()
            await expect(
                page.get_by_role("button", name="Disable tracker writes", exact=True)
            ).to_be_visible()
            assert file_hash(path) == original
            await page.get_by_label("Application to sync").select_option(aid)
            await page.get_by_role(
                "button", name="Preview tracker update", exact=True
            ).click()
            await expect(
                page.get_by_role(
                    "heading", name="Add application to tracker", exact=True
                )
            ).to_be_visible()
            assert file_hash(path) == original
            await page.get_by_role(
                "button", name="Save reviewed tracker update", exact=True
            ).click()
            await expect(
                page.get_by_text(
                    "Tracker saved. The backup, notes, checklist, and handoff are recorded.",
                    exact=True,
                )
            ).to_be_visible()
            assert read_ods(path)["totals"]["tracked"] == 4
            await page.get_by_label("Status", exact=True).select_option("")
            await expect(
                records.get_by_text("4 of 4 records", exact=True)
            ).to_be_visible()
            await page.get_by_role(
                "button", name="Role / requisition", exact=False
            ).click()
            assert not errors
            assert not db.all("SELECT * FROM attempts")
            assert not db.all("SELECT * FROM jobs WHERE kind='submit'")
        finally:
            await browser.close()
