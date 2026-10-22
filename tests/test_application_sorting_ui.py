"""Default queue ordering and column overrides use synthetic response data."""

import pytest
from playwright.async_api import async_playwright, expect

from .test_browser import fixture_server  # noqa: F401


@pytest.mark.usefixtures("fixture_server")
async def test_queue_prioritizes_active_unsubmitted_scores_and_keeps_column_sorting(db):
    rows = [
        ("Submitted low", "submitted", 20),
        ("Review unscored", "review", None),
        ("Submitted high", "submitted", 99),
        ("Review low", "review", 40),
        ("Review high", "review", 80),
        ("Submitted unscored", "submitted", None),
        ("Review zero", "review", 0),
        ("Selected high", "selected", 95),
        ("Closed high", "closed", 100),
        ("Deferred high", "deferred", 98),
        ("Unresolved high", "unresolved", 97),
        ("Rejected high", "rejected", 96),
        ("Discarded high", "skipped", 94),
        ("Paused high", "selected", 93),
        ("Follow up high", "follow_up", 92),
    ]

    async def overview(route):
        response = await route.fetch()
        body = await response.json()
        body["applications"] = [
            {
                "id": str(index), "employer": employer, "role": "Synthetic role",
                "requisition": f"TEST-{index}", "state": state,
                "score": {"total": score} if score is not None else None,
                "payload": {}, "imported": True, "paused": employer == "Paused high",
            }
            for index, (employer, state, score) in enumerate(rows)
        ]
        await route.fulfill(response=response, json=body)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page()
        try:
            await page.route("**/api/overview*", overview)
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_role("button", name="Applications", exact=True).click()
            table = page.get_by_role("table", name="Application queue")
            employers = table.locator("tbody tr td:first-child strong")
            expected = [
                "Selected high", "Review high", "Review low", "Review zero", "Review unscored",
                "Unresolved high", "Submitted high", "Follow up high", "Submitted low", "Submitted unscored",
                "Closed high", "Deferred high", "Rejected high", "Discarded high", "Paused high",
            ]
            await expect(employers).to_have_text(expected)
            await expect(table.get_by_role("columnheader", name="State")).to_have_attribute(
                "aria-sort", "ascending"
            )
            await table.get_by_role("button", name="State").click()
            await expect(employers).to_have_text(expected[10:] + expected[6:10] + expected[5:6] + expected[:5])
            await table.get_by_role("button", name="Fit", exact=False).click()
            await expect(employers).to_have_text([
                "Closed high", "Submitted high", "Deferred high", "Unresolved high",
                "Rejected high", "Selected high", "Discarded high", "Paused high", "Follow up high",
                "Review high", "Review low", "Submitted low", "Review zero",
                "Review unscored", "Submitted unscored",
            ])
            await page.reload()
            await page.get_by_role("button", name="Applications", exact=True).click()
            await expect(employers).to_have_text(expected)
        finally:
            await browser.close()
