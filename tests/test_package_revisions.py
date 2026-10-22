import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright, expect
from jobsearch.api import create_app
from jobsearch.config import atomic_write, file_hash
from jobsearch.db import uid
from jobsearch.documents.content import fixture_document
from jobsearch.llm.contracts import InferenceRequest
from jobsearch.llm.service import InferenceService
from jobsearch.package_review import (
    PackageReview,
    PackageRevisionInput,
    PackageReviewInput,
)
from jobsearch.queue import Worker, enqueue
from jobsearch.workflow import Workflow, ApprovalInput
from .test_llm import application
from .test_browser import fixture_server  # noqa: F401


def prepared(db, reviewed=False):
    app = application(db)
    flow = Workflow(db)
    from jobsearch.fixtures import assessment

    flow.score(app["id"], assessment(db))
    packet = InferenceService(db).context(
        InferenceRequest(task="package_draft", application_id=app["id"])
    )
    folder = db.settings.fixture_workspace / "package" / "original"
    atomic_write(folder / "Document_Content.json", json.dumps(fixture_document(packet)))
    atomic_write(folder / "Application_Review.html", "<p>Synthetic package</p>")
    files = []
    for kind in ["resume.pdf", "cover_letter.pdf"]:
        path = folder / kind
        atomic_write(path, b"%PDF-1.4\nSynthetic package fixture\n%%EOF")
        fid = uid()
        db.execute(
            "INSERT INTO artifacts(id,application_id,path,hash,kind) VALUES(%s,%s,%s,%s,%s)",
            (fid, app["id"], str(path), file_hash(path), kind),
        )
        files.append(
            {"id": fid, "path": str(path), "hash": file_hash(path), "kind": kind}
        )
    atomic_write(
        folder / "Package_Manifest.json",
        json.dumps(
            {
                "version": "original",
                "files": {p.name: file_hash(p) for p in folder.iterdir()},
            }
        ),
    )
    flow.update_payload(
        app["id"],
        {
            "package": {
                "version": "original",
                "review_path": str(folder / "Application_Review.html"),
                "files": files,
            },
            "attachments": [f["id"] for f in files],
        },
    )
    db.execute("UPDATE applications SET state='prepared' WHERE id=%s", (app["id"],))
    if reviewed:
        PackageReview(db).accept(app["id"], PackageReviewInput(version="original"))
    return flow, app["id"]


def request(
    version="original", notes="Shorten the summary and emphasize API delivery."
):
    return PackageRevisionInput(version=version, notes=notes)


def test_revision_is_atomic_version_bound_and_revokes_queued_submission(db):
    flow, aid = prepared(db)
    service = PackageReview(db)
    service.accept(aid, PackageReviewInput(version="original"))
    assert service.status(flow.get(aid))["reviewed"]
    db.execute("UPDATE applications SET state='review' WHERE id=%s", (aid,))
    flow.update_payload(aid, {"browser_hash": flow.review(aid)["hash"]})
    approved = flow.authorize(
        aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
    )
    old = flow.get(aid)["payload"]["package"]
    facts = db.one("SELECT count(*) AS n FROM facts")["n"]
    job = service.revise(aid, request())
    assert service.revise(aid, request()) == job
    assert flow.get(aid)["state"] == "preparing"
    assert flow.get(aid)["payload"]["package"] == old
    assert not service.status(flow.get(aid))["reviewed"]
    assert db.one("SELECT invalidated FROM approvals")["invalidated"]
    assert (
        db.one("SELECT status FROM jobs WHERE id=%s", (approved["job_id"],))["status"]
        == "cancelled"
    )
    assert db.one("SELECT count(*) AS n FROM facts")["n"] == facts
    with pytest.raises(ValueError, match="preparation"):
        service.accept(aid, PackageReviewInput(version="original"))
    with pytest.raises(ValueError, match="already queued"):
        service.revise(aid, request(notes="Different suggestions"))
    assert (
        PackageReview(db).status(flow.get(aid))["history"][0]["notes"]
        == request().notes
    )


@pytest.mark.parametrize(
    "blocker",
    ["attempt", "imported", "paused", "running", "stale", "tampered", "blank"],
)
def test_revision_blocks_invalid_requests_without_mutating_package(db, blocker):
    flow, aid = prepared(db)
    data = request()
    if blocker == "attempt":
        PackageReview(db).accept(aid, PackageReviewInput(version="original"))
        db.execute("UPDATE applications SET state='review' WHERE id=%s", (aid,))
        flow.update_payload(aid, {"browser_hash": flow.review(aid)["hash"]})
        flow.authorize(
            aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
        )
        flow.begin_attempt(aid)
    elif blocker == "imported":
        db.execute("UPDATE applications SET imported=true WHERE id=%s", (aid,))
    elif blocker == "paused":
        flow.control(aid, "pause")
    elif blocker == "running":
        job = enqueue(db, "fill", {"application_id": aid})
        db.execute("UPDATE jobs SET status='running' WHERE id=%s", (job["job_id"],))
    elif blocker == "stale":
        data = request(version="outdated")
    elif blocker == "tampered":
        Path(flow.get(aid)["payload"]["package"]["files"][0]["path"]).write_bytes(
            b"changed"
        )
    elif blocker == "blank":
        data = request(notes="  ")
    before = flow.get(aid)
    with pytest.raises(ValueError):
        PackageReview(db).revise(aid, data)
    assert flow.get(aid) == before


def test_revision_endpoints_require_authentication_and_typed_input(db):
    _, aid = prepared(db)
    with TestClient(create_app(db.settings)) as client:
        url = f"/api/applications/{aid}/package-revisions"
        assert client.post(url, json=request().model_dump()).status_code == 401
        headers = {
            "X-JSA-Token": db.settings.token(),
            "Origin": db.settings.origin,
        }
        assert (
            client.post(
                url, headers=headers, json={"version": "original", "notes": "x" * 2001}
            ).status_code
            == 422
        )
        assert (
            client.post(url, headers=headers, json=request().model_dump()).status_code
            == 200
        )


@pytest.mark.usefixtures("fixture_server")
async def test_review_suggestions_regenerate_and_review_new_version(db, monkeypatch):
    flow, aid = prepared(db)
    old = flow.get(aid)["payload"]["package"]
    hashes = {f["path"]: file_hash(Path(f["path"])) for f in old["files"]}
    # A newer file from an unpublished attempt must not replace the version under review.
    stray = db.settings.fixture_workspace / "unpublished-resume.pdf"
    atomic_write(stray, b"%PDF-1.4 unpublished fixture")
    stray_id = uid()
    db.execute(
        "INSERT INTO artifacts(id,application_id,path,hash,kind) VALUES(%s,%s,%s,%s,'resume.pdf')",
        (stray_id, aid, str(stray), file_hash(stray)),
    )
    original = InferenceService.fixture
    seen = []
    rejected = False

    def fixture(self, req, packet):
        nonlocal rejected
        if req.task in ("package_draft", "package_review"):
            seen.append(
                (
                    req.task,
                    packet["revision_notes"],
                    packet["previous_package_document"],
                )
            )
        output = original(self, req, packet)
        if req.task == "package_draft":
            output["summary"]["text"] = (
                "Software engineer building C# APIs and customer integrations."
                if not req.question
                else "Software engineer focused on C# APIs and customer integrations."
            )
        if req.task == "package_review" and not rejected:
            rejected = True
            return {
                "passed": False,
                "issues": ["Check the requested emphasis once more."],
            }
        return output

    monkeypatch.setattr(InferenceService, "fixture", fixture)
    worker = Worker(db)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_role("button", name="Privacy & settings", exact=True).click()
            await page.get_by_label("Data mode", exact=True).select_option("fixture")
            await page.get_by_role("button", name="Evaluation Demo").click()
            await expect(
                page.get_by_role(
                    "heading", name="Review your resume and cover letter", exact=True
                )
            ).to_be_visible()
            panel = page.get_by_role("region", name="Review application documents")
            displayed = await page.locator(
                "#prepared-documents .artifact-list a"
            ).evaluate_all("links => links.map(a => a.getAttribute('href'))")
            assert set(displayed) == {"/api/artifacts/" + f["id"] for f in old["files"]}
            await panel.get_by_label("What would you like changed?").fill(
                request().notes
            )
            await panel.get_by_role(
                "button", name="Regenerate with my suggestions"
            ).click()
            await expect(page.locator("#work-progress")).to_have_attribute("open", "")
            await expect(page.locator("#package-review").locator("xpath=..")).not_to_have_attribute("open", "")
            await worker.run_once()
            current = flow.get(aid)
            assert current["state"] == "prepared"
            new = current["payload"]["package"]
            assert new["version"] != "original"
            assert {
                f["path"]: file_hash(Path(f["path"])) for f in old["files"]
            } == hashes
            assert set(current["payload"]["attachments"]) == {
                f["id"] for f in new["files"] if f["kind"].endswith(".pdf")
            }
            assert all(
                notes == request().notes and prior["summary"]
                for _, notes, prior in seen
            )
            assert len(seen) == 4
            assert (
                "customer integrations"
                in json.loads(
                    (
                        Path(new["review_path"]).parent / "Document_Content.json"
                    ).read_text()
                )["summary"]["text"]
            )
            await expect(
                panel.get_by_text("The revised documents are ready.", exact=False)
            ).to_be_visible(timeout=10000)
            await panel.get_by_role(
                "button", name="Documents look good", exact=True
            ).click()
            await expect(page.get_by_role("button", name="Continue to application form", exact=True)).to_be_enabled()
            await page.get_by_text("Reviewed documents and revision options", exact=True).click()
            await expect(
                panel.get_by_text("This document version is reviewed.", exact=True)
            ).to_be_visible()
            await page.reload()
            assert PackageReview(db).status(flow.get(aid))["reviewed"]
            assert not db.all("SELECT * FROM approvals") and not db.all(
                "SELECT * FROM attempts"
            )
            job = db.one("SELECT * FROM jobs WHERE kind='package'")
            runs = db.one("SELECT count(*) AS n FROM llm_runs")["n"]
            assert (await Worker(db).dispatch(job))["version"] == new["version"]
            assert db.one("SELECT count(*) AS n FROM llm_runs")["n"] == runs
            with pytest.raises(ValueError, match="changed"):
                PackageReview(db).accept(aid, PackageReviewInput(version="original"))
            assert not errors
        finally:
            await browser.close()
            await worker.browser.close()


async def test_rejected_revision_preserves_documents_and_can_be_requested_again(
    db, monkeypatch
):
    flow, aid = prepared(db)
    old = flow.get(aid)["payload"]["package"]
    original = InferenceService.fixture

    def fixture(self, req, packet):
        if req.task == "package_review":
            return {
                "passed": False,
                "issues": ["Requested qualification lacks confirmed evidence."],
            }
        return original(self, req, packet)

    monkeypatch.setattr(InferenceService, "fixture", fixture)
    service = PackageReview(db)
    first = service.revise(aid, request(notes="Add an unsupported qualification."))
    worker = Worker(db)
    try:
        await worker.run_once()
        assert (
            db.one("SELECT status FROM jobs WHERE id=%s", (first["job_id"],))["status"]
            == "failed"
        )
        assert flow.get(aid)["payload"]["package"] == old
        assert len(db.all("SELECT * FROM artifacts")) == 2
        assert service.status(flow.get(aid))["can_revise"]
        assert not service.status(flow.get(aid))["can_accept"]
        second = service.revise(
            aid,
            request(notes="Keep the confirmed qualifications; shorten the opening."),
        )
        assert second["job_id"] != first["job_id"]
        db.execute(
            "UPDATE jobs SET cancel_requested=true,status='cancelled' WHERE id=%s",
            (second["job_id"],),
        )
        assert not await Worker(db).run_once()
        assert flow.get(aid)["payload"]["package"] == old
        assert len(service.status(flow.get(aid))["history"]) == 2
    finally:
        await worker.browser.close()
