import pytest
import json
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright, expect

from jobsearch.api import create_app
from jobsearch.config import atomic_write, digest, file_hash
from jobsearch.db import uid
from jobsearch.forms import FieldSpec, FormSpec, field_key
from jobsearch.workflow import Workflow
from jobsearch.browser import BrowserWorker
from .test_llm import application
from .test_browser import fixture_server  # noqa: F401


def manual_application(db):
    app = application(db)
    flow = Workflow(db)
    from jobsearch.fixtures import assessment
    flow.score(app["id"], assessment(db))
    spec = FormSpec(
        adapter="greenhouse",
        url=db.settings.origin + "/fixture/form",
        requisition=app["requisition"],
        fields=[
            FieldSpec(
                key=field_key("contact", "Preferred contact method", []),
                name="contact",
                label="Preferred contact method",
                type="text",
                required=True,
            )
        ],
        notices=["Check the employer's remaining privacy choices in your browser."],
        unsupported=["This employer form requires manual completion."],
    )
    path = db.settings.fixture_workspace / "review-fixture.pdf"
    # The control test verifies attachment identity; rendering is tested separately.
    atomic_write(path, b"%PDF-1.4\nSynthetic package-review fixture\n%%EOF\n")
    artifact = uid()
    db.execute(
        "INSERT INTO artifacts(id,application_id,path,hash,kind) VALUES(%s,%s,%s,%s,'resume.pdf')",
        (artifact, app["id"], str(path), file_hash(path)),
    )
    flow.update_payload(
        app["id"],
        {
            "form_schema": [f.model_dump() for f in spec.fields],
            "form_plan": {"spec": spec.model_dump(), "fingerprint": spec.fingerprint},
            "attachments": [artifact],
            "package": {"files": [{"id": artifact, "kind": "resume.pdf"}]},
        },
    )
    q = flow.ask(app["id"], spec.fields[0].key, spec.fields[0].label)
    flow.answer(q["id"], "Email")
    db.execute(
        "UPDATE applications SET state='manual_handoff' WHERE id=%s", (app["id"],)
    )
    return flow, app["id"], q


def test_manual_package_review_survives_restart_without_submission_authority(db):
    flow, aid, q = manual_application(db)
    review = flow.review(aid)
    flow.review_handoff(aid, review["hash"])
    restarted = Workflow(db)
    assert (
        restarted.get(aid)["payload"]["handoff_review"]["reviewed_hash"]
        == restarted.review(aid)["hash"]
    )
    assert restarted.get(aid)["state"] == "manual_handoff"
    assert (
        not db.all("SELECT * FROM approvals")
        and not db.all("SELECT * FROM attempts")
        and not db.all("SELECT * FROM receipts")
    )
    restarted.answer(q["id"], "Phone")
    assert restarted.review(aid)["hash"] != review["hash"]
    with pytest.raises(ValueError, match="changed"):
        restarted.review_handoff(aid, review["hash"])


@pytest.mark.parametrize(
    "blocker",
    [
        "pending_answer",
        "missing_resume",
        "paused",
        "attempt",
        "changed_file",
        "imported",
    ],
)
def test_manual_review_cannot_hide_blockers_or_retry_an_attempt(db, blocker):
    flow, aid, q = manual_application(db)
    review = flow.review(aid)
    if blocker == "pending_answer":
        db.execute("UPDATE questions SET needs_review=true WHERE id=%s", (q["id"],))
    elif blocker == "missing_resume":
        flow.update_payload(aid, {"attachments": []})
        review = flow.review(aid)
    elif blocker == "paused":
        flow.control(aid, "pause")
    elif blocker == "imported":
        db.execute("UPDATE applications SET imported=true WHERE id=%s", (aid,))
    elif blocker == "attempt":
        approval = uid()
        db.execute(
            "INSERT INTO approvals(id,application_id,payload_hash,actor) VALUES(%s,%s,%s,'fixture')",
            (approval, aid, review["hash"]),
        )
        db.execute(
            "INSERT INTO attempts(id,application_id,payload_hash,approval_id,state) VALUES(%s,%s,%s,%s,'unresolved')",
            (uid(), aid, review["hash"], approval),
        )
    else:
        atomic_write(
            db.settings.fixture_workspace / "review-fixture.pdf", b"Changed fixture"
        )
    with pytest.raises(ValueError):
        flow.review_handoff(aid, review["hash"])
    assert "handoff_review" not in flow.get(aid)["payload"]


def test_handoff_review_api_requires_confirmation_and_rejects_payload_injection(db):
    flow, aid, _ = manual_application(db)
    with TestClient(create_app(db.settings)) as client:
        headers = {"X-JSA-Token": db.settings.token(), "Origin": db.settings.origin}
        result = client.post(
            f"/api/applications/{aid}/handoff-review",
            headers=headers,
            json={"reviewed_hash": flow.review(aid)["hash"], "confirm": False},
        )
        assert result.status_code == 422
        with pytest.raises(ValueError, match="Unknown payload"):
            flow.update_payload(aid, {"handoff_review": {"reviewed_hash": digest({})}})


def test_saved_handoff_distinguishes_preparation_from_confirmed_receipt(db):
    flow, aid, _ = manual_application(db)
    worker = BrowserWorker(db)
    worker.handoff(aid, "Prepared fixture")
    path = db.settings.state / "handoffs" / (aid + ".json")
    assert "Review additional questions" in json.loads(path.read_text())["instructions"]
    flow.record_receipt(
        aid,
        None,
        {
            "confirmed": True,
            "text": "Synthetic employer receipt",
            "requisition": flow.get(aid)["requisition"],
        },
        "user_confirmation",
    )
    worker.handoff(aid, "Fixture records")
    assert "do not send a duplicate" in json.loads(path.read_text())["instructions"]


@pytest.mark.usefixtures("fixture_server")
async def test_guided_manual_review_to_portal_and_receipt_with_edit_invalidation(db):
    flow, aid, q = manual_application(db)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page(viewport={"width": 1280, "height": 850})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_label("Workspace", exact=True).select_option("fixture")
            await page.get_by_role("button", name="Applications", exact=True).click()
            await page.get_by_text("Evaluation Demo", exact=True).click()
            guide = page.get_by_role("region", name="Application next step")
            await expect(
                guide.get_by_role(
                    "heading",
                    name="Review your package before opening the employer form",
                )
            ).to_be_visible()
            await expect(
                page.get_by_role(
                    "heading", name="Record employer confirmation", exact=True
                )
            ).to_have_count(0)
            await expect(
                page.get_by_role(
                    "button", name="I confirm employer receipt", exact=True
                )
            ).to_have_count(0)
            await guide.get_by_role(
                "button", name="Review answers and files", exact=True
            ).click()
            mark = page.get_by_role(
                "button", name="Mark package reviewed and continue", exact=True
            )
            await expect(mark).to_be_disabled()
            await page.get_by_role(
                "checkbox", name="I reviewed these answers and selected files."
            ).check()
            await mark.click()
            await expect(
                guide.get_by_role("listitem").filter(has_text="Review & approve")
            ).to_have_attribute("aria-current", "step")
            await expect(
                guide.get_by_role("link", name="Open employer application", exact=True)
            ).to_be_visible()
            assert flow.get(aid)["state"] == "manual_handoff"
            assert not db.all("SELECT * FROM approvals") and not db.all(
                "SELECT * FROM attempts"
            )
            await page.reload()
            await page.get_by_label("Workspace", exact=True).select_option("fixture")
            await page.get_by_role("button", name="Applications", exact=True).click()
            await page.get_by_text("Evaluation Demo", exact=True).click()
            await expect(
                guide.get_by_role("link", name="Open employer application", exact=True)
            ).to_be_visible()
            await page.get_by_text("Prepared answers and sources", exact=True).click()
            await (
                page.get_by_role("row")
                .filter(has_text="Preferred contact method")
                .get_by_role("button", name="Change answer")
                .click()
            )
            editor = page.get_by_role(
                "textbox", name="Preferred contact method", exact=True
            )
            await expect(editor).to_have_value("Email")
            await expect(
                guide.get_by_role("link", name="Open employer application", exact=True)
            ).to_have_count(0)
            await editor.fill("Phone")
            await page.get_by_role("button", name="Confirm answer", exact=True).click()
            await expect(
                guide.get_by_role(
                    "heading",
                    name="Review your package before opening the employer form",
                )
            ).to_be_visible()
            assert (
                flow.review(aid)["hash"]
                != flow.get(aid)["payload"]["handoff_review"]["reviewed_hash"]
            )
            await guide.get_by_role(
                "button", name="Already submitted? Record the confirmation"
            ).click()
            await page.get_by_role("textbox", name="Receipt confirmed by you").fill(
                "Synthetic employer confirmation for EVAL-1 received in the fixture portal."
            )
            await page.get_by_role(
                "button", name="I confirm employer receipt", exact=True
            ).click()
            await expect(
                page.get_by_role("heading", name="Application received", exact=True)
            ).to_be_visible()
            assert (
                flow.get(aid)["state"] == "submitted"
                and len(db.all("SELECT * FROM receipts")) == 1
            )
            assert not errors
        finally:
            await browser.close()
