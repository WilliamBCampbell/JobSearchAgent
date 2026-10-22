"""Selection explains conditional roles without authorizing submission."""

import pytest
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright, expect
from jobsearch.api import create_app
from jobsearch.db import js
from jobsearch.fixtures import assessment, seed
from jobsearch.selection import selection_review
from jobsearch.workflow import Workflow
from .test_scoring_search import official
from .test_browser import fixture_server  # noqa: F401


def conditional_app(db):
    seed(db)
    flow = Workflow(db)
    a = flow.create("Selection Demo", "Senior Software Engineer", "ROLE-262")
    flow.set_official(a["id"], official())
    data = assessment(db)
    # High arithmetic score, with non-central uncertainties, like the reported case.
    for req in data.requirements:
        if req.category in ("ohio", "compensation"):
            req.central = False
            req.assessment = "unconfirmed"
            req.reason = "This information is not confirmed in the assessment."
    a = flow.score(a["id"], data)
    assert a["score"]["total"] >= 80 and a["score"]["band"] == "conditional"
    return flow, a["id"]


def test_conditional_selection_requires_reason_and_keeps_gaps(db):
    flow, aid = conditional_app(db)
    before = flow.get(aid)
    review = selection_review(before)
    assert review["mode"] == "conditional"
    assert review["central_count"] == 0 and len(review["uncertainties"]) == 2
    for reason in (None, "", "   ", 123):
        with pytest.raises(ValueError):
            flow.select(aid, reason, review["review_hash"])
    chosen = flow.select(
        aid,
        "Strong technical fit; clarify the remaining terms during preparation.",
        review["review_hash"],
    )
    assert chosen["state"] == "selected"
    assert chosen["score"] == before["score"]
    assert not db.all("SELECT * FROM approvals")
    assert not db.all("SELECT * FROM attempts")
    assert not db.all("SELECT * FROM jobs WHERE kind='submit'")
    event = db.one(
        "SELECT detail FROM audit WHERE event='workflow.selected' AND subject=%s",
        (aid,),
    )
    assert event["detail"]["conditional_reason"].startswith("Strong technical fit")


@pytest.mark.parametrize(
    "blocker", ["skip", "paused", "imported", "no_score", "changed"]
)
def test_selection_blockers_cannot_be_overridden_with_reason(db, blocker):
    flow, aid = conditional_app(db)
    review = selection_review(flow.get(aid))
    if blocker == "skip":
        score = flow.get(aid)["score"] | {"band": "skip", "primary": False}
        db.execute("UPDATE applications SET score=%s WHERE id=%s", (js(score), aid))
    elif blocker == "paused":
        flow.control(aid, "pause")
    elif blocker == "imported":
        db.execute("UPDATE applications SET imported=true WHERE id=%s", (aid,))
    elif blocker == "no_score":
        db.execute("UPDATE applications SET score='{}' WHERE id=%s", (aid,))
    else:
        db.execute("UPDATE applications SET revision=revision+1 WHERE id=%s", (aid,))
    with pytest.raises(ValueError):
        flow.select(
            aid,
            "Explicit reason",
            review["review_hash"] if blocker == "changed" else None,
        )
    assert flow.get(aid)["state"] == "scored"


def test_selection_api_returns_reasons_and_validates_input(db):
    flow, aid = conditional_app(db)
    with TestClient(create_app(db.settings)) as client:
        headers = {"X-JSA-Token": db.settings.token()}
        app = client.get("/api/applications/" + aid, headers=headers).json()
        assert app["selection_review"]["mode"] == "conditional"
        assert app["selection_review"]["reasons"]
        path = "/api/applications/" + aid + "/select"
        assert (
            client.post(
                path, json={"conditional_reason": 123}, headers=headers
            ).status_code
            == 422
        )
        assert (
            client.post(
                path, json={"conditional_reason": "  "}, headers=headers
            ).status_code
            == 400
        )
        response = client.post(
            path,
            json={
                "conditional_reason": "Review remaining terms during preparation.",
                "reviewed_hash": app["selection_review"]["review_hash"],
            },
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["state"] == "selected"


@pytest.mark.usefixtures("fixture_server")
async def test_guided_conditional_selection_explains_next_step(db):
    flow, aid = conditional_app(db)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_role("button", name="Privacy & settings", exact=True).click()
            await page.get_by_label("Data mode", exact=True).select_option("fixture")
            await page.get_by_role("button", name="Applications", exact=True).click()
            await page.get_by_text("Selection Demo", exact=True).click()
            guide = page.get_by_role("region", name="Application next step")
            await expect(
                guide.get_by_role(
                    "heading", name="Decide whether to pursue this role"
                )
            ).to_be_visible()
            assert await guide.locator(".journey-story").evaluate(
                "node => Array.from(node.querySelectorAll('dt')).every(dt => dt.getBoundingClientRect().right <= dt.nextElementSibling.getBoundingClientRect().left)"
            )
            await guide.get_by_role(
                "button", name="Review selection decision", exact=True
            ).click()
            decision = page.get_by_role("region", name="Role selection decision")
            await expect(
                decision.get_by_text(
                    "At least two material requirements remain unconfirmed", exact=True
                )
            ).to_be_visible()
            button = decision.get_by_role(
                "button", name="Select conditional role", exact=True
            )
            await expect(button).to_be_disabled()
            await expect(
                decision.get_by_text(
                    "To continue, add your reason and check the acknowledgment above.",
                    exact=True,
                )
            ).to_be_visible()
            await decision.get_by_label("Why do you want to pursue this role?").fill(
                "Strong technical match. Clarify the remaining terms during preparation."
            )
            await decision.get_by_role("checkbox").check()
            await expect(button).to_be_enabled()
            await button.click()
            await expect(
                guide.get_by_role("heading", name="Prepare your application documents")
            ).to_be_visible()
            await expect(
                guide.get_by_role("button", name="Prepare documents", exact=True)
            ).to_be_enabled()
            await expect(
                guide.locator(".journey-steps li")
                .nth(0)
                .get_by_text("Complete", exact=True)
            ).to_be_visible()
            assert flow.get(aid)["state"] == "selected"
            assert not errors and not db.all("SELECT * FROM approvals")
        finally:
            await browser.close()
