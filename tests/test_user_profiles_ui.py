import pytest
from playwright.async_api import async_playwright, expect

from jobsearch.db import DB
from jobsearch.user_profiles import UserProfiles
from .test_browser import fixture_server  # noqa: F401
from .test_user_profiles import put_name


@pytest.mark.usefixtures("fixture_server")
async def test_create_switch_and_two_tabs_keep_separate_profiles(db, tmp_path):
    put_name(db, "Alex Example")
    source = tmp_path / "household-documents"
    source.mkdir()
    (source / "resume.txt").write_text("Synthetic household resume")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        context = await browser.new_context()
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await expect(page.get_by_label("User profile", exact=True)).to_have_value(
                "default"
            )
            await expect(page.get_by_role("heading", name="Overview", exact=True)).to_be_visible()
            await expect(page.get_by_text("Create user profile", exact=True)).to_have_count(0)
            await expect(page.get_by_label("Data mode", exact=True)).to_have_count(0)
            await page.get_by_role("button", name="Privacy & settings", exact=True).click()
            await expect(page.get_by_label("Data mode", exact=True)).to_have_value("real")
            await page.get_by_text("Create user profile", exact=True).click()
            await page.get_by_label("Profile name", exact=True).fill("Sam's search")
            await page.get_by_label("Source document folder", exact=True).fill(
                str(source)
            )
            await page.get_by_role("button", name="Create profile", exact=True).click()
            await page.wait_for_url("**/?profile=*")
            profiles = UserProfiles(db.settings)
            profile = next(
                row for row in profiles.list() if row["name"] == "Sam's search"
            )
            await expect(page.get_by_label("User profile", exact=True)).to_have_value(
                profile["id"]
            )
            second = DB(profiles.resolve(profile["id"]))
            put_name(second, "Sam Example")
            await page.get_by_role(
                "button", name="Profile & answers", exact=True
            ).click()
            await expect(page.get_by_text("Sam Example", exact=True)).to_be_visible()
            await expect(page.get_by_text("Alex Example", exact=True)).to_have_count(0)
            other_tab = await context.new_page()
            await other_tab.goto(db.settings.origin + "/")
            await other_tab.get_by_role(
                "button", name="Profile & answers", exact=True
            ).click()
            await expect(
                other_tab.get_by_text("Alex Example", exact=True)
            ).to_be_visible()
            await page.reload()
            await expect(page.get_by_label("User profile", exact=True)).to_have_value(
                profile["id"]
            )
            await page.get_by_role("button", name="Documents", exact=True).click()
            await expect(page.get_by_text(str(source), exact=True)).to_be_visible()
            await page.get_by_label("User profile", exact=True).select_option("default")
            await page.wait_for_url("**/?profile=default")
            await page.get_by_role(
                "button", name="Profile & answers", exact=True
            ).click()
            await expect(page.get_by_text("Alex Example", exact=True)).to_be_visible()
            await expect(page.get_by_text("Sam Example", exact=True)).to_have_count(0)
            assert not errors
            assert (source / "resume.txt").read_text() == "Synthetic household resume"
        finally:
            await browser.close()


@pytest.mark.usefixtures("fixture_server")
async def test_fixture_browser_uses_its_own_profile(db, tmp_path):
    from jobsearch.browser import BrowserWorker
    from jobsearch.workflow import Workflow
    from .test_user_profiles import create_profile

    profiles = UserProfiles(db.settings)
    profile = create_profile(profiles, tmp_path)
    second = DB(profiles.resolve(profile["id"]))
    app = Workflow(second).create("Household Fixture Employer", "Engineer", "FIX-2")
    worker = BrowserWorker(second)
    try:
        _, page = await worker.session(app)
        await expect(
            page.get_by_role("heading", name="Household Fixture Employer")
        ).to_be_visible()
        assert not db.all("SELECT * FROM applications")
        assert not second.all("SELECT * FROM attempts")
    finally:
        await worker.close()


@pytest.mark.usefixtures("fixture_server")
async def test_profile_load_failure_can_retry(db):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page()
        try:
            await page.route("**/api/profiles", lambda route: route.fulfill(
                status=503, content_type="application/json",
                body='{"detail":"Service unavailable"}',
            ))
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            selector = page.get_by_label("User profile", exact=True)
            await expect(selector).to_contain_text("Profiles unavailable")
            await expect(selector).to_be_disabled()
            await expect(page.get_by_role("alert")).to_contain_text("Couldn’t load profiles.")
            await page.unroute("**/api/profiles")
            await page.get_by_role("button", name="Retry profiles", exact=True).click()
            await expect(selector).to_be_enabled()
            await expect(selector).to_have_value("default")
            await expect(selector).to_contain_text("My profile")
            await expect(page.get_by_role("button", name="Retry profiles")).to_have_count(0)
        finally:
            await browser.close()
