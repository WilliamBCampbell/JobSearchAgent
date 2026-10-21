import asyncio
import os
import subprocess
import sys
import time
import httpx
import pytest
from jobsearch.fixtures import seed, assessment
from jobsearch.workflow import Workflow, ApprovalInput
from jobsearch.queue import Worker
from jobsearch.packages import Packages
from .test_scoring_search import official


@pytest.fixture
def fixture_server(db):
    env = os.environ | {
        "JSA_STATE": str(db.settings.state),
        "JSA_DATABASE_URL": db.settings.database,
        "JSA_WORKSPACE": str(db.settings.workspace),
        "JSA_ORIGIN": db.settings.origin,
        "JSA_HEADLESS": "1",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "jobsearch.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8766",
            "--no-access-log",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    for _ in range(100):
        try:
            if httpx.get(db.settings.origin + "/health").status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.05)
    else:
        raise RuntimeError("Fixture server did not start")
    yield process
    process.terminate()
    process.communicate(timeout=10)


async def prepare_browser_app(db, scenario):
    seed(db)
    flow = Workflow(db)
    a = flow.create("Northstar Demo", "Software Engineer, APIs", "NS-101")
    o = official() | {
        "application_url": db.settings.origin + "/fixture/application/" + a["id"]
    }
    flow.set_official(a["id"], o)
    flow.score(a["id"], assessment(db))
    flow.select(a["id"])
    flow.transition(a["id"], "preparing")
    package = await asyncio.to_thread(Packages(db).prepare, flow.get(a["id"]))
    flow.update_payload(
        a["id"],
        {
            "package": package,
            "attachments": [
                f["id"] for f in package["files"] if f["kind"] == "resume.pdf"
            ],
            "scenario": scenario,
        },
    )
    flow.transition(a["id"], "prepared")
    from jobsearch.package_review import PackageReview, PackageReviewInput

    PackageReview(db).accept(a["id"], PackageReviewInput(version=package["version"]))
    return flow, a["id"]


async def answer_missing(db, flow, aid):
    for q in db.all(
        "SELECT * FROM questions WHERE application_id=%s AND answer_fact IS NULL",
        (aid,),
    ):
        flow.answer(q["id"], "120000" if q["key"] == "salary" else "Up to 10%")


async def test_visible_form_missing_answers_approval_and_receipt(db, fixture_server):
    flow, aid = await prepare_browser_app(db, "normal")
    worker = Worker(db)
    try:
        result = await worker.browser.fill(aid)
        assert result["state"] == "missing_answers"
        await answer_missing(db, flow, aid)
        await worker.run_once()
        assert flow.get(aid)["state"] == "review"
        await (
            worker.browser.sessions[aid][1]
            .get_by_role("button", name="Submit application", exact=True)
            .click()
        )
        assert not db.all("SELECT * FROM attempts")
        flow.authorize(
            aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
        )
        await worker.run_once()
        assert flow.get(aid)["state"] == "submitted"
        assert len(db.all("SELECT * FROM receipts")) == 1
        assert db.one("SELECT count(*) AS n FROM artifacts WHERE attached")["n"] == 1
        assert (
            db.one(
                "SELECT count(*) AS n FROM artifacts WHERE kind LIKE 'cover_letter%%' AND attached"
            )["n"]
            == 0
        )
    finally:
        await worker.browser.close()


async def test_verification_challenge_pauses_and_resumes(db, fixture_server):
    flow, aid = await prepare_browser_app(db, "challenge")
    worker = Worker(db)
    try:
        result = await worker.browser.fill(aid)
        assert result["state"] == "awaiting_challenge"
        assert not db.all("SELECT * FROM attempts")
        page = worker.browser.sessions[aid][1]
        await page.get_by_role("button", name="I completed verification").click()
        await worker.browser.fill(aid)
        assert flow.get(aid)["state"] == "missing_answers"
        assert not db.one("SELECT * FROM facts WHERE key LIKE '%%verification%%'")
    finally:
        await worker.browser.close()


async def test_browser_no_receipt_stays_unresolved(db, fixture_server):
    flow, aid = await prepare_browser_app(db, "no_receipt")
    worker = Worker(db)
    try:
        await worker.browser.fill(aid)
        await answer_missing(db, flow, aid)
        await worker.run_once()
        flow.authorize(
            aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
        )
        await worker.run_once()
        assert flow.get(aid)["state"] == "unresolved"
        assert not db.all("SELECT * FROM receipts")
    finally:
        await worker.browser.close()


async def test_new_form_qualification_reopens_scoring(db, fixture_server):
    flow, aid = await prepare_browser_app(db, "new_requirement")
    worker = Worker(db)
    try:
        result = await worker.browser.fill(aid)
        assert result["state"] == "scored"
        app = flow.get(aid)
        assert app["score"]["total"] >= 80 and not app["score"]["primary"]
        assert any(
            r["source"].startswith(db.settings.origin)
            for r in app["score"]["requirements"]
        )
        assert not db.all("SELECT * FROM attempts")
    finally:
        await worker.browser.close()
