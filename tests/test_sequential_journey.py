# ruff: noqa: F811
import pytest
from playwright.async_api import async_playwright, expect
from jobsearch.fixtures import assessment
from jobsearch.workflow import Workflow
from jobsearch.queue import Worker
from .test_browser import fixture_server  # noqa: F401
from .test_llm import application


@pytest.mark.usefixtures("fixture_server")
async def test_selection_documents_answers_final_approval_and_receipt_in_order(db):
    expect.set_options(timeout=15000)
    app = application(db)
    flow = Workflow(db)
    aid = app["id"]
    flow.score(aid, assessment(db))
    worker = Worker(db)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_label("Workspace", exact=True).select_option("fixture")
            await page.get_by_role("button", name="Applications", exact=True).click()
            await page.get_by_text("Evaluation Demo", exact=True).click()
            guide = page.get_by_role("region", name="Application next step")
            await expect(
                guide.get_by_role("heading", name="Decide whether to pursue this role")
            ).to_be_visible()
            await page.get_by_role(
                "button", name="Select this role", exact=True
            ).click()
            await guide.get_by_role(
                "button", name="Prepare documents", exact=True
            ).click()
            await expect(
                guide.get_by_role("heading", name="Preparing your documents")
            ).to_be_visible()
            await worker.run_once()
            await expect(
                guide.get_by_role("heading", name="Review your resume and cover letter")
            ).to_be_visible(timeout=15000)
            assert not db.all("SELECT * FROM jobs WHERE kind='fill'")
            assert not db.all("SELECT * FROM approvals")
            await page.get_by_role(
                "button", name="Documents look good", exact=True
            ).click()
            await expect(
                guide.get_by_role("button", name="Continue to application form")
            ).to_be_enabled()
            package = flow.get(aid)["payload"]["package"]
            # Exercise the reported selected+reviewed recovery after a score update.
            db.execute("UPDATE applications SET state='selected' WHERE id=%s", (aid,))
            flow.score(aid, assessment(db))
            await guide.get_by_role(
                "button", name="Continue to application form"
            ).click()
            await expect(
                guide.get_by_role("heading", name="Preparing the employer application")
            ).to_be_visible()
            await worker.run_once()
            await expect(
                guide.get_by_role("heading", name="Answer 2 outstanding questions")
            ).to_be_visible(timeout=15000)
            assert flow.get(aid)["payload"]["package"] == package
            await page.get_by_role("textbox", name="Your answer", exact=True).fill(
                "125000"
            )
            await page.get_by_role(
                "button", name="Confirm answer", exact=True
            ).first.click()
            await expect(
                guide.get_by_role("heading", name="Answer 1 outstanding question")
            ).to_be_visible()
            await page.get_by_role(
                "combobox", name="Your answer", exact=True
            ).select_option("Up to 10%")
            await page.get_by_role("button", name="Confirm answer", exact=True).click()
            await expect(
                guide.get_by_role("heading", name="Preparing the employer application")
            ).to_be_visible()
            await worker.run_once()
            await expect(
                guide.get_by_role("heading", name="Review and approve this application")
            ).to_be_visible(timeout=15000)
            assert not db.all("SELECT * FROM attempts")
            assert not db.all("SELECT * FROM approvals")
            await guide.get_by_role("button", name="Go to final review").click()
            await page.get_by_role(
                "button", name="Approve & submit fixture application", exact=True
            ).click()
            await expect(
                guide.get_by_role(
                    "heading", name="The agent is completing your approved application"
                )
            ).to_be_visible()
            await worker.run_once()
            await expect(
                page.get_by_role("heading", name="Application received", exact=True)
            ).to_be_visible(timeout=15000)
            assert flow.get(aid)["state"] == "submitted"
            assert db.one("SELECT count(*) n FROM attempts")["n"] == 1
            assert db.one("SELECT count(*) n FROM receipts")["n"] == 1
            assert not errors
        finally:
            await worker.browser.close()
            await browser.close()
