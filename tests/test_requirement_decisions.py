# ruff: noqa: F811
import pytest
from fastapi.testclient import TestClient
from jobsearch.api import create_app
from jobsearch.config import digest
from jobsearch.db import uid, js
from jobsearch.fixtures import assessment
from jobsearch.llm.contracts import InferenceRequest
from jobsearch.llm.service import InferenceService
from jobsearch.notifications import Notifications
from jobsearch.queue import Worker
from jobsearch.requirement_decisions import (
    RequirementDecisions,
    RequirementDecisionInput,
)
from jobsearch.workflow import Workflow
from .test_llm import application
from .test_browser import fixture_server  # noqa: F401


def prepared_assessment(db):
    app = application(db)
    source = InferenceService(db).context(
        InferenceRequest(task="fit_assessment", application_id=app["id"])
    )["sources"][0]["id"]
    data = assessment(db)
    for req in data.requirements:
        req.source = source
    data.requirements[0].assessment = "unconfirmed"
    data.requirements[0].deduction = 8
    flow = Workflow(db)
    app = flow.score(app["id"], data)
    return app, data


def decision(db, aid, value="confirm", note=""):
    service = RequirementDecisions(db)
    view = service.view(Workflow(db).get(aid))
    return service.decide(
        aid,
        RequirementDecisionInput(
            requirement_key=view["requirements"][0]["key"],
            score_hash=view["score_hash"],
            decision=value,
            note=note,
        ),
    )


def test_confirm_and_deny_recompute_fit_retain_history_and_scope(db):
    app, data = prepared_assessment(db)
    aid = app["id"]
    assert not app["score"]["primary"]
    assert Notifications(db).snapshot("fixture")["items"][0]["kind"] == "requirements"
    confirmed = decision(db, aid, note="Delivered these APIs in the fixture project.")
    assert confirmed["score"]["primary"]
    assert confirmed["score"]["requirements"][0]["deduction"] == 0
    fact = db.one("SELECT * FROM facts WHERE key LIKE 'requirement_decision_%%'")
    from jobsearch.memory import Memory

    Memory(db).resolve(fact["key"], "fixture", aid)
    assert fact["scope"] == aid and fact["provenance"]["type"] == "user_confirmation"
    assert not any(
        n["kind"] == "requirements"
        for n in Notifications(db).snapshot("fixture")["items"]
    )
    denied = decision(db, aid, "deny", "Correction: only a prototype.")
    assert not denied["score"]["primary"]
    assert denied["score"]["requirements"][0]["deduction"] == 8
    assert any("Confirmed qualification gap" in r for r in denied["score"]["reasons"])
    assert len(db.all("SELECT * FROM requirement_decisions")) == 2
    assert not db.one("SELECT active FROM facts WHERE id=%s", (fact["id"],))["active"]
    assert not db.one(
        "SELECT valid FROM answer_cache WHERE fact_ids @> %s", (js([fact["id"]]),)
    )["valid"]
    assert not any(
        n["kind"] == "requirements"
        for n in Notifications(db).snapshot("fixture")["items"]
    )
    # A later model assessment cannot overwrite the scoped denial.
    data.requirements[0].assessment = "strong"
    rescored = Workflow(db).score(aid, data)
    assert rescored["score"]["requirements"][0]["assessment"] == "gap"
    other = Workflow(db).create("Evaluation Demo", "API Engineer", "EVAL-2")
    Workflow(db).set_official(other["id"], app["official"])
    data.requirements[0].assessment = "unconfirmed"
    assert not Workflow(db).score(other["id"], data)["score"]["primary"]
    assert not db.all("SELECT * FROM approvals")


def test_stale_decisions_rejected_and_changed_requirements_need_confirmation(db):
    app, data = prepared_assessment(db)
    service = RequirementDecisions(db)
    view = service.view(app)
    request = RequirementDecisionInput(
        requirement_key=view["requirements"][0]["key"],
        score_hash=view["score_hash"],
        decision="confirm",
    )
    service.decide(app["id"], request)
    with pytest.raises(ValueError, match="changed"):
        service.decide(app["id"], request)
    data.requirements[0].text = "C# APIs with a new employer constraint"
    current = Workflow(db).score(app["id"], data)
    assert (
        not current["score"]["primary"]
        and not current["score"]["requirement_decisions"]
    )


def test_confirmation_preserves_other_gates_and_revokes_approval(db):
    app, data = prepared_assessment(db)
    data.ohio = "excluded"
    Workflow(db).score(app["id"], data)
    db.execute("UPDATE applications SET state='authorized' WHERE id=%s", (app["id"],))
    db.execute(
        "INSERT INTO approvals(id,application_id,payload_hash,actor) VALUES(%s,%s,%s,'user-ui')",
        (uid(), app["id"], "a" * 64),
    )
    updated = decision(db, app["id"])
    assert updated["state"] == "review" and not updated["score"]["primary"]
    assert "Ohio explicitly excluded" in updated["score"]["reasons"]
    assert db.one("SELECT invalidated FROM approvals")["invalidated"]
    assert updated["payload"]["browser_hash"] is None


@pytest.mark.parametrize(
    "state,imported", [("scored", True), ("submitted", False), ("unresolved", False)]
)
def test_read_only_and_attempted_states_reject_decisions(db, state, imported):
    app, _ = prepared_assessment(db)
    db.execute(
        "UPDATE applications SET state=%s,imported=%s WHERE id=%s",
        (state, imported, app["id"]),
    )
    with pytest.raises(ValueError):
        decision(db, app["id"])
    assert not db.all("SELECT * FROM requirement_decisions")


async def test_rescore_uses_saved_decisions_and_notes_without_advancing_application(db):
    app, _ = prepared_assessment(db)
    app = decision(db, app["id"], note="Fixture project delivery confirmed.")
    with TestClient(create_app(db.settings)) as client:
        path = f"/api/applications/{app['id']}/rescore"
        body = {"score_hash": digest(app["score"])}
        assert client.post(path, json=body).status_code == 401
        headers = {"X-JSA-Token": db.settings.token()}
        first = client.post(path, json=body, headers=headers).json()
        second = client.post(path, json=body, headers=headers).json()
        assert first["job_id"] == second["job_id"]
    worker = Worker(db)
    try:
        await worker.run_once()
        job = db.one("SELECT * FROM jobs WHERE id=%s", (first["job_id"],))
        assert job["status"] == "complete", job["error"]
        run = db.one("SELECT * FROM llm_runs WHERE id=%s", (job["result"]["run_id"],))
        assert (
            run["input"]["application"]["requirement_decisions"][0]["note"]
            == "Fixture project delivery confirmed."
        )
        current = Workflow(db).get(app["id"])
        matched = next(
            r for r in current["score"]["requirements"] if r["text"] == "C# APIs"
        )
        assert matched["assessment"] == "strong"
        assert current["state"] == "scored"
        assert not db.all("SELECT * FROM approvals") and not db.all(
            "SELECT * FROM attempts"
        )
    finally:
        await worker.browser.close()


async def test_changed_decision_while_rescoring_blocks_stale_model_result(
    db, monkeypatch
):
    app, _ = prepared_assessment(db)
    service = InferenceService(db)
    request = InferenceRequest(task="fit_assessment", application_id=app["id"])
    result = service.run(request)
    decision(db, app["id"], "deny", "A new correction")
    with pytest.raises(ValueError, match="changed"):
        service.apply_score(result["run_id"])
    assert (
        Workflow(db).get(app["id"])["score"]["requirements"][0]["assessment"] == "gap"
    )


@pytest.mark.parametrize("initial_decision", ["Confirm", "Deny"])
async def test_requirement_decisions_and_rescore_ui(
    db, fixture_server, initial_decision
):
    from playwright.async_api import async_playwright, expect

    app, _ = prepared_assessment(db)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        worker = Worker(db)
        try:
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_label("Workspace", exact=True).select_option("fixture")
            await page.get_by_role("button", name="Applications", exact=True).click()
            await page.get_by_text("Evaluation Demo", exact=True).click()
            panel = page.get_by_role("region", name="Central requirement decisions")
            await expect(
                panel.get_by_role("article", name="C# APIs", exact=True)
            ).to_be_visible()
            await expect(
                page.get_by_text("Unresolved central requirement: C# APIs", exact=True)
            ).to_have_count(0)
            await panel.get_by_label("Note for C# APIs", exact=True).fill(
                "I delivered the fixture APIs."
            )
            await panel.get_by_role("button", name=initial_decision, exact=True).click()
            await expect(
                panel.get_by_role("article", name="C# APIs", exact=True)
            ).to_have_count(0)
            await expect(
                panel.get_by_text("No unresolved central requirements.", exact=True)
            ).to_be_visible()
            await expect(
                panel.get_by_role(
                    "button", name="Show resolved requirements (1)", exact=True
                )
            ).to_be_visible()
            await page.reload()
            await page.get_by_label("Workspace", exact=True).select_option("fixture")
            await page.get_by_role("button", name="Applications", exact=True).click()
            await page.get_by_text("Evaluation Demo", exact=True).click()
            await expect(
                panel.get_by_role("article", name="C# APIs", exact=True)
            ).to_have_count(0)
            await panel.get_by_role(
                "button", name="Show resolved requirements (1)", exact=True
            ).click()
            await expect(
                panel.get_by_label("Note for C# APIs", exact=True)
            ).to_have_value("I delivered the fixture APIs.")
            await panel.get_by_role("button", name="Deny", exact=True).click()
            await expect(
                panel.get_by_text("Denied: this qualification gap remains.", exact=True)
            ).to_be_visible()
            await expect(
                page.get_by_text("Confirmed qualification gap: C# APIs", exact=True)
            ).to_have_count(0)
            await panel.get_by_role(
                "button", name="Hide resolved requirements (1)", exact=True
            ).click()
            await expect(
                panel.get_by_role("article", name="C# APIs", exact=True)
            ).to_have_count(0)
            async with page.expect_response(
                lambda r: r.url.endswith("/rescore")
            ) as response:
                await panel.get_by_role(
                    "button", name="Rescore job", exact=True
                ).click()
            assert (await response.value).status == 200
            await worker.run_once()
            assert (
                db.one("SELECT status FROM jobs WHERE kind='rescore'")["status"]
                == "complete"
            )
            await expect(
                panel.get_by_role("button", name="Rescore job", exact=True)
            ).to_be_enabled()
            assert not Workflow(db).get(app["id"])["score"]["primary"]
            assert not db.all("SELECT * FROM attempts")
            assert not errors
        finally:
            await worker.browser.close()
            await browser.close()


def test_manual_handoff_can_edit_fit_without_losing_checkpoint(db):
    app, _ = prepared_assessment(db)
    db.execute(
        "UPDATE applications SET state='manual_handoff' WHERE id=%s", (app["id"],)
    )
    assert RequirementDecisions(db).view(Workflow(db).get(app["id"]))["editable"]
    result = decision(db, app["id"], note="Explicit synthetic confirmation")
    assert result["state"] == "manual_handoff"
    assert not db.all("SELECT * FROM approvals")
    assert not db.all("SELECT * FROM attempts")
