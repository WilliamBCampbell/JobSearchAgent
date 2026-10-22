"""Removed postings preserve saved work and expose a way out of verification."""

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from jobsearch.openings import OfficialOpenings
from jobsearch.progress import progress
from .test_browser import fixture_server  # noqa: F401
from .test_package_revisions import prepared


@pytest.mark.parametrize("status", [404, 410, 429, 503])
@pytest.mark.parametrize("board_missing", [False, True])
def test_only_missing_job_on_verified_board_is_unavailable(
    db, monkeypatch, status, board_missing
):
    service = OfficialOpenings(db)

    def get(url):
        if "/jobs/" not in url and not board_missing:
            return {"name": "Example Company"}
        response = httpx.Response(status, request=httpx.Request("GET", url))
        response.raise_for_status()

    monkeypatch.setattr(service, "get_json", get)
    result = service.verify(
        {
            "company": "Example Company",
            "title": "Software Engineer",
            "requisition": "TEST-1",
            "job_url": "https://job-boards.greenhouse.io/example/jobs/123",
        }
    )
    assert result["open"] is False
    assert result["checked_at"]
    if status in (404, 410) and not board_missing:
        assert result["status"] == "unavailable"
        assert str(status) in result["reason"]
        assert result["requisition"] == "TEST-1"
    else:
        assert result["status"] == "unverified"
        assert str(status) in result["errors"][0]


@pytest.mark.parametrize("status", ["unverified", "unavailable"])
def test_failed_check_is_distinct_from_expired_verification(db, status):
    flow, aid = prepared(db, reviewed=True)
    before = flow.get(aid)["payload"]
    flow.set_official(
        aid,
        {
            "open": False,
            "status": status,
            "reason": "The employer no longer lists this opening (HTTP 404).",
            "errors": ["HTTPStatusError"],
        },
    )
    step = progress(flow.get(aid))
    assert step["opening_blocked"] and step["status"] == "blocked"
    assert step["action"] == "refresh-opening"
    assert step["title"] == (
        "This opening is no longer available"
        if status == "unavailable"
        else "The opening could not be verified"
    )
    assert flow.get(aid)["payload"] == before
    assert flow.get(aid)["state"] == "prepared"
    assert not db.all("SELECT * FROM attempts")


@pytest.mark.usefixtures("fixture_server")
async def test_unavailable_opening_exposes_documents_and_discard(db):
    flow, aid = prepared(db, reviewed=True)
    db.execute("UPDATE applications SET state='manual_handoff' WHERE id=%s", (aid,))
    flow.set_official(
        aid,
        {
            "open": False,
            "status": "unavailable",
            "reason": "The employer no longer lists this opening (HTTP 404).",
            "application_url": "https://job-boards.greenhouse.io/example/jobs/123",
        },
    )
    before = flow.get(aid)["payload"]
    # The fixture's saved package and identity remain untouched by page navigation.
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page()
        try:
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_role(
                "button", name="Privacy & settings", exact=True
            ).click()
            await page.get_by_label("Data mode", exact=True).select_option("fixture")
            await page.get_by_role("button", name="Applications", exact=True).click()
            await page.get_by_text(flow.get(aid)["employer"], exact=True).click()
            guide = page.get_by_role("region", name="Application next step")
            await expect(
                guide.get_by_role("heading", name="This opening is no longer available")
            ).to_be_visible()
            await expect(
                guide.get_by_role("link", name="Inspect employer posting")
            ).to_have_attribute(
                "href", "https://job-boards.greenhouse.io/example/jobs/123"
            )
            await guide.get_by_role(
                "button", name="View saved documents", exact=True
            ).click()
            await expect(page.locator("#prepared-documents")).to_have_attribute(
                "open", ""
            )
            await guide.get_by_role(
                "button", name="Discard this role", exact=True
            ).click()
            await expect(
                guide.get_by_role("heading", name="This role was discarded")
            ).to_be_visible()
            assert flow.get(aid)["state"] == "skipped"
            assert flow.get(aid)["payload"] == before
            assert not db.all("SELECT * FROM attempts")
            assert not db.all("SELECT * FROM approvals")
        finally:
            await browser.close()
