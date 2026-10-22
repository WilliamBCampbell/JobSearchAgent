"""Employer links remain visible across application stages and imported records."""

import pytest
from playwright.async_api import async_playwright, expect

from jobsearch.db import js
from jobsearch.workflow import Workflow
from .test_browser import fixture_server  # noqa: F401


@pytest.mark.usefixtures("fixture_server")
async def test_employer_application_link_stays_in_heading(db):
    target = db.settings.origin + "/example-employer/apply"
    cases = [
        ("missing_answers", False, {"application_url": target}, {}, target),
        ("submitted", False, {"application_url": target}, {}, target),
        ("submitted", True, {"application_url": target}, {}, target),
        ("discovered", True, {}, {}, None),
        ("discovered", True, {"url": target}, {}, target),
        ("discovered", True, {"application_url": "javascript:alert(1)"}, {}, None),
        ("discovered", True, {}, {"form_plan": {"spec": {
            "url": target, "adapter": "lever", "fields": [], "notices": [], "unsupported": [],
        }}}, target),
    ]
    for index, (state, imported, official, payload, _) in enumerate(cases):
        record = Workflow(db).create(f"Link Demo {index}", "Engineer", f"LINK-{index}")
        db.execute(
            "UPDATE applications SET state=%s,imported=%s,official=%s,payload=payload || %s WHERE id=%s",
            (state, imported, js(official), js(payload), record["id"]),
        )

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path="/usr/bin/chromium", headless=True)
        page = await browser.new_page()
        try:
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_role("button", name="Privacy & settings", exact=True).click()
            await page.get_by_label("Data mode", exact=True).select_option("fixture")
            for index, (_, _, official, _, expected) in enumerate(cases):
                await page.get_by_role("button", name="Applications", exact=True).click()
                await page.get_by_text(f"Link Demo {index}", exact=True).click()
                heading = page.locator(".application-heading")
                if expected:
                    label = "Open employer posting" if "url" in official else "Open employer application"
                    link = heading.get_by_role("link", name=label, exact=True)
                    await expect(link).to_be_visible()
                    await expect(link).to_have_attribute("href", expected)
                    await expect(link).to_have_attribute("target", "_blank")
                else:
                    await expect(heading.get_by_text("No employer application link was saved for this record.")).to_be_visible()
                    await expect(heading.get_by_role("link")).to_have_count(0)
        finally:
            await browser.close()
