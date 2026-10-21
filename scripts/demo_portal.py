#!/usr/bin/env python3
"""Run the hosted-form workflow against a local synthetic employer, with UI approval."""

import asyncio
import json
import time
from datetime import datetime, timezone

import httpx
from playwright.async_api import async_playwright

from jobsearch.config import Settings, atomic_write
from jobsearch.db import DB
from jobsearch.fixtures import seed, assessment
from jobsearch.packages import Packages
from jobsearch.workflow import Workflow


class OfflineDemoDB(DB):
    def setting(self, key, default=None):
        # Process-local fixture selection; do not change the user's provider settings.
        if key == "llm":
            return {"backend": "fixture", "enabled": False}
        return super().setting(key, default)


async def main():
    settings = Settings()
    if settings.live_applications:
        raise SystemExit("Keep real sending disabled during this fixture demonstration")
    db = OfflineDemoDB(settings)
    db.migrate()
    if not db.setting("fixture_fact_ids"):
        seed(db)
    flow = Workflow(db)
    requisition = "PORTAL-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    app = flow.create(
        "Portal Workflow Demo",
        "Software Engineer, APIs",
        requisition,
        candidate="fixture",
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
            "attachments": [
                f["id"] for f in package["files"] if f["kind"] == "resume.pdf"
            ],
            "scenario": "lever",
        },
    )
    flow.transition(aid, "prepared")

    async with httpx.AsyncClient(
        base_url=settings.origin, headers={"X-JSA-Token": settings.token()}, timeout=30
    ) as client:

        async def call(path, value=None):
            response = await client.request(
                "GET" if value is None else "POST", "/api" + path, json=value
            )
            response.raise_for_status()
            return response.json()

        async def wait(job):
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                row = await call("/jobs/" + job["job_id"])
                if row["status"] == "complete":
                    return row
                if row["status"] in ("failed", "unresolved", "cancelled"):
                    raise RuntimeError(row.get("error") or row["status"])
                await asyncio.sleep(0.4)
            raise TimeoutError("The local worker did not finish the fixture job")

        assert flow.get(aid)["candidate"] == "fixture"
        await call(
            "/applications/" + aid + "/package-review", {"version": package["version"]}
        )
        await wait(await call("/applications/" + aid + "/continue", {}))
        app = await call("/applications/" + aid)
        for question in app["questions"]:
            if question["answer_fact"]:
                continue
            if question["prompt"] != "Annual base salary target":
                raise RuntimeError(
                    "Unexpected fixture question; no answer was inferred"
                )
            job = await call(
                "/questions/" + question["id"] + "/answer",
                {"value": "120000", "scope": "application"},
            )
            if job.get("job_id"):
                await wait(job)
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                executable_path="/usr/bin/chromium", headless=True
            )
            page = await browser.new_page(viewport={"width": 1280, "height": 1000})
            try:
                await page.goto(settings.origin + "/#token=" + settings.token())
                await page.get_by_label("Workspace", exact=True).select_option(
                    "fixture"
                )
                await page.get_by_role(
                    "button", name="Applications", exact=True
                ).click()
                await page.get_by_role("row").filter(has_text=requisition).click()
                await page.get_by_role(
                    "region", name="Employer application plan"
                ).wait_for()
                await page.get_by_role(
                    "button", name="Approve & submit fixture application", exact=True
                ).wait_for()
                await page.get_by_text(
                    "Document previews and attachment selection", exact=True
                ).click()
                await (
                    page.frame_locator('iframe[title="Resume preview"]')
                    .locator("body")
                    .filter(has_text="Alex Example")
                    .wait_for()
                )
                await page.locator(
                    'iframe[title="Resume preview"]'
                ).scroll_into_view_if_needed()
                folder = settings.state / "demo-evidence"
                folder.mkdir(exist_ok=True, mode=0o700)
                await page.screenshot(
                    path=str(folder / "portal-review.png"), full_page=True
                )
                async with page.expect_response(
                    lambda r: r.url.endswith("/approve")
                ) as pending:
                    await page.get_by_role(
                        "button",
                        name="Approve & submit fixture application",
                        exact=True,
                    ).click()
                response = await pending.value
                if response.status != 200:
                    raise RuntimeError("Fixture approval was rejected")
                await wait(await response.json())
                await page.get_by_role(
                    "heading", name="Application received", exact=True
                ).wait_for()
                await page.screenshot(
                    path=str(folder / "portal-receipt.png"), full_page=True
                )
            finally:
                await browser.close()
        app = await call("/applications/" + aid)
        report = {
            "application_id": aid,
            "candidate": "fixture",
            "state": app["state"],
            "attempts": len(app["attempts"]),
            "receipts": len(app["receipts"]),
            "real_submission_enabled": False,
            "offline_model": True,
        }
        atomic_write(
            settings.state / "demo-evidence" / "portal-demo.json",
            json.dumps(report, indent=2),
        )
        print(json.dumps(report))


if __name__ == "__main__":
    asyncio.run(main())
