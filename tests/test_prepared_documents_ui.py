"""Legacy packages show one latest file per type without changing attachments."""

import os

import pytest
from playwright.async_api import async_playwright, expect

from jobsearch.db import js, uid
from jobsearch.workflow import Workflow
from .test_browser import fixture_server  # noqa: F401


@pytest.mark.parametrize("has_package", [False, True])
@pytest.mark.usefixtures("fixture_server")
async def test_latest_prepared_documents_hide_duplicate_versions(db, has_package):
    app = Workflow(db).create("Document Versions Demo", "Engineer", "DOC-1")
    records = []
    for version in range(3):
        for document in ("resume", "cover_letter"):
            for extension in ("pdf", "docx", "html", "preview.png"):
                kind = f"{document}.{extension}"
                path = db.settings.fixture_workspace / str(version) / kind
                path.parent.mkdir(exist_ok=True)
                path.write_text(f"Fixture version {version}")
                os.utime(path, (1700000000 + version, 1700000000 + version))
                record = {"id": uid(), "kind": kind, "version": version}
                records.append(record)
                db.execute(
                    "INSERT INTO artifacts(id,application_id,path,hash,kind) VALUES(%s,%s,%s,%s,%s)",
                    (record["id"], app["id"], str(path), str(version) * 64, kind),
                )
    # Existing attachment choices and stale package metadata must not select history.
    payload = {"attachments": [records[0]["id"]]}
    if has_package:
        payload["package"] = {"files": records[:8], "previews": []}
    db.execute(
        "UPDATE applications SET payload=%s WHERE id=%s", (js(payload), app["id"])
    )
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_label("Workspace", exact=True).select_option("fixture")
            await page.get_by_role("button", name="Applications", exact=True).click()
            await page.get_by_text("Document Versions Demo", exact=True).click()
            panel = page.locator("section.panel").filter(
                has=page.get_by_role("heading", name="Prepared documents", exact=True)
            )
            visible_files = panel.locator(".artifact-list a")
            await expect(visible_files).to_have_count(6)
            expected = {
                "/api/artifacts/" + r["id"]
                for r in records
                if r["version"] == 2 and not r["kind"].endswith(".png")
            }
            assert (
                set(
                    await visible_files.evaluate_all(
                        "links => links.map(a => a.getAttribute('href'))"
                    )
                )
                == expected
            )
            await expect(panel.locator(".document-preview")).to_have_count(2)
            await expect(panel.get_by_role("checkbox")).to_have_count(2)
            history = panel.locator("details").filter(
                has=page.get_by_text("Previous document versions", exact=True)
            )
            assert await history.get_attribute("open") is None
            await history.locator("summary").click()
            await expect(history.locator("li")).to_have_count(12)
            await expect(
                history.get_by_text("Selected attachment", exact=False)
            ).to_be_visible()
            assert not errors
            assert Workflow(db).get(app["id"])["payload"] == payload
            assert len(db.all("SELECT * FROM artifacts")) == 24
        finally:
            await browser.close()
