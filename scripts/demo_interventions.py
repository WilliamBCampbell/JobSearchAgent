#!/usr/bin/env python3
"""Demonstrate the intervention inbox against a local fixture; never submit."""

import asyncio
import json
import time
from datetime import datetime, timezone

import httpx
from playwright.async_api import async_playwright, expect

from demo_portal import OfflineDemoDB
from jobsearch.config import Settings, atomic_write, digest, file_hash
from jobsearch.fixtures import assessment, seed
from jobsearch.packages import Packages
from jobsearch.workflow import Workflow


async def main():
    settings = Settings()
    if settings.live_applications:
        raise SystemExit("Keep real sending disabled during this demonstration")
    db = OfflineDemoDB(settings)
    db.migrate()

    def originals():
        return {
            "tracker_hash": file_hash(
                settings.workspace / "Job Applications 2026 Real Hunt.ods"
            ),
            "records_hash": digest(
                db.all(
                    "SELECT id,state,payload,revision FROM applications WHERE imported AND candidate='real' ORDER BY id"
                )
            ),
            "attempts": db.one(
                "SELECT count(*) n FROM attempts t JOIN applications a ON a.id=t.application_id WHERE a.candidate='real'"
            )["n"],
        }

    before = originals()
    if not db.setting("fixture_fact_ids"):
        seed(db)
    flow = Workflow(db)
    req = "INT-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    app = flow.create(
        "Intervention Demo", "Software Engineer, APIs", req, candidate="fixture"
    )
    aid = app["id"]
    flow.set_official(
        aid,
        {
            "open": True,
            "status": "verified",
            "application_url": settings.origin + "/fixture/lever/" + aid + "/apply",
            "verified_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    flow.score(aid, assessment(db))
    flow.select(aid)
    flow.transition(aid, "preparing")
    package = await asyncio.to_thread(Packages(db).prepare, flow.get(aid), False)
    flow.update_payload(
        aid,
        {
            "package": package,
            "scenario": "lever",
            "attachments": [
                f["id"] for f in package["files"] if f["kind"] == "resume.pdf"
            ],
        },
    )
    flow.transition(aid, "prepared")
    folder = settings.state / "demo-evidence"
    folder.mkdir(exist_ok=True, mode=0o700)
    async with httpx.AsyncClient(
        base_url=settings.origin, headers={"X-JSA-Token": settings.token()}, timeout=30
    ) as client:
        response = await client.post(f"/api/applications/{aid}/fill", json={})
        response.raise_for_status()
        deadline = time.monotonic() + 90
        while flow.get(aid)["state"] != "missing_answers":
            if time.monotonic() > deadline:
                raise TimeoutError("Fixture form preparation did not finish")
            await asyncio.sleep(0.5)
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                executable_path="/usr/bin/chromium", headless=True
            )
            page = await browser.new_page(viewport={"width": 1280, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                await page.goto(settings.origin + "/#token=" + settings.token())
                await page.get_by_label("Workspace", exact=True).select_option(
                    "fixture"
                )
                await page.get_by_role(
                    "button", name="View notifications", exact=True
                ).click()
                card = page.locator("article.intervention").filter(has_text=req)
                await expect(
                    card.get_by_role("heading", name="Manual answers needed")
                ).to_be_visible()
                await card.screenshot(path=str(folder / "intervention-notice.png"))
                await card.get_by_role("button", name="Mark as read").click()
                await expect(card.get_by_text("SEEN · STILL OPEN")).to_be_visible()
                await card.get_by_role("button", name="Review missing answers").click()
                await expect(
                    page.get_by_role("link", name="Open saved handoff")
                ).to_be_visible()
                await page.get_by_label("Annual base salary target", exact=True).fill(
                    "120000"
                )
                await page.get_by_role(
                    "button", name="Confirm answer", exact=True
                ).click()
                await expect(
                    page.get_by_role(
                        "button",
                        name="Approve & submit fixture application",
                        exact=True,
                    )
                ).to_be_visible(timeout=90000)
                await page.get_by_role(
                    "button", name="View notifications", exact=True
                ).click()
                card = page.locator("article.intervention").filter(has_text=req)
                await expect(
                    card.get_by_role("heading", name="Application ready for review")
                ).to_be_visible()
                await card.screenshot(path=str(folder / "intervention-resolved.png"))
                assert not errors, errors
            finally:
                await browser.close()
    after = originals()
    assert before == after, "Original records or real attempts changed"
    assert flow.get(aid)["state"] == "review"
    assert not db.one("SELECT 1 FROM approvals WHERE application_id=%s", (aid,))
    assert not db.one("SELECT 1 FROM attempts WHERE application_id=%s", (aid,))
    report = {
        "application_id": aid,
        "requisition": req,
        "candidate": "fixture",
        "state": "review",
        "manual_handoff_saved": True,
        "acknowledgment_preserved_blocker": True,
        "answer_resumed_preparation": True,
        "approval_and_attempt_count": 0,
        "originals_unchanged": before == after,
        "real_submission_enabled": False,
        "screenshots": ["intervention-notice.png", "intervention-resolved.png"],
    }
    atomic_write(folder / "intervention-demo.json", json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
